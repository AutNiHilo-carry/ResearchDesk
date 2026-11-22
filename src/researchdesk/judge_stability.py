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


def check_stability_result(evaluation, dataset, source, directory):
    """Validate stored official trials offline, preserving historical labels and failures."""
    from .evaluation_audit import _factuality_audit
    evaluation, source, directory = evaluation.resolve(), source.resolve(), directory.resolve()
    if directory.is_relative_to(evaluation) or directory.is_relative_to(source.parent):
        raise VerificationError('Check output must be outside the source evaluation and stability run.')
    input_path = source.with_name('input.json')
    if not input_path.resolve().is_relative_to(source.parent):
        raise VerificationError('Saved stability input escapes its directory.')
    raw, raw_input = source.read_bytes(), input_path.read_bytes()
    result, stored_input = json.loads(raw.decode('utf-8')), json.loads(raw_input.decode('utf-8'))
    payload, controls = frozen_input(evaluation, dataset)
    recorded_controls = result.get('controls', {})
    keys = [key for key in controls if key != 'baseline_directory']
    if (not isinstance(recorded_controls, dict) or any(key not in recorded_controls for key in controls)
            or payload_hash({k: recorded_controls[k] for k in keys}) != payload_hash({k: controls[k] for k in keys})
            or Path(recorded_controls['baseline_directory']).resolve() != evaluation):
        raise VerificationError('Recorded stability controls differ from the frozen evaluation.')
    if stored_input != payload or payload_hash(stored_input) != controls['input_sha256']:
        raise VerificationError('Recorded stability input differs from the original full payload.')
    old_raw = (evaluation/'citation-quality.json').read_bytes()
    if hashlib.sha256(old_raw).hexdigest() != controls['baseline_quality_sha256']:
        raise VerificationError('Historical labels changed during the check.')
    historical = {row['unit_id']: row for row in json.loads(old_raw.decode('utf-8'))['units']}
    planned, trials, execution = result.get('planned_model_calls'), result.get('trials'), result.get('status')
    if (type(planned) is not int or not 2 <= planned <= 5 or not isinstance(trials, list)
            or len(trials) > planned or execution not in {'running', 'partial', 'complete'}):
        raise VerificationError('Invalid saved stability execution state.')
    task_rows = [dict(task_id=tid, report_changed=False) for tid in sorted({row['task_id'] for row in payload['units']})]
    views, valid, changed = [], 0, False
    for number, trial in enumerate(trials, 1):
        if (type(trial.get('number')) is not int or trial['number'] != number
                or trial.get('input_sha256') != controls['input_sha256']
                or trial.get('status') not in {'started', 'failed', 'complete'}
                or type(trial.get('request_started')) is not bool or type(trial.get('usage_complete')) is not bool):
            raise VerificationError('Invalid trial identity, status or input binding.')
        usage = trial.get('usage')
        known_usage = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                                    for k in ('input_tokens', 'output_tokens', 'total_tokens'))
        if trial['usage_complete'] != known_usage or (not trial['request_started'] and usage is not None):
            raise VerificationError('Trial usage flags do not match recorded usage.')
        view = None
        if trial['status'] == 'complete':
            saved = trial['quality']
            if (not trial['request_started'] or saved.get('prompt_version') != controls['prompt_version']
                    or saved.get('judge_prompt_sha256') != controls['prompt_sha256']):
                raise VerificationError('Completed trial has inconsistent judge identity.')
            def labels(_):
                return dict(units=[{k: row[k] for k in ('unit_id', 'factual', 'reason', 'links')} for row in saved['units']])
            labels.prompt_version, labels.prompt_sha256 = controls['prompt_version'], controls['prompt_sha256']
            checked = assess(payload, labels)
            if payload_hash(checked) != payload_hash(saved):
                raise VerificationError('Saved trial rows or metrics differ from deterministic replay.')
            metrics = {tid: quality_metrics([row for row in checked['units'] if row['task_id'] == tid])
                       for tid in sorted({row['task_id'] for row in checked['units']})}
            if payload_hash(metrics) != payload_hash(trial.get('task_metrics')):
                raise VerificationError('Saved trial task metrics are inconsistent.')
            view = _factuality_audit(task_rows, historical, {row['unit_id']: row for row in checked['units']},
                                     available=True, input_identical=True)
            changed = changed or view['status'] == 'review_required'
            valid += 1
        views.append(dict(number=number, status=trial['status'], factuality_audit=view))
    complete = len(trials) == planned and valid == planned
    if execution != 'running' and (len(trials) != planned or (execution == 'complete') != complete):
        raise VerificationError('Saved completion state does not match its trials.')
    recomputed = summarize_trials(payload, trials) if trials else {}
    for key, value in recomputed.items():
        if (execution != 'running' or key in result) and (key not in result or payload_hash(result[key]) != payload_hash(value)):
            raise VerificationError('Saved stability summary differs from its trial records.')
    complete = complete and execution == 'complete'
    status = 'review_required' if changed else 'passed' if complete else 'unknown'
    reasons = (['historical_factuality_changed'] if changed else []) + ([] if complete else ['execution_incomplete'])
    audit = dict(version='stability-factuality-audit-v1', execution_status=execution,
        planned_trials=planned, valid_trials=valid, trials=views,
        new_trial_factual_agreement=recomputed.get('factual_pairwise_agreement'),
        source_recorded_tokens=recomputed.get('total_tokens'), controls=controls,
        source_result_sha256=hashlib.sha256(raw).hexdigest(), input_file_sha256=hashlib.sha256(raw_input).hexdigest())
    gate = dict(version='stability-factuality-gate-v1', gate='factuality', status=status,
        exit_code=0 if status == 'passed' else 3, reasons=reasons, api_requests=0,
        audit_content_sha256=payload_hash(audit), human_review='pending', final_acceptance='pending',
        scope='historical_factuality_consistency_not_citation_correctness_or_task_acceptance', promotion='none')
    after_payload, after_controls = frozen_input(evaluation, dataset)
    if (source.read_bytes() != raw or input_path.read_bytes() != raw_input
            or after_payload != payload or after_controls != controls):
        raise VerificationError('Stability source or evaluation changed during the check.')
    directory.mkdir(parents=True, exist_ok=False)
    atomic_text(directory/'audit.json', json.dumps(audit, ensure_ascii=False, indent=2))
    atomic_text(directory/'gate.json', json.dumps(gate, ensure_ascii=False, indent=2))
    atomic_text(directory/'summary.md', '# Saved stability factuality check\n\n'
        + f"Status: {status}; exit code: {gate['exit_code']}; valid trials: {valid}/{planned}.\n\n"
        + f"Reasons: {reasons}.\n\nZero new API requests; historical labels unchanged; human review pending.\n"
        + 'Agreement is not correctness or task acceptance. Incomplete trials are not retried.\n')
    return gate


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
    parser.add_argument("--repeats", type=int, help="New scoring repeats, default 3; not accepted with --check-result")
    parser.add_argument("--output", type=Path, default=Path("runs/judge-stability"))
    parser.add_argument("--check-result", type=Path, help="Validate saved trials offline and compare historical factuality; zero API calls")
    args = parser.parse_args()
    if args.check_result is not None:
        if args.repeats is not None:
            parser.error("--check-result cannot replace the recorded repeat budget.")
        directory = args.output / uuid4().hex
        try:
            gate = check_stability_result(args.evaluation, args.dataset, args.check_result, directory)
            print(f"Offline stability factuality gate: {gate['status']}; zero API calls: {directory / 'gate.json'}")
            return gate['exit_code']
        except Exception as error:
            print(f"Saved stability check failed ({type(error).__name__}); no new model request.")
            return 1
    args.repeats = 3 if args.repeats is None else args.repeats
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
