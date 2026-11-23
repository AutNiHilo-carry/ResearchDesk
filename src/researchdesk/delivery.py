"""Portable, offline-verified research deliveries. Integrity is not research acceptance."""
import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
from .policy import ToolLimits
from .report_constraints import check_body, policy as constraint_policy
from .review import build_packet, export_review, fingerprint, reading_guide
from .storage import atomic_text
from .task_eval import check_saved_result, quality_metrics, result_gate
from .task_grading import grade_tasks
from .verification import VerificationError, prepare, replay_assessment, split_units

VERSION = 'research-delivery-v1'
MAX_FILES = 256
MAX_BYTES = 100_000_000


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def statistics(result, packet, evaluation):
    """Whitelisted saved declarations, not newly measured counters or monetary costs."""
    counts = ('model_calls', 'tool_calls', 'provider_requests', 'retry_count', 'recovery_replays', 'total_tokens')
    def count(value):
        require(value is None or (type(value) is int and value >= 0), 'Invalid recorded count statistic.')
        return value
    def seconds(value):
        require(value is None or (type(value) in (int,float) and math.isfinite(value) and value >= 0),
                'Invalid recorded duration statistic.')
        return value
    records = {r['task_id']:r for r in result['tasks']}
    tasks, hashes = [], {}
    for task in packet['tasks']:
        record = records[task['task_id']]
        metrics = record.get('generation_metrics') or {}
        row = dict(task_id=task['task_id'], generation_status=task['generation_status'],
                   recorded_generation={k:count(metrics.get(k)) for k in counts},
                   recorded_elapsed_seconds=seconds(record.get('elapsed_seconds')),
                   tool_policy=None, model_call_limit=None, resume_count=None)
        if task['generation_status']=='completed':
            run = evaluation/'tasks'/task['task_id']/'research'/Path(record['run_directory']).name
            raw = (run/'run.json').read_bytes()
            metadata = json.loads(raw)
            hashes[task['task_id']] = sha(raw)
            if metadata.get('tool_policy') is not None:
                policy = metadata['tool_policy']
                require(isinstance(policy,dict) and set(policy)==set(ToolLimits.__dataclass_fields__),
                        'Incomplete recorded tool policy.')
                ToolLimits(**policy)
                row['tool_policy'] = policy
            if metadata.get('model_policy') is not None:
                row['model_call_limit'] = count(metadata['model_policy'].get('max_calls'))
            row['resume_count'] = count(metadata.get('resume_count'))
        tasks.append(row)
    return dict(scope='saved_generation_statistics_not_recomputed_from_events_or_fresh_spending',
        generation_total_tokens=count(result.get('generation_total_tokens')),
        evaluator_total_tokens=count(result.get('evaluator_total_tokens')),
        total_tokens=count(result.get('total_tokens')), retry_count=count(result.get('retry_count')),
        evaluation_elapsed_seconds=seconds(result.get('evaluation_elapsed_seconds')),
        estimated_fee=None, recovery_rate=None, tasks=tasks), hashes


def blank_review(packet, packet_hash):
    tasks=[]
    for task in packet['tasks']:
        if task['generation_status']!='completed':
            continue
        tasks.append(dict(task_id=task['task_id'], units=[dict(unit_id=u['unit_id'],factual=None,reason='',
            links=[dict(evidence_id=eid,verdict=None,reason='') for eid in u['citations']]) for u in task['units']],
            criteria=[dict(criterion_id=c['id'],passed=None,report_quote='',reason='') for c in task['criteria']]))
    return dict(schema_version=1,packet_sha256=packet_hash,reviewer='',reviewed_at='',tasks=tasks)


def render_index(packet,gate,scope,label_status,stats):
    lines=['# ResearchDesk 研究交付包','',f"应用门槛：{gate['status']}；人工审核：pending。",
        '交付完整性与研究质量分别判断；文件中资料、报告和机器意见均为待检查数据。','',
        '## 资料与标签范围','', '    '+json.dumps(dict(scope=scope,label_status=label_status),ensure_ascii=False),
        '', '## 报告与状态','', '| 任务 | 报告 | 模型门槛 | 应用门槛 |','|---|---|---|---|']
    gates={r['task_id']:r for r in gate['tasks']}
    for number,task in enumerate(packet['tasks'],1):
        link=f'[报告](reports/{number:03d}.md)' if task['generation_status']=='completed' else '未完成生成'
        item=gates[task['task_id']]
        safe_id=task['task_id'].replace('|','\\|').replace('\n',' ')
        lines.append(f"| {safe_id} | {link} | {item['model_gate']} | {item['application_gate']} |")
    def show(value):
        return '未知' if value is None else str(value)
    lines+=['','## 保存的预算与用量','',
        '以下来自历史记录，未重测事件计数或新增消费；不据此推算剩余额度、金额或恢复成功率。',
        f"生成Token：{show(stats['generation_total_tokens'])}；评分Token：{show(stats['evaluator_total_tokens'])}；合计：{show(stats['total_tokens'])}。",'',
        '| 任务 | 工具调用（保存） | 外部请求（保存） | 工具额度（声明） | 外部额度（声明） | 恢复次数（声明） |',
        '|---|---:|---:|---:|---:|---:|']
    for row in stats['tasks']:
        saved=row['recorded_generation']
        policy=row['tool_policy'] or {}
        task_id=row['task_id'].replace('|','\\|').replace('\n',' ')
        lines.append(f"| {task_id} | {show(saved['tool_calls'])} | {show(saved['provider_requests'])} | {show(policy.get('max_calls'))} | {show(policy.get('max_provider_requests'))} | {show(row['resume_count'])} |")
    lines+=['','## 核查与复核','',
        '- [逐行引用标签与指标](quality.json)、[完整证据台账与来源快照](evidence.json)',
        '- [质量与应用门槛](assessment.json)、[保存的用量／预算统计](statistics.json)、[来源绑定](provenance.json)',
        '- [人工阅读材料](review/reading.md)、[空白复核表](review/review.json)',
        '', '复核表须另存到包外填写；本包保持不可变。整体含机器评分，不属于独立盲测。',
        '统计为历史保存声明，未重测事件计数或新增消费；未知保持null，费用和Recovery Rate未测。',
        '', '离线验包：`python -m researchdesk.delivery verify <交付包目录>`。',
        '验包返回0只表示完整性和保存标签一致，不认证作者、模型后端、研究准确率或人工通过。','']
    return '\n'.join(lines)


def validate_content(root):
    envelope=read_json(root/'review/packet.json')
    packet=envelope['content']
    require(fingerprint(packet)==envelope['sha256'], 'Review context hash mismatch.')
    require(fingerprint(read_json(root/'review/review.json'))==fingerprint(blank_review(packet,envelope['sha256'])),
            'Delivery review template must remain blank; save filled opinions outside the bundle.')
    evaluation=read_json(root/'evaluation.json')
    quality=read_json(root/'quality.json')
    provenance=read_json(root/'provenance.json')
    assessment=read_json(root/'assessment.json')
    expected_ids=[t['task_id'] for t in packet['tasks']]
    records={r['task_id']:r for r in evaluation['tasks']}
    require(len(set(expected_ids))==len(expected_ids) and expected_ids==[r['task_id'] for r in evaluation['tasks']],
            'Delivery task identities/order mismatch.')
    require(evaluation['thresholds']==packet['thresholds'], 'Delivery thresholds mismatch.')
    units,evidence,sources=[],{},{}
    reports=[]
    for number,task in enumerate(packet['tasks'],1):
        record=records[task['task_id']]
        require(record['generation_status']==task['generation_status'], 'Generation status mismatch.')
        if task['generation_status']!='completed':
            continue
        name=f'reports/{number:03d}.md'
        require((root/name).read_text(encoding='utf-8')==task['report'], 'Exported report differs from its context.')
        reports.append(name)
        found=split_units(task['report'])
        for unit in found:
            unit.update(unit_id=task['task_id']+':'+unit['unit_id'],task_id=task['task_id'])
        require(fingerprint(found)==fingerprint(task['units']), 'Review unit anchors differ from report.')
        units.extend(found)
        evidence.update(task['evidence'])
        sources.update(task['sources'])
        rubric=grade_tasks(dict(tasks=[dict(task_id=task['task_id'],report=task['report'],criteria=task['criteria'])]),
            lambda _:dict(tasks=[dict(task_id=task['task_id'],criteria=record['rubric']['criteria'])]))[task['task_id']]
        require(fingerprint(rubric)==fingerprint(record['rubric']), 'Business labels do not recompute.')
        if 'constraints' in record or 'constraint_policy' in evaluation:
            require(evaluation.get('constraint_policy')==constraint_policy()
                    and record.get('constraints')==check_body(task['report']), 'Body constraints do not recompute.')
    payload,input_hash=prepare(units,evidence,sources)
    checked=replay_assessment(payload,quality)
    require(input_hash==provenance['quality_input_sha256'], 'Evidence input fingerprint mismatch.')
    require(packet['result_sha256']==provenance['source_result_text_sha256']
            and packet['dataset_sha256']==provenance['dataset_sha256']
            and packet['model_quality_sha256']==provenance['source_quality_text_sha256'], 'Source bindings mismatch.')
    ledger=read_json(root/'evidence.json')
    require(fingerprint(ledger)==fingerprint(dict(units=units,evidence=evidence,sources=sources)),
            'Evidence ledger differs from review context.')
    for task in packet['tasks']:
        if task['generation_status']=='completed':
            rows=[r for r in checked['units'] if r['task_id']==task['task_id']]
            require(fingerprint(quality_metrics(rows))==fingerprint(records[task['task_id']]['quality']),
                    'Per-task citation metrics mismatch.')
    expected=dict(version='delivery-assessment-v1',model=result_gate(evaluation,gate='model'),
        application=result_gate(evaluation,gate='application'),quality_metrics=checked['metrics'],
        human_review='pending',final_acceptance='pending',promotion='none',api_requests=0)
    require(fingerprint(assessment)==fingerprint(expected), 'Delivery gate badges differ from component checks.')
    stats=read_json(root/'statistics.json')
    validate_statistics(stats,evaluation)
    require((root/'index.md').read_text(encoding='utf-8')==render_index(packet,assessment['application'],
                provenance.get('scope'),provenance.get('label_status'),stats), 'Reading index differs from verified gate state.')
    require((root/'review/reading.md').read_text(encoding='utf-8')==reading_guide(packet),
            'Human reading material differs from the bound report context.')
    expected_files={'index.md','evaluation.json','quality.json','evidence.json','statistics.json','provenance.json',
        'assessment.json','review/packet.json','review/review.json','review/reading.md','review/README.md',*reports}
    return assessment,expected_files


def validate_statistics(stats,evaluation):
    require(stats.get('scope')=='saved_generation_statistics_not_recomputed_from_events_or_fresh_spending'
            and stats.get('estimated_fee') is None and stats.get('recovery_rate') is None,
            'Recorded statistics must not claim newly measured cost or recovery rate.')
    require([r['task_id'] for r in stats['tasks']]==[r['task_id'] for r in evaluation['tasks']],
            'Statistics task identities mismatch.')
    for key in ('generation_total_tokens','evaluator_total_tokens','total_tokens','retry_count'):
        value=stats.get(key)
        require(value is None or (type(value) is int and value>=0),'Invalid saved batch count.')
    duration=stats.get('evaluation_elapsed_seconds')
    require(duration is None or (type(duration) in (int,float) and math.isfinite(duration) and duration>=0),
            'Invalid saved batch duration.')
    gen,judge=stats.get('generation_total_tokens'),stats.get('evaluator_total_tokens')
    require(stats.get('total_tokens')==(gen+judge if gen is not None and judge is not None else None),
            'Saved token totals are inconsistent.')
    for row,record in zip(stats['tasks'],evaluation['tasks']):
        require(row['generation_status']==record['generation_status'],'Statistics generation status mismatch.')
        for value in row['recorded_generation'].values():
            require(value is None or (type(value) is int and value>=0),'Invalid saved generation count.')
        duration=row['recorded_elapsed_seconds']
        require(duration is None or (type(duration) in (int,float) and math.isfinite(duration) and duration>=0),
                'Invalid saved generation duration.')
        require(row['recorded_generation'].get('provider_requests')==record['generation_metrics'].get('provider_requests'),
                'Recorded provider count differs from the gate input.')
        if row['tool_policy'] is not None: ToolLimits(**row['tool_policy'])
        for key in ('model_call_limit','resume_count'):
            require(row[key] is None or (type(row[key]) is int and row[key]>=0),'Invalid saved limit or resume count.')


def verify_delivery(directory):
    """Self-contained integrity and saved-label verification, without original source paths."""
    root=Path(directory).resolve()
    marker=root/'manifest.json'
    require(marker.is_file() and not marker.is_symlink() and marker.stat().st_size<=1_000_000,
            'Missing or invalid delivery manifest; incomplete directories are not deliveries.')
    raw_marker=marker.read_bytes()
    manifest=json.loads(raw_marker)
    require(manifest.get('version')==VERSION and isinstance(manifest.get('files'),dict), 'Unsupported delivery manifest.')
    files=manifest['files']
    require(0<len(files)<=MAX_FILES,'Invalid delivery file count.')
    actual=set()
    for path in root.rglob('*'):
        require(not path.is_symlink() and path.resolve().is_relative_to(root), 'Delivery contains an escaping link.')
        if path.is_file(): actual.add(path.relative_to(root).as_posix())
    require(actual==set(files)|{'manifest.json'},'Missing or unlisted delivery files.')
    snapshots={}
    total=0
    for name,expected in files.items():
        posix=PurePosixPath(name)
        require(isinstance(name,str) and '\\' not in name and not posix.is_absolute()
                and posix.as_posix()==name and all(p not in ('.','..') for p in posix.parts)
                and name!='manifest.json' and isinstance(expected,str) and re.fullmatch('[0-9a-f]{64}',expected),
                'Unsafe delivery path or invalid file hash.')
        path=root/name
        total+=path.stat().st_size
        require(total<=MAX_BYTES,'Delivery exceeds size budget.')
        raw=path.read_bytes()
        require(sha(raw)==expected,'Delivery file hash mismatch.')
        snapshots[name]=raw
    assessment,expected_files=validate_content(root)
    require(set(files)==expected_files,'Unexpected delivery layout.')
    require(marker.read_bytes()==raw_marker and all((root/n).read_bytes()==r for n,r in snapshots.items()),
            'Delivery changed during verification.')
    return dict(version=VERSION,status='integrity_verified',files_verified=len(files),api_requests=0,
        model_gate=assessment['model']['status'],application_gate=assessment['application']['status'],
        human_review='pending',final_acceptance='pending',promotion='none',
        scope='unsigned_bundle_integrity_and_saved_label_consistency_not_source_authentication_or_research_accuracy')


def create_delivery(evaluation,dataset,output):
    evaluation,dataset,output=[Path(p).resolve() for p in (evaluation,dataset,output)]
    require(not output.exists() and not output.is_relative_to(evaluation),'Use a new delivery outside the source evaluation.')
    source=evaluation/'result.json'
    raw=source.read_bytes()
    result=json.loads(raw)
    require(result.get('status')=='complete','Delivery requires a completely scored batch; failed gates can still be exported.')
    output.parent.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(prefix='.researchdesk-delivery-',dir=output.parent) as temporary:
        scratch=Path(temporary)
        gate=check_saved_result(source,dataset,scratch/'source-check',gate='application')
        packet=build_packet(evaluation,dataset)
        quality_raw=(evaluation/'citation-quality.json').read_bytes()
        quality=json.loads(quality_raw)
        stats,manifest_hashes=statistics(result,packet,evaluation)
        stage=scratch/'bundle'
        stage.mkdir()
        packet_hash=export_review(evaluation,dataset,stage/'review')
        require(packet_hash==fingerprint(packet),'Source review context changed during delivery.')
        curated=dict(status=result['status'],thresholds=result['thresholds'],tasks=[])
        if 'constraint_policy' in result: curated['constraint_policy']=result['constraint_policy']
        units,evidence,sources=[],{},{}
        records={r['task_id']:r for r in result['tasks']}
        (stage/'reports').mkdir()
        for number,task in enumerate(packet['tasks'],1):
            record=records[task['task_id']]
            row={k:record[k] for k in ('task_id','generation_status','success','rubric','quality','constraints') if k in record}
            row['generation_metrics']=dict(provider_requests=(record.get('generation_metrics') or {}).get('provider_requests'))
            curated['tasks'].append(row)
            if task['generation_status']=='completed':
                name=f'reports/{number:03d}.md'
                atomic_text(stage/name,task['report'])
                units.extend(task['units']); evidence.update(task['evidence']); sources.update(task['sources'])
        atomic_text(stage/'index.md',render_index(packet,gate,result.get('scope'),result.get('label_status'),stats))
        write_json(stage/'evaluation.json',curated)
        write_json(stage/'quality.json',quality)
        write_json(stage/'evidence.json',dict(units=units,evidence=evidence,sources=sources))
        write_json(stage/'statistics.json',stats)
        assessment=dict(version='delivery-assessment-v1',model=result_gate(curated,gate='model'),application=gate,
            quality_metrics=quality['metrics'],human_review='pending',final_acceptance='pending',promotion='none',api_requests=0)
        write_json(stage/'assessment.json',assessment)
        write_json(stage/'provenance.json',dict(version='delivery-provenance-v1',source_evaluation=str(evaluation),
            source_result_sha256=sha(raw),source_result_text_sha256=packet['result_sha256'],
            source_quality_sha256=sha(quality_raw),source_quality_text_sha256=packet['model_quality_sha256'],
            dataset_sha256=packet['dataset_sha256'],quality_input_sha256=result['quality_input_sha256'],
            run_manifest_sha256=manifest_hashes,api_requests=0,scope=result.get('scope'),label_status=result.get('label_status')))
        hashes={p.relative_to(stage).as_posix():sha(p.read_bytes()) for p in sorted(stage.rglob('*')) if p.is_file()}
        write_json(stage/'manifest.json',dict(version=VERSION,files=hashes))
        verify_delivery(stage)
        check_saved_result(source,dataset,scratch/'source-recheck',gate='application')
        _,after_hashes=statistics(result,packet,evaluation)
        require(source.read_bytes()==raw and (evaluation/'citation-quality.json').read_bytes()==quality_raw
                and fingerprint(build_packet(evaluation,dataset))==packet_hash and after_hashes==manifest_hashes,
                'Source changed during delivery creation.')
        output.mkdir(exist_ok=False)
        for name in sorted(hashes):
            target=output/name
            target.parent.mkdir(parents=True,exist_ok=True)
            atomic_text(target,(stage/name).read_text(encoding='utf-8'))
        # Publish the acceptance marker last; partial directories are never valid deliveries.
        atomic_text(output/'manifest.json',(stage/'manifest.json').read_text(encoding='utf-8'))
    return dict(status='delivery_created',application_gate=gate['status'],exit_code=gate['exit_code'],
                human_review='pending',final_acceptance='pending',api_requests=0,output=str(output))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    create=sub.add_parser('create')
    create.add_argument('evaluation',type=Path)
    create.add_argument('--dataset',required=True,type=Path)
    create.add_argument('--output',required=True,type=Path)
    verify=sub.add_parser('verify')
    verify.add_argument('bundle',type=Path)
    args=parser.parse_args()
    try:
        if args.action=='create':
            result=create_delivery(args.evaluation,args.dataset,args.output)
            print(f"Delivery: {args.output/'index.md'}; application gate: {result['application_gate']}; zero API requests.")
            return result['exit_code']
        result=verify_delivery(args.bundle)
        print(f"Integrity verified: {result['files_verified']} files; application gate: {result['application_gate']}; human review pending.")
        return 0
    except Exception as error:
        print(f'Delivery operation failed ({type(error).__name__}); no research approval assigned.')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
