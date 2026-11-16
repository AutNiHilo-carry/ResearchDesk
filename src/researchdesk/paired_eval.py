"""Paired repair experiment on frozen initial reports; no fresh generation baseline."""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from .repair import frozen_feedback
from .evaluation_audit import audit_summary, compare_evaluations
from .run_metrics import run_metrics
from .storage import atomic_text
from .task_eval import (TASK_SUFFIX, evaluate_batch, load_taskset, run_command, summary)
from .task_grading import TASK_PROMPT
from .verification import SYSTEM_PROMPT_V2, VerificationError, digest, load_run, prepare, split_units


def baseline_snapshot(directory, data, tasks, documents):
    baseline = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if baseline.get("status") != "complete":
        raise VerificationError("Paired repair needs a fully scored baseline.")
    if baseline["dataset_sha256"] != digest(json.dumps(data, sort_keys=True, ensure_ascii=False)):
        raise VerificationError("Frozen taskset changed.")
    if baseline["thresholds"] != data["thresholds"] or baseline["task_prompt_suffix_sha256"] != digest(TASK_SUFFIX):
        raise VerificationError("Baseline generation constraints or scoring thresholds changed.")
    if baseline["corpus_hashes"] != {name: row["sha256"] for name, row in documents.items()}:
        raise VerificationError("Frozen corpus changed.")
    if (baseline.get("task_grader_prompt_sha256") != digest(TASK_PROMPT)
            or baseline.get("citation_judge_prompt_sha256") != digest(SYSTEM_PROMPT_V2)):
        raise VerificationError("Evaluator prompts changed; this is not a controlled paired comparison.")
    selected = baseline["selected_tasks"]
    task_map = {task["id"]: task for task in tasks}
    if len(set(selected)) != len(selected) or not set(selected) <= set(task_map):
        raise VerificationError("Baseline selected-task IDs are invalid.")
    records = {row["task_id"]: row for row in baseline["tasks"]}
    if len(records) != len(baseline["tasks"]) or set(records) != set(selected):
        raise VerificationError("Baseline records must match selected tasks exactly once.")
    units, evidence, sources, parents = [], {}, {}, {}
    for task_id in selected:
        record = records[task_id]
        if record["generation_status"] != "completed":
            raise VerificationError("Only completed baseline reports can be paired for repair.")
        run_name = Path(record["run_directory"]).name
        if not re.fullmatch("[0-9a-f]{32}", run_name):
            raise VerificationError("Invalid baseline run directory.")
        run = (directory / "tasks" / task_id / "research" / run_name).resolve()
        if not run.is_relative_to(directory.resolve()):
            raise VerificationError("Baseline run escapes its evaluation directory.")
        report, found_evidence, found_sources, metadata = load_run(run)
        if "```" in report or "~~~" in report:
            raise VerificationError("Baseline has unscored code blocks; full paired input equality cannot be established.")
        if metadata["model"] != baseline["generator_model"]:
            raise VerificationError("Baseline model names are inconsistent.")
        rows = split_units(report)
        for row in rows:
            row["unit_id"] = task_id + ":" + row["unit_id"]
            row["task_id"] = task_id
        units.extend(rows)
        evidence.update(found_evidence)
        sources.update(found_sources)
        parents[task_id] = dict(record, source_directory=str(run), report_sha256=digest(report))
    _, fingerprint = prepare(units, evidence, sources)
    if fingerprint != baseline["quality_input_sha256"]:
        raise VerificationError("Baseline report/evidence input changed after scoring.")
    quality = json.loads((directory / "citation-quality.json").read_text(encoding="utf-8"))
    if quality.get("status") != "complete":
        raise VerificationError("Frozen baseline citation judgments are incomplete.")
    feedback = {task_id: [row for row in quality["units"] if row["task_id"] == task_id] for task_id in selected}
    for task_id in selected:
        report = load_run(Path(parents[task_id]["source_directory"]))[0]
        frozen_feedback(report, feedback[task_id])
    return baseline, [task_map[task_id] for task_id in selected], parents, feedback


def repair_runner(parents, feedback):
    def run(task, paths, directory, timeout):
        directory.mkdir(parents=True, exist_ok=False)
        source = Path(parents[task["id"]]["source_directory"])
        destination = directory / "research" / uuid4().hex
        snapshot = directory / "frozen-feedback.json"
        atomic_text(snapshot, json.dumps(feedback[task["id"]], ensure_ascii=False, indent=2))
        command = [sys.executable, "-m", "researchdesk.repair", str(source),
                   "--destination", str(destination), "--feedback", str(snapshot)]
        outcome = run_command(command, Path.cwd(), directory / "repair.log", timeout)
        metrics = run_metrics(destination) if destination.exists() else {}
        record = dict(task_id=task["id"], model=task["runtime_model"],
            run_directory=str(destination), parent_run_directory=str(source),
            original_report_sha256=parents[task["id"]]["report_sha256"], generation_metrics=metrics,
            generation_status="timed_out" if outcome["timed_out"] else "repair_failed",
            repair_elapsed_seconds=outcome["elapsed_seconds"],
            elapsed_seconds=round(parents[task["id"]]["elapsed_seconds"] + outcome["elapsed_seconds"], 3),
            returncode=outcome["returncode"], timed_out=outcome["timed_out"])
        if (destination / "run.json").is_file() and outcome["returncode"] in {0, 3}:
            manifest = json.loads((destination / "run.json").read_text(encoding="utf-8"))
            if manifest.get("status") == "completed" and manifest["model"] == task["runtime_model"]:
                record["generation_status"] = "completed"
                record["repair_selection"] = manifest["repair"]
        previous = parents[task["id"]]["generation_metrics"].get("total_tokens")
        current = metrics.get("total_tokens")
        record["baseline_generation_tokens"] = previous
        record["repair_added_tokens"] = current-previous if current is not None and previous is not None else None
        if digest(load_run(source)[0]) != record["original_report_sha256"]:
            raise VerificationError("Original baseline report was changed.")
        return record
    return run


def main():
    parser = argparse.ArgumentParser(description="Paired one-round report repair evaluation")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--dataset", type=Path, default=Path("evals/research-tasks-v1.json"))
    parser.add_argument("--output", type=Path, default=Path("runs/paired-evaluations"))
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("timeout must be 1..600 seconds")
    load_dotenv()
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        print("Missing DEEPSEEK_API_KEY.")
        return 2
    artifact = args.output / uuid4().hex
    try:
        data, tasks, documents = load_taskset(args.dataset.resolve())
        baseline, selected, parents, feedback = baseline_snapshot(args.baseline.resolve(), data, tasks, documents)
        model = baseline["generator_model"]
        if os.getenv("DEEPSEEK_MODEL", "deepseek-flash") != model:
            raise VerificationError("Current model differs from the baseline.")
        result = evaluate_batch(data, selected, documents, artifact, model_name=model,
            runner=repair_runner(parents, feedback), timeout=args.timeout)
        result["paired_baseline"] = str(args.baseline.resolve())
        result["baseline_task_success_rate"] = baseline["task_success_rate"]
        result["baseline_dataset_sha256"] = baseline["dataset_sha256"]
        result["feedback_origin"] = "frozen_baseline_judgments; no task rubric sent to repair"
        extra = [row.get("repair_added_tokens") for row in result["tasks"]]
        result["newly_spent_tokens"] = (sum(extra) + result["evaluator_total_tokens"]
            if all(value is not None for value in extra) and result["evaluator_total_tokens"] is not None else None)
        result["per_task_seconds_scope"] = "historical initial generation plus current repair wall time"
        atomic_text(artifact / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
        try:
            audit = compare_evaluations(args.baseline.resolve(), artifact.resolve())
        except VerificationError as error:
            audit = dict(status="audit_unavailable", reason=str(error))
        atomic_text(artifact / "comparison-audit.json", json.dumps(audit, ensure_ascii=False, indent=2))
        report = summary(result) + (f"\nBaseline Task Success Rate: {baseline['task_success_rate']}\n"
            f"New experiment API tokens: {result['newly_spent_tokens']}\n"
            "Initial drafts, corpus, task rubrics and thresholds were frozen; small model-judged sample only.\n")
        report += "\n" + audit_summary(audit)
        atomic_text(artifact / "summary.md", report)
        print(f"Paired evaluation: {artifact / 'summary.md'}")
        print(f"Baseline: {baseline['task_success_rate']}; candidate: {result.get('task_success_rate')}")
        return 0 if result["status"] == "complete" and result["task_success_rate"] == 1 else 3
    except Exception as error:
        print(f"Paired evaluation failed ({type(error).__name__}); artifacts: {artifact}")
        if isinstance(error, VerificationError):
            print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

