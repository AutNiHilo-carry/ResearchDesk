"""Zero-provider checks of recorded stability trials; injected labels are not human references."""
import json
import sys
from pathlib import Path
import pytest
from researchdesk import judge_stability as stability
from researchdesk.verification import VerificationError, SYSTEM_PROMPT_V2, digest
from test_judge_stability_inputs import completed_batch


def recorded(completed_batch, tmp_path, *, factual=True):
    evaluation, dataset, _, _ = completed_batch
    payload, controls = stability.frozen_input(evaluation, dataset)
    class Injected:
        prompt_sha256 = digest(SYSTEM_PROMPT_V2)
        prompt_version = 'report-quote-v2'
        usage = dict(input_tokens=0, output_tokens=0, total_tokens=0)
        def __call__(self, request):
            return dict(units=[dict(unit_id=r['unit_id'], factual=factual, reason='Injected test label',
                links=[dict(evidence_id=eid, verdict='supported' if factual else 'not_applicable', reason='Injected test label')
                       for eid in r['citations']]) for r in request['units']])
    source = tmp_path / 'recorded'
    stability.run_stability(payload, controls, source, Injected, repeats=2)
    return evaluation, dataset, source / 'result.json'


def test_offline_cli_check_does_not_load_credentials_or_call_model(completed_batch, tmp_path, monkeypatch):
    evaluation, dataset, source = recorded(completed_batch, tmp_path)
    before = {p: p.read_bytes() for root in (evaluation, source.parent) for p in root.rglob('*') if p.is_file()}
    def forbidden(*a, **k): pytest.fail('No credentials or new model calls')
    monkeypatch.setattr(stability, 'load_dotenv', forbidden)
    monkeypatch.setattr(stability, 'ModelJudge', forbidden)
    output = tmp_path / 'checks'
    monkeypatch.setattr(sys, 'argv', ['judge_stability', str(evaluation), '--dataset', str(dataset),
                                    '--check-result', str(source), '--output', str(output)])
    assert stability.main() == 0
    verdict = json.loads(next(output.glob('*/gate.json')).read_text(encoding='utf-8'))
    assert verdict['status'] == 'passed' and verdict['api_requests'] == 0
    assert verdict['human_review'] == verdict['final_acceptance'] == 'pending'
    folder = next(output.iterdir())
    context = json.loads((folder/'review-context.json').read_text(encoding='utf-8'))
    form = json.loads((folder/'review-template.json').read_text(encoding='utf-8'))
    audit = json.loads((folder/'audit.json').read_text(encoding='utf-8'))
    assert context['audit_content_sha256'] == verdict['audit_content_sha256'] == stability.payload_hash(audit)
    assert context['payload'] == json.loads(source.with_name('input.json').read_text(encoding='utf-8'))
    assert context['input_sha256'] == stability.payload_hash(context['payload'])
    assert form['context_sha256'] == stability.payload_hash(context)
    assert form['reviewer'] == form['reviewed_at'] == '' and form['status'] == 'pending'
    assert [r['unit_id'] for r in form['units']] == [r['unit_id'] for r in context['payload']['units']]
    assert all(r['factual'] is None and r['atomic_claims'] == [] and r['reason'] == '' for r in form['units'])
    assert 'factual' not in json.dumps(context)
    reading = (folder/'review.md').read_text(encoding='utf-8')
    assert all(r['text'] in reading for r in context['payload']['units'])
    assert all(r['question'] in reading for r in context['tasks'])
    assert 'Injected test label' not in reading
    assert {p: p.read_bytes() for root in (evaluation, source.parent) for p in root.rglob('*') if p.is_file()} == before


def test_new_trials_agreeing_with_each_other_can_still_require_review(completed_batch, tmp_path):
    evaluation, dataset, source = recorded(completed_batch, tmp_path, factual=False)
    assert json.loads(source.read_text(encoding='utf-8'))['factual_pairwise_agreement'] == 1
    gate = stability.check_stability_result(evaluation, dataset, source, tmp_path / 'view')
    assert gate['status'] == 'review_required' and gate['exit_code'] == 3
    assert 'historical_factuality_changed' in gate['reasons']
    form = json.loads((tmp_path/'view/review-template.json').read_text(encoding='utf-8'))
    assert all(r['factual'] is None and not r['reason'] for r in form['units'])


def test_review_reading_keeps_untrusted_backticks_inside_literal_blocks():
    payload = dict(units=[dict(task_id='case', unit_id='case:U1', text='```\n# Embedded heading', citations=[])], evidence=[])
    context, form, reading = stability.stability_review_materials(payload, {}, [dict(id='case', question='Question')])
    assert '````\n```\n# Embedded heading\n````' in reading
    assert context['payload'] == payload and form['units'][0]['factual'] is None


def test_interrupted_result_is_unknown_and_does_not_resume(completed_batch, tmp_path):
    evaluation, dataset, source = recorded(completed_batch, tmp_path)
    result = json.loads(source.read_text(encoding='utf-8'))
    payload = json.loads(source.with_name('input.json').read_text(encoding='utf-8'))
    for key in stability.summarize_trials(payload, result['trials']):
        result.pop(key, None)
    result.update(status='running', trials=result['trials'][:1])
    source.write_text(json.dumps(result), encoding='utf-8')
    gate = stability.check_stability_result(evaluation, dataset, source, tmp_path / 'view')
    assert gate['status'] == 'unknown' and gate['exit_code'] == 3
    assert len(json.loads(source.read_text(encoding='utf-8'))['trials']) == 1


@pytest.mark.parametrize('mutation', ['input', 'quality', 'summary', 'controls', 'trial_number', 'output_inside'])
def test_saved_check_rejects_changed_or_unbound_records(completed_batch, tmp_path, mutation):
    evaluation, dataset, source = recorded(completed_batch, tmp_path)
    result = json.loads(source.read_text(encoding='utf-8'))
    output = tmp_path / 'rejected'
    if mutation == 'input':
        p = source.with_name('input.json')
        payload = json.loads(p.read_text(encoding='utf-8'))
        payload['units'][0]['text'] = 'Changed text'
        p.write_text(json.dumps(payload), encoding='utf-8')
    elif mutation == 'quality':
        result['trials'][0]['quality']['metrics']['citation_precision'] = 0
    elif mutation == 'summary':
        result['factual_pairwise_agreement'] = 0
    elif mutation == 'controls':
        result['controls']['baseline_quality_sha256'] = '0' * 64
    elif mutation == 'trial_number':
        result['trials'][1]['number'] = 1
    else:
        output = source.parent / 'rejected'
    source.write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(VerificationError):
        stability.check_stability_result(evaluation, dataset, source, output)
    assert not output.exists()


def test_offline_check_rejects_replacement_repeat_count(completed_batch, tmp_path, monkeypatch):
    evaluation, dataset, source = recorded(completed_batch, tmp_path)
    monkeypatch.setattr(sys, 'argv', ['judge_stability', str(evaluation), '--dataset', str(dataset),
        '--check-result', str(source), '--repeats', '2', '--output', str(tmp_path / 'view')])
    with pytest.raises(SystemExit) as error:
        stability.main()
    assert error.value.code == 2
