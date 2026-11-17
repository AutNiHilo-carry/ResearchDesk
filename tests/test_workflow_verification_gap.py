"""Actual owned process kills at uncertain verification boundaries; no paid services."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from researchdesk.fault_eval import kill_at_boundary
from researchdesk.model_budget import budget_snapshot
from researchdesk.task_eval import run_command


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def budget(run):
    connection = sqlite3.connect((run / 'application.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    try:
        return dict(snapshot=budget_snapshot(connection), reservations=list(connection.execute(
            'SELECT call_id,role,state,usage FROM model_call_reservations ORDER BY call_id')))
    finally:
        connection.close()


@pytest.mark.parametrize('boundary', ['response', 'validated'])
def test_verification_kill_preserves_uncertainty_and_refuses_paid_replay(tmp_path, boundary):
    worker = Path(__file__).with_name('workflow_cli_worker.py')
    marker, events = tmp_path / 'boundary.json', tmp_path / 'providers.jsonl'
    document = tmp_path / 'source.md'
    document.write_text('Offline fixture; structural verification only.', encoding='utf-8')
    output = tmp_path / 'workflows'
    base = [sys.executable, '-I', str(worker), '--verdict', 'supported', '--events', str(events)]
    command = base + ['--boundary', boundary, '--marker', str(marker), '--', '--local-only', '--document',
        str(document), '--output', str(output), '--max-model-calls', '6', '--max-provider-requests', '0',
        '--max-tool-calls', '2', '--tool-retries', '0', '--', 'Explain the fixture.']
    killed = kill_at_boundary(command, tmp_path, marker, 60)
    assert killed['boundary_reached'] and killed['forced_termination'], killed
    assert read(marker)['boundary'] == boundary
    states = list(output.glob('*/workflow.json'))
    assert len(states) == 1
    workflow = states[0].parent
    state = read(states[0])
    assert state['phase'] == 'repair'
    source = workflow / 'research' / state['source_run_id']
    candidate = workflow / 'candidate' / state['candidate_run_id']
    before = budget(candidate)
    assert before['snapshot']['reserved'] == 3 and before['snapshot']['remaining'] == 3
    expected = {'completed': 2, 'reserved': 1} if boundary == 'response' else {'completed': 3}
    assert before['snapshot']['states'] == expected
    assert read(candidate / 'repair-state.json')['checks_started'] == ['original']
    assert not (candidate / 'revisions/original-quality.json').exists()
    providers = events.read_bytes()
    assert [json.loads(line)['fixture_provider'] for line in providers.decode().splitlines()] == [
        'generation', 'generation', 'verification']
    hashes = {str(run): hashlib.sha256((run / 'report.md').read_bytes()).hexdigest() for run in (source, candidate)}
    for attempt in range(2):
        resumed = run_command(base + ['--', '--resume', str(workflow)], tmp_path,
                              tmp_path / f'resume-{attempt}.log', 60)
        assert not resumed['timed_out'] and resumed['returncode'] == 3, resumed
        after = read(states[0])
        assert after['status'] == 'verification_outcome_uncertain' and after['phase'] == 'repair'
        assert after['source_run_id'] == state['source_run_id'] and after['candidate_run_id'] == state['candidate_run_id']
        assert after['human_review'] == 'pending'
        assert budget(candidate) == before  # Neither refund nor re-admission, even with three slots left.
        assert events.read_bytes() == providers
        assert not (candidate / 'revisions/original-quality.json').exists()
        assert {str(run): hashlib.sha256((run / 'report.md').read_bytes()).hexdigest()
                for run in (source, candidate)} == hashes
