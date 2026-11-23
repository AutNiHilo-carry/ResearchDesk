import copy
import json
import pytest
from researchdesk.ledger import Ledger
from researchdesk.context_experiment import (ARMS, CONTEXT_RULES, FACT_RULES, create_plan,
                                            prepare_plan, execute_plan, validate_plan)
from researchdesk.verification import SYSTEM_PROMPT_V2, VerificationError
from researchdesk.storage import RecoveryError


@pytest.fixture
def definitions(tmp_path):
    with_ledger = Ledger(tmp_path/'run')
    source = with_ledger.add_source('Fixture', 'fixture://18', '# Version 18\n系统支持日志。', 'fixture')
    evidence = with_ledger.add_evidence('日志', source['id'], '系统支持日志。')
    with_ledger.save_report(f"建议：系统支持日志。[{evidence['id']}]\n系统自动恢复；但资料不足。")
    (with_ledger.directory/'run.json').write_text('{"status":"completed"}', encoding='utf-8')
    yield [dict(name='sample', run=str(with_ledger.directory), body='selected')]
    with_ledger.store.close()


def test_factorial_varies_only_intended_prompt_and_payload_factors(definitions):
    plan = create_plan(definitions)
    a,b,c,d = plan['samples'][0]['arms']
    assert [(r['context'],r['fact_hint']) for r in (a,b,c,d)] == list(ARMS)
    assert a['prompt']==SYSTEM_PROMPT_V2 and b['prompt']==a['prompt']+FACT_RULES
    assert c['prompt']==a['prompt']+CONTEXT_RULES and d['prompt']==c['prompt']+FACT_RULES
    assert a['payload']==b['payload'] and c['payload']==d['payload']
    assert a['payload']['units']==c['payload']['units']
    assert plan['max_sdk_requests']==4 and plan['retries']==0


@pytest.mark.parametrize('field', ['prompt','payload','budget','code'])
def test_tampered_plan_rejected_before_credentials_or_execution(definitions,tmp_path,field):
    root=tmp_path/'plan'
    plan=prepare_plan(definitions,root)
    if field=='prompt': plan['samples'][0]['arms'][0]['prompt']+='hack'
    elif field=='payload': plan['samples'][0]['arms'][0]['payload']['units'][0]['text']='changed'
    elif field=='budget': plan['max_sdk_requests']=100
    else: plan['source_code_sha256']={}
    (root/'plan.json').write_text(json.dumps(plan),encoding='utf-8')
    with pytest.raises(RecoveryError):
        execute_plan(root, credentials=lambda: pytest.fail('No credentials before validation'))
    assert not (root/'execution').exists()


class InjectedJudge:
    usage={'total_tokens':7}
    last_output=None
    def __call__(self,payload):
        self.last_output={'units':[dict(unit_id=u['unit_id'], factual=True, reason='Fixture only', links=[
            dict(evidence_id=e,verdict='supported',reason='Injected only') for e in u['citations']]) for u in payload['units']]}
        return self.last_output


def test_completed_plan_is_one_shot_and_context_scope_is_explicit(definitions,tmp_path):
    root=tmp_path/'plan'
    prepare_plan(definitions,root)
    result=execute_plan(root,judge_factory=lambda *a:InjectedJudge())
    assert result['status']=='complete' and result['sdk_attempts']==4 and result['total_tokens']==28
    assert result['rows'][2]['quality']['metric_scope']=='candidate quote plus same-source body support'
    before=(root/'execution/result.json').read_bytes()
    with pytest.raises(FileExistsError):
        execute_plan(root,credentials=lambda:pytest.fail('No repeated credentials'))
    assert before==(root/'execution/result.json').read_bytes()


def test_invalid_response_kept_without_retry_and_unknown_usage_stays_unknown(definitions,tmp_path):
    root=tmp_path/'plan'
    prepare_plan(definitions,root)
    calls=[]
    class Bad(InjectedJudge):
        usage=None
        def __call__(self,payload):
            calls.append(1)
            self.last_output={'unexpected':'data'}
            return self.last_output
    result=execute_plan(root,judge_factory=lambda *a:Bad())
    assert len(calls)==4 and result['status']=='incomplete_scoring'
    assert result['known_returned_tokens']==0 and result['total_tokens'] is None
    assert all(r['status']=='failed' and r['raw_output']=={'unexpected':'data'} for r in result['rows'])


def test_source_drift_or_unknown_candidate_rejected(definitions,tmp_path):
    root=tmp_path/'plan'
    prepare_plan(definitions,root)
    (tmp_path/'run/draft.md').write_text('Changed body',encoding='utf-8')
    with pytest.raises(RecoveryError): validate_plan(root)
    definitions[0]['body']='candidate'
    with pytest.raises(FileNotFoundError): create_plan(definitions)


def test_no_plan_output_inside_original_run(definitions,tmp_path):
    with pytest.raises(RecoveryError): prepare_plan(definitions,tmp_path/'run/plan')
    assert not (tmp_path/'run/plan').exists()
