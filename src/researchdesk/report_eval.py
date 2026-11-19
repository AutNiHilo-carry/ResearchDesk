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
from .task_eval import TASK_SUFFIX, evaluate_batch, load_taskset, run_task, summary, result_gate
from .task_eval import task_suffix, generation_contract_spec, common_suffix_hash
from .task_grading import TASK_PROMPT
from .verification import SYSTEM_PROMPT_V2, digest, VerificationError
from .report_constraints import policy as constraint_policy


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
        if len(task["question"] + task_suffix(task) + text) > 20000:
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
        task_suffix_sha256=common_suffix_hash(tasks), task_judge_sha256=digest(TASK_PROMPT),
        generation_contracts={task['id']: generation_contract_spec(task) for task in tasks},
        evidence_judge_sha256=digest(SYSTEM_PROMPT_V2), context_mode="full",
        constraint_policy=constraint_policy(),
        tool_policy=dict(max_calls=10, max_provider_requests=0, max_retries=0),
        max_sdk_requests=2*len(tasks)*plan["max_model_calls"]+4,
        request_limit_scope="generation per task plus at most two batch graders per arm; no SDK retries; not a token or monetary cap")


def create_plan(dataset, directory, *, model, limit=None, maximum=6, timeout=180, task_ids=None, generation_contract='legacy'):
    generation_contract_spec(dict(generation_contract=generation_contract))
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 3):
        raise RecoveryError("Report pilot supports 1..3 tasks.")
    if task_ids is not None:
        if limit is not None:
            raise RecoveryError('Choose either task IDs or a leading task limit, not both.')
        if (type(task_ids) is not list or not 1 <= len(task_ids) <= 3
                or any(not isinstance(key, str) or not key.strip() for key in task_ids)
                or len(set(task_ids)) != len(task_ids)):
            raise RecoveryError('Select 1..3 distinct nonempty task IDs.')
    data, tasks, documents = load_taskset(dataset.resolve())
    if task_ids is not None:
        available = {task['id']: task for task in tasks}
        if not set(task_ids) <= set(available):
            raise RecoveryError('Selected task IDs must exist in the dataset.')
        tasks = [available[key] for key in task_ids]
    else:
        tasks = tasks[:limit if limit is not None else 3]
    if generation_contract != 'legacy':
        tasks = [dict(task, generation_contract=generation_contract) for task in tasks]
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
        f"两组生成合同：{generation_contract}；实际后缀哈希与计数规则见 plan.json controls。",
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


def application_view(arm):
    """Record-only presentation; a passing view is not bound offline acceptance."""
    try:
        verdict = result_gate(arm, gate='application')
    except (VerificationError, KeyError, TypeError, ValueError, AttributeError) as error:
        return dict(status='unknown', counts=None, tasks={}, validation='invalid_record', error_type=type(error).__name__)
    counts = {value: sum(row['application_gate'] == value for row in verdict['tasks'])
              for value in ('passed', 'failed', 'unknown')}
    return dict(status=verdict['status'], counts=counts,
                tasks={row['task_id']: row for row in verdict['tasks']}, validation='record_only_not_body_revalidated')


def comparison(result):
    lines = ["# 报告写作策略对照", "", f"Status: {result['status']}",
        f"Execution mode: {result['execution_mode']}",
        "固定 v1→v2 顺序，每任务每组一次生成，无修订；短资料模型辅助评分，不构成泛化或显著性证明。",
        "项目自编 rubric 仍待独立人工复核；默认保持 v1。", "",
        "Human review: pending; Final acceptance: pending（本入口不执行人工验收）。",
        "实验 complete 只表示评分流程完成；下表通过率保留原模型口径，不含长度约束。",
        "应用状态从记录中的分项重新计算，不信任缓存徽标；本汇总未重新绑定正文，不替代 --check-result 验收。",
        "缺失检查保持 unknown；invalid_record 表示记录不完整或不一致，不计作通过。", "",
        "| 策略 | 原模型通过率 | 应用状态（记录视图） | 应用通过/失败/未知 | 记录校验 | 生成 Token | 评分 Token |",
        "|---|---:|---|---|---|---:|---:|"]
    for version, arm in result["arms"].items():
        view = application_view(arm)
        counts = view['counts']
        tally = '/'.join(str(counts[key]) for key in ('passed', 'failed', 'unknown')) if counts is not None else 'N/A'
        lines.append(f"| {version} | {arm.get('task_success_rate')} | {view['status']} | {tally} | {view['validation']} "
                     f"| {arm.get('generation_total_tokens')} | {arm.get('evaluator_total_tokens')} |")
    pairs = paired_rows(result['arms']) if {'v1', 'v2'} <= set(result['arms']) else []
    if pairs:
        lines += ["", "差值均为 v2 减 v1；未知保留 None。", "",
            "| 任务 | v1原模型标签 | v2原模型标签 | v1长度 | v2长度 | v1应用 | v2应用 | 引用精度差 | 覆盖率差 | 生成Token差 |",
            "|---|---|---|---|---|---|---|---:|---:|---:|"]
        for row in pairs:
            lines.append(f"| {row['task_id']} | {row['v1_success']} | {row['v2_success']} "
                f"| {row['v1_constraint_gate']} | {row['v2_constraint_gate']} "
                f"| {row['v1_application_gate']} | {row['v2_application_gate']} "
                f"| {row['precision_delta']} | {row['coverage_delta']} | {row['token_delta']} |")
    lines += ["", "各组 summary.md 保留每任务引用精度、覆盖率、任务要求、失败和未知结果。",
        "不把缺失评分计为通过，不把未测成本计为零；无自动晋升。", ""]
    return "\n".join(lines)


def paired_rows(arms):
    def records(arm):
        rows = arm['tasks']
        ids = [row['task_id'] for row in rows]
        if any(not isinstance(key, str) or not key for key in ids) or len(ids) != len(set(ids)):
            raise RecoveryError('Comparison requires distinct task IDs in each arm.')
        return {row['task_id']: row for row in rows}
    left, right = records(arms['v1']), records(arms['v2'])
    views = {key: application_view(arms[key]) for key in ('v1', 'v2')}
    def delta(a, b):
        return round(b-a, 6) if type(a) in {int, float} and type(b) in {int, float} else None
    paired = []
    for key in dict.fromkeys([*left, *right]):
        a, b = left.get(key, {}), right.get(key, {})
        row = dict(task_id=key, v1_success=a.get('success'), v2_success=b.get('success'),
            precision_delta=delta(a.get('quality', {}).get('citation_precision'), b.get('quality', {}).get('citation_precision')),
            coverage_delta=delta(a.get('quality', {}).get('evidence_coverage'), b.get('quality', {}).get('evidence_coverage')),
            token_delta=delta(a.get('generation_metrics', {}).get('total_tokens'), b.get('generation_metrics', {}).get('total_tokens')))
        for version in ('v1','v2'):
            state = views[version]['tasks'].get(key, {})
            row[version+'_constraint_gate'] = state.get('constraint_gate', 'not_checked')
            row[version+'_application_gate'] = state.get('application_gate', 'unknown')
        paired.append(row)
    return paired


def render_comparison(source, directory):
    raw = source.read_bytes()
    result = json.loads(raw.decode('utf-8'))
    if not isinstance(result.get('arms'), dict) or not set(result['arms']) <= {'v1','v2'}:
        raise RecoveryError('Expected a report-policy result with v1/v2 arms.')
    rendered = comparison(result)
    if source.read_bytes() != raw:
        raise RecoveryError('Result changed during comparison rendering.')
    directory.mkdir(parents=True, exist_ok=False)
    atomic_text(directory/'summary.md', rendered)
    atomic_text(directory/'provenance.json', json.dumps(dict(mode='offline_comparison',
        source_sha256=digest(raw.decode('utf-8')), api_requests=0, scoring_recomputed=False,
        scope='record_view_not_frozen_body_acceptance', human_review='pending', final_acceptance='pending'), indent=2))


def execute_plan(directory, *, evaluator=None):
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    if controls(plan) != plan.get("controls"):
        raise RecoveryError("Frozen experiment changed; create a new plan before any model request.")
    output = directory / "execution"
    output.mkdir(exist_ok=False)  # One shot; never silently repeat a partially paid experiment.
    result = dict(status="running", started_at=now(), controls=plan["controls"], arms={},
        execution_mode="model_assisted" if evaluator is None else "injected_offline",
        promotion="no_automatic_promotion", human_review="pending", final_acceptance="pending", default_policy="v1")
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
            atomic_text(arm_path / "summary.md", summary(arm, quality_details=arm['status'] == 'complete'
                                                          and bool(arm.get('quality_input_sha256'))))
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute-plan", type=Path)
    mode.add_argument('--summarize-result', type=Path, help='Render a saved comparison offline; formatting only, no acceptance')
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--limit", type=int)
    parser.add_argument('--task-id', action='append', dest='task_ids', help='Select 1..3 task IDs in the given order; repeat this option')
    parser.add_argument("--max-model-calls", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument('--generation-contract', choices=['legacy','body-length-v1'])
    args = parser.parse_args()
    if args.summarize_result and any(getattr(args,key) is not None for key in ('dataset','model','limit','task_ids','max_model_calls','timeout','generation_contract')):
        parser.error('Offline rendering accepts only a saved result and an output directory.')
    if args.execute_plan and any(getattr(args, key) is not None for key in ("dataset", "model", "limit", "task_ids", "max_model_calls", "timeout", "output", 'generation_contract')):
        parser.error("Execution accepts only the frozen plan directory, no replacements.")
    if args.task_ids is not None and args.limit is not None:
        parser.error('--task-id and --limit are mutually exclusive.')
    try:
        if args.summarize_result:
            directory = (args.output or Path('runs/report-comparison-views'))/uuid4().hex
            render_comparison(args.summarize_result, directory)
            print(f'Offline comparison; zero API; formatting only: {directory / "summary.md"}')
            return 0
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
            model=args.model or "deepseek-flash", limit=args.limit, task_ids=args.task_ids,
            maximum=args.max_model_calls if args.max_model_calls is not None else 6,
            timeout=args.timeout if args.timeout is not None else 180,
            generation_contract=args.generation_contract or 'legacy')
        print(f"Prepared only; no API calls. Plan: {directory / 'plan.json'}")
        print(f"Maximum SDK requests if executed: {plan['controls']['max_sdk_requests']}")
        return 0
    except Exception as error:
        print(f"Report pilot failed ({type(error).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
