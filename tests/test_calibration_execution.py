import json
from pathlib import Path
import pytest
from researchdesk import inference_calibration as module
from researchdesk.verification import VerificationError


@pytest.fixture
def planned(tmp_path):
    data=json.loads((Path(__file__).parents[1]/'evals/inference-calibration-v1.json').read_text(encoding='utf-8'))
    payload,refs=module.compile_cases(data)
    bundle=tmp_path/'bundle'
    module.make_bundle(payload,refs,bundle,origin='offline_test')
    plan=tmp_path/'plan'
    module.prepare_execution(bundle,plan,model='fixture')
    return plan,bundle,payload,refs


def test_offline_plan_has_no_model_side_effect_and_pins_one_request(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ModelJudge',lambda *a:pytest.fail('Must not construct a model during preparation'))
    data=json.loads((Path(__file__).parents[1]/'evals/inference-calibration-v1.json').read_text(encoding='utf-8'))
    payload,refs=module.compile_cases(data)
    bundle=tmp_path/'bundle'
    module.make_bundle(payload,refs,bundle,origin='offline_test')
    plan=tmp_path/'plan'
    request=module.prepare_execution(bundle,plan,model='fixture')
    assert request==module.verify_execution(plan) and request['max_sdk_requests']==1
    assert request['settings']['max_retries']==0 and not (plan/'execution').exists()
    assert (plan/'README.md').is_file() and (plan/'binding.json').is_file()


def test_exactly_one_batch_payload_without_reference_labels_and_unknown_quality(planned):
    plan,bundle,payload,refs=planned
    calls=[]
    class Judge:
        usage={'total_tokens':5}
        raw_content='scripted response'
        def __call__(self,sent):
            calls.append(sent)
            assert sent==payload
            assert 'reference' not in json.dumps(sent) and 'reason_review_question' not in json.dumps(sent)
            state=json.loads((plan/'execution/request-state.json').read_text())
            assert state['status']=='request_started' and state['sdk_attempts']==1
            return module.reference_output(payload,refs)
    result=module.execute_batch(plan,judge=Judge())
    assert len(calls)==1 and result['execution_mode']=='injected_offline' and result['api_requests']==0
    assert result['overall_quality'] is None and result['human_review']=='pending'
    assert result['candidate_case_agreement']==1
    state=json.loads((plan/'execution/request-state.json').read_text())
    assert state['status']=='complete' and state['usage']['total_tokens']==5
    assert (plan/'execution/raw-response.json').is_file() and (plan/'execution/summary.md').is_file()
    with pytest.raises(FileExistsError): module.execute_batch(plan,judge=Judge())
    assert len(calls)==1


@pytest.mark.parametrize('field,value',[('model','replacement'),('max_sdk_requests',2),('prompt_sha256','changed')])
def test_request_replacements_are_rejected_before_sdk_or_output(planned,field,value):
    plan,_,_,_=planned
    path=plan/'request.json'
    request=json.loads(path.read_text())
    request[field]=value
    path.write_text(json.dumps(request))
    with pytest.raises(VerificationError): module.execute_batch(plan,judge=lambda _:pytest.fail('No model allowed'))
    assert not (plan/'execution').exists()


def test_reference_drift_fails_before_request(planned):
    plan,bundle,_,_=planned
    path=bundle/'references.json'
    ref=json.loads(path.read_text())
    ref['version_same']['scope']='uncertain'
    path.write_text(json.dumps(ref))
    with pytest.raises(VerificationError): module.execute_batch(plan,judge=lambda _:pytest.fail('No model allowed'))
    assert not (plan/'execution').exists()


def test_reference_changes_during_model_call_preserve_raw_and_reject_acceptance(planned):
    plan,bundle,payload,refs=planned
    def judge(sent):
        path=bundle/'references.json'
        changed=json.loads(path.read_text())
        changed['version_same']['scope']='uncertain'
        path.write_text(json.dumps(changed))
        return module.reference_output(payload,refs)
    with pytest.raises(VerificationError): module.execute_batch(plan,judge=judge)
    assert (plan/'execution/unaccepted-output.json').is_file()
    assert not (plan/'execution/result.json').exists()
    assert json.loads((plan/'execution/request-state.json').read_text())['status']=='failed'


@pytest.mark.parametrize('error',[TimeoutError,KeyboardInterrupt])
def test_failed_or_interrupted_request_cannot_be_retried_and_usage_stays_unknown(planned,error):
    plan,_,_,_=planned
    calls=[]
    def stop(payload):
        calls.append(1)
        raise error()
    with pytest.raises(error): module.execute_batch(plan,judge=stop)
    state=json.loads((plan/'execution/request-state.json').read_text())
    assert state['sdk_attempts']==1 and state['usage'] is None
    assert state['status']==('interrupted' if error is KeyboardInterrupt else 'failed')
    with pytest.raises(FileExistsError): module.execute_batch(plan,judge=stop)
    assert len(calls)==1 and not (plan/'execution/result.json').exists()


def test_omitted_case_is_retained_as_invalid_output_without_retries(planned):
    plan,_,payload,refs=planned
    raw=module.reference_output(payload,refs)
    raw['units'].pop()
    with pytest.raises(VerificationError): module.execute_batch(plan,judge=lambda _:raw)
    assert json.loads((plan/'execution/unaccepted-output.json').read_text())==raw
    assert not (plan/'execution/result.json').exists()
    with pytest.raises(FileExistsError): module.execute_batch(plan,judge=lambda _:pytest.fail('No paid retry'))
