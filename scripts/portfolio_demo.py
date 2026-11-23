"""Internship case demo: prepare offline, explicitly execute once, package offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
from dotenv import load_dotenv
from researchdesk.delivery import create_delivery, verify_delivery
from researchdesk.report_eval import create_plan, execute_plan, controls, application_view
from researchdesk.storage import RecoveryError, atomic_text
from researchdesk.task_eval import load_taskset

REPO=Path(__file__).resolve().parents[1]
DATASET=REPO/'evals/portfolio-cases-v1.json'
SELECTED=['sqlite_live_backup','postgres_queue_claim','api_v1_retry_key']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def check_sources(dataset=DATASET):
    data,tasks,documents=load_taskset(dataset)
    registry_path=dataset.parent/data['source_manifest']
    registry=read(registry_path)
    rows={row['document']:row for row in registry['sources']}
    if set(rows)!=set(documents) or any(rows[name]['sha256']!=doc['sha256'] for name,doc in documents.items()):
        raise RecoveryError('Public note manifest differs from the task documents.')
    return data,tasks,registry_path


def prepare(directory,model='deepseek-flash'):
    directory=directory.resolve()
    if directory.exists(): raise RecoveryError('Use a new demo plan directory.')
    _,tasks,registry=check_sources()
    plan=create_plan(DATASET,directory,model=model,task_ids=SELECTED,maximum=4,timeout=240,
                     generation_contract='body-length-v1')
    metadata=dict(version='portfolio-demo-v1',dataset_sha256=digest(DATASET),registry_sha256=digest(registry),
        runner_sha256=digest(Path(__file__)),selected=SELECTED,
        not_executed=[t['id'] for t in tasks if t['id'] not in SELECTED],
        max_sdk_requests=plan['controls']['max_sdk_requests'],human_review='pending')
    atomic_text(directory/'portfolio.json',json.dumps(metadata,ensure_ascii=False,indent=2))
    return metadata


def validate_plan(directory):
    metadata=read(directory/'portfolio.json')
    _,_,registry=check_sources()
    if (metadata.get('version')!='portfolio-demo-v1' or metadata.get('dataset_sha256')!=digest(DATASET)
            or metadata.get('registry_sha256')!=digest(registry) or metadata.get('runner_sha256')!=digest(Path(__file__))
            or metadata.get('selected')!=SELECTED or metadata.get('max_sdk_requests')!=28):
        raise RecoveryError('Demo inputs or runner changed; prepare a new plan, never repair frozen hashes.')
    plan=read(directory/'plan.json')
    if controls(plan)!=plan.get('controls') or [r['id'] for r in plan['tasks']]!=SELECTED:
        raise RecoveryError('Frozen model plan changed.')
    return metadata


def package(directory,output):
    metadata=validate_plan(directory)
    result=read(directory/'execution/result.json')
    if output.exists(): raise RecoveryError('Use a new showcase directory; original packages are immutable.')
    output.mkdir(parents=True)
    packages={}
    for version in ('v1','v2'):
        arm=result.get('arms',{}).get(version)
        if not isinstance(arm,dict) or arm.get('status')!='complete':
            packages[version]=dict(status='not_packaged',reason='scoring_incomplete_or_missing',application_gate='unknown')
            continue
        try:
            record=create_delivery(directory/'execution'/version,DATASET,output/version)
            verify_delivery(output/version)
            packages[version]=dict(status='packaged',application_gate=record['application_gate'],exit_code=record['exit_code'])
        except Exception as error:
            packages[version]=dict(status='not_packaged',error_type=type(error).__name__,application_gate='unknown')
    _,tasks,_=check_sources()
    lines=['# ResearchDesk 实习案例演示','',
        f"原实验状态：{result.get('status')}；执行模式：{result.get('execution_mode','unknown')}；本次整理零API；人工审核pending。",
        '模型与应用门槛分别列出。资料是项目中文摘记，候选rubric未经独立人工确认。','',
        '| 任务 | 执行状态 | v1模型／应用 | v2模型／应用 |','|---|---|---|---|']
    views={v:application_view(result['arms'][v]) if v in result.get('arms',{}) else dict(tasks={}) for v in ('v1','v2')}
    for task in tasks:
        cells=[]
        for version in ('v1','v2'):
            row=views[version]['tasks'].get(task['id'],{})
            cells.append(row.get('model_gate','unknown')+' / '+row.get('application_gate','unknown'))
        selected=task['id'] in metadata['selected']
        lines.append(f"| {task['id']} | {'已列入本轮，状态见原记录' if selected else 'not_executed'} | "
                     +(' | '.join(cells) if selected else '未执行 | 未执行')+' |')
    lines+=['','## 交付物','']
    for version,record in packages.items():
        lines.append(f"- {version}：[打开交付包]({version}/index.md)，应用门槛{record['application_gate']}。"
                     if record['status']=='packaged' else f"- {version}：未生成完整交付包，原始执行／评分状态保留；{record.get('reason',record.get('error_type'))}。")
    lines+=['','## 审阅与解释','',
        '- 阅读每份报告并逐条核对关键结论与引用；工作副本另存，人工表留空不算完成。',
        '- unknown不是失败率0%，模型通过不是应用通过，验包通过不是研究正确。',
        '- 失败与超长报告完整保留，不为演示挑最高分，不自动修订、补轮或晋升策略。',
        '- Token来自原执行记录，不是打包的新消费；金额未测，固定顺序和单模型限制仍在。',
        '- 两个未执行案例仅作为后续候选，不称为独立held-out benchmark。','']
    receipt=dict(version='portfolio-showcase-v1',execution_status=result.get('status'),execution_mode=result.get('execution_mode'),packages=packages,
        selected=metadata['selected'],not_executed=metadata['not_executed'],api_requests=0,human_review='pending',
        promotion='none',source_execution_sha256=digest(directory/'execution/result.json'))
    atomic_text(output/'index.md','\n'.join(lines))
    atomic_text(output/'result.json',json.dumps(receipt,ensure_ascii=False,indent=2))
    return receipt


def execute(directory):
    validate_plan(directory)
    if (directory/'execution').exists(): raise RecoveryError('Execution exists; refusing any repeated paid work.')
    load_dotenv(REPO/'.env')
    if not os.getenv('DEEPSEEK_API_KEY','').strip(): raise RecoveryError('Missing DEEPSEEK_API_KEY.')
    result=execute_plan(directory)
    return result,package(directory,directory/'showcase')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='action',required=True)
    prep=commands.add_parser('prepare')
    prep.add_argument('--output',type=Path)
    prep.add_argument('--model',default='deepseek-flash')
    run=commands.add_parser('execute')
    run.add_argument('plan',type=Path)
    pack=commands.add_parser('package')
    pack.add_argument('plan',type=Path)
    pack.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.action=='prepare':
            directory=args.output or REPO/'runs/portfolio-cases'/uuid4().hex
            data=prepare(directory,args.model)
            print(f"Prepared only, zero API: {directory}; if executed at most {data['max_sdk_requests']} SDK requests, not a monetary cap.")
            return 0
        if args.action=='package':
            package(args.plan.resolve(),args.output.resolve())
            print(f'Offline showcase: {args.output / "index.md"}; zero API.')
            return 0
        result,showcase=execute(args.plan.resolve())
        print(f"Showcase: {args.plan/'showcase/index.md'}; execution {result['status']}; human review pending.")
        return 0 if all(row.get('application_gate')=='passed' for row in showcase['packages'].values()) else 3
    except Exception as error:
        print(f'Demo stopped ({type(error).__name__}); no automatic retry or policy promotion.')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
