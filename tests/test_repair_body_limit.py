"""Hard body limits around one repair, with explicitly scripted quality labels."""
import json
import sqlite3
import sys
import pytest
from langchain_core.messages import AIMessage
from researchdesk import repair
from researchdesk.ledger import Ledger
from researchdesk.model_budget import budget_snapshot, model_budget_for
from researchdesk.report_constraints import check_body, counting_instruction
from researchdesk.storage import RecoveryError
from researchdesk.verification import assess,prepare,split_units
from test_repair import source_run,Checker
from test_repair_budget_context import enable


def frozen(source,body):
    ledger=Ledger(source,resume=True)
    try:
        ledger.save_report(body)
        payload,_=prepare(split_units(body),ledger.evidence,ledger.sources)
        return assess(payload,Checker())['units']
    finally:
        ledger.store.close()


def test_length_failure_triggers_one_repair_even_when_original_model_gate_passes(source_run,tmp_path):
    source,_,eid,_,_=source_run
    original='事实A。'+'长'*201+f'[{eid}]'
    feedback=frozen(source,original)  # Deliberately permissive fixture labels, not semantic correctness.
    before=(source/'draft.md').read_bytes()
    calls=[]
    checker=Checker()
    def graph(ledger,prepared,*a,**k):
        message=prepared['messages'][-1]['content']
        data=json.loads(message.rsplit('\n',1)[-1])
        assert data['issues']==[] and data['body_constraint']['counted_chars']>200
        assert counting_instruction() in message
        calls.append(1)
        return dict(messages=[AIMessage(content=f'事实A。[{eid}]')])
    destination=tmp_path/'candidate'
    state=repair.repair_run(source,destination,feedback_rows=feedback,checker=checker,graph_runner=graph,body_limit=200)
    assert state['selection']['accepted'] and state['selection']['selected']=='candidate'
    assert not state['original_body_constraints']['passed'] and state['candidate_body_constraints']['passed']
    assert state['selected_body_constraints']['passed'] and calls==[1] and checker.calls==1
    assert (source/'draft.md').read_bytes()==before
    assert repair.repair_run(None,destination,resume=True,checker=checker,graph_runner=graph)==state
    assert calls==[1] and checker.calls==1


def test_overlong_candidate_is_rejected_without_paying_verifier_and_releases_tail(source_run,tmp_path):
    source,_,eid,original,feedback=source_run
    enable(source)
    checker=Checker()
    def graph(ledger,prepared,*a,**k):
        data=json.loads(prepared['messages'][-1]['content'].rsplit('\n',1)[-1])
        assert data['budget_context']['model']['verification_reserved']==1
        budget=model_budget_for(ledger)
        budget.admit('generation','generation')
        budget.finish('generation','completed',dict(total_tokens=1))
        return dict(messages=[AIMessage(content='长'*201+f'[{eid}]')])
    destination=tmp_path/'candidate'
    state=repair.repair_run(source,destination,feedback_rows=feedback,checker=checker,graph_runner=graph,
                            body_limit=200,budget_context='snapshot-v1')
    assert state['selection']['reasons']==['candidate_body_constraint_failed']
    assert state['selection']['selected']=='original' and not state['selection']['accepted']
    assert state['candidate_body_constraints']['counted_chars']==201 and checker.calls==0
    assert not (destination/'revisions/candidate-quality.json').exists()
    assert (destination/'draft.md').read_text(encoding='utf-8')==original
    ledger=Ledger(destination,resume=True)
    try:
        info=budget_snapshot(ledger.store.conn)
        assert info['reserved']==1 and info['remaining']==3 and info['verification_slots_held']==0
    finally:
        ledger.store.close()
    assert repair.repair_run(None,destination,resume=True,checker=checker,graph_runner=graph)==state


def test_short_candidate_still_has_to_pass_semantic_gate(source_run,tmp_path):
    source,_,eid,_,feedback=source_run
    checker=Checker()
    state=repair.repair_run(source,tmp_path/'candidate',feedback_rows=feedback,checker=checker,
        graph_runner=lambda *a,**k:dict(messages=[AIMessage(content=f'没有依据的断言。[{eid}]')]),body_limit=200)
    assert state['candidate_body_constraints']['passed']
    assert checker.calls==1 and not state['selection']['accepted']


def test_already_compliant_supported_original_needs_no_repair(source_run,tmp_path):
    source,_,eid,_,_=source_run
    feedback=frozen(source,f'事实A。[{eid}]')
    def forbidden(*a,**k):
        pytest.fail('No new request for an already compliant original')
    state=repair.repair_run(source,tmp_path/'candidate',feedback_rows=feedback,checker=forbidden,
                            graph_runner=forbidden,body_limit=200)
    assert state['rounds_used']==0 and state['selection']['accepted']
    assert state['selected_body_constraints']['passed']
    assert repair.repair_run(None,tmp_path/'candidate',resume=True,checker=forbidden,graph_runner=forbidden)==state


@pytest.mark.parametrize('mutation',['input','missing_input','missing_hash','policy','downgrade','instruction'])
def test_resume_refuses_drift_before_model_callbacks(source_run,tmp_path,monkeypatch,mutation):
    source,_,_,_,feedback=source_run
    destination=tmp_path/'candidate'
    def interrupted(*a,**k):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        repair.repair_run(source,destination,feedback_rows=feedback,checker=Checker(),
                          graph_runner=interrupted,body_limit=200)
    state_path=destination/'repair-state.json'
    metadata_path=destination/'run.json'
    state=json.loads(state_path.read_text(encoding='utf-8'))
    metadata=json.loads(metadata_path.read_text(encoding='utf-8'))
    path=destination/'repair-input.json'
    if mutation=='input':
        path.write_text(path.read_text(encoding='utf-8')+' ',encoding='utf-8')
    elif mutation=='missing_input':
        path.rename(destination/'hidden-input.json')
    elif mutation=='missing_hash':
        state.pop('body_limit_input_sha256')
    elif mutation=='policy':
        state['body_limit_policy']=metadata['body_limit_policy']=repair.body_limit_policy(199)
    elif mutation=='downgrade':
        state.pop('body_limit_policy')
        metadata.pop('body_limit_policy')
        state.pop('body_limit_input_sha256')
    else:
        current=repair.counting_instruction
        monkeypatch.setattr(repair,'counting_instruction',lambda limit:current(limit)+' changed')
    state_path.write_text(json.dumps(state),encoding='utf-8')
    metadata_path.write_text(json.dumps(metadata),encoding='utf-8')
    def forbidden(*a,**k):
        pytest.fail('No provider calls for changed repair state')
    with pytest.raises(RecoveryError):
        repair.repair_run(None,destination,resume=True,checker=forbidden,graph_runner=forbidden)


@pytest.mark.parametrize('mutation',['original','candidate','draft','receipt','manifest'])
def test_completed_resume_checks_bodies_and_receipts_before_noop(source_run,tmp_path,mutation):
    source,_,eid,_,feedback=source_run
    destination=tmp_path/'candidate'
    repair.repair_run(source,destination,feedback_rows=feedback,checker=Checker(),
        graph_runner=lambda *a,**k:dict(messages=[AIMessage(content='长'*201+f'[{eid}]')]),body_limit=200)
    if mutation in {'original','candidate'}:
        path=destination/'revisions'/f'{mutation}.md'
    elif mutation=='draft':
        path=destination/'draft.md'
    else:
        path=destination/('repair-state.json' if mutation=='receipt' else 'run.json')
    if mutation in {'original','candidate','draft'}:
        path.write_text(path.read_text(encoding='utf-8')+' changed',encoding='utf-8')
    else:
        data=json.loads(path.read_text(encoding='utf-8'))
        data['selected_body_constraints' if mutation=='receipt' else 'body_constraints']['counted_chars']+=1
        path.write_text(json.dumps(data),encoding='utf-8')
    def forbidden(*a,**k):
        pytest.fail('No provider calls for changed completed bodies')
    with pytest.raises(RecoveryError):
        repair.repair_run(None,destination,resume=True,checker=forbidden,graph_runner=forbidden)


@pytest.mark.parametrize('limit',[True,0,-1,100001,'200'])
def test_invalid_body_limit_is_rejected_before_fork(source_run,tmp_path,limit):
    with pytest.raises(RecoveryError):
        repair.fork_run(source_run[0],tmp_path/'candidate',body_limit=limit)
    assert not (tmp_path/'candidate').exists()


def test_cli_resume_cannot_replace_body_limit_before_loading_credentials(monkeypatch):
    monkeypatch.setattr(repair,'load_dotenv',lambda:pytest.fail('No credentials'))
    monkeypatch.setattr(sys,'argv',['repair','--resume','candidate','--body-limit','200'])
    with pytest.raises(SystemExit) as error:
        repair.main()
    assert error.value.code==2


def test_insufficient_original_budget_still_stops_before_generation_with_body_feedback(source_run,tmp_path):
    source,_,_,_,feedback=source_run
    enable(source,1)
    def forbidden(*a,**k):
        pytest.fail('Body constraint must not bypass verifier reservation')
    destination=tmp_path/'candidate'
    state=repair.repair_run(source,destination,feedback_rows=feedback,checker=forbidden,
                            graph_runner=forbidden,body_limit=5)
    assert state['rounds_used']==0 and not state['selected_body_constraints']['passed']
    assert state['selection']['reasons']==['insufficient_budget_for_repair_and_verification']
    assert repair.repair_run(None,destination,resume=True,checker=forbidden,graph_runner=forbidden)==state


def test_new_fork_requires_an_explicit_limit_instead_of_inheriting_parent_policy(source_run,tmp_path):
    source,_,eid,_,feedback=source_run
    parent=tmp_path/'parent'
    repair.repair_run(source,parent,feedback_rows=feedback,checker=Checker(),
        graph_runner=lambda *a,**k:dict(messages=[AIMessage(content='长'*201+f'[{eid}]')]),body_limit=200)
    child=tmp_path/'child'
    repair.fork_run(parent,child)
    state=json.loads((child/'repair-state.json').read_text(encoding='utf-8'))
    manifest=json.loads((child/'run.json').read_text(encoding='utf-8'))
    assert 'body_limit_policy' not in state and 'body_limit_policy' not in manifest
    with sqlite3.connect(child/'application.sqlite3') as connection:
        assert connection.execute("SELECT COUNT(*) FROM records WHERE kind='repair_body_policy'").fetchone()[0]==0


def test_citation_only_candidate_cannot_pass_an_empty_body_constraint(source_run,tmp_path):
    source,_,eid,_,feedback=source_run
    checker=Checker()
    state=repair.repair_run(source,tmp_path/'candidate',feedback_rows=feedback,checker=checker,
        graph_runner=lambda *a,**k:dict(messages=[AIMessage(content=f'[{eid}]')]),body_limit=200)
    assert state['candidate_body_constraints']['status']=='invalid' and checker.calls==0
    assert state['selection']['reasons']==['candidate_body_constraint_failed']
