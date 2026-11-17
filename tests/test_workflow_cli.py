"""Whole command-line workflow with real graph and storage; scripted provider labels only."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from researchdesk.run_metrics import run_metrics
from researchdesk.task_eval import run_command


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


@pytest.mark.parametrize('maximum,verdict,code,status', [
    (3, 'supported', 0, 'model_support_gate_passed'),
    (3, 'unsupported', 3, 'needs_review'),
    (0, 'supported', 4, 'budget_exhausted'),
])
def test_real_workflow_cli_status_budget_and_completed_resume(tmp_path, maximum, verdict, code, status):
    document = tmp_path / 'source.md'
    document.write_text('Offline fixture; structural verification only.', encoding='utf-8')
    events = tmp_path / 'provider-events.jsonl'
    worker = Path(__file__).with_name('workflow_cli_worker.py')
    environment = os.environ.copy()
    environment.pop('DEEPSEEK_API_KEY', None)
    environment.pop('TAVILY_API_KEY', None)
    def command(options, name):
        outcome = run_command([sys.executable, '-I', str(worker), '--verdict', verdict, '--events', str(events),
                               '--', *options], tmp_path, tmp_path / (name + '.log'), 90, environment)
        assert not outcome['timed_out'], (tmp_path / (name + '.log')).read_text(encoding='utf-8')
        assert outcome['returncode'] == code, (tmp_path / (name + '.log')).read_text(encoding='utf-8')
    output = tmp_path / 'workflows'
    command(['--local-only', '--document', str(document), '--max-model-calls', str(maximum),
             '--max-provider-requests', '0', '--max-tool-calls', '2', '--tool-retries', '0',
             '--output', str(output), '--', 'Explain the fixed fixture.'], 'initial')
    directories = list(output.glob('*/workflow.json'))
    assert len(directories) == 1
    directory = directories[0].parent
    state = read(directories[0])
    assert state['status'] == status and state['last_returncode'] == code
    assert state['human_review'] == 'pending'
    source = directory / 'research' / state['source_run_id']
    source_metrics = run_metrics(source)
    assert source_metrics['provider_requests'] == 0
    assert (source / 'checkpoints.sqlite3').is_file()
    runs = [source]
    if maximum:
        assert state['phase'] == 'done'
        assert state['result']['selected'] == 'original' and state['result']['repair_rounds'] == 0
        assert state['result']['human_review'] == 'pending'
        candidate = directory / 'candidate' / state['candidate_run_id']
        runs.append(candidate)
        assert run_metrics(candidate)['model_calls'] == maximum
        assert Path(state['diagnostics'], 'selected.json').is_file()
        if verdict == 'unsupported':
            repair = read(candidate / 'repair-state.json')
            assert repair['budget_fallback_reason'] == 'insufficient_budget_for_repair_and_verification'
    else:
        assert source_metrics['model_calls'] == 0
    provider_before = events.read_bytes() if events.exists() else b''
    providers = [json.loads(line)['fixture_provider'] for line in provider_before.decode('utf-8').splitlines()]
    assert providers == (['generation', 'generation', 'verification'] if maximum else [])
    metrics_before = [run_metrics(run) for run in runs]
    reports_before = {str(run): hashlib.sha256((run / 'report.md').read_bytes()).hexdigest()
                      for run in runs if (run / 'report.md').exists()}
    command(['--resume', str(directory)], 'resume')
    resumed = read(directories[0])
    assert resumed['source_run_id'] == state['source_run_id'] and resumed['candidate_run_id'] == state['candidate_run_id']
    assert resumed['status'] == status
    assert (events.read_bytes() if events.exists() else b'') == provider_before
    assert [run_metrics(run) for run in runs] == metrics_before
    assert {str(run): hashlib.sha256((run / 'report.md').read_bytes()).hexdigest()
            for run in runs if (run / 'report.md').exists()} == reports_before
    if maximum:
        assert resumed == state
