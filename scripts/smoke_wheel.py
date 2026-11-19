"""Run with the wheel-installed interpreter; source checkout and editable installs are rejected."""
import argparse
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
if module == 'researchdesk.task_eval':
    import dotenv
    def no_credentials(*args, **kwargs):
        raise RuntimeError('Credential loading disabled during offline gate smoke')
    dotenv.load_dotenv = no_credentials
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
    checks = dict(non_editable_wheel_import=True, installed_code_matches_wheel=True,
                  console_entrypoint_help=True, isolated_module_help=True,
                  mechanism_checks_passed=True, historical_summary_readonly=True,
                  evaluation_gate_process_matrix=True, standalone_body_limit_rejected=True,
                  evaluation_gate_sources_readonly=True)
    result = dict(version='wheel-smoke-v1', status='passed', checks=checks,
                  wheel=wheel.name, wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                  python=sys.version.split()[0], package_version=distribution.version,
                  checked_python_files=checked_files, model_requests=0, search_requests=0,
                  evaluation_gates=gates,
                  scope='installed wheel, fixed fixtures; not live research quality or universal platform support')
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    (output / 'summary.md').write_text('# Wheel 安装验收\n\n非 editable 安装，在源码目录之外运行。\n\n'
        + '\n'.join(f'- {name}: passed' for name in checks)
        + '\n\n| 离线样例 | 选择口径 | 退出码 | 状态 |\n|---|---|---:|---|\n'
        + '\n'.join(f"| {row['case']} | {row['gate']} | {row['exit_code']} | {row['status']} |" for row in gates['cases'])
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
