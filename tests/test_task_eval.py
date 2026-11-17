import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from langchain_core.messages import AIMessage
from researchdesk.ledger import Ledger
from researchdesk.run_metrics import run_metrics
from researchdesk.task_eval import (evaluate_batch, load_taskset, run_command, run_task,
                                   task_success)
from researchdesk.task_grading import grade_tasks
from researchdesk.verification import VerificationError


def task_data():
    path = Path(__file__).parents[1] / "evals" / "research-tasks-v1.json"
    return load_taskset(path)


class RubricJudge:
    usage = {"total_tokens": 3}
    def __call__(self, payload):
        return {"tasks": [dict(task_id=row["task_id"], criteria=[
            dict(criterion_id=item["id"], passed=True, report_quote="Fixture fact.", reason="测试桩")
            for item in row["criteria"]]) for row in payload["tasks"]]}


class EvidenceJudge:
    usage = {"total_tokens": 3}
    def __init__(self, verdict="supported"):
        self.verdict = verdict
    def __call__(self, payload):
        return {"units": [dict(unit_id=row["unit_id"], factual=True, reason="测试桩",
            links=[dict(evidence_id=eid, verdict=self.verdict, reason="测试桩")
                   for eid in row["citations"]]) for row in payload["units"]]}


def fixture_runner(task, paths, directory, timeout):
    ledger = Ledger(directory / "research" / ("a" * 32))
    source = ledger.add_source("Fixture", str(paths[0]), "Fixture fact.", "local")
    evidence = ledger.add_evidence("Fixture fact.", source["id"], "Fixture fact.")
    ledger.save_report(f"Fixture fact.[{evidence['id']}]")
    (ledger.directory / "run.json").write_text(json.dumps({"status": "completed", "mode": "fixture"}))
    ledger.store.close()
    return dict(task_id=task["id"], generation_status="completed", elapsed_seconds=1,
                model=task["runtime_model"], run_directory=str(ledger.directory),
                generation_metrics=dict(token_usage_complete=True, total_tokens=7, provider_requests=0,
                                        retry_count=0, tool_success_rate=1))


def test_task_success_requires_quality_and_rubric_not_only_completion():
    record = {"generation_status": "completed", "generation_metrics": {"provider_requests": 0}}
    limits = {"citation_precision": 0.9, "evidence_coverage": 0.6}
    quality = {"citation_precision": 1, "evidence_coverage": 1, "invalid_links": 0}
    assert task_success(record, {"rubric_passed": True}, quality, limits)
    assert not task_success(record, {"rubric_passed": False}, quality, limits)
    assert not task_success(record, {"rubric_passed": True}, dict(quality, evidence_coverage=None), limits)
    assert not task_success(record, {"rubric_passed": True}, dict(quality, invalid_links=1), limits)


def test_evaluation_orchestrates_full_artifacts_and_sums_generation_and_judge_tokens(tmp_path):
    data, tasks, docs = task_data()
    result = evaluate_batch(data, tasks[:1], docs, tmp_path / "eval", runner=fixture_runner,
                            model_name="fixture", task_judge=RubricJudge(), evidence_judge=EvidenceJudge())
    assert result["status"] == "complete"
    assert result["task_success_rate"] == result["generation_completion_rate"] == 1
    assert result["generation_total_tokens"] == 7
    assert result["evaluator_total_tokens"] == 6
    assert result["total_tokens"] == 13
    assert result["estimated_fee"] is None and result["recovery_rate"] is None
    assert result['tasks'][0]['assessment'] == dict(scoring_status='valid', model_gate='passed',
                                                    human_review='pending', final_acceptance='pending')
    assert result['final_acceptance'] == result['human_review'] == 'pending'
    assert (tmp_path / "eval" / "citation-quality.json").is_file()


def test_completed_generation_with_unsupported_answer_fails_task(tmp_path):
    data, tasks, docs = task_data()
    result = evaluate_batch(data, tasks[:1], docs, tmp_path / "eval", runner=fixture_runner,
                            model_name="fixture", task_judge=RubricJudge(), evidence_judge=EvidenceJudge("unsupported"))
    assert result["generation_completion_rate"] == 1
    assert result["task_success_rate"] == 0


def test_grading_failure_does_not_count_unknown_as_success(tmp_path):
    data, tasks, docs = task_data()
    def invalid(payload):
        return {"tasks": []}
    result = evaluate_batch(data, tasks[:1], docs, tmp_path / "eval", runner=fixture_runner,
                            model_name="fixture", task_judge=invalid, evidence_judge=EvidenceJudge())
    assert result["status"] == "scoring_failed"
    assert result["task_success_rate"] is None
    assert result["tasks"][0]["success"] is None
    assert result['tasks'][0]['assessment']['scoring_status'] == 'invalid'
    assert result['tasks'][0]['assessment']['model_gate'] == 'unknown'


def test_failed_generation_is_in_denominator_and_needs_no_grader(tmp_path):
    data, tasks, docs = task_data()
    def failed(task, paths, directory, timeout):
        return {"task_id": task["id"], "generation_status": "timed_out"}
    def never(payload):
        raise AssertionError("No report to grade")
    result = evaluate_batch(data, tasks[:1], docs, tmp_path / "eval", runner=failed,
                            task_judge=never, evidence_judge=never)
    assert result["task_success_rate"] == 0
    assert result["generation_completion_rate"] == 0
    assert result["generation_total_tokens"] is None
    assert result['tasks'][0]['assessment']['scoring_status'] == 'not_scored'


def test_rubric_grader_cannot_invent_anchors_or_omit_criteria():
    payload = {"tasks": [{"task_id": "t", "report": "Actual answer.",
                          "criteria": [{"id": "c", "description": "A criterion"}]}]}
    bad = {"tasks": [{"task_id": "t", "criteria": [
        {"criterion_id": "c", "passed": True, "report_quote": "Invented answer.", "reason": "test"}]}]}
    with pytest.raises(VerificationError, match="invented a report quote"):
        grade_tasks(payload, lambda p: bad)
    with pytest.raises(VerificationError, match="criteria"):
        grade_tasks(payload, lambda p: {"tasks": [{"task_id": "t", "criteria": []}]})


def test_command_deadline_terminates_our_subprocess_tree(tmp_path):
    result = run_command([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path,
                         tmp_path / "child.log", timeout=0.5)
    assert result["timed_out"]
    assert result["returncode"] != 0
    assert result["elapsed_seconds"] < 12


def test_generation_command_excludes_rubric_and_enforces_local_scope(tmp_path, monkeypatch):
    import researchdesk.task_eval as module
    captured = []
    def command(args, directory, log, timeout, environment):
        captured.append(args)
        assert environment["DEEPSEEK_MODEL"] == "frozen-model"
        output = Path(args[args.index("--output")+1]) / ("b" * 32)
        output.mkdir(parents=True)
        (output / "run.json").write_text(json.dumps({"status": "completed", "model": "frozen-model"}))
        return {"returncode": 0, "timed_out": False, "elapsed_seconds": 1}
    monkeypatch.setattr(module, "run_command", command)
    monkeypatch.setattr(module, "run_metrics", lambda path: {"provider_requests": 0})
    task = {"id": "t", "question": "Question", "runtime_model": "frozen-model",
            "criteria": [{"description": "PRIVATE_RUBRIC_MARKER"}]}
    run_task(task, [tmp_path / "doc.md"], tmp_path / "task", 30)
    assert "--local-only" in captured[0]
    assert captured[0][captured[0].index("--max-provider-requests")+1] == "0"
    assert "PRIVATE_RUBRIC_MARKER" not in " ".join(captured[0])


def test_token_metrics_are_full_run_and_unknown_is_not_zero(tmp_path):
    ledger = Ledger(tmp_path / "run")
    ledger.store.add_event(dict(event="model_started", model_call_id="a"))
    ledger.store.add_event(dict(event="model_completed", model_call_id="a",
                               usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}))
    ledger.store.add_event(dict(event="model_started", model_call_id="b"))
    ledger.store.add_event(dict(event="model_completed", model_call_id="b",
                               usage={"input_tokens": 7, "output_tokens": 11, "total_tokens": 18}))
    metrics = run_metrics(ledger.directory)
    assert metrics["model_calls"] == 2 and metrics["total_tokens"] == 23
    ledger.store.add_event(dict(event="model_started", model_call_id="c"))
    ledger.store.add_event(dict(event="model_failed", model_call_id="c", error_type="TimeoutError"))
    assert run_metrics(ledger.directory)["total_tokens"] is None
    ledger.store.close()


def test_taskset_document_traversal_is_rejected(tmp_path):
    path = tmp_path / "taskset" / "tasks.json"
    path.parent.mkdir()
    data, _, _ = task_data()
    data["tasks"][0]["documents"] = ["../outside.md"]
    path.write_text(json.dumps(data))
    with pytest.raises(VerificationError, match="inside the dataset"):
        load_taskset(path)


def test_oversized_frozen_input_is_rejected_before_generation(tmp_path):
    data, tasks, documents = task_data()
    documents[tasks[0]["documents"][0]]["content"] = "x" * 20001
    calls = []
    def never(*args):
        calls.append(args)
        raise AssertionError("No paid generation should start")
    with pytest.raises(VerificationError, match="generation character budget"):
        evaluate_batch(data, tasks[:1], documents, tmp_path / "eval", runner=never)
    assert not calls

