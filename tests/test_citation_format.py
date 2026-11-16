import hashlib
import json
import pytest
from researchdesk.citations import archive_and_normalize, normalize_citation_delimiters
from researchdesk.claims import split_clauses, validate_anchors
from researchdesk.ledger import CitationValidationError, Ledger
from researchdesk.verification import VerificationError, split_units


@pytest.fixture
def saved(tmp_path):
    ledger = Ledger(tmp_path / "run")
    source = ledger.add_source("Fixture", "fixture://facts", "事实A。")
    evidence = ledger.add_evidence("事实A", source["id"], "事实A。")
    yield ledger, evidence["id"]
    ledger.store.close()


def test_fullwidth_report_keeps_exact_input_and_canonical_draft(saved):
    ledger, eid = saved
    raw = f"全角文字ＡＢＣ及网址https://example.org/Ａ。［{eid}］\r\n事实A。[{eid}]"
    counts = ledger.store.counters()
    canonical = raw.replace(f"［{eid}］", f"[{eid}]")
    path = ledger.save_report(raw)
    assert path.read_bytes().startswith(canonical.encode())
    assert (ledger.directory / "draft.md").read_bytes() == canonical.encode()
    digest = hashlib.sha256(raw.encode()).hexdigest()
    archive = ledger.directory / "report-inputs"
    assert (archive / (digest + ".md")).read_bytes() == raw.encode()
    receipt = json.loads((archive / (digest + ".json")).read_text())
    assert receipt["changed_references"] == 1
    assert receipt["normalized_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert ledger.validate_report(raw) == [eid]
    assert ledger.store.counters() == counts
    assert archive_and_normalize(ledger.directory, raw) == canonical  # CRLF-safe replay


@pytest.mark.parametrize("bad", [
    "［E0000000000000000］", "［E1］", "［Efake］", "［Ｅ123］",
    "［EＡ123］", "［EABCDEF0123456789］", "[E123］", "［E123]",
    "［E123", "［E123 456］", "[Ebad[E123]]",
])
def test_bad_reference_cannot_hide_beside_valid_citation(saved, bad):
    ledger, eid = saved
    raw = f"事实A。[{eid}]\n{bad}"
    with pytest.raises(CitationValidationError):
        ledger.save_report(raw)
    assert not (ledger.directory / "report.md").exists()
    assert (ledger.directory / "draft.md").read_bytes() == raw.encode()
    assert list((ledger.directory / "report-inputs").glob("*.md"))[0].read_bytes() == raw.encode()


def test_ascii_identity_and_non_citation_fullwidth_text(saved):
    ledger, eid = saved
    text = f"［普通全角括号］事实A。[{eid}]"
    assert normalize_citation_delimiters(text) == text
    ledger.save_report(text)
    assert (ledger.directory / "draft.md").read_bytes() == text.encode()


def test_verifier_and_clause_anchors_preserve_raw_offsets(saved):
    _, eid = saved
    raw = f"事实A。［{eid}］；事实A。[{eid}]"
    units = split_units(raw)
    assert units[0]["text"] == raw and units[0]["citations"] == [eid]
    clauses = split_clauses(units)
    validate_anchors(clauses)
    assert "".join(row["text"] for row in clauses) == raw
    assert all(row["citations"] == [eid] for row in clauses)
    with pytest.raises(VerificationError, match="Malformed"):
        split_units(raw + "［E123]")
