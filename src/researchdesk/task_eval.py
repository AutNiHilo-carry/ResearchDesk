"""Controlled-corpus, end-to-end task evaluation. No automatic promotion."""
import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field
from .ledger import now
from .storage import atomic_text
from .verification import (ModelJudge, VerificationError, assess, digest, failure_diagnostics,
                           load_run, prepare, split_units)
from .task_grading import TaskJudge, grade_tasks
from .run_metrics import run_metrics

TASK_SUFFIX = ("\n仅使用附带的本地资料，不进行外部检索。正文控制在200字以内。"
               "每个事实单独一行并紧跟完整证据引用，纯建议和证据不足声明单独成行。"
               "至少记录并引用一条资料中可确认的事实。")


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    description: str = Field(min_length=1, max_length=1000)


class ResearchTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    question: str = Field(min_length=1, max_length=4000)
    documents: list[str] = Field(min_length=1, max_length=4)
    criteria: list[Criterion] = Field(min_length=1, max_length=8)


def load_taskset(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != "research-tasks-v1":
        raise VerificationError("Unsupported research taskset.")
    tasks = [ResearchTask.model_validate(row).model_dump() for row in data["tasks"]]
    if not 1 <= len(tasks) <= 10 or len({row["id"] for row in tasks}) != len(tasks):
        raise VerificationError("Taskset has invalid task count or duplicate IDs.")
    documents = {}
    for task in tasks:
        if len({row["id"] for row in task["criteria"]}) != len(task["criteria"]):
            raise VerificationError("Duplicate criterion IDs.")
        for name in task["documents"]:
            location = (path.parent / name).resolve()
            if not location.is_relative_to(path.parent.resolve()) or location.suffix.lower() not in {".txt", ".md"}:
                raise VerificationError("Task document must stay inside the dataset directory.")
            raw = location.read_bytes()
            if len(raw) > 200000:
                raise VerificationError("Task document exceeds 200 KB.")
            documents[name] = dict(content=raw.decode("utf-8"), sha256=hashlib.sha256(raw).hexdigest())
    thresholds = data.get("thresholds", {})
    if set(thresholds) != {"citation_precision", "evidence_coverage"} or not all(
            isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
            for value in thresholds.values()):
        raise VerificationError("Invalid task quality thresholds.")
    return data, tasks, documents


def stop_owned(process):
    """Stop only a subprocess/tree created by this evaluator."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                 capture_output=True, timeout=10)
        if stopped.returncode != 0 and process.poll() is None:
            process.kill()
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def run_command(command, directory, log_path, timeout, environment=None):
    started = time.monotonic()
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                   env=environment, **options)
        timed_out = False
        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_owned(process)
        finally:
            if process.poll() is None:
                stop_owned(process)
    return dict(returncode=process.returncode, timed_out=timed_out,
                elapsed_seconds=round(time.monotonic()-started, 3))


def run_task(task, paths, directory, timeout):
    directory.mkdir(parents=True, exist_ok=False)
    research = directory / "research"
    question = task["question"] + TASK_SUFFIX  # Rubric/expected criteria never go to generator.
    command = [sys.executable, "-m", "researchdesk.cli", question, "--local-only",
               "--output", str(research), "--max-tool-calls", "10", "--max-provider-requests", "0",
               "--tool-retries", "0"]
    if task.get("context_strategy") is not None:
        command.extend(["--context-mode", task["context_strategy"]])
    if task.get("report_policy") is not None:
        command.extend(["--report-policy", task["report_policy"]])
    if task.get("max_model_calls") is not None:
        command.extend(["--max-model-calls", str(task["max_model_calls"])])
    for path in paths:
        command.extend(["--document", str(path)])
    environment = os.environ.copy()
    environment["DEEPSEEK_MODEL"] = task.get("runtime_model", os.getenv("DEEPSEEK_MODEL", "deepseek-flash"))
    outcome = run_command(command, Path.cwd(), directory / "generation.log", timeout, environment)
    runs = list(research.glob("[0-9a-f]" * 32))
    run = runs[0] if len(runs) == 1 else None
    status = "timed_out" if outcome["timed_out"] else "generation_failed"
    record = dict(task_id=task["id"], question_sha256=digest(question), **outcome)
    if run is not None:
        record["run_directory"] = str(run)
        record["generation_metrics"] = run_metrics(run)
        if (run / "run.json").is_file():
            manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
            if outcome["returncode"] == 0 and manifest.get("status") == "completed":
                status = "completed"
            record["model"] = manifest.get("model")
    record["generation_status"] = status
    return record


def quality_metrics(rows):
    factual = [row for row in rows if row["factual"]]
    links = [link for row in factual for link in row["links"]]
    return dict(factual_units=len(factual), citation_links=len(links),
                invalid_links=sum(link["verdict"] == "invalid_evidence" for row in rows for link in row["links"]),
                citation_precision=sum(link["verdict"] == "supported" for link in links)/len(links) if links else None,
                evidence_coverage=sum(row["covered"] for row in factual)/len(factual) if factual else None)


def task_success(record, rubric, metrics, thresholds):
    return (record["generation_status"] == "completed" and rubric["rubric_passed"]
            and metrics["invalid_links"] == 0
            and metrics["citation_precision"] is not None and metrics["evidence_coverage"] is not None
            and metrics["citation_precision"] >= thresholds["citation_precision"]
            and metrics["evidence_coverage"] >= thresholds["evidence_coverage"]
            and record.get("generation_metrics", {}).get("provider_requests") == 0)


def evaluate_batch(data, tasks, documents, artifact, *, runner=run_task, model_name="deepseek-flash",
                   task_judge=None, evidence_judge=None, timeout=180):
    evaluation_started = time.monotonic()
    estimated_rubric = []
    for task in tasks:
        texts = [documents[name]["content"] for name in task["documents"]]
        if len(task["question"] + TASK_SUFFIX + "".join(texts)) > 20000:
            raise VerificationError("Controlled task input exceeds generation character budget.")
        estimated_rubric.append(dict(question=task["question"], criteria=task["criteria"],
                                      documents=texts, report="x" * 4000))
    if len(json.dumps(estimated_rubric, ensure_ascii=False)) > 60000:
        raise VerificationError("Task batch exceeds preflight grader character budget.")
    artifact.mkdir(parents=True, exist_ok=False)
    corpus = artifact / "corpus"
    corpus.mkdir()
    paths = {}
    for number, (name, document) in enumerate(documents.items(), 1):
        path = corpus / f"{number:02d}-{Path(name).name}"
        atomic_text(path, document["content"])
        paths[name] = path.resolve()
    result = dict(status="running", dataset_version=data["version"], label_status=data.get("label_status"),
                  dataset_sha256=digest(json.dumps(data, sort_keys=True, ensure_ascii=False)),
                  selected_tasks=[task["id"] for task in tasks], scope=data.get("scope"),
                  started_at=now(), generator_model=model_name, judge_model=model_name,
                  model_settings=dict(temperature=0, generator_max_tokens=4096, evaluator_max_tokens=8192),
                  thresholds=data["thresholds"], corpus_hashes={name: row["sha256"] for name, row in documents.items()},
                  task_prompt_suffix_sha256=digest(TASK_SUFFIX), task_timeout_seconds=timeout,
                  tasks=[], promotion="human_review_required")
    def save():
        atomic_text(artifact / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
    save()
    for index, task in enumerate(tasks, 1):
        print(f"Research task {index}/{len(tasks)}: {task['id']}", flush=True)
        try:
            record = runner(dict(task, runtime_model=model_name), [paths[name] for name in task["documents"]],
                            artifact / "tasks" / task["id"], timeout)
        except Exception as error:
            record = dict(task_id=task["id"], generation_status="execution_error", error_type=type(error).__name__)
        result["tasks"].append(record)
        save()
        print(f"Generation: {record['generation_status']}", flush=True)
    completed = [row for row in result["tasks"] if row["generation_status"] == "completed"]
    task_map = {task["id"]: task for task in tasks}
    all_units, all_evidence, all_sources, grade_input = [], {}, {}, []
    for record in completed:
        try:
            if record.get("model") != model_name:
                raise VerificationError("Generator model differs from the frozen evaluation configuration.")
            report, evidence, sources, _ = load_run(Path(record["run_directory"]))
            units = split_units(report)
        except Exception as error:
            record.update(generation_status="invalid_artifacts", error_type=type(error).__name__)
            continue
        for unit in units:
            unit["unit_id"] = record["task_id"] + ":" + unit["unit_id"]
            unit["task_id"] = record["task_id"]
        all_units.extend(units)
        all_evidence.update(evidence)
        all_sources.update(sources)
        task = task_map[record["task_id"]]
        grade_input.append(dict(task_id=task["id"], question=task["question"], criteria=task["criteria"],
            report=report, documents=[documents[name]["content"] for name in task["documents"]]))
    completed = [row for row in result["tasks"] if row["generation_status"] == "completed"]
    judges = []
    grading_started = time.monotonic()
    try:
        if completed:
            payload, fingerprint = prepare(all_units, all_evidence, all_sources)
            rubric_payload = {"tasks": grade_input}
            if len(json.dumps(rubric_payload, ensure_ascii=False)) > 60000:
                raise VerificationError("Rubric input budget exceeded.")
            # Both payloads validated before the first grader request.
            task_judge = task_judge or TaskJudge(model_name)
            evidence_judge = evidence_judge or ModelJudge(model_name, prompt_version="v2")
            judges = [task_judge, evidence_judge]
            grades = grade_tasks(rubric_payload, task_judge)
            result["task_grades"] = grades
            save()
            quality = assess(payload, evidence_judge)
            atomic_text(artifact / "citation-quality.json", json.dumps(quality, ensure_ascii=False, indent=2))
            result["quality_input_sha256"] = fingerprint
            for record in completed:
                rows = [row for row in quality["units"] if row["task_id"] == record["task_id"]]
                metrics = quality_metrics(rows)
                record.update(quality=metrics, rubric=grades[record["task_id"]],
                              success=task_success(record, grades[record["task_id"]], metrics, data["thresholds"]))
        for record in result["tasks"]:
            if record["generation_status"] != "completed":
                record["success"] = False
        result["status"] = "complete"
        result["task_success_rate"] = sum(row["success"] for row in result["tasks"])/len(tasks)
    except Exception as error:
        result.update(status="scoring_failed", task_success_rate=None, **failure_diagnostics(error))
        result["unaccepted_evaluator_outputs"] = [getattr(judge, "last_output", None) for judge in judges]
        for record in completed:
            record["success"] = None
    result["generation_completion_rate"] = len(completed)/len(tasks)
    result["grading_elapsed_seconds"] = round(time.monotonic()-grading_started, 3)
    result["task_grader_prompt_version"] = getattr(task_judge, "prompt_version", "research-task-rubric-v1")
    result["task_grader_prompt_sha256"] = getattr(task_judge, "prompt_sha256", None)
    result["citation_judge_prompt_version"] = getattr(evidence_judge, "prompt_version", "report-quote-v2")
    result["citation_judge_prompt_sha256"] = getattr(evidence_judge, "prompt_sha256", None)
    generation = [row.get("generation_metrics", {}) for row in result["tasks"]]
    outcomes_known = all(isinstance(row.get("tool_outcomes"), int) and
                         isinstance(row.get("tool_successes"), int) for row in generation)
    total_outcomes = sum(row["tool_outcomes"] for row in generation) if outcomes_known else 0
    result["tool_success_rate"] = (sum(row["tool_successes"] for row in generation)/total_outcomes
                                   if outcomes_known and total_outcomes else None)
    result["retry_count"] = (sum(row["retry_count"] for row in generation)
                             if all(isinstance(row.get("retry_count"), int) for row in generation) else None)
    known = all(row.get("token_usage_complete") for row in generation)
    result["generation_total_tokens"] = sum(row["total_tokens"] for row in generation) if known else None
    result["evaluator_usage"] = [getattr(judge, "usage", None) for judge in judges]
    evaluator_known = all(isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int)
                          for usage in result["evaluator_usage"])
    result["evaluator_total_tokens"] = sum(row["total_tokens"] for row in result["evaluator_usage"]) if evaluator_known else None
    result["total_tokens"] = (result["generation_total_tokens"] + result["evaluator_total_tokens"]
                              if known and evaluator_known else None)
    result.update(estimated_fee=None, recovery_rate=None, finished_at=now(),
                  evaluation_elapsed_seconds=round(time.monotonic()-evaluation_started, 3))
    save()
    return result


def summary(result):
    lines = ["# End-to-End Research Task Evaluation", "", "受控资料，模型辅助评分；需人工复核。",
             f"Scope: {result.get('scope') or 'unspecified'}",
             f"Label status: {result.get('label_status') or 'unspecified'}", "",
             f"Status: {result['status']}", f"Task Success Rate: {result.get('task_success_rate')}",
             f"Generation Completion Rate: {result.get('generation_completion_rate')}", "",
             "| Task | Generation | Success | Citation precision | Coverage | Tokens | Seconds | Retries |",
             "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in result["tasks"]:
        metrics, quality = row.get("generation_metrics", {}), row.get("quality", {})
        lines.append(f"| {row['task_id']} | {row['generation_status']} | {row.get('success')} "
                     f"| {quality.get('citation_precision')} | {quality.get('evidence_coverage')} "
                     f"| {metrics.get('total_tokens')} | {row.get('elapsed_seconds')} | {metrics.get('retry_count')} |")
    lines += ["", f"Generation tokens: {result.get('generation_total_tokens')}",
              f"Evaluator tokens: {result.get('evaluator_total_tokens')}",
              "Monetary cost and Recovery Rate: N/A (not measured by this batch).", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Controlled-corpus end-to-end research evaluation")
    parser.add_argument("--dataset", type=Path, default=Path("evals/research-tasks-v1.json"))
    parser.add_argument("--output", type=Path, default=Path("runs/task-evaluations"))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10 or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 600:
        parser.error("limit must be 1..10 and timeout 1..600 seconds")
    load_dotenv()
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        print("Missing DEEPSEEK_API_KEY.", file=sys.stderr)
        return 2
    artifact = args.output / uuid4().hex
    try:
        data, tasks, documents = load_taskset(args.dataset.resolve())
        result = evaluate_batch(data, tasks[:args.limit], documents, artifact,
                                model_name=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"), timeout=args.timeout)
        atomic_text(artifact / "summary.md", summary(result))
        print(f"Task evaluation: {artifact / 'summary.md'}")
        print(f"Status: {result['status']}; Task Success Rate: {result.get('task_success_rate')}")
        return 0 if result["status"] == "complete" and result["task_success_rate"] == 1 else 3
    except Exception as error:
        print(f"Task evaluation failed ({type(error).__name__}); artifacts: {artifact}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

