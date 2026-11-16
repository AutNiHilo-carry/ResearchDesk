"""Frozen, one-shot report-policy pilot; separate task and citation grading."""
import argparse
import json
import math
import os
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from .ledger import now
from .model_budget import VERSION
from .report_policy import policy_spec
from .storage import RecoveryError, atomic_text
from .task_eval import TASK_SUFFIX, evaluate_batch, load_taskset, run_task, summary
from .task_grading import TASK_PROMPT
from .verification import SYSTEM_PROMPT_V2, digest


def code_hashes():
    return {path.name: digest(path.read_text(encoding="utf-8"))
            for path in sorted(Path(__file__).parent.glob("*.py"))}


def controls(plan):
    tasks, documents = plan["tasks"], plan["documents"]
    if (plan.get("version") != 1 or not 1 <= len(tasks) <= 3
            or not isinstance(plan.get("model"), str) or not plan["model"].strip()
            or type(plan.get("max_model_calls")) is not int or not 1 <= plan["max_model_calls"] <= 12
            or type(plan.get("timeout")) not in {int, float} or not math.isfinite(plan["timeout"])
            or not 1 <= plan["timeout"] <= 600):
        raise RecoveryError("Invalid report experiment controls.")
    if len({task["id"] for task in tasks}) != len(tasks):
        raise RecoveryError("Duplicate experiment task IDs.")
    rubric = []
    for task in tasks:
        text = "".join(documents[name]["content"] for name in task["documents"])
        if len(task["question"] + TASK_SUFFIX + text) > 20000:
            raise RecoveryError("Experiment input exceeds the generation budget.")
        rubric.append(dict(question=task["question"], criteria=task["criteria"], documents=text, report="x"*4000))
    if len(json.dumps(rubric, ensure_ascii=False)) > 60000:
        raise RecoveryError("Experiment exceeds the grader budget.")
    for document in documents.values():
        if digest(document["content"]) != document["sha256"]:
            raise RecoveryError("Frozen document content changed.")
    payload = {key: plan[key] for key in ("data", "tasks", "documents", "model", "max_model_calls", "timeout")}
    return dict(input_sha256=digest(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        source_code_sha256=code_hashes(), report_policies={v: policy_spec(v) for v in ("v1", "v2")},
        task_suffix_sha256=digest(TASK_SUFFIX), task_judge_sha256=digest(TASK_PROMPT),
        evidence_judge_sha256=digest(SYSTEM_PROMPT_V2), context_mode="full",
        tool_policy=dict(max_calls=10, max_provider_requests=0, max_retries=0),
        max_sdk_requests=2*len(tasks)*plan["max_model_calls"]+4,
        request_limit_scope="generation per task plus at most two batch graders per arm; no SDK retries; not a token or monetary cap")


def create_plan(dataset, directory, *, model, limit=3, maximum=6, timeout=180):
    if type(limit) is not int or not 1 <= limit <= 3:
        raise RecoveryError("Report pilot supports 1..3 tasks.")
    data, tasks, documents = load_taskset(dataset.resolve())
    tasks = tasks[:limit]
    names = {name for task in tasks for name in task["documents"]}
    plan = dict(version=1, created_at=now(), data=data, tasks=tasks,
        documents={name: doc for name, doc in documents.items() if name in names},
        model=model, max_model_calls=maximum, timeout=timeout)
    plan["controls"] = controls(plan)
    directory.mkdir(parents=True, exist_ok=False)
    atomic_text(directory / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
    lines = ["# 报告策略实验计划", "", "仅准备，未调用 API；人工审核待进行。",
        f"模型：{model}；两组 v1→v2；每任务每组生成上限：{maximum} 次。",
        f"含批量评分最多 {plan['controls']['max_sdk_requests']} 次 SDK 请求，不是费用上限。",
        "接收方为配置的 DeepSeek API；任务生成发送问题与资料，评分另含 rubric 和报告。", ""]
    for task in tasks:
        lines += [f"- {task['id']}：{task['question']}", f"  资料：{', '.join(task['documents'])}"]
    lines += ["", "本任务集已用于开发；单轮模型评分不能证明显著性或泛化，默认不晋升。", ""]
    atomic_text(directory / "plan.md", "\n".join(lines))
    return plan


def policy_runner(version, maximum):
    def run(task, paths, directory, timeout):
        record = run_task(dict(task, report_policy=version, max_model_calls=maximum,
                               context_strategy="full"), paths, directory, timeout)
        if record["generation_status"] == "completed":
            manifest = json.loads((Path(record["run_directory"]) / "run.json").read_text(encoding="utf-8"))
            expected = dict(version=VERSION, max_calls=maximum)
            if (manifest.get("report_policy") != policy_spec(version) or manifest.get("model_policy") != expected
                    or manifest.get("context_policy", {}).get("strategy") != "full"
                    or manifest.get("allowed_tools") != ["read_source", "record_evidence"]
                    or any(manifest.get("tool_policy", {}).get(key) != value for key, value in
                           dict(max_calls=10, max_provider_requests=0, max_retries=0).items())):
                raise RecoveryError("Report arm changed frozen runtime controls.")
        record["report_policy"] = version
        return record
    return run


def comparison(result):
    lines = ["# 报告写作策略对照", "", f"Status: {result['status']}",
        f"Execution mode: {result['execution_mode']}",
        "固定 v1→v2 顺序，每任务每组一次生成，无修订；短资料模型辅助评分，不构成泛化或显著性证明。",
        "项目自编 rubric 仍待独立人工复核；默认保持 v1。", "",
        "| 策略 | 任务通过率 | 生成 Token | 评分 Token |", "|---|---:|---:|---:|"]
    for version, arm in result["arms"].items():
        lines.append(f"| {version} | {arm.get('task_success_rate')} | {arm.get('generation_total_tokens')} | {arm.get('evaluator_total_tokens')} |")
    if result.get("pairs"):
        lines += ["", "差值均为 v2 减 v1；未知保留 None。", "",
            "| 任务 | v1通过 | v2通过 | 引用精度差 | 覆盖率差 | 生成Token差 |",
            "|---|---|---|---:|---:|---:|"]
        for row in result["pairs"]:
            lines.append(f"| {row['task_id']} | {row['v1_success']} | {row['v2_success']} | {row['precision_delta']} | {row['coverage_delta']} | {row['token_delta']} |")
    lines += ["", "各组 summary.md 保留每任务引用精度、覆盖率、任务要求、失败和未知结果。",
        "不把缺失评分计为通过，不把未测成本计为零；无自动晋升。", ""]
    return "\n".join(lines)


def paired_rows(arms):
    left = {row["task_id"]: row for row in arms["v1"]["tasks"]}
    right = {row["task_id"]: row for row in arms["v2"]["tasks"]}
    def delta(a, b):
        return round(b-a, 6) if type(a) in {int, float} and type(b) in {int, float} else None
    return [dict(task_id=key, v1_success=a.get("success"), v2_success=right.get(key, {}).get("success"),
        precision_delta=delta(a.get("quality", {}).get("citation_precision"), right.get(key, {}).get("quality", {}).get("citation_precision")),
        coverage_delta=delta(a.get("quality", {}).get("evidence_coverage"), right.get(key, {}).get("quality", {}).get("evidence_coverage")),
        token_delta=delta(a.get("generation_metrics", {}).get("total_tokens"), right.get(key, {}).get("generation_metrics", {}).get("total_tokens")))
        for key, a in left.items()]


def execute_plan(directory, *, evaluator=None):
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    if controls(plan) != plan.get("controls"):
        raise RecoveryError("Frozen experiment changed; create a new plan before any model request.")
    output = directory / "execution"
    output.mkdir(exist_ok=False)  # One shot; never silently repeat a partially paid experiment.
    result = dict(status="running", started_at=now(), controls=plan["controls"], arms={},
        execution_mode="model_assisted" if evaluator is None else "injected_offline",
        promotion="no_automatic_promotion", human_review="pending", default_policy="v1")
    def save():
        atomic_text(output / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
        atomic_text(output / "summary.md", comparison(result))
    save()
    try:
        for version in ("v1", "v2"):
            if controls(plan) != plan["controls"]:
                raise RecoveryError("Controls changed between report arms.")
            arm_path = output / version
            arm = (evaluator or evaluate_batch)(plan["data"], plan["tasks"], plan["documents"], arm_path,
                model_name=plan["model"], timeout=plan["timeout"], runner=policy_runner(version, plan["max_model_calls"]))
            result["arms"][version] = arm
            atomic_text(arm_path / "summary.md", summary(arm))
            save()
            if controls(plan) != plan["controls"]:
                raise RecoveryError("Controls changed while a report arm was running.")
        result["pairs"] = paired_rows(result["arms"])
        result["status"] = "complete" if all(a["status"] == "complete" for a in result["arms"].values()) else "incomplete_scoring"
    except BaseException as error:
        result.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error_type=type(error).__name__)
        raise
    finally:
        result["finished_at"] = now()
        save()
    return result


def main():
    parser = argparse.ArgumentParser(description="Prepare a frozen report policy pilot or execute a saved plan once")
    parser.add_argument("--execute-plan", type=Path)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-model-calls", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.execute_plan and any(getattr(args, key) is not None for key in ("dataset", "model", "limit", "max_model_calls", "timeout", "output")):
        parser.error("Execution accepts only the frozen plan directory, no replacements.")
    try:
        if args.execute_plan:
            load_dotenv()
            if not os.getenv("DEEPSEEK_API_KEY", "").strip():
                print("Missing DEEPSEEK_API_KEY.")
                return 2
            result = execute_plan(args.execute_plan.resolve())
            print(f"Report pilot: {args.execute_plan / 'execution' / 'summary.md'}")
            return 0 if result["status"] == "complete" else 3
        directory = (args.output or Path("runs/report-evaluations")) / uuid4().hex
        plan = create_plan(args.dataset or Path("evals/research-tasks-v1.json"), directory,
            model=args.model or "deepseek-flash", limit=args.limit if args.limit is not None else 3,
            maximum=args.max_model_calls if args.max_model_calls is not None else 6,
            timeout=args.timeout if args.timeout is not None else 180)
        print(f"Prepared only; no API calls. Plan: {directory / 'plan.json'}")
        print(f"Maximum SDK requests if executed: {plan['controls']['max_sdk_requests']}")
        return 0
    except Exception as error:
        print(f"Report pilot failed ({type(error).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
