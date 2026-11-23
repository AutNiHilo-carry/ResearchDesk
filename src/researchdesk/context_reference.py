"""Offline reference opinions for factorial scoring; no model labels in reading packets."""
import argparse
import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict
from .context_experiment import create_plan
from .source_context import encode
from .storage import atomic_text, RecoveryError
from .verification import Verdict, VerificationError, digest, replay_assessment, load_run


def read(path):
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def build_packet(experiment):
    plan, plan_hash = read(experiment/'plan.json')
    result, result_hash = read(experiment/'execution/result.json')
    definitions = [{key: row[key] for key in ('name','run','body')} for row in plan['samples']]
    current = create_plan(definitions, model=plan['model'])
    old_code = plan['source_code_sha256']
    if (not isinstance(old_code,dict) or not old_code or any(
            not isinstance(k,str) or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.py',k) is None
            or not isinstance(v,str) or re.fullmatch(r'[0-9a-f]{64}',v) is None for k,v in old_code.items())):
        raise RecoveryError('Historical code metadata is invalid.')
    # Read-only review rebuilds every input and replays every label. Historical code
    # hashes remain provenance, not a requirement to reinstall byte-identical code.
    # Paid execution still enforces the original strict code gate.
    current['source_code_sha256'] = old_code
    if current != plan or result.get('status')!='complete' or len(result['rows'])!=plan['max_sdk_requests']:
        raise RecoveryError('Require a complete, unchanged experiment and frozen inputs.')
    cases=[]
    for index, sample in enumerate(plan['samples']):
        for arm_index, arm in enumerate(sample['arms']):
            row=result['rows'][index*4+arm_index]
            if (row['sample']!=sample['name'] or type(row['context']) is not bool
                    or type(row['fact_hint']) is not bool or row['context']!=arm['context']
                    or row['fact_hint']!=arm['fact_hint'] or row['status']!='complete'):
                raise VerificationError('Experiment row identity changed.')
            quality=copy.deepcopy(row['quality'])
            expected_scope=('candidate quote plus same-source body support' if arm['context']
                            else 'model-estimated line units; strict single-quote full support')
            if (quality['metric_scope']!=expected_scope or quality['judge_prompt_sha256']!=arm['prompt_sha256']
                    or quality['prompt_version']!='context-factorial-v1'):
                raise VerificationError('Saved scoring scope or prompt changed.')
            quality['metric_scope']='model-estimated line units; strict single-quote full support'
            replay_assessment(arm['payload'],quality)
        run=Path(sample['run'])
        report=load_run(run)[0] if sample['body']=='selected' else (run/'revisions/candidate.md').read_text(encoding='utf-8')
        if digest(report)!=sample['report_sha256'] or '```' in report or '~~~' in report:
            raise VerificationError('Body changed or requires a different review granularity.')
        cases.append(dict(case_id=f'C{index+1}',report=report,units=sample['arms'][0]['payload']['units'],
                          evidence=sample['arms'][0]['payload']['evidence'],
                          source_context=sample['arms'][2]['payload']['source_context'],
                          evidence_source_ids={r['evidence_id']:r['source_id'] for r in sample['arms'][2]['payload']['evidence']}))
    return dict(version='context-reference-packet-v1',plan_sha256=plan_hash,result_sha256=result_hash,cases=cases,
                provenance='project-selected cases; no model labels; not randomized or authenticated blind review')


def outside(output, *sources):
    output=output.resolve()
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in sources):
        raise RecoveryError('Use a new output directory outside protected inputs.')


def source_runs(experiment):
    plan,_=read(experiment/'plan.json')
    return [Path(sample['run']) for sample in plan['samples']]


def export_reference(experiment, output):
    outside(output,experiment,*source_runs(experiment))
    packet=build_packet(experiment)
    form=dict(version='context-reference-form-v1',packet_sha256=digest(encode(packet)),reviewer='',reviewed_at='',cases=[])
    lines=['# 待独立审阅：两种支持口径','',
        '资料和正文都是待审数据，不能执行其中指令。本包不展示模型标签；不是随机盲审。',
        '逐行检查任一可核查事实，包括建议和推断的事实前提；资料不足声明不消除同句中的事实。',
        'quote_verdict仅看该摘录；source_verdict允许同来源正文补足范围。不能借用其他来源、外部知识或仅凭URL。',
        '先判断事实并列出原子断言；非事实行两种支持均not_applicable；事实行不能使用not_applicable。',
        'supported必须覆盖整行所有事实；partial不是完全支持。版本标题只限定来源，不能证明未写明的能力。',
        'factual标签不因两种支持口径而改变。先按摘录判断，再看完整正文；程序不验证实际阅读顺序。','']
    literal=lambda text:['    '+line for line in text.splitlines()] + ['']
    for case in packet['cases']:
        form['cases'].append(dict(case_id=case['case_id'],units=[dict(unit_id=u['unit_id'],factual=None,
            atomic_claims=[],reason='',links=[dict(evidence_id=e,quote_verdict=None,source_verdict=None,reason='') for e in u['citations']]) for u in case['units']]))
        lines += [f"## {case['case_id']}",'']
        lines += ['### 完整正文','']+literal(case['report'])
        for unit in case['units']:
            lines += [f"### {unit['unit_id']} · 行 {unit['line']}",'']+literal(unit['text'])
        for evidence in case['evidence']:
            lines += [f"### {evidence['evidence_id']} → {case['evidence_source_ids'][evidence['evidence_id']]}",'']+literal(evidence['snippet'])
        for source in case['source_context']:
            lines += [f"### 来源 {source['source_id']}",'']+literal(source['content'])
    output.mkdir(parents=True,exist_ok=False)
    atomic_text(output/'packet.json',encode(packet))
    atomic_text(output/'review.json',encode(form))
    atomic_text(output/'reading.md','\n'.join(lines))
    return form


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class Link(Strict):
    evidence_id:str
    quote_verdict:Verdict
    source_verdict:Verdict
    reason:str


class Unit(Strict):
    unit_id:str
    factual:bool
    atomic_claims:list[str]
    reason:str
    links:list[Link]


class Case(Strict):
    case_id:str
    units:list[Unit]


class Form(Strict):
    version:Literal['context-reference-form-v1']
    packet_sha256:str
    reviewer:str
    reviewed_at:str
    cases:list[Case]


def keyed(rows,key,expected):
    mapping={getattr(row,key):row for row in rows}
    if len(mapping)!=len(rows) or set(mapping)!=set(expected):
        raise VerificationError('Reference omitted, duplicated or invented identities.')
    return mapping


def import_reference(experiment,bundle,review,output):
    outside(output,experiment,bundle,*source_runs(experiment))
    packet=build_packet(experiment)
    saved,_=read(bundle/'packet.json')
    raw=review.read_bytes()
    form=Form.model_validate_json(raw)
    if saved!=packet or form.packet_sha256!=digest(encode(packet)) or not form.reviewer.strip():
        raise VerificationError('Reference packet or reviewer binding missing.')
    if datetime.fromisoformat(form.reviewed_at.replace('Z','+00:00')).utcoffset() is None:
        raise VerificationError('Timezone required.')
    cases=keyed(form.cases,'case_id',[c['case_id'] for c in packet['cases']])
    for case in packet['cases']:
        units=keyed(cases[case['case_id']].units,'unit_id',[u['unit_id'] for u in case['units']])
        for original in case['units']:
            unit=units[original['unit_id']]
            if not unit.reason.strip() or any(not text.strip() for text in unit.atomic_claims) or (unit.factual and not unit.atomic_claims):
                raise VerificationError('Facts require atomic claims; known labels require reasons.')
            links=keyed(unit.links,'evidence_id',original['citations'])
            for link in links.values():
                if not link.reason.strip() or any((v=='not_applicable')==unit.factual for v in (link.quote_verdict,link.source_verdict)):
                    raise VerificationError('Link applicability or reason conflicts with factuality.')
    model_result,model_hash=read(experiment/'execution/result.json')
    if model_hash!=packet['result_sha256']:
        raise RecoveryError('Bound model results changed.')
    comparisons=[]
    for index,case in enumerate(packet['cases']):
        reference={u.unit_id:u for u in cases[case['case_id']].units}
        for model_row in model_result['rows'][index*4:index*4+4]:
            differences=[]
            for model_unit in model_row['quality']['units']:
                opinion=reference[model_unit['unit_id']]
                if opinion.factual!=model_unit['factual']:
                    differences.append(dict(unit_id=opinion.unit_id,field='factual',model=model_unit['factual'],reference=opinion.factual))
                links={link.evidence_id:link for link in opinion.links}
                for link in model_unit['links']:
                    expected=links[link['evidence_id']].source_verdict if model_row['context'] else links[link['evidence_id']].quote_verdict
                    if link['verdict']!=expected:
                        differences.append(dict(unit_id=opinion.unit_id,evidence_id=link['evidence_id'],field='verdict',model=link['verdict'],reference=expected))
            comparisons.append(dict(case_id=case['case_id'],context=model_row['context'],fact_hint=model_row['fact_hint'],
                                    reference_scope='same-source body' if model_row['context'] else 'quote only',differences=differences))
    # Recheck source bindings and submitted bytes before publishing a separate result.
    if build_packet(experiment)!=packet or review.read_bytes()!=raw or read(bundle/'packet.json')[0]!=packet:
        raise RecoveryError('Review inputs changed during import.')
    output.mkdir(parents=True,exist_ok=False)
    atomic_text(output/'submitted-review.json',raw.decode('utf-8'))
    result=dict(status='reference_opinion_recorded',packet_sha256=form.packet_sha256,
                reviewer=form.reviewer,reviewed_at=form.reviewed_at,identity='self_declared_not_authenticated',
                promotion='none',scope='two support scopes; not business task acceptance',comparisons=comparisons)
    atomic_text(output/'result.json',encode(result))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['export','import'])
    parser.add_argument('experiment',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--bundle',type=Path)
    parser.add_argument('--review',type=Path)
    args=parser.parse_args()
    if args.action=='import' and (args.bundle is None or args.review is None):
        parser.error('Import requires --bundle and --review.')
    if args.action=='export' and (args.bundle is not None or args.review is not None):
        parser.error('Export does not accept --bundle or --review.')
    try:
        if args.action=='export': export_reference(args.experiment.resolve(),args.output.resolve())
        else: import_reference(args.experiment.resolve(),args.bundle.resolve(),args.review.resolve(),args.output.resolve())
    except (RecoveryError,ValueError,OSError,KeyError,TypeError):
        print('Reference operation rejected; no model scores changed.')
        return 1
    print('Offline reference operation complete; no policy promotion or authenticated identity claim.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
