"""One controlled full-versus-catalog context pilot; no strategy promotion."""
import argparse
import json
import math
import os
import sqlite3
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from .context_policy import POLICY_VERSION, STRATEGIES, verify_saved_input
from .ledger import now
from .storage import atomic_text
from .task_eval import TASK_SUFFIX, evaluate_batch, load_taskset, run_task, summary
from .task_grading import TASK_PROMPT
from .verification import SYSTEM_PROMPT_V2, VerificationError, digest


def code_fingerprints():
    return {name: digest(Path(__file__).with_name(name).read_text(encoding="utf-8")) for name in
            ("agent.py", "cli.py", "tools.py", "policy.py", "context_policy.py", "task_eval.py")}


def strategy_runner(strategy):
    def run(task, paths, directory, timeout):
        record = run_task(dict(task, context_strategy=strategy), paths, directory, timeout)
        record["context_strategy"] = strategy
        if record["generation_status"] != "completed":
            return record
        run_path = Path(record["run_directory"])
        manifest = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
        initial = json.loads((run_path / "input.json").read_text(encoding="utf-8"))
        policy = manifest.get("context_policy")
        if not isinstance(policy, dict) or policy.get("strategy") != strategy:
            raise VerificationError("Run did not use the assigned context strategy.")
        verify_saved_input(initial, policy)
        if (manifest.get("allowed_tools") != ["read_source", "record_evidence"]
                or manifest["tool_policy"]["max_calls"] != 10
                or manifest["tool_policy"]["max_provider_requests"] != 0
                or manifest["tool_policy"]["max_retries"] != 0):
            raise VerificationError("Context arm changed the frozen tool policy.")
        connection = sqlite3.connect((run_path / "application.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
        try:
            reads = list(connection.execute("SELECT attempts,result FROM dispatches WHERE tool='read_source'"))
        finally:
            connection.close()
        record.update(context_policy=policy, read_source_operations=len(reads),
            read_source_attempts=sum(row[0] for row in reads),
            successful_source_reads=sum(row[1] is not None and "error" not in json.loads(row[1]) for row in reads))
        return record
    return run


def paired_rows(arms):
    mappings = {strategy: {row["task_id"]: row for row in result["tasks"]} for strategy, result in arms.items()}
    rows = []
    for task_id in arms["full"]["selected_tasks"]:
        left, right = mappings["full"][task_id], mappings["catalog"][task_id]
        def delta(key, nested=None):
            a, b = (left.get(nested, {}), right.get(nested, {})) if nested else (left, right)
            x, y = a.get(key), b.get(key)
            if type(x) not in {int, float} or type(y) not in {int, float}:
                return None
            return round(y-x, 3) if float in {type(x), type(y)} else y-x
        rows.append(dict(task_id=task_id, full_success=left.get("success"), catalog_success=right.get("success"),
            full_quality=left.get("quality"), catalog_quality=right.get("quality"),
            initial_char_delta=delta("initial_chars", "context_policy"),
            generation_token_delta=delta("total_tokens", "generation_metrics"),
            elapsed_seconds_delta=delta("elapsed_seconds"), source_read_delta=delta("read_source_operations")))
    return rows


def evaluate_context(data, tasks, documents, directory, *, model_name, timeout=180, evaluator=evaluate_batch):
    if not math.isfinite(timeout) or not 1 <= timeout <= 600 or not tasks or len(tasks) > 3:
        raise VerificationError("Context pilot supports 1..3 tasks with timeout 1..600 seconds.")
    for task in tasks:
        if len(task["question"] + TASK_SUFFIX + "".join(documents[name]["content"] for name in task["documents"])) > 20000:
            raise VerificationError("Controlled input budget exceeded before the experiment.")
    directory.mkdir(parents=True, exist_ok=False)
    frozen_code = code_fingerprints()
    controls = dict(dataset_sha256=digest(json.dumps(data, ensure_ascii=False, sort_keys=True)),
        selected_tasks=[row["id"] for row in tasks], corpus_hashes={name: row["sha256"] for name, row in documents.items()},
        question_suffix_sha256=digest(TASK_SUFFIX), task_judge_prompt_sha256=digest(TASK_PROMPT),
        citation_judge_prompt_sha256=digest(SYSTEM_PROMPT_V2), context_policy_version=POLICY_VERSION,
        source_code_sha256=frozen_code, model=model_name, thresholds=data["thresholds"])
    result = dict(status="running", started_at=now(), controls=controls, arm_order=list(STRATEGIES), arms={},
        design="one independent generation per task per arm; full then catalog; no repair; no statistical significance claim",
        default_strategy="full", promotion="no_automatic_promotion")
    def save():
        atomic_text(directory / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
    save()
    for strategy in STRATEGIES:
        if code_fingerprints() != frozen_code:
            raise VerificationError("Experiment code changed between context arms.")
        print(f"Context arm: {strategy}", flush=True)
        arm_path = directory / strategy
        arm = evaluator(data, tasks, documents, arm_path, model_name=model_name,
                        timeout=timeout, runner=strategy_runner(strategy))
        if code_fingerprints() != frozen_code:
            raise VerificationError("Experiment code changed while an arm was running.")
        result["arms"][strategy] = arm
        atomic_text(arm_path / "summary.md", summary(arm))
        save()
    result["pairs"] = paired_rows(result["arms"])
    result["status"] = "complete" if all(arm["status"] == "complete" for arm in result["arms"].values()) else "incomplete_scoring"
    result["finished_at"] = now()
    totals = [arm.get("total_tokens") for arm in result["arms"].values()]
    result["experiment_total_tokens"] = sum(totals) if all(type(value) is int for value in totals) else None
    save()
    atomic_text(directory / "summary.md", comparison_summary(result))
    return result


def comparison_summary(result):
    lines = ["# Context策略初步对照", "", f"Status: {result['status']}；默认策略保持full。",
        "单轮、短文档、固定full→catalog顺序、同模型辅助评分；不构成显著性或泛化证明。", "",
        "| 策略 | Task Success Rate | 生成Token | 评分Token |", "|---|---:|---:|---:|"]
    for name, arm in result["arms"].items():
        lines.append(f"| {name} | {arm.get('task_success_rate')} | {arm.get('generation_total_tokens')} | {arm.get('evaluator_total_tokens')} |")
    lines += ["", "下表差值为catalog减full，Token包括全部生成模型轮次：", "",
              "| 任务 | full通过 | catalog通过 | 初始字符差 | 生成Token差 | 读取调用差 | 耗时差(秒) |",
              "|---|---|---|---:|---:|---:|---:|"]
    for row in result["pairs"]:
        lines.append(f"| {row['task_id']} | {row['full_success']} | {row['catalog_success']} | {row['initial_char_delta']} | {row['generation_token_delta']} | {row['source_read_delta']} | {row['elapsed_seconds_delta']} |")
    lines += ["", f"整个实验Token（含两组评分器）：{result['experiment_total_tokens']}。",
        "字符数不是Token。catalog只减少初始正文注入；read_source仍返回全文，不能声称长任务上下文始终有界。",
        "保留失败和评分分歧，不自动推广策略；参考标签与语义质量仍需人工复核。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Controlled full-versus-catalog context pilot")
    parser.add_argument("--dataset", type=Path, default=Path("evals/research-tasks-v1.json"))
    parser.add_argument("--output", type=Path, default=Path("runs/context-evaluations"))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if not 1 <= args.limit <= 3 or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 600:
        parser.error("limit must be 1..3 and timeout 1..600 seconds")
    directory = args.output / uuid4().hex
    try:
        data, tasks, documents = load_taskset(args.dataset.resolve())
        load_dotenv()
        if not os.getenv("DEEPSEEK_API_KEY", "").strip():
            print("Missing DEEPSEEK_API_KEY.")
            return 2
        result = evaluate_context(data, tasks[:args.limit], documents, directory,
            model_name=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"), timeout=args.timeout)
        print(f"Context pilot: {directory / 'summary.md'}")
        return 0 if result["status"] == "complete" else 3
    except Exception as error:
        print(f"Context pilot failed ({type(error).__name__}); artifacts: {directory}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
