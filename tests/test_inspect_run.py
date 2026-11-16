import json
from dataclasses import asdict
from pathlib import Path
import sys
import pytest
from researchdesk.inspect_run import inspect_run, main
from researchdesk.ledger import Ledger
from researchdesk.policy import ToolLimits
from researchdesk.run_metrics import run_metrics
from researchdesk.storage import atomic_text
from researchdesk.tools import ResearchTools


@pytest.fixture
def run(tmp_path):
    directory = tmp_path / "run"
    ledger = Ledger(directory)
    source = ledger.add_source("Private source title", "fixture://private-location", "PRIVATE_SOURCE_QUOTE", "local")
    runtime = ResearchTools(ledger, "", ToolLimits(max_calls=3, max_provider_requests=0))
    evidence = runtime.executor.execute("record_evidence", dict(claim="PRIVATE_CLAIM", source_id=source["id"],
        snippet="PRIVATE_SOURCE_QUOTE"), operation_id="tool1")
    ledger.save_report(f"PRIVATE_REPORT_BODY[{evidence['id']}]")
    atomic_text(directory / "run.json", json.dumps(dict(format_version=2, status="completed", model="fixture",
        mode="live", question="PRIVATE_USER_PROMPT", tool_policy=asdict(runtime.executor.limits), resume_count=1)))
    ledger.store.add_event(dict(event="model_started", model_call_id="model1", prompt="PRIVATE_PROMPT",
                               args={"api_key": "PRIVATE_KEY"}))
    ledger.store.add_event(dict(event="model_completed", model_call_id="model1", elapsed_ms=12,
        usage=dict(input_tokens=2, output_tokens=3, total_tokens=5), response="PRIVATE_MODEL_REPLY"))
    yield directory, ledger, runtime, evidence
    ledger.store.close()


def test_offline_diagnosis_has_metrics_and_zero_budget_without_private_bodies(run):
    directory, ledger, runtime, evidence = run
    before = ledger.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    result = inspect_run(directory)
    assert result["metrics"]["total_tokens"] == 5
    assert result["budget"]["requests"] == dict(used=0, limit=0, remaining=0)
    assert result["citations"]["structural_valid"] is True
    assert result["citations"]["semantic_assessment"] == "not_evaluated"
    assert result["model_calls"][0]["elapsed_ms"] == 12
    exported = json.dumps(result)
    for marker in ("PRIVATE_", "private-location", "Private source title"):
        assert marker not in exported
    assert ledger.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_diagnosis_rejects_mixed_delimiters_even_with_known_id(run):
    directory, _, _, evidence = run
    (directory / "draft.md").write_text(f"Fact.[{evidence['id']}］", encoding="utf-8")
    result = inspect_run(directory)
    assert result["citations"]["structural_valid"] is False
    assert "report_citation_integrity_gap" in {row["code"] for row in result["findings"]}


def test_missing_completion_event_does_not_override_a_durable_success(run):
    directory, ledger, runtime, evidence = run
    ledger.store.conn.execute("DELETE FROM events WHERE json_extract(payload,'$.event')='completed'")
    ledger.store.conn.commit()
    result = inspect_run(directory)
    assert result["tool_operations"][0]["status"] == "completed"
    assert result["metrics"]["tool_success_rate"] == 1
    assert "durable_result_without_completion_event" in {row["code"] for row in result["findings"]}


def test_receipt_replay_and_recovery_reexecution_are_separate(run):
    directory, ledger, runtime, evidence = run
    ledger.store.add_event(dict(event="replayed", tool="record_evidence", call_id="tool1"))
    ledger.store.add_event(dict(event="started", tool="record_evidence", call_id="tool1", recovery_replay=True))
    operation = inspect_run(directory)["tool_operations"][0]
    assert operation["receipt_replays"] == operation["recovery_reexecutions"] == 1
    assert operation["extra_executions"] == 0  # Audit events alone do not prove another execution attempt.


@pytest.mark.parametrize("events", [
    [dict(event="model_started", model_call_id="a"), dict(event="model_completed", model_call_id="b")],
    [dict(event="model_started", model_call_id="a"), dict(event="model_started", model_call_id="a"),
     dict(event="model_completed", model_call_id="a"), dict(event="model_completed", model_call_id="a")],
    [dict(event="model_completed", model_call_id="a"), dict(event="model_started", model_call_id="a")],
    [dict(event="model_started"), dict(event="model_completed")],
    [dict(event="model_started", model_call_id=""), dict(event="model_completed", model_call_id="")],
    [dict(event="model_started", model_call_id=" "), dict(event="model_completed", model_call_id=" ")],
])
def test_ambiguous_model_events_cannot_claim_complete_token_usage(tmp_path, events):
    ledger = Ledger(tmp_path / "run")
    try:
        for row in events:
            if row["event"] == "model_completed":
                row = dict(row, usage=dict(input_tokens=1, output_tokens=1, total_tokens=2))
            ledger.store.add_event(row)
        assert run_metrics(ledger.directory)["total_tokens"] is None
    finally:
        ledger.store.close()


def test_boolean_usage_cannot_be_reported_as_integer_tokens(run):
    directory, ledger, *_ = run
    ledger.store.conn.execute("UPDATE events SET payload=json_set(payload,'$.usage.input_tokens',json('true')) WHERE json_extract(payload,'$.event')='model_completed'")
    ledger.store.conn.commit()
    assert inspect_run(directory)["metrics"]["total_tokens"] is None


def test_pending_tool_and_unknown_model_outcome_are_not_treated_as_failed_remote_calls(run):
    directory, ledger, runtime, evidence = run
    ledger.store.begin_call("pending", "record_evidence", "fingerprint", 3)
    ledger.store.add_event(dict(event="model_started", model_call_id="uncertain"))
    result = inspect_run(directory)
    assert result["metrics"]["total_tokens"] is None
    assert result["model_calls"][-1]["status"] == "inflight_or_interrupted"
    assert result["tool_operations"][-1]["status"] == "pending"
    assert "tool_pending_or_interrupted" in {row["code"] for row in result["findings"]}


def test_bad_citation_is_located_without_exporting_report_text(run):
    directory, ledger, runtime, evidence = run
    atomic_text(directory / "draft.md", "PRIVATE_BAD_CLAIM[Ebad]")
    result = inspect_run(directory)
    assert result["citations"]["structural_valid"] is False
    assert result["citations"]["issues"] == [dict(line=1, evidence_id="malformed_reference")]
    assert "PRIVATE_BAD_CLAIM" not in json.dumps(result)


def test_repair_rejection_and_uncertain_verification_are_visible(run):
    directory, *_ = run
    atomic_text(directory / "repair-state.json", json.dumps(dict(rounds_used=1, checks_started=["candidate"],
        selection=dict(accepted=False, selected="original", reasons=["precision_below_target"]))))
    result = inspect_run(directory)
    assert result["repair"]["selected"] == "original"
    assert {"repair_candidate_rejected", "verification_outcome_uncertain"} <= {row["code"] for row in result["findings"]}


def test_cli_writes_separate_report_and_rejects_source_output(run, tmp_path, monkeypatch):
    directory, *_ = run
    monkeypatch.setattr(sys, "argv", ["inspect", str(directory), "--output", str(tmp_path / "diagnostics")])
    assert main() == 0
    assert len(list((tmp_path / "diagnostics").glob("*/summary.md"))) == 1
    monkeypatch.setattr(sys, "argv", ["inspect", str(directory), "--output", str(directory / "diagnostics")])
    assert main() == 1 and not (directory / "diagnostics").exists()
