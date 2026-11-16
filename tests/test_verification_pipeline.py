import json
import pytest
from researchdesk.ledger import Ledger
from researchdesk.verification import SYSTEM_PROMPT_V2, VerificationError, assess, digest, prepare, split_units
from researchdesk.verification_pipeline import verify
from researchdesk.storage import RecoveryError


@pytest.fixture
def run(tmp_path):
    ledger = Ledger(tmp_path / "run")
    source = ledger.add_source("Fixture", "fixture://doc", "Fact.")
    eid = ledger.add_evidence("Fact", source["id"], "Fact.")["id"]
    body = f"Fact.[{eid}]"
    ledger.save_report(body)
    (ledger.directory / "run.json").write_text(json.dumps(dict(status="completed")))
    payload, fingerprint = prepare(split_units(body), ledger.evidence, ledger.sources)
    result = assess(payload, lambda p: dict(units=[dict(unit_id="U1", factual=True, reason="fixture",
        links=[dict(evidence_id=eid, verdict="supported", reason="fixture")])]))
    quality = tmp_path / "quality.json"
    quality.write_text(json.dumps(dict(input_sha256=fingerprint, prompt_sha256=digest(SYSTEM_PROMPT_V2), result=result)))
    yield ledger, source, quality, result
    ledger.store.close()


def receipt(ledger, source, tool):
    index = ledger.store.conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    result = dict(sources=[source]) if tool == "search_sources" else source
    ledger.store.conn.execute("INSERT INTO dispatches(id,fingerprint,tool,result) VALUES(?,?,?,?)",
                            (str(index), str(index), tool, json.dumps(result)))
    ledger.store.conn.commit()


def test_composition_keeps_semantic_metrics_and_original_files(run):
    ledger, source, quality, original = run
    receipt(ledger, source, "read_source")
    paths = [quality, ledger.directory / "report.md", ledger.directory / "draft.md"]
    before = [p.read_bytes() for p in paths]
    result = verify(ledger.directory, quality, require_origin="extracted_body")
    assert result["status"] == "checks_passed_pending_human"
    assert result["semantic"]["metrics"] == original["metrics"]
    assert result["provenance"]["passed"] and result["api_calls"] == 0
    assert [p.read_bytes() for p in paths] == before


@pytest.mark.parametrize("tools,status", [([], "inconclusive"), (["search_sources"], "mismatch"),
    (["read_source", "search_sources"], "inconclusive")])
def test_origin_failure_does_not_rewrite_supported_labels(run, tools, status):
    ledger, source, quality, original = run
    for tool in tools:
        receipt(ledger, source, tool)
    result = verify(ledger.directory, quality, require_origin="extracted_body")
    assert result["status"] == "needs_review"
    assert result["provenance"]["citations"][0]["status"] == status
    assert result["semantic"]["metrics"] == original["metrics"]


@pytest.mark.parametrize("field", ["input_sha256", "prompt_sha256"])
def test_stale_or_different_prompt_cache_rejected(run, field):
    ledger, _, quality, _ = run
    data = json.loads(quality.read_text())
    data[field] = "changed"
    quality.write_text(json.dumps(data))
    with pytest.raises(RecoveryError):
        verify(ledger.directory, quality, require_origin="extracted_body")


def test_cli_refuses_existing_output(run, monkeypatch):
    from researchdesk.verification_pipeline import main
    ledger, _, quality, _ = run
    before = quality.read_bytes()
    monkeypatch.setattr("sys.argv", ["verification_pipeline", str(ledger.directory), "--quality", str(quality),
        "--require-origin", "extracted_body", "--output", str(quality)])
    with pytest.raises(SystemExit):
        main()
    assert quality.read_bytes() == before


def test_origin_match_cannot_rescue_unsupported_fact(run):
    ledger, source, quality, _ = run
    receipt(ledger, source, "read_source")
    data = json.loads(quality.read_text())
    data["result"]["units"][0]["links"][0]["verdict"] = "unsupported"
    quality.write_text(json.dumps(data))
    result = verify(ledger.directory, quality, require_origin="extracted_body")
    assert result["provenance"]["passed"]
    assert result["status"] == "needs_review"
    assert result["semantic"]["metrics"]["citation_precision"] == 0
