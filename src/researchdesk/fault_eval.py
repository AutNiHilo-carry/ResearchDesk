"""Process-kill recovery experiments using the normal CLI and checkpoint primitives."""
import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from .ledger import now
from .run_metrics import run_metrics
from .storage import atomic_text
from .task_eval import TASK_SUFFIX, load_taskset, run_command, stop_owned
from .verification import CITATION, VerificationError, digest, evidence_context, load_run

POINTS = ("evidence", "receipt", "report")


def snapshot(directory):
    connection = sqlite3.connect((directory / "application.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        records = {kind: {rid: json.loads(payload) for rid, payload in connection.execute(
            "SELECT id,payload FROM records WHERE kind=?", (kind,))} for kind in ("sources", "evidence")}
        dispatches = {rid: dict(tool=tool, fingerprint=fp, attempts=attempts,
            result=json.loads(result) if result is not None else None) for rid, tool, fp, attempts, result in
            connection.execute("SELECT id,tool,fingerprint,attempts,result FROM dispatches")}
        receipts = {fp: json.loads(value) for fp, value in connection.execute("SELECT fingerprint,result FROM receipts")}
        counts = dict(connection.execute("SELECT name,value FROM counters"))
    finally:
        connection.close()
    manifest = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    return dict(**records, dispatches=dispatches, receipts=receipts, counters=counts,
        input_sha256=digest((directory / "input.json").read_text(encoding="utf-8")),
        saved_config={key: manifest[key] for key in ("model", "question", "tool_policy", "allowed_tools", "tool_scope")},
        manifest_status=manifest["status"], metrics=run_metrics(directory))


def recovery_checks(before, after, directory, point):
    checks = dict(completed=after["manifest_status"] == "completed",
        saved_input_unchanged=before["input_sha256"] == after["input_sha256"],
        saved_config_unchanged=before["saved_config"] == after["saved_config"],
        prior_records_preserved=all(after[kind].get(rid) == record for kind in ("sources", "evidence")
                                   for rid, record in before[kind].items()),
        completed_receipts_preserved=all(after["receipts"].get(fp) == value for fp, value in before["receipts"].items()),
        dispatch_binding_preserved=all(rid in after["dispatches"] and all(after["dispatches"][rid][key] == value[key]
            for key in ("tool", "fingerprint")) for rid, value in before["dispatches"].items()),
        completed_operations_not_reexecuted=all(after["dispatches"].get(rid) == value
            for rid, value in before["dispatches"].items() if value["result"] is not None),
        pending_operations_settled=all(value["result"] is not None for value in after["dispatches"].values()),
        budget_not_reset=all(after["counters"][key] >= before["counters"][key] for key in ("calls", "requests")),
        budget_within_limit=after["counters"]["calls"] <= after["saved_config"]["tool_policy"]["max_calls"],
        no_external_requests=after["counters"]["requests"] == 0,
        checkpoint_present=(directory / "checkpoints.sqlite3").is_file())
    if point == "report":
        checks["finished_graph_not_reinvoked"] = before["metrics"]["model_calls"] == after["metrics"]["model_calls"]
    report, evidence, sources, _ = load_run(directory)
    cited = set(CITATION.findall(report))
    checks["report_citations_valid"] = bool(cited) and all(evidence_context(eid, evidence, sources)["integrity"] == "valid" for eid in cited)
    checks["exports_match_database"] = True
    for kind in ("sources", "evidence"):
        rows = [json.loads(line) for line in (directory / (kind + ".jsonl")).read_text(encoding="utf-8").splitlines() if line]
        if len(rows) != len(after[kind]) or {row["id"]: row for row in rows} != after[kind]:
            checks["exports_match_database"] = False
    return checks


def worker_command(root, marker, cli_args, point, scripted):
    return [sys.executable, "-m", "researchdesk.fault_worker", "--point", point, "--root", str(root),
            "--marker", str(marker)] + (["--scripted"] if scripted else []) + ["--"] + cli_args


def find_run(root):
    runs = [path for path in root.iterdir() if path.is_dir() and re.fullmatch("[0-9a-f]{32}", path.name)]
    if len(runs) != 1:
        raise VerificationError("Experiment needs exactly one owned research run.")
    return runs[0]


def kill_at_boundary(command, directory, marker, timeout):
    """Only stop the process tree spawned here; marker PID is informational only."""
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})
    started = time.monotonic()
    with (directory / "initial.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=Path.cwd(), stdout=log, stderr=subprocess.STDOUT, **options)
        reached = False
        try:
            while process.poll() is None and time.monotonic()-started < timeout:
                if marker.is_file():
                    reached = True
                    break
                time.sleep(0.05)
            termination_requested = process.poll() is None
            stop_owned(process)
            return dict(boundary_reached=reached, termination_requested=termination_requested,
                        forced_termination=reached and termination_requested and process.returncode != 0,
                        returncode=process.returncode, elapsed_seconds=round(time.monotonic()-started, 3))
        finally:
            if process.poll() is None:
                stop_owned(process)


def run_case(task, paths, directory, *, point, scripted=False, timeout=180):
    directory.mkdir(parents=True, exist_ok=False)
    root, marker = directory / "research", directory / "fault-ready.json"
    initial = [task["question"] + TASK_SUFFIX, "--local-only", "--output", str(root),
               "--max-tool-calls", "10", "--max-provider-requests", "0", "--tool-retries", "0"]
    for path in paths:
        initial.extend(["--document", str(path)])
    command = worker_command(root, marker, initial, point, scripted)
    record = dict(point=point, injected=False, passed=False)
    if point == "none":
        record["initial"] = run_command(command, Path.cwd(), directory / "initial.log", timeout)
        if record["initial"]["returncode"] != 0 or record["initial"]["timed_out"]:
            return record
        run = find_run(root)
        after = snapshot(run)
        record.update(run_directory=str(run), metrics=after["metrics"], checks=recovery_checks(after, after, run, point))
        record["passed"] = all(record["checks"].values())
        return record
    record["initial"] = kill_at_boundary(command, directory, marker, timeout)
    if not record["initial"]["forced_termination"]:
        record["failure"] = "fault_boundary_not_reached_or_not_terminated"
        return record
    run = find_run(root)
    ready = json.loads(marker.read_text(encoding="utf-8"))
    if ready["point"] != point or ready["run_id"] != run.name:
        raise VerificationError("Fault marker does not identify the owned run.")
    before = snapshot(run)
    pending = [row for row in before["dispatches"].values() if row["tool"] == "record_evidence" and row["result"] is None]
    boundary_valid = bool(before["evidence"]) and before["manifest_status"] == "running"
    if point == "evidence":
        boundary_valid = boundary_valid and bool(pending)
    elif point == "receipt":
        boundary_valid = boundary_valid and any(row["tool"] == "record_evidence" and row["result"] is not None
                                               for row in before["dispatches"].values())
    elif point == "report":
        boundary_valid = boundary_valid and (run / "report.md").is_file()
    if not boundary_valid:
        record["failure"] = "durable_state_does_not_match_claimed_fault_boundary"
        return record
    record.update(injected=True, run_directory=str(run), before_metrics=before["metrics"])
    atomic_text(directory / "case.json", json.dumps(record, ensure_ascii=False, indent=2))
    atomic_text(directory / "before.json", json.dumps(before, ensure_ascii=False, indent=2))
    resume_args = ["--resume", run.name, "--output", str(root)]
    resume = worker_command(root, marker, resume_args, "none", scripted)
    record["resume"] = run_command(resume, Path.cwd(), directory / "resume.log", timeout)
    if record["resume"]["returncode"] != 0 or record["resume"]["timed_out"]:
        record["failure"] = "resume_failed"
        record["metrics"] = run_metrics(run)
        return record
    after = snapshot(run)
    atomic_text(directory / "after.json", json.dumps(after, ensure_ascii=False, indent=2))
    checks = recovery_checks(before, after, run, point)
    report_hash = digest((run / "report.md").read_text(encoding="utf-8"))
    record["second_resume"] = run_command(resume, Path.cwd(), directory / "second-resume.log", timeout)
    twice = snapshot(run)
    checks["completed_resume_is_noop"] = (record["second_resume"]["returncode"] == 0
        and not record["second_resume"]["timed_out"] and twice == after
        and digest((run / "report.md").read_text(encoding="utf-8")) == report_hash)
    record.update(checks=checks, passed=all(checks.values()), metrics=after["metrics"])
    return record


def evaluate_recovery(data, task, documents, directory, *, scripted=False, timeout=180, points=POINTS):
    if not math.isfinite(timeout) or not 1 <= timeout <= 600 or not points or len(set(points)) != len(points) or not set(points) <= set(POINTS):
        raise VerificationError("Invalid fault experiment limits.")
    if len(task["question"] + TASK_SUFFIX + "".join(documents[name]["content"] for name in task["documents"])) > 20000:
        raise VerificationError("Recovery task exceeds the controlled input character budget.")
    directory.mkdir(parents=True, exist_ok=False)
    corpus = directory / "corpus"
    corpus.mkdir()
    paths = []
    for number, name in enumerate(task["documents"], 1):
        path = corpus / f"{number:02d}-{Path(name).name}"
        atomic_text(path, documents[name]["content"])
        paths.append(path.resolve())
    result = dict(status="running", started_at=now(), task_id=task["id"],
        execution_mode="scripted_runtime_simulation" if scripted else "real_model_local_documents",
        model="scripted-recovery-fixture" if scripted else os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        taskset_sha256=digest(json.dumps(data, ensure_ascii=False, sort_keys=True)),
        document_hashes={name: documents[name]["sha256"] for name in task["documents"]},
        question_sha256=digest(task["question"] + TASK_SUFFIX), timeout_per_process=timeout, cases=[],
        recovery_scope="process kill at three selected local-evidence/report boundaries; structural completion only",
        business_quality="not scored", remote_exactly_once="not claimed")
    def save():
        atomic_text(directory / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
    save()
    for point in ("none", *points):
        print(f"Recovery scenario: {point}", flush=True)
        try:
            record = run_case(task, paths, directory / point, point=point, scripted=scripted, timeout=timeout)
        except Exception as error:
            saved = directory / point / "case.json"
            record = json.loads(saved.read_text(encoding="utf-8")) if saved.is_file() else dict(point=point, injected=False)
            record.update(passed=False, error_type=type(error).__name__)
            if record.get("run_directory"):
                try:
                    record["metrics"] = run_metrics(Path(record["run_directory"]))
                except Exception:
                    pass
        result["cases"].append(record)
        save()
        print(f"Passed: {record['passed']}", flush=True)
    faults = result["cases"][1:]
    injected = [row for row in faults if row["injected"]]
    result.update(status="complete", injected_cases=len(injected), planned_fault_cases=len(points),
        recovery_rate=sum(row["passed"] for row in injected)/len(injected) if injected else None,
        scenario_pass_rate=sum(row["passed"] for row in faults)/len(points),
        fault_boundary_reach_rate=len(injected)/len(points), all_passed=all(row["passed"] for row in result["cases"]),
        finished_at=now())
    known = all(row.get("metrics", {}).get("token_usage_complete") for row in result["cases"])
    result["total_tokens"] = sum(row["metrics"]["total_tokens"] for row in result["cases"]) if known else None
    save()
    atomic_text(directory / "summary.md", summary(result))
    return result


def summary(result):
    lines = ["# 任务层进程终止与恢复实验", "", f"执行模式：{result['execution_mode']}。",
        f"故障到达：{result['injected_cases']}/{result['planned_fault_cases']}；Recovery Rate：{result['recovery_rate']}。",
        f"全部计划故障的通过率：{result['scenario_pass_rate']}；Token：{result['total_tokens']}。", "",
        "| 场景 | 已注入 | 通过 | 未通过检查 |", "|---|---|---|---|"]
    for row in result["cases"]:
        failures = [key for key, value in row.get("checks", {}).items() if not value]
        lines.append(f"| {row['point']} | {row['injected']} | {row['passed']} | {failures or row.get('failure', '')} |")
    lines += ["", "只衡量所选进程终止窗口的结构性恢复，不代表语义任务成功、断电恢复或远程副作用exactly-once。",
              "未到达故障窗口计入计划场景失败；Recovery Rate只以实际确认注入的场景为分母。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Bounded end-to-end process-kill recovery evaluation")
    parser.add_argument("--dataset", type=Path, default=Path("evals/research-tasks-v1.json"))
    parser.add_argument("--task", default="offline_selection")
    parser.add_argument("--output", type=Path, default=Path("runs/recovery-evaluations"))
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--scripted", action="store_true")
    args = parser.parse_args()
    directory = args.output / uuid4().hex
    try:
        data, tasks, documents = load_taskset(args.dataset.resolve())
        task = next((row for row in tasks if row["id"] == args.task), None)
        if task is None:
            raise VerificationError("Task not found.")
        if not args.scripted:
            load_dotenv()
            if not os.getenv("DEEPSEEK_API_KEY", "").strip():
                print("Missing DEEPSEEK_API_KEY.")
                return 2
        result = evaluate_recovery(data, task, documents, directory, scripted=args.scripted, timeout=args.timeout)
        print(f"Recovery experiment: {directory / 'summary.md'}")
        return 0 if result["all_passed"] else 3
    except Exception as error:
        print(f"Recovery evaluation failed ({type(error).__name__}); artifacts: {directory}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
