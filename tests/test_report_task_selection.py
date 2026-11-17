import copy
import json
from pathlib import Path
import sys

import pytest

from researchdesk import report_eval
from researchdesk.storage import RecoveryError
from researchdesk.task_eval import load_taskset

DATASET = Path(__file__).parents[1] / 'evals/technical-selection-v1.json'


def test_ids_freeze_requested_order_and_only_required_documents_without_api(tmp_path, monkeypatch):
    monkeypatch.setattr(report_eval, 'load_dotenv', lambda: pytest.fail('No credentials during prepare'))
    monkeypatch.setattr(report_eval, 'evaluate_batch', lambda *a, **k: pytest.fail('No API during prepare'))
    ids = ['version_scope', 'mvcc_write_contention']
    plan = report_eval.create_plan(DATASET, tmp_path / 'plan', model='fixture', task_ids=ids)
    data, tasks, documents = load_taskset(DATASET)
    assert plan['data'] == data
    assert plan['tasks'] == [next(t for t in tasks if t['id'] == key) for key in ids]
    names = {name for task in plan['tasks'] for name in task['documents']}
    assert plan['documents'] == {name: value for name, value in documents.items() if name in names}
    assert plan['controls']['max_sdk_requests'] == 28
    assert plan['controls'] == report_eval.controls(plan)
    assert not (tmp_path / 'plan/execution').exists()
    changed = copy.deepcopy(plan)
    changed['tasks'].reverse()
    assert report_eval.controls(changed)['input_sha256'] != plan['controls']['input_sha256']


@pytest.mark.parametrize('ids', [[], ['version_scope', 'version_scope'], ['unknown'], [''],
                               ['version_scope', 3], 'version_scope',
                               ['version_scope', 'wal_default_claim', 'local_low_write', 'performance_unknown']])
def test_bad_selection_is_rejected_without_creating_output(tmp_path, ids):
    with pytest.raises(RecoveryError):
        report_eval.create_plan(DATASET, tmp_path / 'plan', model='fixture', task_ids=ids)
    assert not (tmp_path / 'plan').exists()


def test_default_first_three_and_explicit_limit_are_unchanged(tmp_path):
    _, tasks, _ = load_taskset(DATASET)
    default = report_eval.create_plan(DATASET, tmp_path / 'default', model='fixture')
    limited = report_eval.create_plan(DATASET, tmp_path / 'limited', model='fixture', limit=1)
    assert default['tasks'] == tasks[:3] and limited['tasks'] == tasks[:1]
    assert default['controls']['max_sdk_requests'] == 40
    with pytest.raises(RecoveryError):
        report_eval.create_plan(DATASET, tmp_path / 'mixed', model='fixture', limit=1, task_ids=['version_scope'])


@pytest.mark.parametrize('extra', [['--limit', '1'], ['--execute-plan', 'old-plan']])
def test_cli_rejects_ambiguous_or_execution_time_selection_before_credentials(monkeypatch, extra):
    monkeypatch.setattr(report_eval, 'load_dotenv', lambda: pytest.fail('No credentials on invalid command'))
    monkeypatch.setattr(sys, 'argv', ['report_eval', '--task-id', 'version_scope', *extra])
    with pytest.raises(SystemExit) as error:
        report_eval.main()
    assert error.value.code == 2


def test_cli_prepares_later_task_and_does_not_load_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(report_eval, 'load_dotenv', lambda: pytest.fail('No credentials during prepare'))
    monkeypatch.setattr(report_eval, 'evaluate_batch', lambda *a, **k: pytest.fail('No API during prepare'))
    monkeypatch.setattr(sys, 'argv', ['report_eval', '--dataset', str(DATASET), '--output', str(tmp_path),
                                    '--model', 'fixture', '--task-id', 'external_effect_unknown'])
    assert report_eval.main() == 0
    path = next(tmp_path.glob('*/plan.json'))
    plan = json.loads(path.read_text(encoding='utf-8'))
    assert [t['id'] for t in plan['tasks']] == ['external_effect_unknown']
    assert plan['controls']['max_sdk_requests'] == 16
