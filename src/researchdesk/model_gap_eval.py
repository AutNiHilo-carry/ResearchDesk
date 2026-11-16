"""Scripted process-kill experiment after a model result, before graph commit."""
import argparse
import json
import math
import sqlite3
from pathlib import Path
from uuid import uuid4
from .fault_eval import find_run, kill_at_boundary, recovery_checks, snapshot, worker_command
from .model_budget import budget_snapshot
from .storage import atomic_text
from .task_eval import run_command
from .verification import VerificationError


def state(run):
    found = snapshot(run)
    with sqlite3.connect((run / "application.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as conn:
        found["model_budget"] = budget_snapshot(conn)
        found["model_reservations"] = {rid: dict(role=role, state=status, usage=usage)
            for rid, role, status, usage in conn.execute("SELECT call_id,role,state,usage FROM model_call_reservations")}
        found["model_denials"] = sum(json.loads(raw).get("event") == "model_budget_denied"
                                      for (raw,) in conn.execute("SELECT payload FROM events"))
    manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
    found["model_policy"] = manifest["model_policy"]
    return found


def case(directory, document, maximum, timeout):
    directory.mkdir(parents=True, exist_ok=False)
    root, marker = directory / "research", directory / "fault-ready.json"
    initial = ["Summarize the supplied fixture.", "--local-only", "--document", str(document),
        "--output", str(root), "--max-model-calls", str(maximum), "--max-tool-calls", "4",
        "--max-provider-requests", "0", "--tool-retries", "0"]
    killed = kill_at_boundary(worker_command(root, marker, initial, "model_response", True), directory, marker, timeout)
    result = dict(max_model_calls=maximum, injection=killed, injection_confirmed=False, passed=False)
    if not killed["forced_termination"]:
        return dict(result, failure="boundary_not_reached_or_not_killed")
    run = find_run(root)
    ready = json.loads(marker.read_text(encoding="utf-8"))
    before = state(run)
    confirmed = (ready["point"] == "model_response" and ready["run_id"] == run.name
        and before["manifest_status"] == "running" and before["model_budget"]["reserved"] == 1
        and before["model_budget"]["states"] == {"completed": 1}
        and before["metrics"]["model_calls"] == before["metrics"]["model_completed"] == 1
        and before["model_policy"]["max_calls"] == before["model_budget"]["limit"] == maximum
        and not before["evidence"] and not before["dispatches"] and not (run / "report.md").exists())
    if not confirmed:
        return dict(result, failure="persisted_state_does_not_match_model_return_boundary")
    result.update(injection_confirmed=True, run_directory=str(run))
    atomic_text(directory / "case.json", json.dumps(result, ensure_ascii=False, indent=2))
    atomic_text(directory / "before.json", json.dumps(before, ensure_ascii=False, indent=2))
    # Do not pass another quota on resume: the original manifest is authoritative.
    args = ["--resume", run.name, "--output", str(root)]
    command = worker_command(root, marker, args, "none", True)
    resume = run_command(command, Path.cwd(), directory / "resume.log", timeout)
    after = state(run)
    atomic_text(directory / "after.json", json.dumps(after, ensure_ascii=False, indent=2))
    checks = dict(original_model_slot_preserved=all(after["model_reservations"].get(k) == v
        for k, v in before["model_reservations"].items()), model_policy_unchanged=after["model_policy"] == before["model_policy"],
        saved_input_unchanged=after["input_sha256"] == before["input_sha256"],
        saved_config_unchanged=after["saved_config"] == before["saved_config"],
        prior_records_preserved=all(after[kind].get(rid) == record for kind in ("sources", "evidence")
            for rid, record in before[kind].items()),
        model_quota_within_limit=after["model_budget"]["reserved"] <= maximum,
        no_external_tool_requests=after["counters"]["requests"] == 0)
    if maximum == 3:
        if resume["returncode"] == 0:
            checks.update(recovery_checks(before, after, run, "model_response"))
        checks["completed_after_reexecution"] = (not resume["timed_out"] and resume["returncode"] == 0
            and after["manifest_status"] == "completed")
        checks["lost_model_result_charged"] = after["model_budget"]["reserved"] == after["metrics"]["model_calls"] == 3
        checks["completed_resume_is_noop"] = False
        if checks["completed_after_reexecution"]:
            second = run_command(command, Path.cwd(), directory / "second-resume.log", timeout)
            checks["completed_resume_is_noop"] = not second["timed_out"] and second["returncode"] == 0 and state(run) == after
    else:
        checks["budget_stop_instead_of_reexecution"] = (not resume["timed_out"] and resume["returncode"] == 4
            and after["model_budget"]["reserved"] == after["metrics"]["model_calls"] == 1
            and after["model_budget"]["remaining"] == 0 and after["model_denials"] > before["model_denials"]
            and after["manifest_status"] == "budget_exhausted"
            and not after["evidence"] and not (run / "report.md").exists())
    if maximum == 3:
        outcome = "completed_with_reexecution" if checks["completed_after_reexecution"] else "recovery_not_completed"
    else:
        outcome = "stopped_at_budget" if checks["budget_stop_instead_of_reexecution"] else "budget_stop_not_verified"
    result.update(resume=resume, checks=checks, passed=all(checks.values()), outcome=outcome,
        script_model_calls_before=before["metrics"]["model_calls"], script_model_calls_after=after["metrics"]["model_calls"])
    atomic_text(directory / "case.json", json.dumps(result, ensure_ascii=False, indent=2))
    return result


def evaluate(directory, *, timeout=120):
    if not math.isfinite(timeout) or not 1 <= timeout <= 600:
        raise VerificationError("Timeout must be finite and between 1 and 600 seconds")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    doc = directory / "fixture.txt"
    atomic_text(doc, "Fixed offline model-response checkpoint-gap fixture.")
    cases = []
    for name, maximum in [("sufficient", 3), ("exhausted", 1)]:
        try:
            record = case(directory / name, doc.resolve(), maximum, timeout)
        except Exception as error:
            saved = directory / name / "case.json"
            record = json.loads(saved.read_text(encoding="utf-8")) if saved.is_file() else dict(
                max_model_calls=maximum, injection_confirmed=False)
            record.update(passed=False, error_type=type(error).__name__)
        cases.append(record)
    result = dict(version="model-response-gap-v1", execution_mode="scripted_runtime_simulation", cases=cases,
        all_checks_passed=all(c["passed"] for c in cases), confirmed_injections=sum(c["injection_confirmed"] for c in cases),
        paid_model_requests=0, external_tool_requests=0,
        scope="inline model-end callback after persisted quota/event, before returning result to graph; not remote exactly-once",
        note="A budget stop is a verified safe outcome, not successful task recovery. Scripted calls are not paid requests.")
    atomic_text(directory / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
    lines = ["# 模型响应与检查点间隙实验", "", "固定脚本模型，真实图与自有子进程强杀，无付费API。", "",
        "| 场景 | 确认注入 | 检查通过 | 脚本模型轮次（前/后） |", "|---|---|---|---|"]
    lines += [f"| {c['max_model_calls']}次额度 | {c['injection_confirmed']} | {c['passed']} | {c.get('script_model_calls_before')} / {c.get('script_model_calls_after')} |" for c in cases]
    lines += ["", "3次额度场景允许补做并完成；1次额度场景必须停止，不能计作任务恢复成功。原消耗不退回，不能宣称远程模型调用exactly-once。", ""]
    atomic_text(directory / "summary.md", "\n".join(lines))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/model-gap-evaluations"))
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    directory = args.output / uuid4().hex
    result = evaluate(directory, timeout=args.timeout)
    print(f"Model gap checks: {result['all_checks_passed']}; {directory / 'summary.md'}")
    return 0 if result["all_checks_passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
