import json
from pathlib import Path
import pytest
from researchdesk.context_eval import evaluate_context, paired_rows, strategy_runner
from researchdesk.task_eval import load_taskset
from researchdesk.verification import VerificationError


def task_data():
    return load_taskset(Path(__file__).parents[1] / "evals" / "research-tasks-v1.json")


def test_context_pilot_keeps_failed_quality_and_does_not_promote_smaller_context(tmp_path):
    data, tasks, docs = task_data()
    seen = []
    def evaluator(data, tasks, documents, artifact, **options):
        strategy = artifact.name
        seen.append(strategy)
        artifact.mkdir()
        return dict(status="complete", selected_tasks=[tasks[0]["id"]], tasks=[dict(task_id=tasks[0]["id"],
            success=strategy == "full", generation_status="completed", context_policy=dict(initial_chars=100 if strategy == "full" else 10),
            generation_metrics=dict(total_tokens=20 if strategy == "full" else 30), elapsed_seconds=1,
            read_source_operations=0 if strategy == "full" else 1)], task_success_rate=1 if strategy == "full" else 0,
            generation_total_tokens=20 if strategy == "full" else 30, evaluator_total_tokens=5, total_tokens=25 if strategy == "full" else 35)
    result = evaluate_context(data, tasks[:1], docs, tmp_path / "pilot", model_name="fixture", evaluator=evaluator)
    assert seen == ["full", "catalog"]
    assert result["default_strategy"] == "full" and result["promotion"] == "no_automatic_promotion"
    assert result["pairs"][0]["generation_token_delta"] == 10
    assert result["pairs"][0]["initial_char_delta"] == -90
    assert result["pairs"][0]["catalog_success"] is False
    assert result["experiment_total_tokens"] == 60


def test_unknown_cost_and_quality_do_not_become_zero_or_success():
    def arm(success, tokens):
        return dict(selected_tasks=["case"], tasks=[dict(task_id="case", success=success,
                    generation_metrics=dict(total_tokens=tokens))])
    row = paired_rows(dict(full=arm(True, 3), catalog=arm(None, None)))[0]
    assert row["catalog_success"] is None and row["generation_token_delta"] is None


def test_context_runner_passes_strategy_without_leaking_rubric(tmp_path, monkeypatch):
    import researchdesk.context_eval as module
    import researchdesk.task_eval as task_module
    captured = []
    def command(args, cwd, log, timeout, environment):
        captured.append(args)
        return dict(returncode=1, timed_out=False, elapsed_seconds=1)
    monkeypatch.setattr(task_module, "run_command", command)
    task = dict(id="case", question="Question", runtime_model="fixture", criteria=[dict(description="PRIVATE_RUBRIC")])
    record = strategy_runner("catalog")(task, [tmp_path / "doc.md"], tmp_path / "case", 10)
    assert captured[0][captured[0].index("--context-mode")+1] == "catalog"
    assert "PRIVATE_RUBRIC" not in " ".join(captured[0])
    assert record["generation_status"] == "generation_failed"


def test_oversized_context_experiment_stops_before_requests(tmp_path):
    data, tasks, docs = task_data()
    docs[tasks[0]["documents"][0]]["content"] = "x" * 20001
    with pytest.raises(VerificationError):
        evaluate_context(data, tasks[:1], docs, tmp_path / "rejected", model_name="fixture")
    assert not (tmp_path / "rejected").exists()
