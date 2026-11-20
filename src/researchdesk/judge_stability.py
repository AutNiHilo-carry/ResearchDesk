"""Bounded repeated citation judgments on one frozen payload. No majority promotion."""
import argparse
import copy
import itertools
import hashlib
import json
import os
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from .ledger import now
from .review import build_packet
from .storage import atomic_text
from .task_eval import (load_taskset, quality_metrics, saved_quality, generation_contract_spec,
                        task_suffix, common_suffix_hash)
from .verification import ModelJudge, VerificationError, assess, digest, failure_diagnostics, prepare, SYSTEM_PROMPT_V2


def payload_hash(payload):
    return digest(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def frozen_input(evaluation, dataset):
    """Validate a scored batch without imposing paired-repair generation defaults."""
    evaluation, dataset = evaluation.resolve(), dataset.resolve()
    raw_result = (evaluation / 'result.json').read_text(encoding='utf-8')
    baseline = json.loads(raw_result)
    _, tasks, _ = load_taskset(dataset)
    packet = build_packet(evaluation, dataset)
    quality, binding = saved_quality(evaluation, dataset, baseline)
    if packet['result_sha256'] != digest(raw_result):
        raise VerificationError('Evaluation changed during stability preflight.')
    if (baseline.get('citation_judge_prompt_sha256') != digest(SYSTEM_PROMPT_V2)
            or baseline.get('citation_judge_prompt_version') != 'report-quote-v2'
            or quality.get('judge_prompt_sha256') != digest(SYSTEM_PROMPT_V2)
            or quality.get('prompt_version') != 'report-quote-v2'):
        raise VerificationError('Saved citation prompt differs from the supported stability judge.')
    model = baseline.get('judge_model')
    if not isinstance(model, str) or not model.strip():
        raise VerificationError('Missing frozen citation judge model.')
    selected = baseline['selected_tasks']
    task_map = {task['id']: task for task in tasks}
    records = {row['task_id']: row for row in baseline['tasks']}
    contracts = baseline.get('generation_contracts')
    if contracts is not None and (not isinstance(contracts, dict) or set(contracts) != set(selected)):
        raise VerificationError('Generation contract identities differ from selected tasks.')
    units, evidence, sources, selected_tasks, manifests = [], {}, {}, [], {}
    for task in packet['tasks']:
        task_id = task['task_id']
        record = records[task_id]
        if task['generation_status'] != 'completed':
            raise VerificationError('Stability requires completed reports for every selected task.')
        recorded_contract = contracts[task_id] if contracts is not None else None
        version = recorded_contract.get('version') if isinstance(recorded_contract, dict) else 'legacy'
        selected_task = dict(task_map[task_id], generation_contract=version)
        spec = generation_contract_spec(selected_task)
        if contracts is not None and recorded_contract != spec:
            raise VerificationError('Saved generation contract changed or is unsupported.')
        selected_tasks.append(selected_task)
        run = evaluation / 'tasks' / task_id / 'research' / Path(record['run_directory']).name
        manifest_path = run / 'run.json'
        manifest_raw = manifest_path.read_text(encoding='utf-8')
        manifest = json.loads(manifest_raw)
        manifests[manifest_path] = manifest_raw
        if record.get('model') != baseline.get('generator_model') or manifest.get('model') != baseline.get('generator_model'):
            raise VerificationError('Generator model differs from the saved evaluation.')
        question = selected_task['question'] + task_suffix(selected_task)
        if version != 'legacy' and (record.get('generation_contract') != spec
                or record.get('question_sha256') != digest(question) or manifest.get('question') != question):
            raise VerificationError('Generation contract is not bound to the recorded research question.')
        if version == 'legacy' and ((record.get('question_sha256') is not None and record['question_sha256'] != digest(question))
                or (manifest.get('question') is not None and manifest['question'] != question)):
            raise VerificationError('Legacy research question changed.')
        units.extend(task['units'])
        evidence.update(task['evidence'])
        sources.update(task['sources'])
    if ('task_prompt_suffix_sha256' not in baseline
            or baseline['task_prompt_suffix_sha256'] != common_suffix_hash(selected_tasks)):
        raise VerificationError('Saved generation suffix differs from recorded contracts.')
    payload, fingerprint = prepare(units, evidence, sources)
    if fingerprint != baseline['quality_input_sha256'] or fingerprint != binding['input_sha256']:
        raise VerificationError('Frozen judge input differs from the original scored input.')
    if ((evaluation / 'result.json').read_text(encoding='utf-8') != raw_result
            or hashlib.sha256((evaluation / 'citation-quality.json').read_bytes()).hexdigest() != binding['quality_sha256']
            or any(path.read_text(encoding='utf-8') != raw for path, raw in manifests.items())):
        raise VerificationError('Saved evaluation or labels changed during stability preflight.')
    controls = dict(baseline_directory=str(evaluation), baseline_result_sha256=digest(raw_result),
        baseline_quality_sha256=binding['quality_sha256'], dataset_sha256=baseline['dataset_sha256'],
        input_sha256=fingerprint, generation_contracts=contracts,
        task_prompt_suffix_sha256=baseline['task_prompt_suffix_sha256'],
        judge_model=model, prompt_sha256=baseline['citation_judge_prompt_sha256'],
        prompt_version=baseline['citation_judge_prompt_version'],
        settings=dict(temperature=0, max_tokens=8192, timeout=90, max_retries=0,
                      response_format='json_object', thinking='disabled'))
    return payload, controls


def summarize_trials(payload, trials):
    valid = [row for row in trials if row["status"] == "complete"]
    maps = [{unit["unit_id"]: unit for unit in trial["quality"]["units"]} for trial in valid]
    pairs = list(itertools.combinations(range(len(valid)), 2))
    factual_matches = link_matches = factual_total = link_total = 0
    distributions = []
    for unit in payload["units"]:
        uid = unit["unit_id"]
        factual = Counter(str(rows[uid]["factual"]).lower() for rows in maps)
        links = {eid: Counter(next(link["verdict"] for link in rows[uid]["links"] if link["evidence_id"] == eid)
                              for rows in maps) for eid in unit["citations"]}
        for left, right in pairs:
            factual_total += 1
            factual_matches += maps[left][uid]["factual"] == maps[right][uid]["factual"]
            a = {link["evidence_id"]: link["verdict"] for link in maps[left][uid]["links"]}
            b = {link["evidence_id"]: link["verdict"] for link in maps[right][uid]["links"]}
            link_total += len(unit["citations"])
            link_matches += sum(a[eid] == b[eid] for eid in unit["citations"])
        distributions.append(dict(unit_id=uid, text=unit["text"], factual_counts=dict(factual),
            link_verdict_counts={eid: dict(labels) for eid, labels in links.items()},
            unstable=(len(factual) > 1 or any(len(labels) > 1 for labels in links.values())) if pairs else None))
    complete_usage = all(row.get("usage_complete") for row in trials)
    return dict(valid_trials=len(valid), planned_trials=len(trials),
        schema_completion_rate=len(valid)/len(trials), successful_trial_pairs=len(pairs),
        agreement_scope="conditional on valid trials; agreement is not correctness",
        factual_pairwise_agreement=factual_matches/factual_total if factual_total else None,
        link_pairwise_agreement=link_matches/link_total if link_total else None,
        stable_unit_fraction=sum(row["unstable"] is False for row in distributions)/len(distributions) if pairs else None,
        unstable_unit_ids=[row["unit_id"] for row in distributions if row["unstable"]], units=distributions,
        total_tokens=sum(row["usage"]["total_tokens"] for row in trials) if complete_usage else None,
        token_usage_complete=complete_usage, majority_label_applied=False, promotion="no_automatic_promotion")


def run_stability(payload, controls, directory, judge_factory, *, repeats=3):
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 2 <= repeats <= 5:
        raise VerificationError("Planned repeats must be 2..5.")
    fingerprint = payload_hash(payload)
    units = payload.get("units", [])
    if (not 1 <= len(units) <= 60 or len(json.dumps(payload, ensure_ascii=False)) > 60000
            or len({row["unit_id"] for row in units}) != len(units)):
        raise VerificationError("Invalid or oversized frozen input.")
    if fingerprint != controls["input_sha256"]:
        raise VerificationError("Frozen input fingerprint mismatch.")
    if any(len(set(row["citations"])) != len(row["citations"]) for row in units):
        raise VerificationError("Duplicate citation identities in frozen input.")
    directory.mkdir(parents=True, exist_ok=False)
    result = dict(status="running", controls=controls, started_at=now(), trials=[],
                  planned_model_calls=repeats, original_reports_modified=False, human_review="pending")
    atomic_text(directory / "input.json", json.dumps(payload, ensure_ascii=False, indent=2))
    def save():
        atomic_text(directory / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
    save()
    for number in range(1, repeats + 1):
        trial = dict(number=number, status="started", input_sha256=fingerprint, usage=None,
                     usage_complete=False, request_started=False)
        result["trials"].append(trial)
        save()  # Uncertain process death stays visible; there is no automatic resume/retry.
        judge, started = None, time.monotonic()
        try:
            judge = judge_factory()
            if getattr(judge, "prompt_sha256", None) != controls["prompt_sha256"]:
                raise VerificationError("Judge prompt differs from the frozen control.")
            request = copy.deepcopy(payload)
            trial["request_started"] = True
            save()
            raw = judge(request)
            trial["unaccepted_output"] = raw
            if payload_hash(request) != fingerprint:
                raise VerificationError("Judge mutated the frozen request data.")
            def accepted_output(_):
                return raw
            accepted_output.prompt_version = getattr(judge, "prompt_version", controls.get("prompt_version"))
            accepted_output.prompt_sha256 = controls["prompt_sha256"]
            quality = assess(payload, accepted_output)
            trial.pop("unaccepted_output")
            groups = sorted({row.get("task_id", "all") for row in quality["units"]})
            trial.update(status="complete", quality=quality, task_metrics={task_id: quality_metrics(
                [row for row in quality["units"] if row.get("task_id", "all") == task_id]) for task_id in groups})
        except Exception as error:
            trial.update(status="failed", **failure_diagnostics(error, judge))
        finally:
            trial["elapsed_seconds"] = round(time.monotonic()-started, 3)
            trial["usage"] = getattr(judge, "usage", None) if trial["request_started"] else None
            usage = trial["usage"]
            trial["usage_complete"] = isinstance(usage, dict) and all(type(usage.get(key)) is int
                and usage[key] >= 0 for key in ("input_tokens", "output_tokens", "total_tokens"))
            save()
    result.update(summarize_trials(payload, result["trials"]))
    result.update(status="complete" if result["valid_trials"] == repeats else "partial",
                  finished_at=now())
    save()
    atomic_text(directory / "summary.md", summary(result))
    return result


def summary(result):
    unstable = result["unstable_unit_ids"]
    return ("# 固定输入的引用评分一致性\n\n"
        f"Status: {result['status']}；有效轮次：{result['valid_trials']}/{result['planned_trials']}。\n\n"
        f"引用标签两两一致率：{result['link_pairwise_agreement']}；"
        f"事实分类两两一致率：{result['factual_pairwise_agreement']}。\n\n"
        f"出现分歧的行：{unstable}。\n\n"
        f"本轮Token：{result['total_tokens']}；未知用量不按0计算。\n\n"
        "一致性不等于正确性；仅统计有效轮次，格式失败另记。没有多数表决替换原标签、人工标注或策略晋升。\n"
        "小样本、同模型、同一次批处理上下文；不能推断总体方差或泛化能力。\n")


def main():
    parser = argparse.ArgumentParser(description="Bounded repeatability check on frozen citation-judge inputs")
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("runs/judge-stability"))
    args = parser.parse_args()
    if not 2 <= args.repeats <= 5:
        parser.error("repeats must be 2..5")
    directory = args.output / uuid4().hex
    try:
        payload, controls = frozen_input(args.evaluation.resolve(), args.dataset)
        load_dotenv()
        if not os.getenv("DEEPSEEK_API_KEY", "").strip():
            print("Missing DEEPSEEK_API_KEY.")
            return 2
        if os.getenv("DEEPSEEK_MODEL", "deepseek-flash") != controls["judge_model"]:
            raise VerificationError("Current model differs from the frozen judge model.")
        result = run_stability(payload, controls, directory,
            lambda: ModelJudge(controls["judge_model"], prompt_version="v2"), repeats=args.repeats)
        print(f"Judge stability: {directory / 'summary.md'}; valid trials: {result['valid_trials']}/{args.repeats}")
        return 0 if result["status"] == "complete" else 3
    except Exception as error:
        print(f"Stability check failed ({type(error).__name__}); artifacts: {directory}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
