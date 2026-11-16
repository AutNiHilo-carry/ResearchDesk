import json
from types import SimpleNamespace
from pathlib import Path
import sys
import httpx
import pytest
from researchdesk.ledger import Ledger
from researchdesk.tools import ResearchTools
from researchdesk.search_context import bind_policy,policy_spec,render_search_sources,PREVIEW_HINT
from researchdesk.storage import RecoveryError,atomic_text


def set_mode(ledger,mode):
    path=ledger.directory/'run.json'
    meta=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    meta['search_context_policy']=policy_spec(mode)
    atomic_text(path,json.dumps(meta))


@pytest.fixture
def provider(monkeypatch):
    requests=[]
    text='Search snippet. '+('large retained text '*900)
    body='Full body evidence. '+('body '*500)
    def handle(request):
        requests.append(request.url.path)
        if request.url.path=='/search':
            return httpx.Response(200,json={'results':[dict(title='Fixture',url='https://example.org/doc',content=text)]})
        return httpx.Response(200,json={'results':[dict(url='https://example.org/doc',raw_content=body)]})
    original=httpx.Client
    monkeypatch.setattr('researchdesk.tools.httpx.Client',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    return requests,text,body


def test_preview_keeps_full_saved_snippet_and_source_identity_with_durable_receipt(tmp_path,provider):
    requests,text,_=provider
    directory=tmp_path/'run'
    ledger=Ledger(directory)
    set_mode(ledger,'preview')
    runtime=ResearchTools(ledger,'fixture')
    first=runtime.executor.execute('search_sources',{'query':'Question'},operation_id='search-once')
    shown=first['sources'][0]
    saved=ledger.sources[shown['id']]
    assert 'content' not in shown and shown['preview']==text[:600]
    assert saved['content']==text[:12000] and shown['saved_snippet_chars']==12000 and shown['preview_truncated']
    assert shown['sha256']==saved['sha256'] and shown['location']==saved['location']
    assert first['source_text_kind']=='search_snippet_preview'
    ledger.store.close()
    restored=Ledger(directory,resume=True)
    try:
        again=ResearchTools(restored,'fixture').executor.execute('search_sources',{'query':'Question'},operation_id='search-once')
        assert again==first and requests==['/search']
        assert restored.sources[shown['id']]['content']==text[:12000]
        assert restored.store.counters()['requests']==1
    finally: restored.store.close()


def test_legacy_full_return_shape_and_snapshot_are_unchanged(tmp_path,provider):
    _,text,_=provider
    ledger=Ledger(tmp_path/'run')
    try:
        result=ResearchTools(ledger,'fixture').executor.execute('search_sources',{'query':'Question'})
        assert set(result)=={'sources'} and result['sources'][0]['content']==text[:12000]
        assert result['sources'][0]==ledger.sources[result['sources'][0]['id']]
        assert ledger.store.records('search_context_policy')==[]
        assert render_search_sources(result['sources'],policy_spec())==result
    finally: ledger.store.close()


def test_preview_does_not_shorten_extraction_or_change_evidence_validation(tmp_path,provider):
    requests,_,body=provider
    ledger=Ledger(tmp_path/'run')
    set_mode(ledger,'preview')
    try:
        runtime=ResearchTools(ledger,'fixture')
        row=runtime.executor.execute('search_sources',{'query':'Question'})['sources'][0]
        full=runtime.executor.execute('read_source',{'source_id':row['id']})
        assert full['content']==body and full['id']!=row['id']
        evidence=runtime.executor.execute('record_evidence',{'source_id':full['id'],'claim':'Fact','snippet':'Full body evidence.'})
        assert evidence['source_id']==full['id'] and requests==['/search','/extract']
        assert len(ledger.sources)==2 and ledger.sources[row['id']]['content'].startswith('Search snippet.')
    finally: ledger.store.close()


@pytest.mark.parametrize('change',['switch','remove','forge'])
def test_policy_changes_fail_before_receipt_replay_or_any_request(tmp_path,provider,change):
    requests,_,_=provider
    ledger=Ledger(tmp_path/'run')
    set_mode(ledger,'preview')
    runtime=ResearchTools(ledger,'fixture')
    runtime.executor.execute('search_sources',{'query':'Question'})
    path=ledger.directory/'run.json'
    meta=json.loads(path.read_text())
    if change=='switch': meta['search_context_policy']=policy_spec('full')
    elif change=='remove': del meta['search_context_policy']
    else: meta['search_context_policy']['instruction_sha256']='forged'
    atomic_text(path,json.dumps(meta))
    try:
        with pytest.raises(RecoveryError): ResearchTools(ledger,'fixture')
        assert requests==['/search']
    finally: ledger.store.close()


def test_legacy_tool_history_cannot_be_relabelled_as_preview(tmp_path,provider):
    ledger=Ledger(tmp_path/'run')
    try:
        ResearchTools(ledger,'fixture').executor.execute('search_sources',{'query':'Question'})
        set_mode(ledger,'preview')
        with pytest.raises(RecoveryError,match='historical'): bind_policy(ledger)
    finally: ledger.store.close()


def test_unknown_prior_model_reservation_also_blocks_relabelling(tmp_path):
    from researchdesk.model_budget import ModelCallBudget
    ledger=Ledger(tmp_path/'run')
    try:
        ModelCallBudget(ledger.store,1).admit('uncertain','generation')
        set_mode(ledger,'preview')
        with pytest.raises(RecoveryError,match='historical'): bind_policy(ledger)
    finally: ledger.store.close()


def test_actual_agent_prompt_explains_preview_and_default_prompt_stays_frozen(tmp_path,monkeypatch):
    from researchdesk import agent
    captured=[]
    monkeypatch.setattr(agent,'ChatDeepSeek',lambda **kwargs:SimpleNamespace(callbacks=[]))
    monkeypatch.setattr(agent,'create_deep_agent',lambda **kwargs:captured.append(kwargs['system_prompt']))
    full=Ledger(tmp_path/'full')
    preview=Ledger(tmp_path/'preview')
    try:
        agent.build_agent(full)
        set_mode(preview,'preview')
        agent.build_agent(preview)
        assert captured[0]==agent.PROMPT
        assert captured[1]==agent.PROMPT+'\n'+PREVIEW_HINT
    finally:
        full.store.close()
        preview.store.close()


def test_workflow_forwards_web_preview_and_rejects_full_source_manifest(tmp_path):
    from researchdesk.workflow import create_workflow,run_workflow,validate_manifest
    from researchdesk.model_budget import VERSION
    root=tmp_path/'flow'
    created=create_workflow(root,'Question',[],model='fixture',search_context='preview')
    calls=[]
    def research(args,**kwargs):
        calls.append(args)
        assert args[args.index('--search-context')+1]=='preview'
        raise KeyboardInterrupt()
    state=run_workflow(root,research_runner=research)
    assert state['status']=='interrupted' and len(calls)==1
    plan=created['plan']
    metadata=dict(question='Question',model='fixture',model_policy=dict(version=VERSION,max_calls=12),
        context_policy={'strategy':'full'},allowed_tools=None,tool_policy=plan['tool_policy'],
        search_context_policy=policy_spec('full'))
    with pytest.raises(RecoveryError,match='search context'): validate_manifest(metadata,plan)


@pytest.mark.parametrize('module_name',['cli','workflow'])
def test_resume_cannot_override_search_presentation(monkeypatch,tmp_path,module_name):
    from researchdesk import cli,workflow
    module=cli if module_name=='cli' else workflow
    monkeypatch.setattr(module,'load_dotenv',lambda:None)
    run='a'*32 if module_name=='cli' else str(tmp_path/'flow')
    monkeypatch.setattr(sys,'argv',['researchdesk','--resume',run,'--search-context','preview'])
    with pytest.raises(SystemExit) as error: module.main()
    assert error.value.code==2


def test_local_only_preview_rejected_without_creating_workflow(tmp_path):
    from researchdesk.workflow import create_workflow
    with pytest.raises(RecoveryError): create_workflow(tmp_path/'flow','Question',[],model='fixture',local_only=True,search_context='preview')
    assert not (tmp_path/'flow').exists()


def test_completed_shortcut_cannot_claim_preview_without_its_persisted_binding(tmp_path):
    from researchdesk.search_context import verify_completed_policy
    ledger=Ledger(tmp_path/'run')
    try:
        set_mode(ledger,'preview')
        with pytest.raises(RecoveryError,match='no persisted'): verify_completed_policy(ledger.directory)
        bind_policy(ledger)
        verify_completed_policy(ledger.directory)
        set_mode(ledger,'full')
        with pytest.raises(RecoveryError,match='differs'): verify_completed_policy(ledger.directory)
    finally: ledger.store.close()
