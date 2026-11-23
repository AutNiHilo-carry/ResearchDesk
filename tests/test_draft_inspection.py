import copy
import json
from pathlib import Path
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from researchdesk.ledger import Ledger
from researchdesk.policy import ToolLimits
from researchdesk.tools import ResearchTools,build_tools
from researchdesk.draft_inspection import bind_policy,policy_spec,inspect,verify_completed_policy
from researchdesk.storage import RecoveryError
from researchdesk.verification import VerificationError


@pytest.fixture
def sample(tmp_path):
    ledger=Ledger(tmp_path/'run')
    (ledger.directory/'run.json').write_text(json.dumps({'draft_inspection_policy':policy_spec(20)}),encoding='utf-8')
    source=ledger.add_source('Fixture','fixture://draft','系统支持日志。','fixture')
    evidence=ledger.add_evidence('日志',source['id'],'系统支持日志。')
    yield ledger,source,evidence
    ledger.store.close()


def test_preflight_counts_complete_body_and_shows_quotes_without_semantic_acceptance(sample):
    ledger,source,evidence=sample
    report=f"系统支持日志和自动恢复。[{evidence['id']}]\n资料不足。"
    result=inspect(report,ledger.evidence,ledger.sources,5)
    assert result['body_constraints']['status']=='failed'
    assert result['citation_structure']=='passed' and result['semantic_support']=='not_assessed'
    assert result['units'][0]['text'].startswith('系统支持日志和自动恢复')
    assert result['evidence'][0]['snippet']=='系统支持日志。'
    assert result['report_saved'] is False and not (ledger.directory/'draft.md').exists()


def test_unknown_reference_and_tampered_source_do_not_look_valid(sample):
    ledger,source,evidence=sample
    result=inspect('事实。[E0000000000000000]',ledger.evidence,ledger.sources,200)
    assert result['citation_structure']=='failed'
    sources=copy.deepcopy(ledger.sources)
    sources[source['id']]['content']+='伪造'
    result=inspect(f"事实。[{evidence['id']}]",ledger.evidence,sources,200)
    assert result['evidence'][0]['integrity']=='invalid'


def test_uncited_mixed_sentence_retained_and_fences_refused(sample):
    ledger,_,evidence=sample
    text='保存以端点开始执行为界；但资料不足。'
    result=inspect(text,ledger.evidence,ledger.sources,200)
    assert result['units'][0]['text']==text and result['citation_structure']=='failed'
    assert 'factual' not in result['units'][0]
    with pytest.raises(VerificationError): inspect('```\n隐藏\n```',ledger.evidence,ledger.sources,200)


def test_tool_uses_call_budget_and_replays_receipt_without_provider_calls(sample):
    ledger,_,evidence=sample
    runtime=ResearchTools(ledger,'',limits=ToolLimits(max_calls=1,max_provider_requests=0))
    report=f"事实。[{evidence['id']}]"
    result=runtime.executor.execute('inspect_report',{'report':report},operation_id='one')
    assert result['body_constraints']['policy']['max_chars']==20
    assert runtime.executor.execute('inspect_report',{'report':report},operation_id='one')==result
    assert runtime.executor.execute('inspect_report',{'report':report},operation_id='two')['error_code']=='call_budget_exceeded'
    assert ledger.store.counters()=={'calls':1,'requests':0}


@pytest.mark.parametrize('change',['remove','limit','hint'])
def test_persisted_policy_cannot_be_removed_or_replaced(sample,change):
    ledger,_,_=sample
    bind_policy(ledger)
    manifest=json.loads((ledger.directory/'run.json').read_text())
    if change=='remove': manifest.pop('draft_inspection_policy')
    elif change=='limit': manifest['draft_inspection_policy']=policy_spec(21)
    else: manifest['draft_inspection_policy']['hint_sha256']='0'*64
    (ledger.directory/'run.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises((RecoveryError,ValueError)): bind_policy(ledger)
    with pytest.raises((RecoveryError,ValueError)): verify_completed_policy(ledger.directory)


def test_legacy_default_unchanged_and_no_late_enable(tmp_path):
    ledger=Ledger(tmp_path/'legacy')
    try:
        assert len(build_tools(ledger,''))==3
        runtime=ResearchTools(ledger,'')
        runtime.executor.execute('read_source',{'source_id':'S0000000000000000'})
        (ledger.directory/'run.json').write_text(json.dumps({'draft_inspection_policy':policy_spec(200)}),encoding='utf-8')
        with pytest.raises(RecoveryError): ResearchTools(ledger,'')
    finally: ledger.store.close()


def test_real_graph_can_inspect_then_return_report(sample,monkeypatch):
    from researchdesk import agent
    ledger,_,evidence=sample
    report=f"日志。[{evidence['id']}]"
    class Model(GenericFakeChatModel):
        def bind_tools(self,tools,**kwargs):
            assert 'task' not in {tool.name for tool in tools}
            assert 'inspect_report' in {tool.name for tool in tools}
            return self
    model=Model(messages=iter([AIMessage(content='',tool_calls=[dict(name='inspect_report',args={'report':report},id='inspect',type='tool_call')]),AIMessage(content=report)]))
    monkeypatch.setattr(agent,'ChatDeepSeek',lambda **kwargs:model)
    graph=agent.build_agent(ledger,ToolLimits(max_provider_requests=0),model_name='fixture')
    result=graph.invoke({'messages':[{'role':'user','content':'给出报告'}]})
    assert result['messages'][-1].content==report
    assert ledger.store.counters()['calls']==1
    assert not (ledger.directory/'draft.md').exists()


def test_cli_local_scope_persists_opt_in_and_completed_resume_checks_policy(tmp_path,monkeypatch):
    from researchdesk import cli,recovery
    document=tmp_path/'doc.md'
    document.write_text('系统支持日志。',encoding='utf-8')
    output=tmp_path/'runs'
    def graph(ledger,initial,limits,model_name,*,allowed_tools):
        assert set(allowed_tools)=={'read_source','record_evidence','inspect_report'}
        runtime=ResearchTools(ledger,'',limits=limits,allowed_tools=allowed_tools)
        source=next(iter(ledger.sources.values()))
        item=ledger.add_evidence('日志',source['id'],'系统支持日志。')
        report=f"系统支持日志。[{item['id']}]"
        assert runtime.executor.execute('inspect_report',{'report':report})['body_constraints']['passed']
        return {'messages':[AIMessage(content=report)]}
    monkeypatch.setattr(recovery,'run_graph',graph)
    monkeypatch.setattr(cli,'load_dotenv',lambda:None)
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture')
    assert cli.main(['研究','--document',str(document),'--local-only','--draft-inspection-limit','200','--output',str(output)])==0
    run=next(output.iterdir())
    assert cli.main(['--resume',run.name,'--output',str(output)])==0
    manifest=json.loads((run/'run.json').read_text(encoding='utf-8'))
    manifest['draft_inspection_policy']=policy_spec(201)
    (run/'run.json').write_text(json.dumps(manifest),encoding='utf-8')
    assert cli.main(['--resume',run.name,'--output',str(output)])==1


@pytest.mark.parametrize('args',[
    ['研究','--draft-inspection-limit','0'],
    ['研究','--draft-inspection-limit','100001'],
    ['--demo','--draft-inspection-limit','200'],
    ['--resume','f'*32,'--draft-inspection-limit','200'],
])
def test_invalid_or_replacement_cli_options_stop_before_credentials(args,monkeypatch):
    from researchdesk import cli
    monkeypatch.setattr(cli,'load_dotenv',lambda:pytest.fail('Refuse before credentials'))
    with pytest.raises(SystemExit) as error: cli.main(args)
    assert error.value.code==2


def test_model_cannot_override_frozen_length_limit(sample):
    ledger,_,_=sample
    runtime=ResearchTools(ledger,'')
    result=runtime.executor.execute('inspect_report',{'report':'文本','max_chars':100000})
    assert 'error_code' in result
    assert ledger.store.counters()=={'calls':1,'requests':0}  # Rejected attempts retain the existing call-budget semantics.
