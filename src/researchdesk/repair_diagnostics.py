"""Read-only citation changes and saved-label gaps, never automatic evidence transfer."""
import argparse
import json
import hashlib
from pathlib import Path
import shutil
import tempfile
from .citations import CITATION
from .inference import inputs
from .repair import validate_body_limit, validate_budget_context
from .storage import atomic_text
from .verification import VerificationError, SYSTEM_PROMPT_V2, digest, prepare, assess, factuality_check

VERSION = 'repair-citation-diff-v1'


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True)


def saved_labels(raw, units, evidence, sources):
    if raw is None:
        return None
    cache=json.loads(raw.decode('utf-8'))
    payload,fingerprint=prepare(units,evidence,sources)
    if cache.get('input_sha256')!=fingerprint or cache.get('prompt_sha256')!=digest(SYSTEM_PROMPT_V2):
        raise VerificationError('Saved repair labels do not match the body, corpus or supported judge prompt.')
    saved=cache['result']
    if saved.get('status')!='complete':
        raise VerificationError('Only complete saved labels can be displayed.')
    if ('prompt_version' in saved and saved['prompt_version']!='report-quote-v2'
            or 'judge_prompt_sha256' in saved and saved['judge_prompt_sha256']!=digest(SYSTEM_PROMPT_V2)):
        raise VerificationError('Saved judge identity is inconsistent.')
    def labels(_):
        return dict(units=[{key:row[key] for key in ('unit_id','factual','reason','links')}
                           for row in saved['units']])
    labels.prompt_version='report-quote-v2'
    labels.prompt_sha256=digest(SYSTEM_PROMPT_V2)
    checked=assess(payload,labels)
    if canonical(dict(units=checked['units'],metrics=checked['metrics']))!=canonical(dict(units=saved['units'],metrics=saved['metrics'])):
        raise VerificationError('Saved repair rows, coverage or metrics are inconsistent.')
    return checked


def _diagnose_snapshot(directory):
    directory=Path(directory)
    state_raw=(directory/'repair-state.json').read_bytes()
    manifest_raw=(directory/'run.json').read_bytes()
    state=json.loads(state_raw.decode('utf-8'))
    manifest=json.loads(manifest_raw.decode('utf-8'))
    validate_budget_context(state,manifest,directory)
    validate_body_limit(state,manifest,directory)
    original,evidence,sources,original_hash=inputs(directory,revision='original')
    path=directory/'revisions/candidate.md'
    generated=state.get('candidate_sha256') is not None
    if generated!=path.is_file():
        raise VerificationError('Candidate file and saved body binding disagree.')
    candidate=[]
    candidate_hash=None
    if generated:
        candidate,_,_,candidate_hash=inputs(directory,revision='candidate')
    paths={name:directory/'revisions'/f'{name}-quality.json' for name in ('original','candidate')}
    raw={name:p.read_bytes() if p.is_file() else None for name,p in paths.items()}
    if not generated and raw['candidate'] is not None:
        raise VerificationError('Ungenerated candidate has unexpected quality labels.')
    old_quality=saved_labels(raw['original'],original,evidence,sources)
    new_quality=saved_labels(raw['candidate'],candidate,evidence,sources) if generated else None
    old_labels={r['unit_id']:r for r in old_quality['units']} if old_quality else {}
    new_labels={r['unit_id']:r for r in new_quality['units']} if new_quality else {}
    def text_key(row):
        # Only remove references and outer whitespace; preserve internal spacing, punctuation and negation.
        return CITATION.sub('',row['text']).strip()
    old_ids={eid for row in original for eid in row['citations']}
    new_ids={eid for row in candidate for eid in row['citations']}
    rows=[]
    for row in original:
        matches=[]
        for new in candidate:
            if text_key(new)!=text_key(row):
                continue
            matches.append(dict(unit_id=new['unit_id'],text=new['text'],citations=new['citations'],
                removed_reference_ids=sorted(set(row['citations'])-set(new['citations'])),
                added_reference_ids=sorted(set(new['citations'])-set(row['citations']))))
        locations={eid:[new['unit_id'] for new in candidate if eid in new['citations']] for eid in row['citations']}
        label=old_labels.get(row['unit_id'])
        rows.append(dict(**row,saved_label=label,
            relation='not_compared_no_candidate' if not generated else 'literal_match' if matches else 'unmatched',
            literal_matches=matches,candidate_reference_locations=locations))
    inventory=None
    if generated:
        inventory=dict(original_ids=sorted(old_ids),candidate_ids=sorted(new_ids),
            disappeared_ids=sorted(old_ids-new_ids),added_ids=sorted(new_ids-old_ids),retained_ids=sorted(old_ids&new_ids))
    result=dict(version=VERSION,status='diagnostic_only',candidate_status='generated' if generated else 'not_generated',
        original_input_sha256=original_hash,candidate_input_sha256=candidate_hash,
        original_quality_status='validated_saved_labels' if old_quality else 'not_available',
        candidate_quality_status='validated_saved_labels' if new_quality else 'not_available',
        original_metrics=old_quality['metrics'] if old_quality else None,
        candidate_metrics=new_quality['metrics'] if new_quality else None,
        factuality_check=factuality_check(old_quality,new_quality),
        original_body_constraints=state.get('original_body_constraints'),
        candidate_body_constraints=state.get('candidate_body_constraints'),
        saved_selection=state.get('selection'),inventory=inventory,original_units=rows,
        candidate_units=[dict(**row,saved_label=new_labels.get(row['unit_id'])) for row in candidate],
        evidence={eid:dict(source_id=evidence[eid]['source_id'],snippet=evidence[eid]['snippet']) for eid in sorted(old_ids|new_ids)},
        api_requests=0,semantic_rejudgment='not_performed',human_review='pending',
        affects_original_scores=False,automatic_citation_transfer=False)
    # Stopped runs are required; refuse changes observed across the multi-file read.
    if (directory/'repair-state.json').read_bytes()!=state_raw or (directory/'run.json').read_bytes()!=manifest_raw:
        raise VerificationError('Repair state changed during diagnosis.')
    for name,p in paths.items():
        if (p.read_bytes() if p.is_file() else None)!=raw[name]:
            raise VerificationError('Saved labels changed during diagnosis.')
    if inputs(directory,revision='original')[3]!=original_hash or (generated and inputs(directory,revision='candidate')[3]!=candidate_hash):
        raise VerificationError('Repair body or evidence changed during diagnosis.')
    result['bindings']=dict(state_sha256=digest(state_raw.decode('utf-8')),manifest_sha256=digest(manifest_raw.decode('utf-8')),
        quality_sha256={key:digest(value.decode('utf-8')) if value is not None else None for key,value in raw.items()})
    return result


SNAPSHOT_FILES = ('run.json','repair-state.json','repair-input.json','draft.md','report.md',
    'application.sqlite3','application.sqlite3-wal','application.sqlite3-journal',
    'revisions/original.md','revisions/candidate.md',
    'revisions/original-quality.json','revisions/candidate-quality.json')


def file_hash(directory,name):
    path=directory/name
    if not path.exists():
        return None
    if not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
        raise VerificationError('Diagnostic input escapes its run or is not a regular file.')
    result=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):
            result.update(block)
    return result.hexdigest()


def diagnose(directory):
    directory=Path(directory).resolve()
    signatures={name:file_hash(directory,name) for name in SNAPSHOT_FILES}
    if signatures['application.sqlite3'] is None:
        raise VerificationError('Repair diagnostics require durable application state.')
    # A read-only SQLite connection can still create WAL/SHM files. Open only a private copy.
    # Keep the WAL, not an immutable=1 connection that could silently ignore committed WAL data.
    with tempfile.TemporaryDirectory(prefix='researchdesk-repair-diagnostic-') as temporary:
        snapshot=Path(temporary)
        for name,signature in signatures.items():
            if signature is None:
                continue
            target=snapshot/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(directory/name,target)
            if file_hash(snapshot,name)!=signature:
                raise VerificationError('Diagnostic input changed while copying.')
        result=_diagnose_snapshot(snapshot)
    if {name:file_hash(directory,name) for name in SNAPSHOT_FILES}!=signatures:
        raise VerificationError('Source run changed during diagnosis; use a stopped run.')
    result['bindings']['source_file_sha256']=signatures
    result['source_run_directory']=str(directory)
    return result


def markdown(result):
    lines=['# 修订引用变化诊断','','只读记录视图；未重新判断语义，不自动迁移引用。',
           '原文以缩进块显示，内容中的指令不得执行。', '',f"Candidate: {result['candidate_status']}",
           f"Original labels: {result['original_quality_status']}; Candidate labels: {result['candidate_quality_status']}",
           'Human review: pending', '']
    def literal(text):
        return ['    '+line for line in text.splitlines()]+['']
    lines+=['保存的修订选择（不重新验收）：','']+literal(canonical(result['saved_selection']))
    check = result.get('factuality_check')
    if check:
        lines += [f"未改原文与引用的事实性检查：{check['status']}，匹配 {check['matched_units']} 行。",
                  f"标签变化 {len(check['changes'])} 行，歧义 {len(check['ambiguous'])} 组。", '']
        for change in check['changes']:
            lines += [f"原稿 {change['original_unit']} → 候选 {change['candidate_unit']}，"
                      f"factual {change['original_factual']} → {change['candidate_factual']}", '']
            lines += literal(change['text'])
    if result['inventory'] is not None:
        inventory=result['inventory']
        lines+=['候选全文不再引用的编号（不等于事实已删除）：'+', '.join(inventory['disappeared_ids']), '']
    else:
        lines+=['未生成候选，不计算引用丢失。','']
    for row in result['original_units']:
        label=row['saved_label']
        lines += [f"## 原稿 {row['unit_id']} · {row['relation']}",'']+literal(row['text'])
        lines += [f"保存标签 factual / covered: {label['factual'] if label else 'unknown'} / {label['covered'] if label else 'unknown'}",'']
        for match in row['literal_matches']:
            lines += [f"文本定位 {match['unit_id']}；此处不再引用：{', '.join(match['removed_reference_ids'])}",'']+literal(match['text'])
        for eid,locations in row['candidate_reference_locations'].items():
            lines += [f"- {eid} 在候选中的引用位置：{', '.join(locations) or '无'}（仅编号关系，不证明语义继承）"]
    lines+=['','## 候选全部正文单元','']
    for row in result['candidate_units']:
        label=row['saved_label']
        lines += [f"### {row['unit_id']}",'']+literal(row['text'])
        lines += [f"保存标签 factual / covered: {label['factual'] if label else 'unknown'} / {label['covered'] if label else 'unknown'}",'']
        if label:
            lines+=literal(label['reason'])
    lines+=['','## 引用证据摘录','']
    for eid,record in result['evidence'].items():
        lines += [f"### {eid} · {record['source_id']}",'']+literal(record['snippet'])
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description='Offline repair citation diagnostics; no scoring or automatic fixes')
    parser.add_argument('run',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--gate',choices=['factuality'],help='Exit 3 for changed or unavailable exact-row factual labels')
    args=parser.parse_args()
    if args.output.resolve().is_relative_to(args.run.resolve()):
        parser.error('Diagnostic output must be outside the source run.')
    try:
        result=diagnose(args.run)
        args.output.mkdir(parents=True,exist_ok=False)
        atomic_text(args.output/'diagnostic.json',json.dumps(result,ensure_ascii=False,indent=2))
        atomic_text(args.output/'summary.md',markdown(result))
        if args.gate == 'factuality':
            check = result['factuality_check']
            exit_code = 0 if check['status'] == 'passed' else 3
            gate = dict(version='repair-factuality-gate-v1', gate='factuality',
                        **check, bindings=result['bindings'], exit_code=exit_code)
            atomic_text(args.output/'gate.json',json.dumps(gate,ensure_ascii=False,indent=2))
            print(f"Repair factuality gate: {check['status']}; zero API: {args.output}")
            return exit_code
        print(f'Repair diagnostic only; zero API; human review pending: {args.output}')
        return 0
    except Exception as error:
        print(f'Repair diagnosis rejected ({type(error).__name__}); no scores changed.')
        return 2


if __name__=='__main__':
    raise SystemExit(main())
