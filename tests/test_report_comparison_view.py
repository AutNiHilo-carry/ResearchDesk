"""Record-only contrast views retain uncertainty and do not claim body acceptance."""
import copy
import json
import sys
import pytest
from researchdesk import report_eval
from researchdesk.report_constraints import check_body, policy
from researchdesk.storage import RecoveryError


def task(key='case', *, body='短文', legacy=False, success=True):
    row = dict(task_id=key, generation_status='completed', success=success,
        rubric=dict(rubric_passed=True),
        quality=dict(invalid_links=0, citation_precision=1 if success else 0.5, evidence_coverage=1),
        generation_metrics=dict(provider_requests=0,total_tokens=10), constraints=check_body(body),
        application_assessment=dict(application_gate='passed'))
    if legacy:
        row.pop('constraints')
    return row


def arm(*rows, status='complete'):
    return dict(status=status,tasks=list(rows),constraint_policy=policy(),
        thresholds=dict(citation_precision=0.9,evidence_coverage=0.6),task_success_rate=1,
        generation_total_tokens=10,evaluator_total_tokens=3)


def contrast(left, right):
    return dict(status='complete',execution_mode='injected_offline',arms=dict(v1=left,v2=right),
                pairs=[dict(task_id='stale-cache',v1_application_gate='passed',v2_application_gate='passed')])


def test_model_pass_can_pair_with_application_failure_and_stale_badges_are_ignored():
    result = contrast(arm(task(body='长' * 201)),arm(task()))
    before = copy.deepcopy(result)
    pair = report_eval.paired_rows(result['arms'])[0]
    assert pair['v1_success'] is pair['v2_success'] is True
    assert pair['v1_application_gate'] == pair['v1_constraint_gate'] == 'failed'
    assert pair['v2_application_gate'] == pair['v2_constraint_gate'] == 'passed'
    assert pair['precision_delta'] == pair['coverage_delta'] == pair['token_delta'] == 0
    text = report_eval.comparison(result)
    assert '| v1 | 1 | failed | 0/1/0 |' in text
    assert '| v2 | 1 | passed | 1/0/0 |' in text
    assert 'stale-cache' not in text and '未重新绑定正文' in text
    assert result == before


def test_pass_fail_unknown_counts_preserve_denominator_and_missing_legacy_checks():
    view = report_eval.application_view(arm(task('ok'),task('long',body='长'*201),task('legacy',legacy=True)))
    assert view['counts'] == dict(passed=1,failed=1,unknown=1)
    assert view['status'] == 'failed'
    assert view['tasks']['legacy']['constraint_gate'] == 'not_checked'
    assert view['tasks']['legacy']['application_gate'] == 'unknown'
    unknown = report_eval.application_view(arm(task(legacy=True)))
    assert unknown['status'] == 'unknown' and unknown['counts']['passed'] == 0


def test_unknown_scores_and_invalid_empty_records_do_not_turn_into_application_pass():
    view = report_eval.application_view(arm(task(),status='scoring_failed'))
    assert view['status'] == 'unknown' and view['counts'] == dict(passed=0,failed=0,unknown=1)
    empty = report_eval.application_view(arm())
    assert empty['status'] == 'unknown' and empty['counts'] is None
    assert empty['validation'] == 'invalid_record'
    bad = task()
    bad['success'] = False  # Saved components still imply a model pass.
    assert report_eval.application_view(arm(bad))['validation'] == 'invalid_record'


def test_missing_counterparts_keep_both_arm_task_ids_and_do_not_invent_deltas():
    pairs = report_eval.paired_rows(dict(v1=arm(task('left')),v2=arm(task('right'))))
    assert [r['task_id'] for r in pairs] == ['left','right']
    assert pairs[0]['v2_success'] is pairs[1]['v1_success'] is None
    assert pairs[0]['v2_application_gate'] == pairs[1]['v1_application_gate'] == 'unknown'
    assert all(r['precision_delta'] is r['coverage_delta'] is r['token_delta'] is None for r in pairs)


def test_duplicate_task_ids_are_refused_instead_of_silently_collapsed():
    with pytest.raises(RecoveryError):
        report_eval.paired_rows(dict(v1=arm(task(),task()),v2=arm(task())))


def test_incomplete_experiment_can_render_without_inventing_the_missing_arm():
    result = dict(status='interrupted',execution_mode='injected_offline',arms=dict(v1=arm(task())))
    text = report_eval.comparison(result)
    assert 'Status: interrupted' in text and '| v1 |' in text
    assert '| v2 |' not in text


def test_offline_cli_is_read_only_and_does_not_load_credentials_or_prepare_paid_work(tmp_path, monkeypatch):
    result = contrast(arm(task(legacy=True)),arm(task(legacy=True)))
    source = tmp_path/'result.json'
    source.write_text(json.dumps(result),encoding='utf-8')
    before = source.read_bytes()
    def forbidden(*a,**k):
        pytest.fail('No model, credentials, or plan preparation during offline rendering')
    for name in ('load_dotenv','evaluate_batch','create_plan','execute_plan'):
        monkeypatch.setattr(report_eval,name,forbidden)
    monkeypatch.setattr(sys,'argv',['report_eval','--summarize-result',str(source),'--output',str(tmp_path/'views')])
    assert report_eval.main() == 0  # Formatting success, never an application acceptance code.
    output = next((tmp_path/'views').iterdir())
    text = (output/'summary.md').read_text(encoding='utf-8')
    provenance = json.loads((output/'provenance.json').read_text(encoding='utf-8'))
    assert '| v1 | 1 | unknown | 0/0/1 |' in text
    assert provenance['api_requests'] == 0 and provenance['scoring_recomputed'] is False
    assert provenance['scope'] == 'record_view_not_frozen_body_acceptance'
    assert source.read_bytes() == before
    with pytest.raises(FileExistsError):
        report_eval.render_comparison(source,output)


@pytest.mark.parametrize('extra', [['--model','replacement'], ['--task-id','case'], ['--execute-plan','plan']])
def test_offline_rendering_rejects_execution_and_generation_replacements(monkeypatch, extra):
    monkeypatch.setattr(sys,'argv',['report_eval','--summarize-result','result.json']+extra)
    with pytest.raises(SystemExit) as error:
        report_eval.main()
    assert error.value.code == 2
