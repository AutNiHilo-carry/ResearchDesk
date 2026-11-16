import json
from pathlib import Path
import pytest
from researchdesk import report_eval
from researchdesk.storage import RecoveryError


def plan(tmp_path, limit=1):
    root = tmp_path / "pilot"
    data = report_eval.create_plan(Path(__file__).parents[1] / "evals/research-tasks-v1.json",
                                   root, model="fixture", limit=limit)
    return root, data


def fake_evaluator(data, tasks, documents, artifact, **kwargs):
    artifact.mkdir()
    return dict(status="complete", tasks=[], task_success_rate=0 if artifact.name == "v1" else None,
                generation_total_tokens=10, evaluator_total_tokens=None)


def test_plan_freezes_material_and_can_be_prepared_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(report_eval, "load_dotenv", lambda: pytest.fail("Plan must not load credentials"))
    monkeypatch.setattr(report_eval, "evaluate_batch", lambda *a, **k: pytest.fail("Plan must not invoke models"))
    root, data = plan(tmp_path, limit=3)
    assert data["controls"]["max_sdk_requests"] == 40
    assert data["controls"] == report_eval.controls(data)
    assert (root / "plan.json").is_file() and not (root / "execution").exists()
    assert all(row["content"] for row in data["documents"].values())


def test_unknown_scores_and_failures_are_preserved_without_promotion(tmp_path):
    root, _ = plan(tmp_path)
    result = report_eval.execute_plan(root, evaluator=fake_evaluator)
    assert result["execution_mode"] == "injected_offline"
    assert result["arms"]["v2"]["task_success_rate"] is None
    assert result["default_policy"] == "v1" and result["human_review"] == "pending"
    assert result["promotion"] == "no_automatic_promotion"
    with pytest.raises(FileExistsError):
        report_eval.execute_plan(root, evaluator=lambda *a, **k: pytest.fail("Must not repeat paid work"))


@pytest.mark.parametrize("change", ["question", "document", "source"])
def test_changed_controls_fail_before_any_request(tmp_path, monkeypatch, change):
    root, data = plan(tmp_path)
    if change == "question":
        data["tasks"][0]["question"] += "Changed"
    elif change == "document":
        next(iter(data["documents"].values()))["content"] += "Changed"
    else:
        monkeypatch.setattr(report_eval, "code_hashes", lambda: {"agent.py": "changed"})
    (root / "plan.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RecoveryError):
        report_eval.execute_plan(root, evaluator=lambda *a, **k: pytest.fail("No request allowed"))
    assert not (root / "execution").exists()


def test_between_arm_drift_stops_second_arm_and_preserves_first(tmp_path, monkeypatch):
    root, _ = plan(tmp_path)
    calls = []
    def evaluator(*args, **kwargs):
        calls.append(args[3].name)
        value = fake_evaluator(*args, **kwargs)
        monkeypatch.setattr(report_eval, "code_hashes", lambda: {"changed": "yes"})
        return value
    with pytest.raises(RecoveryError):
        report_eval.execute_plan(root, evaluator=evaluator)
    saved = json.loads((root / "execution/result.json").read_text(encoding="utf-8"))
    assert calls == ["v1"] and saved["status"] == "failed" and "v1" in saved["arms"]
    assert saved["arms"]["v1"]["task_success_rate"] == 0


def test_task_command_pins_same_model_budget_and_policy_without_rubric_leak(tmp_path, monkeypatch):
    from researchdesk import task_eval
    captured = []
    def command(args, cwd, log, timeout, environment):
        captured.append((args, environment))
        return dict(returncode=1, timed_out=False, elapsed_seconds=1)
    monkeypatch.setattr(task_eval, "run_command", command)
    task = dict(id="case", question="Question", runtime_model="fixture", criteria=[{"description": "SECRET_RUBRIC"}])
    result = report_eval.policy_runner("v2", 6)(task, [tmp_path / "doc.md"], tmp_path / "case", 10)
    args, env = captured[0]
    for flag, value in (("--report-policy", "v2"), ("--max-model-calls", "6"), ("--context-mode", "full")):
        assert args[args.index(flag)+1] == value
    assert env["DEEPSEEK_MODEL"] == "fixture"
    assert "SECRET_RUBRIC" not in " ".join(args)
    assert result["generation_status"] == "generation_failed"


def test_runtime_policy_mismatch_is_not_graded_as_valid_completion(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(report_eval, "run_task", lambda *a: dict(generation_status="completed", run_directory=str(run)))
    with pytest.raises(RecoveryError, match="runtime controls"):
        report_eval.policy_runner("v2", 6)({}, [], tmp_path, 10)


def test_interrupted_experiment_retains_status_and_cannot_be_reexecuted(tmp_path):
    root, _ = plan(tmp_path)
    def stop(*args, **kwargs):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        report_eval.execute_plan(root, evaluator=stop)
    saved = json.loads((root / "execution/result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "interrupted"
    with pytest.raises(FileExistsError):
        report_eval.execute_plan(root, evaluator=stop)


def test_paired_quality_and_cost_deltas_preserve_unknown_and_failed_tasks():
    left = dict(task_id="a", success=False, quality=dict(citation_precision=0.5, evidence_coverage=1),
                generation_metrics=dict(total_tokens=10))
    right = dict(task_id="a", success=None, quality=dict(citation_precision=1, evidence_coverage=None))
    row = report_eval.paired_rows(dict(v1=dict(tasks=[left]), v2=dict(tasks=[right])))[0]
    assert row["v1_success"] is False and row["v2_success"] is None
    assert row["precision_delta"] == 0.5
    assert row["coverage_delta"] is row["token_delta"] is None
