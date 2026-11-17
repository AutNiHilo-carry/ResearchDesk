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
    guard = '''import runpy, sys
def guard(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('Network disabled during wheel smoke')
sys.addaudithook(guard)
module = sys.argv[1]
sys.argv = [module] + sys.argv[2:]
runpy.run_module(module, run_name='__main__')
'''
    def module(name, *args):
        subprocess.run([sys.executable, '-I', '-c', guard, name, *map(str, args)], cwd=output,
                       env=environment, check=True, capture_output=True, timeout=90)
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
    checks = dict(non_editable_wheel_import=True, installed_code_matches_wheel=True,
                  console_entrypoint_help=True, isolated_module_help=True,
                  mechanism_checks_passed=True, historical_summary_readonly=True)
    result = dict(version='wheel-smoke-v1', status='passed', checks=checks,
                  wheel=wheel.name, wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                  python=sys.version.split()[0], package_version=distribution.version,
                  checked_python_files=checked_files, model_requests=0, search_requests=0,
                  scope='installed wheel, fixed fixtures; not live research quality or universal platform support')
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    (output / 'summary.md').write_text('# Wheel 安装验收\n\n非 editable 安装，在源码目录之外运行。\n\n'
        + '\n'.join(f'- {name}: passed' for name in checks)
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
