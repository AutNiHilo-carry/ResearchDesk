import json
from pathlib import Path
import pytest
from researchdesk.model_gap_eval import evaluate, case
from researchdesk.verification import VerificationError


def test_model_return_gap_is_charged_or_stops_before_reexecution(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    root = tmp_path / "gap"
    result = evaluate(root, timeout=45)
    assert result["all_checks_passed"], json.dumps(result)
    assert result["confirmed_injections"] == 2
    assert result["paid_model_requests"] == result["external_tool_requests"] == 0
    enough, stopped = result["cases"]
    assert enough["outcome"] == "completed_with_reexecution"
    assert (enough["script_model_calls_before"], enough["script_model_calls_after"]) == (1, 3)
    assert enough["checks"]["completed_resume_is_noop"]
    assert stopped["outcome"] == "stopped_at_budget"
    assert stopped["script_model_calls_before"] == stopped["script_model_calls_after"] == 1
    assert stopped["checks"]["budget_stop_instead_of_reexecution"]
    before = json.loads((root / "exhausted/before.json").read_text(encoding="utf-8"))
    after = json.loads((root / "exhausted/after.json").read_text(encoding="utf-8"))
    assert before["model_reservations"] == after["model_reservations"]
    assert after["model_budget"]["remaining"] == 0


@pytest.mark.parametrize("timeout", [0, 601, float("nan"), float("inf")])
def test_invalid_limits_fail_before_creating_experiment(tmp_path, timeout):
    root = tmp_path / "gap"
    with pytest.raises(VerificationError):
        evaluate(root, timeout=timeout)
    assert not root.exists()


def test_unreached_boundary_cannot_be_reported_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr("researchdesk.model_gap_eval.kill_at_boundary", lambda *a: dict(forced_termination=False))
    result = case(tmp_path / "case", tmp_path / "not-read.txt", 3, 1)
    assert not result["injection_confirmed"] and not result["passed"]
    assert result["failure"] == "boundary_not_reached_or_not_killed"


def test_failure_after_confirmed_injection_preserves_confirmation(tmp_path, monkeypatch):
    def broken(directory, document, maximum, timeout):
        directory.mkdir()
        (directory / "case.json").write_text(json.dumps(dict(max_model_calls=maximum,
            injection_confirmed=True, passed=False)))
        raise RuntimeError("Fixture failure after confirmed injection")
    monkeypatch.setattr("researchdesk.model_gap_eval.case", broken)
    result = evaluate(tmp_path / "gap", timeout=1)
    assert result["confirmed_injections"] == 2 and not result["all_checks_passed"]
    assert all(c["error_type"] == "RuntimeError" for c in result["cases"])


def test_failed_resume_is_not_retried_or_labeled_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    calls = []
    def failed(*args):
        calls.append(args)
        return dict(returncode=1, timed_out=False)
    monkeypatch.setattr("researchdesk.model_gap_eval.run_command", failed)
    document = tmp_path / "fixture.txt"
    document.write_text("Fixed fixture for a deliberately failed resume")
    result = case(tmp_path / "failure", document, 3, 45)
    assert result["injection_confirmed"] and not result["passed"]
    assert result["outcome"] == "recovery_not_completed" and len(calls) == 1
