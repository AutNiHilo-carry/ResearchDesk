import json
from pathlib import Path
import pytest
from researchdesk import inference_check as checker, inference_calibration as calibration
from researchdesk.verification import VerificationError,digest


def data():
    return json.loads((Path(__file__).parents[1]/'evals/inference-contract-v2-cases.json').read_text(encoding='utf-8'))


def bundle(tmp_path,version='v2'):
    payload,refs=calibration.compile_cases(data())
    root=tmp_path/'bundle'
    calibration.make_bundle(payload,refs,root,origin='offline_contract_test',prompt_version=version)
    return root,payload,refs


def test_v1_prompt_is_byte_for_byte_frozen_and_still_default():
    assert digest(checker.PROMPT)=='f25bc5634e6090d08d7456ee2d5570d21f44a2341ef73d9b22676ae4bf682b16'
    assert checker.judge_prompt()==checker.PROMPT
    assert checker.judge_prompt('v1')==checker.PROMPT
    assert checker.judge_prompt('v2').startswith(checker.PROMPT+'\n')
    with pytest.raises(VerificationError): checker.judge_prompt('v3')


def test_default_bundle_and_execution_remain_v1(tmp_path):
    payload,refs=calibration.compile_cases(data())
    root=tmp_path/'bundle'
    meta=calibration.make_bundle(payload,refs,root,origin='offline_test')
    assert meta['judge_prompt_version']=='v1' and meta['prompt_sha256']==digest(checker.PROMPT)
    request=calibration.execution_spec(root,'fixture')
    assert request['judge_prompt_version']=='v1'


def test_opt_in_v2_is_bound_through_bundle_request_and_local_result(tmp_path):
    root,payload,refs=bundle(tmp_path)
    request=calibration.prepare_execution(root,tmp_path/'plan',model='fixture')
    assert request['judge_prompt_version']=='v2' and request['prompt_sha256']==digest(checker.judge_prompt('v2'))
    raw=calibration.reference_output(payload,refs)
    result=calibration.score(root,raw)
    assert result['judge_prompt_version']=='v2' and result['prompt_sha256']==request['prompt_sha256']
    assert result['overall_quality'] is None and result['human_review']=='pending'


def test_v2_version_cannot_be_silently_replaced_by_v1(tmp_path):
    root,_,_=bundle(tmp_path)
    path=root/'manifest.json'
    meta=json.loads(path.read_text())
    meta['judge_prompt_version']='v1'
    path.write_text(json.dumps(meta))
    with pytest.raises(VerificationError): calibration.prepare_execution(root,tmp_path/'plan',model='fixture')
    assert not (tmp_path/'plan').exists()


def test_real_dispatcher_selects_frozen_v2_before_any_network(tmp_path,monkeypatch):
    root,_,_=bundle(tmp_path)
    plan=tmp_path/'plan'
    calibration.prepare_execution(root,plan,model='fixture')
    captured=[]
    def factory(model,*,prompt_version):
        captured.append((model,prompt_version))
        raise RuntimeError('Test stops before network')
    monkeypatch.setattr(calibration,'ModelJudge',factory)
    with pytest.raises(RuntimeError): calibration.execute_batch(plan)
    assert captured==[('fixture','v2')]
    assert json.loads((plan/'execution/request-state.json').read_text())['sdk_attempts']==0


def test_adapter_sends_v2_system_prompt_not_v1(monkeypatch):
    import langchain_deepseek
    from types import SimpleNamespace
    captured=[]
    class Client:
        def __init__(self,**kwargs): assert kwargs['max_retries']==0
        def bind(self,**kwargs): return self
        def invoke(self,messages):
            captured.extend(messages)
            return SimpleNamespace(content='{"units":[]}',usage_metadata=None,response_metadata={})
    monkeypatch.setattr(langchain_deepseek,'ChatDeepSeek',Client)
    checker.ModelJudge('fixture',prompt_version='v2')({'conclusions':[]})
    assert captured[0]['content']==checker.judge_prompt('v2')


def test_new_candidate_cases_distinguish_comparison_overreach_and_metric_units():
    payload,refs=calibration.compile_cases(data())
    assert len(payload['conclusions'])==8 and 'reference' not in json.dumps(payload)
    # These are assertions about the declared candidate contract, not model accuracy.
    assert refs['Q03']['scope']=='consistent' and refs['Q03']['inference']=='valid'
    assert refs['Q06']['scope']=='consistent' and refs['Q06']['inference']=='invalid'
    assert refs['Q08']['scope']=='inconsistent' and refs['Q08']['inference']=='invalid'
    assert refs['Q02']['scope']=='inconsistent'


def test_same_raw_labels_have_unchanged_derived_rules_and_explicit_prompt_hash():
    payload,refs=calibration.compile_cases(data())
    raw=calibration.reference_output(payload,refs)
    v1=checker.assess(payload,raw)
    v2=checker.assess(payload,raw,prompt_version='v2')
    assert v1['units']==v2['units']
    assert v1['prompt_sha256']!=v2['prompt_sha256']
    assert v2['human_review']=='pending' and v2['affects_original_scores'] is False
