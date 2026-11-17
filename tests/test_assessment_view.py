import copy
import hashlib
import json
import sys

import pytest

from researchdesk import task_eval


def row():
    return dict(task_id='case', generation_status='completed', success=True,
                rubric={'rubric_passed': True}, quality={'citation_precision': 1, 'evidence_coverage': 1})


@pytest.mark.parametrize('status,change,scoring,gate', [
    ('complete', {}, 'valid', 'passed'),
    ('complete', {'success': False}, 'valid', 'failed'),
    ('scoring_failed', {}, 'invalid', 'unknown'),
    ('complete', {'generation_status': 'timed_out', 'success': False}, 'not_scored', 'unknown'),
    ('complete', {'success': 1}, 'invalid', 'unknown'),
    ('complete', {'quality': None}, 'invalid', 'unknown'),
    ('running', {}, 'pending', 'unknown'),
])
def test_model_scoring_never_confers_human_acceptance(status, change, scoring, gate):
    record = dict(row(), **change)
    assert task_eval.assessment(record, status) == dict(scoring_status=scoring, model_gate=gate,
                                                       human_review='pending', final_acceptance='pending')


def test_legacy_summary_ignores_stale_assessment_and_keeps_original_scores():
    record = row()
    record['assessment'] = {'model_gate': 'passed', 'final_acceptance': 'approved'}
    result = dict(status='scoring_failed', tasks=[record], task_success_rate=None)
    before = copy.deepcopy(result)
    text = task_eval.summary(result)
    assert '| invalid | unknown |' in text
    assert 'Final acceptance: pending' in text and 'approved' not in text
    assert result == before


def test_offline_cli_does_not_load_credentials_or_rewrite_source(tmp_path, monkeypatch):
    source = tmp_path / 'old-result.json'
    source.write_text(json.dumps(dict(status='complete', tasks=[row()], task_success_rate=1)), encoding='utf-8')
    before = source.read_bytes()
    monkeypatch.setattr(task_eval, 'load_dotenv', lambda: pytest.fail('No credentials during rendering'))
    monkeypatch.setattr(task_eval, 'evaluate_batch', lambda *a, **k: pytest.fail('No grading during rendering'))
    monkeypatch.setattr(sys, 'argv', ['task_eval', '--summarize', str(source), '--output', str(tmp_path / 'views')])
    assert task_eval.main() == 0
    assert source.read_bytes() == before
    folder = next((tmp_path / 'views').iterdir())
    manifest = json.loads((folder / 'provenance.json').read_text(encoding='utf-8'))
    assert manifest['source_sha256'] == hashlib.sha256(before).hexdigest()
    assert manifest['scoring_recomputed'] is False and manifest['api_requests'] == 0
    assert '| valid | passed |' in (folder / 'summary.md').read_text(encoding='utf-8')
    assert 'Final acceptance: pending' in (folder / 'summary.md').read_text(encoding='utf-8')
    with pytest.raises(FileExistsError):
        task_eval.render_summary(source, folder)


def test_bad_result_does_not_create_a_valid_looking_summary(tmp_path):
    source = tmp_path / 'bad.json'
    source.write_text('{}', encoding='utf-8')
    with pytest.raises(KeyError):
        task_eval.render_summary(source, tmp_path / 'view')
    assert not (tmp_path / 'view').exists()
