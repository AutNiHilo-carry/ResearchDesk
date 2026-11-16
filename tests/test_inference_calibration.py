import copy
import json
from pathlib import Path
import pytest
from researchdesk import inference_calibration as module
from researchdesk.verification import VerificationError


def dataset():
    return json.loads((Path(__file__).parents[1]/'evals/inference-calibration-v1.json').read_text(encoding='utf-8'))


def bundle(tmp_path):
    payload, refs=module.compile_cases(dataset())
    directory=tmp_path/'bundle'
    module.make_bundle(payload,refs,directory,origin='scripted_test_not_real_model')
    return directory,payload,refs


def test_four_pairs_have_both_positive_and_negative_references_without_label_leak():
    data=dataset()
    payload,refs=module.compile_cases(data)
    assert len(payload['conclusions'])==8
    assert 'reference' not in json.dumps(payload) and 'reason_review_question' not in json.dumps(payload)
    result=module.bundle_check(payload,refs)
    by_id={row['unit_id']:row['assessment'] for row in result['units']}
    for pair in {row['pair_id'] for row in data['cases']}:
        ids=[row['id'] for row in data['cases'] if row['pair_id']==pair]
        assert len(ids)==2
        assert {by_id[key] for key in ids}=={'model_support_established','model_support_not_established'}


def test_perfect_candidate_labels_do_not_establish_rationale_or_overall_quality(tmp_path):
    directory,payload,refs=bundle(tmp_path)
    result=module.score(directory,module.reference_output(payload,refs))
    assert result['candidate_field_agreement']==result['candidate_case_agreement']==1
    assert result['overall_quality'] is None and result['rationale_review']=='pending'
    assert all(c['rationale_correct'] is None and c['labels_and_rationale_accepted'] is None for c in result['cases'])
    assert result['human_review']=='pending' and result['api_requests']==0 and result['affects_original_scores'] is False


def test_false_support_is_counted_without_overwriting_reference_or_original_output(tmp_path):
    directory,payload,refs=bundle(tmp_path)
    raw=module.reference_output(payload,refs)
    row=next(r for r in raw['units'] if r['unit_id']=='absence_product_negative')
    row.update(scope='consistent',inference='valid',strength='within_evidence')
    saved=copy.deepcopy(raw)
    result=module.score(directory,raw)
    assert result['false_support_count']==1 and result['candidate_case_agreement']==7/8
    assert raw==saved and result['overall_quality'] is None


@pytest.mark.parametrize('file', ['model-input.json','references.json','manifest.json'])
def test_changed_frozen_input_reference_or_prompt_is_rejected(tmp_path,file):
    directory,payload,refs=bundle(tmp_path)
    path=directory/file
    value=json.loads(path.read_text(encoding='utf-8'))
    if file=='manifest.json': value['prompt_sha256']='changed'
    elif file=='references.json': value['absence_product_negative']['strength']='within_evidence'
    else: value['conclusions'][0]['text']='Changed claim'
    path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(VerificationError): module.score(directory,module.reference_output(payload,refs))


def test_missing_judgment_is_not_scored_as_zero_or_success(tmp_path):
    directory,payload,refs=bundle(tmp_path)
    raw=module.reference_output(payload,refs)
    raw['units'].pop()
    with pytest.raises(VerificationError): module.score(directory,raw)


@pytest.mark.parametrize('change',['duplicate_case','invalid_quote','missing_reference','extra_reference_field'])
def test_invalid_dataset_rejected_before_any_artifact(change):
    data=dataset()
    if change=='duplicate_case': data['cases'][1]['id']=data['cases'][0]['id']
    elif change=='invalid_quote': data['cases'][0]['premises'][0]['quote']='Not in source'
    elif change=='missing_reference': data['cases'][0]['reference']['support']={}
    else: data['cases'][0]['reference']['gold_score']=1
    with pytest.raises(VerificationError): module.compile_cases(data)


def test_archived_real_failure_keeps_positive_labels_but_does_not_approve_reason(tmp_path):
    live=json.loads((Path(__file__).parents[1]/'evals/inference-live-regression-v1.json').read_text(encoding='utf-8'))
    directory=tmp_path/'archive'
    module.make_bundle(live['payload'],live['references'],directory,origin=live['origin'])
    result=module.score(directory,live['observed_output'])
    assert result['candidate_field_agreement']==result['candidate_case_agreement']==1
    assert result['overall_quality'] is None and result['rationale_review']=='pending'
    assert result['cases'][0]['reason']==live['observed_output']['units'][0]['reason']
    assert live['known_rationale_issue']['reason_review']=='pending_independent_review'


def test_no_network_is_needed_for_prepare_and_saved_output_scoring(tmp_path,monkeypatch):
    import socket
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('No network allowed'))
    directory,payload,refs=bundle(tmp_path)
    assert module.score(directory,module.reference_output(payload,refs))['api_requests']==0


def test_changed_scoring_logic_cannot_silently_rescore_frozen_bundle(tmp_path,monkeypatch):
    directory,payload,refs=bundle(tmp_path)
    monkeypatch.setattr(module,'logic_hashes',lambda:{'changed':'yes'})
    with pytest.raises(VerificationError): module.score(directory,module.reference_output(payload,refs))
