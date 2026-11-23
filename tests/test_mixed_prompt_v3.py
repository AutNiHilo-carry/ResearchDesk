"""Candidate prompt wiring only. Injected results are not model quality evidence."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from researchdesk.verification import ModelJudge, SYSTEM_PROMPT, SYSTEM_PROMPT_V2, SYSTEM_PROMPT_V3, digest


@pytest.mark.parametrize('version,prompt',[('v1',SYSTEM_PROMPT),('v2',SYSTEM_PROMPT_V2),('v3',SYSTEM_PROMPT_V3)])
def test_versioned_prompt_and_no_retry_settings(monkeypatch,version,prompt):
    class Fake:
        def bind(self,**kwargs):
            assert kwargs=={'response_format':{'type':'json_object'}}
            return self
        def invoke(self,messages):
            assert messages[0]['content']==prompt
            return SimpleNamespace(content='{"units":[]}',response_metadata={'finish_reason':'stop'},usage_metadata={'total_tokens':1})
    def factory(**kwargs):
        assert kwargs['max_retries']==0 and kwargs['timeout']==90
        return Fake()
    monkeypatch.setattr('langchain_deepseek.ChatDeepSeek',factory)
    judge=ModelJudge('fixture',prompt_version=version)
    assert judge({})=={'units':[]}
    assert judge.prompt_version=='report-quote-'+version and judge.prompt_sha256==digest(prompt)
    assert digest(SYSTEM_PROMPT_V2)=='d2556557c2871b028b22f80e359cdaac84c045e6e929292cd70f5658eecfbbfd'


def test_v3_clause_mode_rejected_before_credentials(monkeypatch):
    from researchdesk import quality
    monkeypatch.setattr('sys.argv',['quality','verify','f'*32,'--granularity','clause','--judge-prompt','v3'])
    monkeypatch.setattr(quality,'load_dotenv',lambda:pytest.fail('Invalid combination must stop before credentials'))
    with pytest.raises(SystemExit) as error: quality.main()
    assert error.value.code==2


def test_explicit_v3_benchmark_cli_uses_candidate_without_changing_default(monkeypatch,tmp_path):
    from researchdesk import quality
    from researchdesk.evaluation import benchmark_inputs
    from test_factuality_mixed_regression import injected_reply
    dataset=Path(__file__).parents[1]/'evals/factuality-mixed-dev-v1.json'
    _,_,_,references=benchmark_inputs(json.loads(dataset.read_text(encoding='utf-8')))
    versions=[]
    class FakeJudge:
        usage={'total_tokens':1}
        def __init__(self,model,*,prompt_version='v1'):
            versions.append(prompt_version)
            self.prompt_version='report-quote-'+prompt_version
            self.prompt_sha256=digest(SYSTEM_PROMPT_V3)
        def __call__(self,payload): return injected_reply(payload,references)
    monkeypatch.setattr(quality,'ModelJudge',FakeJudge)
    monkeypatch.setattr(quality,'load_dotenv',lambda:None)
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture')
    monkeypatch.setattr('sys.argv',['quality','benchmark','--dataset',str(dataset),'--judge-prompt','v3','--output',str(tmp_path)])
    assert quality.main()==0 and versions==['v3']
    saved=list(tmp_path.rglob('result.json'))
    assert len(saved)==1
    result=json.loads(saved[0].read_text(encoding='utf-8'))
    assert result['prompt_version']=='report-quote-v3'
    assert result['benchmark']['label_status']=='agent_authored_candidate_references_not_human_gold'
