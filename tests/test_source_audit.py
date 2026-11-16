import json
import pytest
from researchdesk.ledger import Ledger
from researchdesk.source_audit import audit


@pytest.fixture
def run(tmp_path):
    ledger = Ledger(tmp_path / "run")
    source = ledger.add_source("Untrusted title §2", "https://example.org/doc", "Saved body.")
    evidence = ledger.add_evidence("Claim", source["id"], "Saved body.")
    ledger.save_report(f"Claim.[{evidence['id']}]")
    yield ledger, source, evidence
    ledger.store.close()


def receipt(ledger, tool, result):
    index = ledger.store.conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    ledger.store.conn.execute("INSERT INTO dispatches(id,fingerprint,tool,result) VALUES(?,?,?,?)",
        (str(index), str(index), tool, json.dumps(result)))
    ledger.store.conn.commit()


@pytest.mark.parametrize("tool,origin", [("read_source", "extracted_body"), ("search_sources", "search_snippet")])
def test_exact_receipt_is_required_and_read_is_non_mutating(run, tool, origin):
    ledger, source, _ = run
    before = (ledger.directory / "draft.md").read_bytes()
    assert audit(ledger.directory)["citations"][0]["observed_origin"] == "unknown"
    receipt(ledger, tool, {"sources": [source]} if tool == "search_sources" else source)
    changes = ledger.store.conn.total_changes
    result = audit(ledger.directory)
    assert result["citations"][0]["observed_origin"] == origin
    assert result["semantic_assessment"] == "not_performed"
    assert ledger.store.conn.total_changes == changes
    assert (ledger.directory / "draft.md").read_bytes() == before


def test_identical_snapshot_from_both_tools_is_ambiguous(run):
    ledger, source, _ = run
    receipt(ledger, "search_sources", {"sources": [source]})
    receipt(ledger, "read_source", source)
    assert audit(ledger.directory)["citations"][0]["observed_origin"] == "both"


@pytest.mark.parametrize("change", [{"content": "Other body"}, {"location": "https://other.org"},
                                    {"content": None, "preview": "Saved"}, {"error": "failed"}])
def test_bad_or_partial_receipts_cannot_prove_body_origin(run, change):
    ledger, source, _ = run
    receipt(ledger, "read_source", dict(source, **change))
    assert audit(ledger.directory)["citations"][0]["observed_origin"] == "unknown"


def test_invalid_evidence_is_never_given_provenance(run):
    ledger, source, evidence = run
    receipt(ledger, "read_source", source)
    row = dict(evidence, snippet="Fabricated")
    ledger.store.conn.execute("UPDATE records SET payload=? WHERE kind='evidence' AND id=?",
                            (json.dumps(row), evidence["id"]))
    ledger.store.conn.commit()
    result = audit(ledger.directory)["citations"][0]
    assert result["integrity"] == "invalid" and result["observed_origin"] == "unknown"
