"""Synthetic submissions only; tests do not create independent human reference labels."""
import json
import sys
from pathlib import Path
import pytest
from researchdesk import judge_stability, stability_review
from test_judge_stability_inputs import completed_batch
from test_stability_saved_check import recorded


def fixture(completed_batch, tmp_path):
    evaluation, dataset, source = recorded(completed_batch, tmp_path)
    bundle = tmp_path/'bundle'
    judge_stability.check_stability_result(evaluation, dataset, source, bundle)
    form = json.loads((bundle/'review-template.json').read_text(encoding='utf-8'))
    form.update(reviewer='Synthetic test submitter; not real human review', reviewed_at='2026-11-22T12:00:00+08:00')
    for row in form['units']:
        row.update(factual=True, atomic_claims=['Synthetic test claim'], reason='Synthetic test opinion')
    review = tmp_path/'submitted.json'
    review.write_text(json.dumps(form), encoding='utf-8')
    return evaluation, dataset, source, bundle, review, form


def snapshot(*roots):
    return {p:p.read_bytes() for root in roots for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('unknown',[False,True])
def test_bound_review_cli_preserves_unknown_and_originals(completed_batch,tmp_path,monkeypatch,unknown):
    evaluation,dataset,source,bundle,review,form = fixture(completed_batch,tmp_path)
    if unknown:
        form['units'][0].update(factual=None, atomic_claims=[], reason='Not enough material to judge')
        review.write_text(json.dumps(form), encoding='utf-8')
    before=snapshot(evaluation,source.parent,bundle)
    import dotenv
    from researchdesk import verification
    def forbidden(*a,**k): pytest.fail('Offline import must not load credentials or construct a model')
    monkeypatch.setattr(dotenv,'load_dotenv',forbidden)
    monkeypatch.setattr(judge_stability,'load_dotenv',forbidden)
    monkeypatch.setattr(verification,'ModelJudge',forbidden)
    monkeypatch.setattr(judge_stability,'ModelJudge',forbidden)
    output=tmp_path/'accepted'
    monkeypatch.setattr(sys,'argv',['stability_review',str(evaluation),'--dataset',str(dataset),
        '--source',str(source),'--bundle',str(bundle),'--review',str(review),'--output',str(output)])
    assert stability_review.main() == (3 if unknown else 0)
    result=json.loads((output/'result.json').read_text(encoding='utf-8'))
    assert result['unknown_units']==int(unknown) and result['known_units']==len(form['units'])-int(unknown)
    assert result['units'][0]['factual'] is (None if unknown else True)
    assert result['identity']=='self_declared_not_authenticated'
    assert result['accuracy'] is None and result['promotion']=='none' and result['final_acceptance']=='pending'
    assert result['api_requests']==0 and snapshot(evaluation,source.parent,bundle)==before
    assert (output/'submitted-review.json').read_bytes()==review.read_bytes()


@pytest.mark.parametrize('mutation',['blank','timestamp','duplicate','missing','extra','numeric_bool',
    'reason','blank_claim','context','self_consistent_context','reading','source','false_completion','output_inside'])
def test_invalid_review_is_rejected_before_output(completed_batch,tmp_path,mutation):
    evaluation,dataset,source,bundle,review,form=fixture(completed_batch,tmp_path)
    output=tmp_path/'rejected'
    if mutation=='blank': form['reviewer']=' '
    elif mutation=='timestamp': form['reviewed_at']='2026-11-22T12:00:00'
    elif mutation=='duplicate': form['units'].append(form['units'][0])
    elif mutation=='missing': form['units'].pop()
    elif mutation=='extra': form['units'][0]['approved']=True
    elif mutation=='numeric_bool': form['units'][0]['factual']=1
    elif mutation=='reason': form['units'][0]['reason']=' '
    elif mutation=='blank_claim': form['units'][0]['atomic_claims']=[' ']
    elif mutation=='context': form['context_sha256']='0'*64
    elif mutation=='self_consistent_context':
        path=bundle/'review-context.json'
        changed=json.loads(path.read_text(encoding='utf-8'))
        changed['payload']['units'][0]['text']='Edited context'
        path.write_text(json.dumps(changed),encoding='utf-8')
        form['context_sha256']=judge_stability.payload_hash(changed)
    elif mutation=='reading': (bundle/'review.md').write_text('Edited instructions',encoding='utf-8')
    elif mutation=='source':
        path=source.with_name('input.json')
        payload=json.loads(path.read_text(encoding='utf-8'))
        payload['units'][0]['text']='Edited source'
        path.write_text(json.dumps(payload),encoding='utf-8')
    elif mutation=='false_completion':
        form.update(status='complete')
        form['units'][0]['factual']=None
    else: output=bundle/'rejected'
    review.write_text(json.dumps(form),encoding='utf-8')
    with pytest.raises(ValueError):
        stability_review.record_review(evaluation,dataset,source,bundle,review,output)
    assert not output.exists()


def test_import_never_overwrites_prior_opinions(completed_batch,tmp_path):
    evaluation,dataset,source,bundle,review,_=fixture(completed_batch,tmp_path)
    output=tmp_path/'accepted-opinion'
    stability_review.record_review(evaluation,dataset,source,bundle,review,output)
    before=snapshot(output)
    with pytest.raises(ValueError):
        stability_review.record_review(evaluation,dataset,source,bundle,review,output)
    assert snapshot(output)==before


def test_review_drift_before_write_is_rejected(completed_batch,tmp_path,monkeypatch):
    evaluation,dataset,source,bundle,review,_=fixture(completed_batch,tmp_path)
    original=stability_review.frozen_input
    def changed(*args):
        result=original(*args)
        review.write_text('{}',encoding='utf-8')
        return result
    monkeypatch.setattr(stability_review,'frozen_input',changed)
    output=tmp_path/'rejected'
    with pytest.raises(ValueError):
        stability_review.record_review(evaluation,dataset,source,bundle,review,output)
    assert not output.exists()
