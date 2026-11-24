import json
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from researchdesk.ledger import Ledger
from researchdesk.final_body_gate import policy_spec,bind_policy,verify_completed,FinalBodyRejected
from researchdesk.draft_inspection import policy_spec as draft_spec
from researchdesk.tools import ResearchTools
from researchdesk.run_metrics import run_metrics
from researchdesk.storage import RecoveryError


@pytest.fixture
def sample(tmp_path):
    ledger=Ledger(tmp_path/'run')
    (ledger.directory/'run.json').write_text(json.dumps({'status':'running','final_body_policy':policy_spec(20),'draft_inspection_policy':draft_spec(20)}),encoding='utf-8')
    bind_policy(ledger)
    source=ledger.add_source('Fixture','fixture://final','系统支持日志。','fixture')
    evidence=ledger.add_evidence('日志',source['id'],'系统支持日志。')
    yield ledger,evidence
    ledger.store.close()


def complete(ledger):
    path=ledger.directory/'run.json'
    value=json.loads(path.read_text(encoding='utf-8'))
    value['status']='completed'
    path.write_text(json.dumps(value),encoding='utf-8')


def test_changed_final_body_is_checked_without_extra_tool_calls(sample):
    ledger,evidence=sample
    runtime=ResearchTools(ledger,'')
    runtime.executor.execute('inspect_report',{'report':f"日志。[{evidence['id']}]"})
    body=f"系统支持日志。[{evidence['id']}]"
    ledger.save_report(body)
    receipt=json.loads((ledger.directory/'final-body-check.json').read_text(encoding='utf-8'))
    assert receipt['status']=='passed' and receipt['matches_last_preflight'] is False
    assert receipt['semantic_support']=='not_assessed' and receipt['publication']=='not_claimed'
    assert ledger.store.counters()=={'calls':1,'requests':0}
    complete(ledger)
    verify_completed(ledger.directory)


@pytest.mark.parametrize('body',['长'*21,'事实。[E0000000000000000]','```\n事实\n```','事实。[E1]'])
def test_invalid_final_retains_candidate_and_receipt_without_publishing(sample,body):
    ledger,_=sample
    with pytest.raises(FinalBodyRejected): ledger.save_report(body)
    assert (ledger.directory/'draft.md').exists() and not (ledger.directory/'report.md').exists()
    receipt=json.loads((ledger.directory/'final-body-check.json').read_text(encoding='utf-8'))
    assert receipt['status']=='failed' and receipt['api_requests']==0
    assert ledger.store.counters()=={'calls':0,'requests':0}


@pytest.mark.parametrize('change',['policy','draft','receipt','published_body'])
def test_completed_shortcut_rejects_changed_binding_or_body(sample,change):
    ledger,evidence=sample
    ledger.save_report(f"日志。[{evidence['id']}]")
    complete(ledger)
    verify_completed(ledger.directory)
    if change=='policy':
        path=ledger.directory/'run.json'
        value=json.loads(path.read_text(encoding='utf-8'))
        value['final_body_policy']=policy_spec(21)
        path.write_text(json.dumps(value),encoding='utf-8')
    elif change=='draft': (ledger.directory/'draft.md').write_text('changed',encoding='utf-8')
    elif change=='published_body':
        path=ledger.directory/'report.md'
        path.write_text('changed'+path.read_text(encoding='utf-8'),encoding='utf-8')
    else:
        path=ledger.directory/'final-body-check.json'
        value=json.loads(path.read_text(encoding='utf-8'))
        value['matches_last_preflight']=True
        path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises((RecoveryError,ValueError)): verify_completed(ledger.directory)


def test_legacy_unlimited_save_unchanged_and_late_enable_rejected(tmp_path):
    ledger=Ledger(tmp_path/'legacy')
    try:
        source=ledger.add_source('Fixture','fixture://legacy','事实','fixture')
        evidence=ledger.add_evidence('事实',source['id'],'事实')
        ledger.save_report('长'*300+f"[{evidence['id']}]")
        assert not (ledger.directory/'final-body-check.json').exists()
        ResearchTools(ledger,'').executor.execute('read_source',{'source_id':source['id']})
        (ledger.directory/'run.json').write_text(json.dumps({'final_body_policy':policy_spec(200)}),encoding='utf-8')
        with pytest.raises(RecoveryError): bind_policy(ledger)
    finally: ledger.store.close()


@pytest.mark.parametrize('args',[
    ['研究','--final-body-limit','0'],['--demo','--final-body-limit','200'],
    ['--resume','f'*32,'--final-body-limit','200']])
def test_invalid_cli_options_rejected_before_credentials(monkeypatch,args):
    from researchdesk import cli
    monkeypatch.setattr(cli,'load_dotenv',lambda:pytest.fail('No credentials'))
    with pytest.raises(SystemExit) as error: cli.main(args)
    assert error.value.code==2


def test_publication_failure_resumes_finished_real_checkpoint_without_new_model_call(tmp_path,monkeypatch):
    from researchdesk import cli,agent
    from researchdesk import ledger as ledger_module
    output=tmp_path/'runs'
    document=tmp_path/'source.md'
    document.write_text('系统支持日志。',encoding='utf-8')
    monkeypatch.setattr(cli,'load_dotenv',lambda:None)
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture')
    original=agent.build_agent
    builds=[]
    class Model(GenericFakeChatModel):
        def bind_tools(self,tools,**kwargs): return self
    def builder(ledger,**kwargs):
        source=next(iter(ledger.sources.values()))
        evidence=ledger.add_evidence('日志',source['id'],'系统支持日志。')
        model=Model(messages=iter([AIMessage(content=f"系统支持日志。[{evidence['id']}]")] if not builds else []))
        builds.append(1)
        monkeypatch.setattr(agent,'ChatDeepSeek',lambda **k:model)
        return original(ledger,**kwargs)
    monkeypatch.setattr(agent,'build_agent',builder)
    write=ledger_module.atomic_text
    failures=[]
    def interrupt(path,text):
        if path.name=='report.md' and not failures:
            failures.append(1)
            raise OSError('Injected publication fault')
        return write(path,text)
    monkeypatch.setattr(ledger_module,'atomic_text',interrupt)
    assert cli.main(['研究','--document',str(document),'--local-only','--final-body-limit','200','--max-model-calls','1','--output',str(output)])==1
    run=next(output.iterdir())
    assert not (run/'report.md').exists()
    receipt=json.loads((run/'final-body-check.json').read_text(encoding='utf-8'))
    assert receipt['status']=='passed' and receipt['publication']=='not_claimed'
    assert run_metrics(run)['model_calls']==1
    assert cli.main(['--resume',run.name,'--output',str(output)])==0
    assert run_metrics(run)['model_calls']==1
    verify_completed(run)
    monkeypatch.setattr(agent,'build_agent',lambda *a,**k:pytest.fail('Completed shortcut must not build graph'))
    assert cli.main(['--resume',run.name,'--output',str(output)])==0
