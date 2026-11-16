"""Offline candidate-reference calibration. Label agreement never proves rationale correctness."""
import argparse
import json
from pathlib import Path
from .inference_check import PROMPT, ModelJudge, assess, fingerprint, code_hashes, judge_prompt
from .inference import ClaimTrace
from .storage import atomic_text
from .verification import VerificationError, digest, evidence_context

VERSION = 'inference-calibration-v1'


def logic_hashes():
    return dict(code_hashes(), inference_calibration=digest(Path(__file__).read_text(encoding='utf-8')))


def reference_output(payload, references):
    if set(references) != {c['unit_id'] for c in payload['conclusions']}:
        raise VerificationError('Reference case identities do not match the payload.')
    rows = []
    for c in payload['conclusions']:
        ClaimTrace.model_validate(c)
        ref = references[c['unit_id']]
        if set(ref) != {'support', 'scope', 'inference', 'strength', 'reason_review_question'}:
            raise VerificationError('Unexpected candidate reference fields.')
        if set(ref['support']) != {p['id'] for p in c['premises']} or not ref['reason_review_question'].strip():
            raise VerificationError('Missing premise references or rationale review question.')
        rows.append(dict(unit_id=c['unit_id'], premises=[dict(premise_id=p['id'], support=ref['support'][p['id']],
            anchors=[dict(evidence_id=eid, quote=payload['evidence'][eid]['snippet']) for eid in p['evidence_ids']],
            reason='Project-authored candidate reference, not independent annotation') for p in c['premises']],
            scope=ref['scope'], inference=ref['inference'], strength=ref['strength'],
            reason='Project-authored candidate reference, not independent annotation'))
    return {'units': rows}


def bundle_check(payload, references):
    ids = [c['unit_id'] for c in payload['conclusions']]
    if not ids or len(ids) > 8 or len(set(ids)) != len(ids) or payload['selected_units'] != ids:
        raise VerificationError('Calibration needs 1..8 explicitly selected unique cases.')
    if len(json.dumps(payload, ensure_ascii=False)) > 60000:
        raise VerificationError('Calibration input too large; no truncation allowed.')
    for key, source in payload['sources'].items():
        found = digest(source['location']+'\0'+source['content'])
        if source['id'] != key or key != 'S'+found[:16] or source['sha256'] != found:
            raise VerificationError('Invalid calibration source snapshot.')
    for c in payload['conclusions']:
        if not c['premises'] or len({p['id'] for p in c['premises']}) != len(c['premises']):
            raise VerificationError('Missing or duplicate calibration premises.')
        for p in c['premises']:
            if not p['statement'].strip() or not p['evidence_ids'] or len(set(p['evidence_ids'])) != len(p['evidence_ids']):
                raise VerificationError('Missing premise statement or unique evidence.')
            for eid in p['evidence_ids']:
                if evidence_context(eid, payload['evidence'], payload['sources'])['integrity'] != 'valid':
                    raise VerificationError('Invalid calibration evidence.')
    # Reuse production output enums, identity checks, anchors and derived-assessment rules.
    return assess(payload, reference_output(payload, references))


def make_bundle(payload, references, directory, *, origin, prompt_version='v1'):
    bundle_check(payload, references)
    meta = dict(version=VERSION, payload_sha256=fingerprint(payload), references_sha256=fingerprint(references),
        judge_prompt_version=prompt_version, prompt_sha256=digest(judge_prompt(prompt_version)), logic_sha256=logic_hashes(), label_status='project_authored_candidates_pending_independent_review',
        origin=origin, human_review='pending', api_requests=0)
    directory.mkdir(parents=True, exist_ok=False)
    for name, value in (('manifest.json',meta), ('model-input.json',payload), ('references.json',references)):
        atomic_text(directory/name, json.dumps(value, ensure_ascii=False, indent=2))
    return meta


def compile_cases(dataset):
    if dataset.get('version') != VERSION or not 1 <= len(dataset.get('cases', [])) <= 8:
        raise VerificationError('Unsupported or empty calibration dataset.')
    conclusions, sources, evidence, references = [], {}, {}, {}
    for case in dataset['cases']:
        cid = case['id']
        if cid in references:
            raise VerificationError('Duplicate calibration case ID.')
        premises = []
        for number, p in enumerate(case['premises'], 1):
            pid = f'P{number}'
            location = f'fixture://inference/{cid}/{pid}'
            text, quote = p['source'], p['quote']
            found = digest(location+'\0'+text)
            sid = 'S'+found[:16]
            sources[sid] = dict(id=sid, location=location, content=text, sha256=found)
            eid = 'E'+digest(p['statement']+'\0'+sid+'\0'+quote)[:16]
            evidence[eid] = dict(id=eid, claim=p['statement'], source_id=sid, snippet=quote, location=location)
            premises.append(dict(id=pid, statement=p['statement'], evidence_ids=[eid]))
        conclusions.append(dict(unit_id=cid, text=case['conclusion'], kind=case['kind'],
                                premises=premises, rationale=case['candidate_rationale']))
        references[cid] = case['reference']
    ids = [c['unit_id'] for c in conclusions]
    payload = dict(scope='calibration_cases_only_not_report_or_task', selected_units=ids,
        task_question='仅判断各个候选结论是否由自己的前提和对应来源支持，不混用其他案例的资料。',
        report_units=[dict(unit_id=c['unit_id'], line=i, text=c['text'], citations=[]) for i,c in enumerate(conclusions,1)],
        conclusions=conclusions, sources=sources, evidence=evidence)
    bundle_check(payload, references)
    return payload, references


def load_bundle(directory):
    meta = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    payload = json.loads((directory/'model-input.json').read_text(encoding='utf-8'))
    references = json.loads((directory/'references.json').read_text(encoding='utf-8'))
    if (meta.get('version') != VERSION or meta.get('prompt_sha256') != digest(judge_prompt(meta.get('judge_prompt_version','v1')))
            or meta.get('logic_sha256') != logic_hashes()
            or meta.get('payload_sha256') != fingerprint(payload) or meta.get('references_sha256') != fingerprint(references)):
        raise VerificationError('Frozen calibration inputs or prompt changed.')
    bundle_check(payload, references)
    return meta, payload, references


def score(directory, raw):
    meta, payload, references = load_bundle(directory)
    targets = {c['unit_id']: c for c in bundle_check(payload, references)['units']}
    actual = assess(payload, raw, prompt_version=meta.get('judge_prompt_version','v1'))
    cases, matched_fields, fields = [], 0, 0
    for row in actual['units']:
        expected = targets[row['unit_id']]
        expected_p = {p['premise_id']: p['support'] for p in expected['premises']}
        checks = [dict(field='premise:'+p['premise_id'], expected=expected_p[p['premise_id']], actual=p['support']) for p in row['premises']]
        checks += [dict(field=key, expected=expected[key], actual=row[key]) for key in ('scope','inference','strength')]
        n = sum(c['expected'] == c['actual'] for c in checks)
        matched_fields += n
        fields += len(checks)
        cases.append(dict(case_id=row['unit_id'], labels_match=n==len(checks), comparisons=checks,
            expected_assessment=expected['assessment'], actual_assessment=row['assessment'], reason=row['reason'],
            reason_review_question=references[row['unit_id']]['reason_review_question'],
            rationale_correct=None, labels_and_rationale_accepted=None))
    return dict(version=VERSION, status='labels_scored_rationale_pending', judgment_origin='supplied_output_not_authenticated',
        judge_prompt_version=meta.get('judge_prompt_version','v1'),prompt_sha256=meta['prompt_sha256'],
        payload_sha256=fingerprint(payload), references_sha256=fingerprint(references), judgment_sha256=fingerprint(raw),
        label_status=meta['label_status'], candidate_field_agreement=matched_fields/fields,
        candidate_case_agreement=sum(c['labels_match'] for c in cases)/len(cases),
        false_support_count=sum(c['actual_assessment']=='model_support_established' and c['expected_assessment']!='model_support_established' for c in cases),
        cases=cases, overall_quality=None, rationale_review='pending', human_review='pending',
        affects_original_scores=False, promotion='no_automatic_promotion', api_requests=0)


def execution_spec(bundle, model):
    if not isinstance(model,str) or not model.strip():
        raise VerificationError('Pin a nonempty model name.')
    meta,payload,_ = load_bundle(bundle)
    return dict(version='inference-calibration-execution-v1', bundle=str(bundle.resolve()), model=model,
        bundle_manifest_sha256=fingerprint(meta), payload=payload, payload_sha256=fingerprint(payload),
        judge_prompt_version=meta.get('judge_prompt_version','v1'),prompt_sha256=meta['prompt_sha256'], logic_sha256=logic_hashes(), max_sdk_requests=1,
        settings=dict(temperature=0,timeout=90,max_retries=0,max_tokens=8192))


def prepare_execution(bundle, directory, *, model):
    request=execution_spec(bundle,model)
    directory.mkdir(parents=True,exist_ok=False)
    atomic_text(directory/'request.json',json.dumps(request,ensure_ascii=False,indent=2))
    atomic_text(directory/'binding.json',json.dumps(dict(request_sha256=fingerprint(request)),indent=2))
    ids=request['payload']['selected_units']
    atomic_text(directory/'README.md','# 批量推断校准待执行\n\n'
        f"模型：{model}；判定合同：{request['judge_prompt_version']}；{len(ids)} 个虚构案例；合并为最多1次SDK请求。\n\n"
        '当前仅准备，没有调用API。执行将候选结论、前提、证据和全部虚构来源正文发送给配置的DeepSeek API，产生费用。候选参考标签与理由复核问题不发送。\n\n'
        '请求失败、中断或结果不确定不自动重试。1次请求不是Token或费用上限。\n\n'
        '标签一致率相对于项目自编候选参考，不能证明理由正确或整体质量；独立人工复核仍待完成。\n\n'
        '案例：'+', '.join(ids)+'。\n')
    return request


def verify_execution(directory):
    request=json.loads((directory/'request.json').read_text(encoding='utf-8'))
    binding=json.loads((directory/'binding.json').read_text(encoding='utf-8'))
    if binding.get('request_sha256') != fingerprint(request) or request != execution_spec(Path(request['bundle']),request['model']):
        raise VerificationError('Frozen batch request changed before execution.')
    return request


def batch_summary(result):
    lines=['# 批量推断校准', '', f"Execution mode: {result['execution_mode']}",
        f"候选字段一致率：{result['candidate_field_agreement']}；候选案例一致率：{result['candidate_case_agreement']}。",
        f"相对候选参考的误支持数：{result['false_support_count']}。整体质量未知，理由与独立人工复核待完成。", '',
        '| 案例 | 标签全部一致 | 候选参考结论 | 本次判定 |', '|---|---|---|---|']
    for row in result['cases']:
        lines.append(f"| {row['case_id']} | {row['labels_match']} | {row['expected_assessment']} | {row['actual_assessment']} |")
    lines += ['', '以下保留模型理由和项目自编复核问题；不代表人工审核完成。', '']
    for row in result['cases']:
        lines += [f"## {row['case_id']}", '', '模型理由：'+row['reason'], '', '待复核：'+row['reason_review_question'], '']
    return '\n'.join(lines)


def execute_batch(directory, *, judge=None):
    request=verify_execution(directory)
    output=directory/'execution'
    output.mkdir(exist_ok=False)
    state=dict(status='prepared',max_sdk_requests=1,request_sha256=fingerprint(request),
        execution_mode='model_assisted' if judge is None else 'injected_offline',
        sdk_attempts=0,usage=None,human_review='pending',affects_original_scores=False)
    def save():
        atomic_text(output/'request-state.json',json.dumps(state,ensure_ascii=False,indent=2))
    save()
    actual=judge
    try:
        actual=judge if judge is not None else ModelJudge(request['model'],prompt_version=request['judge_prompt_version'])
        state.update(status='request_started',sdk_attempts=1)
        save()
        raw=actual(request['payload'])  # Only this field is sent; reference labels stay local.
        atomic_text(output/'unaccepted-output.json',json.dumps(raw,ensure_ascii=False,indent=2))
        if verify_execution(directory) != request:
            raise VerificationError('Batch request changed while the model was running.')
        result=score(Path(request['bundle']),raw)
        result.update(execution_mode=state['execution_mode'],
            judgment_origin='single_model_request' if judge is None else 'injected_output',
            api_requests=1 if judge is None else 0)
        atomic_text(output/'result.json',json.dumps(result,ensure_ascii=False,indent=2))
        atomic_text(output/'summary.md',batch_summary(result))
        state['status']='complete'
        return result
    except BaseException as error:
        state.update(status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',error_type=type(error).__name__)
        raise
    finally:
        state['usage']=getattr(actual,'usage',None)
        response=getattr(actual,'raw_content',None)
        if response is not None:
            atomic_text(output/'raw-response.json',json.dumps(response,ensure_ascii=False))
        save()


def main():
    parser = argparse.ArgumentParser(description='Inference calibration: offline preparation/scoring or one frozen batch request')
    commands = parser.add_subparsers(dest='action', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('dataset', type=Path)
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--judge-prompt',choices=['v1','v2'],default='v1')
    compare = commands.add_parser('score')
    compare.add_argument('bundle', type=Path)
    compare.add_argument('--judgments', type=Path, required=True)
    compare.add_argument('--output', type=Path, required=True)
    plan=commands.add_parser('prepare-execution')
    plan.add_argument('bundle',type=Path)
    plan.add_argument('--model',required=True)
    plan.add_argument('--output',type=Path,required=True)
    execute=commands.add_parser('execute')
    execute.add_argument('directory',type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            data = json.loads(args.dataset.read_text(encoding='utf-8'))
            payload, references = compile_cases(data)
            make_bundle(payload, references, args.output, origin='synthetic_development_pairs',prompt_version=args.judge_prompt)
        elif args.action == 'score':
            raw = json.loads(args.judgments.read_text(encoding='utf-8'))
            result = score(args.bundle, raw)
            args.output.mkdir(parents=True, exist_ok=False)
            atomic_text(args.output/'result.json', json.dumps(result, ensure_ascii=False, indent=2))
        elif args.action == 'prepare-execution':
            prepare_execution(args.bundle,args.output,model=args.model)
        else:
            from dotenv import load_dotenv
            load_dotenv()
            execute_batch(args.directory)
            print(f'Batch calibration: {args.directory / "execution" / "summary.md"}; rationale review pending.')
            return 0
        print(f'Offline calibration saved: {args.output}; no API calls; reference and rationale review pending.')
        return 0
    except Exception as error:
        print(f'Calibration rejected ({type(error).__name__}); no accepted quality score assigned.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
