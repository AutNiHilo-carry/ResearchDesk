import copy
import json
from pathlib import Path
import sys
import pytest
from researchdesk import report_constraints as module
from researchdesk import task_eval
from researchdesk.verification import VerificationError
from test_task_eval import task_data, fixture_runner, RubricJudge, EvidenceJudge


@pytest.mark.parametrize('body,limit,expected,status', [
    ('中' * 200, 200, 200, 'passed'),
    ('中' * 201, 200, 201, 'failed'),
    ('a b\n中\t文\u3000', 4, 4, 'passed'),
    ('事实。[E0123456789abcdef]', 3, 3, 'passed'),
    ('事实。［E0123456789abcdef］', 3, 3, 'passed'),
    ('[1]甲', 4, 4, 'passed'),
    ('[Eunknown]甲', 4, 11, 'failed'),
    ('**甲**', 4, 5, 'failed'),
    ('```甲```', 4, 7, 'failed'),
    ('甲\u200b乙', 2, 3, 'failed'),
    ('e\u0301😀', 2, 3, 'failed'),
    (' \n\t', 2, 0, 'invalid'),
    ('[E0123456789abcdef]', 2, 0, 'invalid'),
    ('甲[Ebroken', 20, 9, 'invalid'),
])
def test_explicit_counting_and_invalid_inputs(body, limit, expected, status):
    original = body
    result = module.check_body(body, max_chars=limit)
    assert result['counted_chars'] == expected
    assert result['status'] == status and result['passed'] is (status == 'passed')
    assert result['api_calls'] == 0 and body == original


@pytest.mark.parametrize('limit', [True, 0, -1, 100001, 1.5, '200', None])
def test_limit_is_an_explicit_positive_integer(limit):
    with pytest.raises(ValueError):
        module.check_body('body', max_chars=limit)


def test_cli_is_offline_and_never_overwrites_outputs(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('No network'))
    body = tmp_path/'body.md'
    body.write_text('甲' * 201, encoding='utf-8')
    output = tmp_path/'result.json'
    monkeypatch.setattr(sys, 'argv', ['check', str(body), '--output', str(output)])
    assert module.main() == 3
    before = output.read_bytes()
    assert json.loads(before)['counted_chars'] == 201
    assert module.main() == 2
    assert output.read_bytes() == before


def completed_record(body):
    return dict(generation_status='completed', success=True, rubric={}, quality={}, constraints=module.check_body(body))


def test_model_pass_does_not_hide_constraint_failure_or_claim_human_acceptance():
    record = completed_record('甲' * 201)
    assert task_eval.assessment(record, 'complete')['model_gate'] == 'passed'
    app = task_eval.application_assessment(record, 'complete')
    assert app['constraint_gate'] == app['application_gate'] == 'failed'
    assert app['human_review'] == app['final_acceptance'] == 'pending'


@pytest.mark.parametrize('change', ['missing', 'wrong_policy', 'boolean_count', 'false_pass', 'missing_hash'])
def test_old_or_invalid_cache_cannot_be_promoted_to_application_pass(change):
    record = completed_record('短文')
    if change == 'missing':
        record.pop('constraints')
    elif change == 'wrong_policy':
        record['constraints']['policy']['version'] = 'unknown'
    elif change == 'boolean_count':
        record['constraints']['counted_chars'] = True
    elif change == 'false_pass':
        record['constraints']['counted_chars'] = 201
    else:
        record['constraints'].pop('body_sha256')
    app = task_eval.application_assessment(record, 'complete')
    assert app['application_gate'] != 'passed'
    assert app['constraint_gate'] == ('not_checked' if change == 'missing' else 'invalid')


def test_unknown_model_score_stays_unknown_even_if_body_passes():
    record = completed_record('短文')
    app = task_eval.application_assessment(record, 'scoring_failed')
    assert app['constraint_gate'] == 'passed' and app['application_gate'] == 'unknown'


def test_application_pass_requires_both_checks_and_failed_generation_never_passes():
    record = completed_record('短文')
    assert task_eval.application_assessment(record, 'complete')['application_gate'] == 'passed'
    record['success'] = False
    assert task_eval.application_assessment(record, 'complete')['application_gate'] == 'failed'
    record.update(success=True, generation_status='timed_out')
    assert task_eval.application_assessment(record, 'complete')['application_gate'] == 'failed'


def long_runner(task, paths, directory, timeout):
    record = fixture_runner(task, paths, directory, timeout)
    # Modify only the offline fixture output; body still has exactly one genuine citation.
    from researchdesk.ledger import Ledger
    ledger = Ledger(Path(record['run_directory']), resume=True)
    eid = next(iter(ledger.evidence))
    ledger.save_report('Fixture fact.' + '甲' * 201 + f'[{eid}]')
    ledger.store.close()
    return record


def test_new_batch_keeps_original_model_metric_and_records_separate_failed_application_gate(tmp_path):
    data, tasks, docs = task_data()
    result = task_eval.evaluate_batch(data, tasks[:1], docs, tmp_path/'eval', runner=long_runner,
        model_name='fixture', task_judge=RubricJudge(), evidence_judge=EvidenceJudge())
    assert result['task_success_rate'] == 1  # Existing model metric intentionally unchanged.
    row = result['tasks'][0]
    assert row['assessment']['model_gate'] == 'passed'
    assert row['application_assessment']['application_gate'] == 'failed'
    assert row['constraints']['counted_chars'] == 213
    text = task_eval.summary(result)
    assert '| passed | failed | failed |' in text and '213/200' in text
    assert '不含长度约束' in text
    task_eval.saved_quality(tmp_path/'eval', Path(__file__).parents[1]/'evals/research-tasks-v1.json', result)
    altered = copy.deepcopy(result)
    altered['tasks'][0]['constraints'] = module.check_body('短文')
    with pytest.raises(VerificationError, match='body constraint'):
        task_eval.saved_quality(tmp_path/'eval', Path(__file__).parents[1]/'evals/research-tasks-v1.json', altered)


def test_constraint_failure_survives_unknown_semantic_grading(tmp_path):
    data, tasks, docs = task_data()
    result = task_eval.evaluate_batch(data, tasks[:1], docs, tmp_path/'eval', runner=long_runner,
        model_name='fixture', task_judge=lambda _: {}, evidence_judge=EvidenceJudge())
    row = result['tasks'][0]
    assert result['status'] == 'scoring_failed' and row['success'] is None
    assert row['application_assessment']['constraint_gate'] == 'failed'
    assert row['application_assessment']['application_gate'] == 'failed'
