import copy
import json
import sys
from dataclasses import asdict
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from researchdesk.policy import ToolLimits
from researchdesk.storage import RecoveryError,atomic_text
from researchdesk.workflow import create_workflow,run_workflow,validate_manifest


@pytest.fixture
def limited(tmp_path,monkeypatch):
    from researchdesk import recovery,cli
    limits=ToolLimits(max_calls=9,max_provider_requests=2,max_retries=0,timeout_seconds=7,max_duration_seconds=40)
    document=tmp_path/'doc.md'
    document.write_text('A bounded fact.',encoding='utf-8')
    root=tmp_path/'workflow'
    create_workflow(root,'Question',[document],model='fixture',local_only=True,maximum=10,tool_limits=limits)
    graph_calls=[]
    def graph(ledger,initial,received,model,**kwargs):
        graph_calls.append(asdict(received))
        assert received==limits
        source=next(iter(ledger.sources.values()))
        ev=ledger.add_evidence('A fact',source['id'],'A bounded fact.')
        return {'messages':[AIMessage(content=f"A bounded fact.[{ev['id']}]")]}
    monkeypatch.setattr(recovery,'run_graph',graph)
    monkeypatch.setattr(cli,'load_dotenv',lambda:None)
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-fixture')
    repair_calls=[]
    def pause(*args,**kwargs):
        repair_calls.append(1)
        raise KeyboardInterrupt()
    return root,limits,graph_calls,repair_calls,pause


def test_custom_limits_reach_actual_research_cli_and_persist_in_source_and_fork(limited):
    from researchdesk.repair import fork_run
    root,limits,graphs,repairs,pause=limited
    state=run_workflow(root,repair_runner=pause)
    assert state['status']=='interrupted' and state['phase']=='repair'
    assert state['plan']['tool_policy']==asdict(limits) and graphs==[asdict(limits)] and repairs==[1]
    source=root/'research'/state['source_run_id']
    assert json.loads((source/'run.json').read_text(encoding='utf-8'))['tool_policy']==asdict(limits)
    destination=root/'fork-check'
    fork_run(source,destination)
    assert json.loads((destination/'run.json').read_text(encoding='utf-8'))['tool_policy']==asdict(limits)


def test_source_policy_tampering_blocks_resume_before_repair_or_new_generation(limited):
    root,limits,graphs,repairs,pause=limited
    state=run_workflow(root,repair_runner=pause)
    path=root/'research'/state['source_run_id']/'run.json'
    metadata=json.loads(path.read_text(encoding='utf-8'))
    metadata['tool_policy']['max_provider_requests']=99
    atomic_text(path,json.dumps(metadata))
    again=run_workflow(root,repair_runner=pause)
    assert again['status']=='needs_inspection' and len(graphs)==len(repairs)==1
    assert 'tool policy' in again['reason']


def test_plan_policy_replacement_cannot_reuse_existing_source(limited):
    root,limits,graphs,repairs,pause=limited
    run_workflow(root,repair_runner=pause)
    path=root/'workflow.json'
    state=json.loads(path.read_text(encoding='utf-8'))
    state['plan']['tool_policy']['max_calls']=99
    atomic_text(path,json.dumps(state))
    assert run_workflow(root,repair_runner=pause)['status']=='needs_inspection'
    assert len(graphs)==len(repairs)==1


def test_legacy_plan_without_tool_policy_keeps_previous_manifest_validation(limited):
    root,limits,_,_,pause=limited
    state=run_workflow(root,repair_runner=pause)
    source=root/'research'/state['source_run_id']
    meta=json.loads((source/'run.json').read_text(encoding='utf-8'))
    plan=copy.deepcopy(state['plan'])
    del plan['tool_policy']
    validate_manifest(meta,plan)


@pytest.mark.parametrize('flag',['--max-tool-calls','--max-provider-requests','--tool-retries','--tool-timeout','--tool-deadline'])
def test_resume_rejects_tool_limit_overrides_before_loading_configuration(tmp_path,monkeypatch,flag):
    from researchdesk import workflow
    monkeypatch.setattr(workflow,'load_dotenv',lambda:pytest.fail('Resume override must be rejected early'))
    monkeypatch.setattr(sys,'argv',['workflow','--resume',str(tmp_path/'old'),flag,'1'])
    with pytest.raises(SystemExit) as error: workflow.main()
    assert error.value.code==2


def test_invalid_tool_limit_type_cannot_create_workflow(tmp_path):
    directory=tmp_path/'workflow'
    with pytest.raises(RecoveryError):
        create_workflow(directory,'Question',[],model='fixture',tool_limits={'max_calls':1})
    assert not directory.exists()
