import copy
import json
from pathlib import Path
import sys
import pytest
from researchdesk.fault_eval import evaluate_recovery, kill_at_boundary, recovery_checks, snapshot
from researchdesk.ledger import Ledger
from researchdesk.policy import ToolLimits
from researchdesk.tools import ResearchTools
from dataclasses import asdict
from researchdesk.storage import atomic_text
from researchdesk.task_eval import load_taskset
from researchdesk.verification import VerificationError


def task_data():
    return load_taskset(Path(__file__).parents[1] / "evals" / "research-tasks-v1.json")


def test_scripted_full_cli_kill_resume_matrix(tmp_path, monkeypatch):
    # Child workers must load this checkout, not the installed editable PR2 copy.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    data, tasks, docs = task_data()
    directory = tmp_path / "matrix"
    result = evaluate_recovery(data, tasks[0], docs, directory, scripted=True, timeout=45)
    assert result["all_passed"], json.dumps(result, ensure_ascii=False)
    assert result["execution_mode"] == "scripted_runtime_simulation"
    assert result["injected_cases"] == 3 and result["recovery_rate"] == result["scenario_pass_rate"] == 1
    assert all(row["checks"]["completed_resume_is_noop"] for row in result["cases"][1:])
    evidence_before = json.loads((directory / "evidence" / "before.json").read_text(encoding="utf-8"))
    assert any(row["result"] is None for row in evidence_before["dispatches"].values())
    receipt_before = json.loads((directory / "receipt" / "before.json").read_text(encoding="utf-8"))
    assert receipt_before["receipts"]
    report_case = result["cases"][-1]
    assert report_case["checks"]["finished_graph_not_reinvoked"]


def test_unreached_boundary_is_failure_and_not_a_successful_injection(tmp_path):
    marker = tmp_path / "absent.json"
    result = kill_at_boundary([sys.executable, "-c", "raise SystemExit(0)"], tmp_path, marker, 5)
    assert result["boundary_reached"] is False and result["forced_termination"] is False
    assert result["returncode"] == 0


def test_recovery_failure_after_confirmed_injection_stays_in_denominator(tmp_path, monkeypatch):
    import researchdesk.fault_eval as module
    data, tasks, docs = task_data()
    def case(task, paths, directory, *, point, **kwargs):
        directory.mkdir()
        if point == "none":
            return dict(point=point, injected=False, passed=True)
        atomic_text(directory / "case.json", json.dumps(dict(point=point, injected=True, passed=False)))
        raise RuntimeError("private details")
    monkeypatch.setattr(module, "run_case", case)
    result = evaluate_recovery(data, tasks[0], docs, tmp_path / "experiment", points=("evidence",))
    assert result["injected_cases"] == 1 and result["recovery_rate"] == 0
    assert result["scenario_pass_rate"] == 0 and not result["all_passed"]
    assert "private details" not in (tmp_path / "experiment" / "result.json").read_text()


def test_no_injection_does_not_claim_perfect_recovery_rate(tmp_path, monkeypatch):
    import researchdesk.fault_eval as module
    data, tasks, docs = task_data()
    monkeypatch.setattr(module, "run_case", lambda *args, point, **kwargs:
        dict(point=point, injected=False, passed=point == "none"))
    result = evaluate_recovery(data, tasks[0], docs, tmp_path / "experiment", points=("receipt",))
    assert result["recovery_rate"] is None and result["scenario_pass_rate"] == 0


@pytest.mark.parametrize("mutation,failed_check", [("budget", "budget_not_reset"),
    ("receipt", "completed_receipts_preserved"), ("evidence", "prior_records_preserved"),
    ("input", "saved_input_unchanged")])
def test_invariant_checker_detects_persisted_state_regressions(tmp_path, mutation, failed_check):
    run = tmp_path / "run"
    ledger = Ledger(run)
    source = ledger.add_source("Fixture", "fixture://source", "Saved fact.", "local")
    runtime = ResearchTools(ledger, "", limits=ToolLimits(max_calls=3, max_provider_requests=0))
    evidence = runtime.executor.execute("record_evidence", dict(claim="Fact", source_id=source["id"], snippet="Saved fact."), operation_id="op")
    ledger.save_report(f"Saved fact.[{evidence['id']}]")
    atomic_text(run / "input.json", "{}"); atomic_text(run / "checkpoints.sqlite3", "fixture")
    metadata = dict(status="completed", model="fixture", question="Question", allowed_tools=["record_evidence"],
                    tool_scope="local_documents_only", tool_policy=asdict(runtime.executor.limits))
    atomic_text(run / "run.json", json.dumps(metadata))
    ledger.store.close()
    before = snapshot(run)
    after = copy.deepcopy(before)
    if mutation == "budget":
        after["counters"]["calls"] = 0
    elif mutation == "receipt":
        after["receipts"][next(iter(after["receipts"]))] = {"changed": True}
    elif mutation == "evidence":
        after["evidence"] = {}
    else:
        after["input_sha256"] = "changed"
    assert recovery_checks(before, after, run, "receipt")[failed_check] is False


def test_timeout_terminates_the_owned_worker_without_claiming_injection(tmp_path):
    result = kill_at_boundary([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path,
                              tmp_path / "absent.json", 0.5)
    assert not result["boundary_reached"] and not result["forced_termination"]
    assert result["returncode"] != 0


def test_oversized_task_is_rejected_before_any_process(tmp_path):
    data, tasks, docs = task_data()
    docs[tasks[0]["documents"][0]]["content"] = "x" * 20001
    with pytest.raises(VerificationError, match="character budget"):
        evaluate_recovery(data, tasks[0], docs, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("timeout,points", [(float("nan"), ("receipt",)), (0, ("receipt",)), (10, ("receipt", "receipt"))])
def test_invalid_experiment_limits_fail_before_process_creation(tmp_path, timeout, points):
    data, tasks, docs = task_data()
    with pytest.raises(VerificationError):
        evaluate_recovery(data, tasks[0], docs, tmp_path / "rejected", timeout=timeout, points=points)
    assert not (tmp_path / "rejected").exists()
