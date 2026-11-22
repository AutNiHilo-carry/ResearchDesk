"""Run with the wheel-installed interpreter; source checkout and editable installs are rejected."""
import argparse
import copy
import shutil
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import zipfile


class SmokeFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SmokeFailure(message)


OFFLINE_GUARD = '''import runpy, sys
def guard(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('Network disabled during wheel smoke')
sys.addaudithook(guard)
module = sys.argv[1]
if module in {'researchdesk.task_eval', 'researchdesk.evaluation_audit', 'researchdesk.judge_stability', 'researchdesk.stability_review'}:
    import dotenv
    def no_credentials(*args, **kwargs):
        raise RuntimeError('Credential loading disabled during offline gate smoke')
    dotenv.load_dotenv = no_credentials
if module in {'researchdesk.judge_stability', 'researchdesk.stability_review'}:
    import researchdesk.verification
    def no_model(*args, **kwargs):
        raise RuntimeError('Model construction disabled during offline stability smoke')
    researchdesk.verification.ModelJudge = no_model
sys.argv = [module] + sys.argv[2:]
runpy.run_module(module, run_name='__main__')
'''


def gate_smoke(root, module):
    """Fixed injected labels; execute actual offline acceptance CLIs in child processes."""
    from researchdesk.ledger import Ledger
    from researchdesk.task_eval import evaluate_batch, load_taskset
    root.mkdir()
    dataset = root/'dataset.json'
    (root/'notes.md').write_text('Fixture fact.', encoding='utf-8')
    dataset.write_text(json.dumps(dict(version='research-tasks-v1',
        scope='fixed injected offline labels; not research quality', label_status='scripted_not_model_or_human',
        thresholds=dict(citation_precision=0.9,evidence_coverage=0.6),tasks=[dict(id='case',
        question='State the fixture fact.', documents=['notes.md'],
        criteria=[dict(id='fact',description='State the documented fact.')])])), encoding='utf-8')
    data,tasks,documents = load_taskset(dataset)
    dataset_before={path:path.read_bytes() for path in (dataset,root/'notes.md')}
    print('Preparing injected offline gate fixtures; zero model/search requests.',flush=True)

    def build(name, *, long=False):
        def runner(task,paths,directory,timeout):
            ledger=Ledger(directory/'research'/('a'*32))
            try:
                source=ledger.add_source('Fixture',str(paths[0]),'Fixture fact.','local')
                evidence=ledger.add_evidence('Fixture fact.',source['id'],'Fixture fact.')
                ledger.save_report('Fixture fact.' + ('X'*201 if long else '') + f"[{evidence['id']}]")
                (ledger.directory/'run.json').write_text(json.dumps(dict(status='completed',mode='fixture')),encoding='utf-8')
            finally:
                ledger.store.close()
            return dict(task_id=task['id'],generation_status='completed',run_directory=str(ledger.directory),
                model='fixture',generation_metrics=dict(provider_requests=0,token_usage_complete=True,total_tokens=0))
        def rubric(payload):
            return dict(tasks=[dict(task_id='case',criteria=[dict(criterion_id='fact',passed=True,
                report_quote='Fixture fact.',reason='Injected offline fixture label')])])
        def evidence(payload):
            return dict(units=[dict(unit_id=row['unit_id'],factual=True,reason='Injected offline fixture label',
                links=[dict(evidence_id=eid,verdict='supported',reason='Injected offline fixture label')
                       for eid in row['citations']]) for row in payload['units']])
        directory=root/name
        result=evaluate_batch(data,tasks,documents,directory,runner=runner,model_name='fixture',
                              task_judge=rubric,evidence_judge=evidence)
        require(result['task_success_rate']==1,'Fixture model gate must pass before testing application rejection.')
        return directory,result

    short,_=build('short')
    long,_=build('long',long=True)
    legacy,legacy_result=build('legacy')
    legacy_result.pop('constraint_policy')
    legacy_result['tasks'][0].pop('constraints')
    (legacy/'result.json').write_text(json.dumps(legacy_result),encoding='utf-8')
    tampered,tampered_result=build('tampered')
    tampered_result['tasks'][0]['constraints']['counted_chars'] += 1
    (tampered/'result.json').write_text(json.dumps(tampered_result),encoding='utf-8')
    def snapshot(directory):
        return {str(p.relative_to(directory)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.rglob('*') if p.is_file()}
    sources={name:snapshot(directory) for name,directory in
             (('short',short),('long',long),('legacy',legacy),('tampered',tampered))}
    receipts=[]
    for name,directory,gate,code,status in (
        ('short_application',short,'application',0,'passed'),
        ('long_application',long,'application',3,'failed'),
        ('long_model',long,'model',0,'passed'),
        ('legacy_application',legacy,'application',3,'unknown'),
        ('tampered_application',tampered,'application',1,None)):
        target=root/(name+'-view')
        module('researchdesk.task_eval','--check-result',directory/'result.json','--dataset',dataset,
               '--output',target,'--gate',gate,expected=code)
        outputs=list(target.glob('*/gate.json'))
        if status is None:
            require(not outputs,'Tampered inputs must not receive a gate verdict.')
        else:
            require(len(outputs)==1,'Expected exactly one installed CLI gate verdict.')
            verdict=json.loads(outputs[0].read_text(encoding='utf-8'))
            provenance=json.loads(outputs[0].with_name('provenance.json').read_text(encoding='utf-8'))
            require(verdict['gate']==gate and verdict['status']==status and verdict['exit_code']==code,
                    'Installed gate status disagrees with its exit code.')
            require(verdict['human_review']==verdict['final_acceptance']=='pending','Gate must retain human acceptance boundary.')
            require(provenance['api_requests']==0,'Offline gate reported provider calls.')
        receipts.append(dict(case=name,gate=gate,exit_code=code,status=status or 'input_rejected'))
    for name,directory in (('short',short),('long',long),('legacy',legacy),('tampered',tampered)):
        require(snapshot(directory)==sources[name],'Offline gate changed a fixture evaluation.')
    body=long/'tasks/case/research'/('a'*32)/'draft.md'
    module('researchdesk.report_constraints',body,'--max-chars','200','--output',root/'body-check.json',expected=3)
    count=json.loads((root/'body-check.json').read_text(encoding='utf-8'))
    require(count['counted_chars']==213 and count['status']=='failed','Installed body checker missed fixture length violation.')
    require(snapshot(long)==sources['long'],'Standalone body check changed its input.')
    require(all(path.read_bytes()==raw for path,raw in dataset_before.items()),'Offline gate changed its dataset.')
    return dict(execution_mode='injected_offline',cases=receipts,source_files_unchanged=True,
                body_count=count['counted_chars'],model_requests=0,search_requests=0)


def factuality_gate_smoke(root, module):
    """Installed-process checks using injected labels, never real research judgments."""
    from researchdesk.ledger import Ledger
    from researchdesk.review import fingerprint
    from researchdesk.task_eval import (evaluate_batch, load_taskset, quality_metrics, task_success,
                                        assessment, application_assessment)
    from researchdesk.task_grading import TASK_PROMPT
    from researchdesk.verification import assess, digest, SYSTEM_PROMPT_V2
    root.mkdir()
    dataset = root/'dataset.json'
    notes = root/'notes.md'
    notes.write_text('Fixture fact.', encoding='utf-8')
    dataset.write_text(json.dumps(dict(version='research-tasks-v1',
        label_status='injected_offline_not_model_or_human',
        thresholds=dict(citation_precision=0.9, evidence_coverage=0.6),
        tasks=[dict(id='case', question='State the fixture fact.', documents=['notes.md'],
                    criteria=[dict(id='fact', description='State the documented fact.')])])), encoding='utf-8')
    originals = {p:p.read_bytes() for p in (dataset, notes)}
    data, tasks, documents = load_taskset(dataset)
    frozen = []
    def runner(task, paths, directory, timeout):
        ledger = Ledger(directory/'research'/('a'*32))
        try:
            source = ledger.add_source('Fixture', str(paths[0]), 'Fixture fact.', 'local')
            evidence = ledger.add_evidence('Fixture fact.', source['id'], 'Fixture fact.')
            ledger.save_report(f"Fixture fact.[{evidence['id']}]\nUncited fixture assertion.")
            (ledger.directory/'run.json').write_text(json.dumps(dict(status='completed',mode='fixture',model='fixture')),encoding='utf-8')
        finally:
            ledger.store.close()
        return dict(task_id=task['id'],generation_status='completed',run_directory=str(ledger.directory),
                    model='fixture',generation_metrics=dict(provider_requests=0,total_tokens=0,token_usage_complete=True))
    def rubric(payload):
        return dict(tasks=[dict(task_id='case',criteria=[dict(criterion_id='fact',passed=True,
            report_quote='Fixture fact.',reason='Injected offline reference')])])
    rubric.prompt_version = 'research-task-rubric-v1'
    rubric.prompt_sha256 = digest(TASK_PROMPT)
    rubric.usage = dict(input_tokens=0,output_tokens=0,total_tokens=0)
    def judge(payload):
        frozen.append(copy.deepcopy(payload))
        return dict(units=[dict(unit_id=r['unit_id'],factual=True,reason='Injected offline reference',
            links=[dict(evidence_id=eid,verdict='supported',reason='Injected offline reference') for eid in r['citations']])
            for r in payload['units']])
    judge.prompt_version = 'report-quote-v2'
    judge.prompt_sha256 = digest(SYSTEM_PROMPT_V2)
    judge.usage = dict(input_tokens=0,output_tokens=0,total_tokens=0)
    baseline = root/'baseline'
    initial = evaluate_batch(data,tasks,documents,baseline,runner=runner,model_name='fixture',
                             task_judge=rubric,evidence_judge=judge)
    require(initial['task_success_rate']==0,'Uncited fixture must fail the initial model gate.')
    stable,excluded,missing = [root/name for name in ('stable','excluded','missing')]
    for destination in (stable,excluded,missing):
        shutil.copytree(baseline,destination)
    original_quality = json.loads((baseline/'citation-quality.json').read_text(encoding='utf-8'))
    def relabel(_):
        return dict(units=[dict(unit_id=r['unit_id'],factual=bool(r['citations']),reason='Injected exclusion',
            links=[dict(evidence_id=eid,verdict='supported',reason='Injected exclusion') for eid in r['citations']])
            for r in frozen[0]['units']])
    relabel.prompt_version, relabel.prompt_sha256 = judge.prompt_version,judge.prompt_sha256
    quality = assess(frozen[0],relabel)
    changed = copy.deepcopy(initial)
    record = changed['tasks'][0]
    record['quality'] = quality_metrics(quality['units'])
    record['success'] = task_success(record,record['rubric'],record['quality'],changed['thresholds'])
    record['assessment'] = assessment(record,changed['status'])
    record['application_assessment'] = application_assessment(record,changed['status'])
    changed['task_success_rate'] = int(record['success'])
    require(changed['task_success_rate']==1,'Exclusion must raise fixture model score without editing prose.')
    require(len(original_quality['units'])==len(quality['units'])==2,'Expected the whole two-line fixture.')
    (excluded/'result.json').write_text(json.dumps(changed),encoding='utf-8')
    (excluded/'citation-quality.json').write_text(json.dumps(quality),encoding='utf-8')
    (missing/'citation-quality.json').unlink()
    def snapshot(directory):
        return {str(p.relative_to(directory)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.rglob('*') if p.is_file()}
    source_dirs = (baseline,stable,excluded,missing)
    before = {p:snapshot(p) for p in source_dirs}
    receipts = []
    for name,directory,code,status in (
        ('stable_factuality',stable,0,'passed'),
        ('excluded_factuality',excluded,3,'review_required'),
        ('missing_factuality',missing,3,'unknown')):
        target = root/(name+'-view')
        module('researchdesk.evaluation_audit',baseline,directory,'--output',target,'--gate','factuality',expected=code)
        verdict = json.loads((target/'gate.json').read_text(encoding='utf-8'))
        audit = json.loads((target/'result.json').read_text(encoding='utf-8'))
        require(verdict['gate']=='factuality' and verdict['status']==status and verdict['exit_code']==code,
                'Installed factuality gate lost its status/exit boundary.')
        require(verdict['audit_content_sha256']==fingerprint(audit),'Installed verdict lost audit binding.')
        require(verdict['human_review']==verdict['final_acceptance']=='pending' and verdict['promotion']=='none',
                'Factuality consistency cannot promote task or human acceptance.')
        if status=='review_required':
            require(audit['factuality_audit']['coverage_increase_task_ids']==['case'],'Exclusion-driven gain was not identified.')
        receipts.append(dict(case=name,gate='factuality',exit_code=code,status=status))
        print(f'Factuality gate smoke {name}: {status}; exit {code}',flush=True)
    stability_checks = saved_stability_smoke(root/'saved-stability', module, baseline, dataset, judge, relabel)
    require(all(snapshot(p)==before[p] for p in source_dirs),'Factuality gate modified fixture inputs.')
    require(all(p.read_bytes()==raw for p,raw in originals.items()),'Factuality gate changed its dataset.')
    return dict(execution_mode='injected_offline',cases=receipts,source_files_unchanged=True,
                fixture_model_rates=dict(baseline=0,excluded=1),saved_stability=stability_checks,
                model_requests=0,search_requests=0)


def saved_stability_smoke(root, module, baseline, dataset, stable_judge, excluded_judge):
    """Scripted saved trials, checked through the real isolated installed CLI."""
    from researchdesk.judge_stability import frozen_input, run_stability, summarize_trials, payload_hash
    root.mkdir()
    payload, controls = frozen_input(baseline, dataset)
    stable = root/'stable'
    excluded = root/'excluded'
    # Callback factories only; run_stability never constructs a provider in this fixture.
    run_stability(payload, controls, stable, lambda: stable_judge, repeats=2)
    excluded_judge.usage = dict(input_tokens=0, output_tokens=0, total_tokens=0)
    run_stability(payload, controls, excluded, lambda: excluded_judge, repeats=2)
    interrupted, corrupt = root/'interrupted', root/'corrupt'
    for directory in (interrupted, corrupt):
        shutil.copytree(stable, directory)
    partial = json.loads((interrupted/'result.json').read_text(encoding='utf-8'))
    for key in summarize_trials(payload, partial['trials']):
        partial.pop(key, None)
    partial.update(status='running', trials=partial['trials'][:1])
    (interrupted/'result.json').write_text(json.dumps(partial), encoding='utf-8')
    invalid = json.loads((corrupt/'result.json').read_text(encoding='utf-8'))
    invalid['trials'][1]['number'] = 1
    (corrupt/'result.json').write_text(json.dumps(invalid), encoding='utf-8')
    directories = (stable, excluded, interrupted, corrupt)
    before = {p:p.read_bytes() for directory in directories for p in directory.rglob('*') if p.is_file()}
    receipts = []
    for name, directory, code, status in (
        ('saved_stable', stable, 0, 'passed'),
        ('saved_excluded', excluded, 3, 'review_required'),
        ('saved_interrupted', interrupted, 3, 'unknown'),
        ('saved_corrupt', corrupt, 1, 'input_rejected')):
        target = root/(name+'-view')
        module('researchdesk.judge_stability', baseline, '--dataset', dataset, '--check-result',
               directory/'result.json', '--output', target, expected=code)
        if code == 1:
            require(not target.exists(), 'Rejected saved records must not emit a gate.')
        else:
            gates = list(target.glob('*/gate.json'))
            require(len(gates)==1, 'Expected exactly one saved-result gate.')
            gate = json.loads(gates[0].read_text(encoding='utf-8'))
            audit = json.loads(gates[0].with_name('audit.json').read_text(encoding='utf-8'))
            require(gate['status']==status and gate['exit_code']==code and gate['api_requests']==0,
                    'Installed saved-result check lost its status or zero-request boundary.')
            require(gate['audit_content_sha256']==payload_hash(audit), 'Saved-result audit binding changed.')
            context = json.loads(gates[0].with_name('review-context.json').read_text(encoding='utf-8'))
            form = json.loads(gates[0].with_name('review-template.json').read_text(encoding='utf-8'))
            require(context['audit_content_sha256']==gate['audit_content_sha256']
                    and context['payload']==payload and form['context_sha256']==payload_hash(context),
                    'Installed review material lost its full-input or audit binding.')
            require([r['unit_id'] for r in form['units']]==[r['unit_id'] for r in payload['units']]
                    and form['reviewer']==form['reviewed_at']=='' and form['status']=='pending'
                    and all(r['factual'] is None and r['reason']=='' and r['atomic_claims']==[] for r in form['units']),
                    'Installed review template must cover all units and keep human fields blank.')
            require(gates[0].with_name('review.md').is_file(), 'Installed review reading material missing.')
            require(gate['human_review']==gate['final_acceptance']=='pending' and gate['promotion']=='none',
                    'Saved-result consistency must not promote human or task acceptance.')
            if status == 'review_required':
                require(audit['new_trial_factual_agreement']==1, 'Expected agreement despite historical drift.')
            if status == 'unknown':
                require(audit['valid_trials']==1 and audit['planned_trials']==2, 'Incomplete run was resumed.')
        receipts.append(dict(case=name, gate='saved_stability_factuality', exit_code=code, status=status))
        print(f'Saved stability smoke {name}: {status}; exit {code}', flush=True)
    bundle = next((root/'saved_stable-view').glob('*/gate.json')).parent
    opinions = review_import_smoke(root/'opinion-import', module, baseline, dataset, stable/'result.json', bundle)
    require({p:p.read_bytes() for directory in directories for p in directory.rglob('*') if p.is_file()}==before,
            'Saved-result check modified source trial files.')
    return dict(cases=receipts, opinion_import=opinions, source_files_unchanged=True, execution_mode='injected_offline',
                model_requests=0, search_requests=0)


def review_import_smoke(root, module, baseline, dataset, source, bundle):
    """Synthetic submissions only; no independent human labels are supplied by CI."""
    root.mkdir()
    before = {p:p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
    template = json.loads((bundle/'review-template.json').read_text(encoding='utf-8'))
    complete = copy.deepcopy(template)
    complete.update(reviewer='Synthetic test submitter; not real human review', reviewed_at='2026-11-22T12:00:00+08:00')
    for row in complete['units']:
        row.update(factual=True, atomic_claims=['Synthetic claim'], reason='Synthetic test opinion')
    partial = copy.deepcopy(complete)
    partial['units'][0].update(factual=None, atomic_claims=[], reason='Insufficient material; synthetic test')
    unbound = copy.deepcopy(complete)
    unbound['context_sha256'] = '0'*64
    receipts = []
    for name, form, code, status in (
        ('opinion_complete', complete, 0, 'complete'),
        ('opinion_partial', partial, 3, 'partial'),
        ('opinion_blank', template, 1, 'input_rejected'),
        ('opinion_unbound', unbound, 1, 'input_rejected')):
        review = root/(name+'.json')
        review.write_text(json.dumps(form), encoding='utf-8')
        output = root/(name+'-record')
        module('researchdesk.stability_review', baseline, '--dataset', dataset, '--source', source,
               '--bundle', bundle, '--review', review, '--output', output, expected=code)
        if code == 1:
            require(not output.exists(), 'Rejected opinions must not emit an accepted record.')
        else:
            result = json.loads((output/'result.json').read_text(encoding='utf-8'))
            require(result['completeness']==status and result['exit_code']==code and result['api_requests']==0,
                    'Installed opinion import lost its completion or request boundary.')
            require(result['unknown_units']==int(code==3) and result['accuracy'] is None
                    and result['promotion']=='none' and result['final_acceptance']=='pending'
                    and result['identity']=='self_declared_not_authenticated',
                    'Opinion completeness cannot promote research quality or authenticate identity.')
            require((output/'submitted-review.json').read_bytes()==review.read_bytes(), 'Submitted opinion changed.')
        receipts.append(dict(case=name, gate='opinion_recording_only', exit_code=code, status=status))
        print(f'Stability opinion smoke {name}: {status}; exit {code}', flush=True)
    require({p:p.read_bytes() for p in bundle.rglob('*') if p.is_file()}==before, 'Opinion import modified its source bundle.')
    return dict(cases=receipts, source_files_unchanged=True, label_status='synthetic_not_independent_human',
                model_requests=0, search_requests=0)


def smoke(wheel, output):
    checkout = Path(__file__).resolve().parents[1]
    output = output.resolve()
    require(not output.is_relative_to(checkout), 'Smoke output/cwd must be outside the source checkout.')
    require(not output.exists(), 'Use a new output directory.')
    import researchdesk
    package = Path(researchdesk.__file__).resolve().parent
    require(package.is_relative_to(Path(sys.prefix).resolve()), 'Package must come from this interpreter environment.')
    require(not package.is_relative_to(checkout), 'Source-checkout imports are forbidden.')
    distribution = importlib.metadata.distribution('researchdesk')
    direct = json.loads(distribution.read_text('direct_url.json') or '{}')
    require(not direct.get('dir_info', {}).get('editable'), 'Editable installs are forbidden.')
    checked_files = 0
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        for name in members:
            parts = Path(name).parts
            require(not any(p in {'.git', '.env', 'runs', '.venv', 'tests'} or p.startswith('.env.') for p in parts),
                    'Wheel contains a forbidden local artifact.')
            if name.startswith('researchdesk/') and name.endswith('.py'):
                installed = package / name.removeprefix('researchdesk/')
                require(installed.is_file() and installed.read_bytes() == archive.read(name),
                        'Installed code differs from the supplied wheel.')
                checked_files += 1
    require(checked_files > 0, 'Wheel has no application code.')
    output.mkdir(parents=True)
    environment = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'DEEPSEEK_API_KEY', 'TAVILY_API_KEY'):
        environment.pop(key, None)
    environment['PYTHONUTF8'] = '1'
    def module(name, *args, expected=0):
        process=subprocess.run([sys.executable, '-I', '-c', OFFLINE_GUARD, name, *map(str, args)], cwd=output,
                               env=environment, check=False, capture_output=True, timeout=90)
        require(process.returncode==expected,f'{name} returned {process.returncode}; expected {expected}.')
    executable = Path(sysconfig.get_path('scripts')) / ('researchdesk.exe' if os.name == 'nt' else 'researchdesk')
    require(executable.is_file(), 'Console entry point missing.')
    subprocess.run([str(executable), '--help'], cwd=output, env=environment,
                   check=True, capture_output=True, timeout=30)
    module('researchdesk.cli', '--help')
    module('researchdesk.acceptance_demo', '--output', output / 'mechanisms')
    demo = json.loads((output / 'mechanisms/result.json').read_text(encoding='utf-8'))
    require(demo['status'] == 'mechanism_checks_passed' and all(demo['checks'].values()), 'Mechanism checks failed.')
    require(demo['model_requests'] == demo['external_requests'] == 0, 'Unexpected provider requests.')
    source = output / 'historical-result.json'
    source.write_text(json.dumps(dict(status='scoring_failed', tasks=[dict(task_id='fixture',
        generation_status='completed', success=None)], task_success_rate=None)), encoding='utf-8')
    before = source.read_bytes()
    module('researchdesk.task_eval', '--summarize', source, '--output', output / 'views')
    require(source.read_bytes() == before, 'Historical result changed.')
    summaries = list((output / 'views').glob('*/summary.md'))
    require(len(summaries) == 1, 'Expected one summary.')
    summary = summaries[0].read_text(encoding='utf-8')
    require('| invalid | unknown |' in summary and 'Final acceptance: pending' in summary,
            'Installed summary lost assessment boundaries.')
    gates=gate_smoke(output/'evaluation-gates',module)
    factuality_gates=factuality_gate_smoke(output/'factuality-gates',module)
    checks = dict(non_editable_wheel_import=True, installed_code_matches_wheel=True,
                  console_entrypoint_help=True, isolated_module_help=True,
                  mechanism_checks_passed=True, historical_summary_readonly=True,
                  evaluation_gate_process_matrix=True, standalone_body_limit_rejected=True,
                  evaluation_gate_sources_readonly=True, factuality_gate_process_matrix=True,
                  factuality_gate_sources_readonly=True, saved_stability_process_matrix=True,
                  saved_stability_sources_readonly=True, stability_opinion_process_matrix=True)
    result = dict(version='wheel-smoke-v1', status='passed', checks=checks,
                  wheel=wheel.name, wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                  python=sys.version.split()[0], package_version=distribution.version,
                  checked_python_files=checked_files, model_requests=0, search_requests=0,
                  evaluation_gates=gates, factuality_gates=factuality_gates,
                  scope='installed wheel, fixed fixtures; not live research quality or universal platform support')
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    (output / 'summary.md').write_text('# Wheel 安装验收\n\n非 editable 安装，在源码目录之外运行。\n\n'
        + '\n'.join(f'- {name}: passed' for name in checks)
        + '\n\n| 离线样例 | 选择口径 | 退出码 | 状态 |\n|---|---|---:|---|\n'
        + '\n'.join(f"| {row['case']} | {row['gate']} | {row['exit_code']} | {row['status']} |" for row in gates['cases'] + factuality_gates['cases'] + factuality_gates['saved_stability']['cases'] + factuality_gates['saved_stability']['opinion_import']['cases'])
        + '\n\n固定资料、零模型/搜索调用；不代表研究质量验收。\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = smoke(args.wheel.resolve(), args.output)
        print(f'Wheel smoke {result["status"]}: {args.output / "summary.md"}')
        return 0
    except SmokeFailure as error:
        print(f'Wheel smoke failed: {error}')
        return 1
    except Exception as error:
        print(f'Wheel smoke failed ({type(error).__name__}).')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
