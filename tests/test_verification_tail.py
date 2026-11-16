import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from langchain_core.messages import AIMessage
from researchdesk.ledger import Ledger
from researchdesk.model_budget import ModelCallBudget,ModelBudgetExceeded,VerificationTailProtected,model_budget_for,budget_snapshot,VERSION
from researchdesk.repair import repair_run,fork_run
from researchdesk.storage import atomic_text,RecoveryError
from researchdesk.task_eval import stop_owned
from researchdesk.verification import digest
from test_repair import source_run,Checker
from test_workflow import workflow,Checker as WorkflowChecker


def enable(source,maximum):
    path=source/'run.json'
    meta=json.loads(path.read_text())
    meta['model_policy']=dict(version=VERSION,max_calls=maximum)
    atomic_text(path,json.dumps(meta))


def snapshot(directory):
    ledger=Ledger(directory,resume=True)
    try: return budget_snapshot(ledger.store.conn)
    finally: ledger.store.close()


def content(ledger,sid,eid):
    second=ledger.add_evidence('事实B',sid,'事实B。')
    return f"事实A。[{eid}]\n事实B。[{second['id']}]"


def test_one_slot_after_original_check_skips_generation_and_returns_review(source_run,tmp_path):
    source,_,_,original,_=source_run
    enable(source,2)
    checker=Checker()
    dest=tmp_path/'candidate'
    state=repair_run(source,dest,checker=checker,graph_runner=lambda *a,**k:pytest.fail('Must not generate without a validation runway'))
    assert state['status']=='completed' and state['rounds_used']==0 and checker.calls==1
    assert state['selection']['selected']=='original' and not state['selection']['accepted']
    assert state['budget_fallback_reason']=='insufficient_budget_for_repair_and_verification'
    assert (dest/'revisions/original.md').read_text(encoding='utf-8')==original
    assert not (dest/'revisions/candidate.md').exists() and snapshot(dest)['remaining']==1


def test_exact_generation_plus_verifier_slots_validate_candidate(source_run,tmp_path):
    source,sid,eid,_,_=source_run
    enable(source,3)
    checker=Checker()
    def graph(ledger,*args,**kwargs):
        budget=model_budget_for(ledger)
        assert budget_snapshot(ledger.store.conn)['verification_slots_held']==1
        budget.admit('generate','generation')
        budget.finish('generate','completed')
        return {'messages':[AIMessage(content=content(ledger,sid,eid))]}
    dest=tmp_path/'candidate'
    state=repair_run(source,dest,checker=checker,graph_runner=graph)
    assert state['selection']['accepted'] and checker.calls==2
    info=snapshot(dest)
    assert info['reserved']==3 and info['remaining']==0
    assert info['verification_slots_held']==0 and info['verification_holds'][0]['state']=='consumed'


def test_multistep_generation_cannot_use_tail_and_cached_resume_does_not_retry(source_run,tmp_path):
    source,_,_,_,_=source_run
    enable(source,3)
    checker=Checker()
    calls=[]
    def graph(ledger,*args,**kwargs):
        calls.append(1)
        budget=model_budget_for(ledger)
        budget.admit('first','generation')
        budget.finish('first','completed')
        budget.admit('second','generation')
        pytest.fail('Generation must stop before consuming the tail')
    dest=tmp_path/'candidate'
    state=repair_run(source,dest,checker=checker,graph_runner=graph)
    assert state['selection']['selected']=='original' and checker.calls==1 and calls==[1]
    assert state['budget_fallback_reason']=='repair_generation_stopped_for_verification_budget'
    info=snapshot(dest)
    assert info['reserved']==2 and info['remaining']==1 and info['verification_holds'][0]['state']=='released'
    assert repair_run(None,dest,checker=checker,graph_runner=graph,resume=True)==state
    assert checker.calls==1 and calls==[1]
    from researchdesk.inspect_run import inspect_run
    diagnosis=inspect_run(dest)
    codes={r['code'] for r in diagnosis['findings']}
    assert 'verification_tail_preserved' in codes and 'model_call_budget_exhausted' not in codes
    assert diagnosis['repair']['reasons']==['repair_generation_stopped_for_verification_budget']


def test_hold_blocks_framework_summary_and_failed_verifier_is_not_refunded(tmp_path):
    ledger=Ledger(tmp_path/'run')
    try:
        budget=ModelCallBudget(ledger.store,2)
        assert budget.hold_verification('tail') and budget.hold_verification('tail')
        budget.admit('first','generation')
        budget.finish('first','completed')
        with pytest.raises(VerificationTailProtected): budget.admit('summary','context_summarization')
        budget.admit('verify','repair_verification')
        budget.finish('verify','failed')
        budget.release_verification('tail')
        info=budget_snapshot(ledger.store.conn)
        assert info['states']=={'completed':1,'failed':1} and info['remaining']==0
        assert info['verification_holds'][0]['state']=='consumed'
        with pytest.raises(ModelBudgetExceeded): budget.admit('again','repair_verification')
    finally: ledger.store.close()


def test_parallel_generation_cannot_steal_verifier_slot(tmp_path):
    ledger=Ledger(tmp_path/'run')
    try:
        budget=ModelCallBudget(ledger.store,3)
        assert budget.hold_verification('tail')
        def admit(index):
            role='repair_verification' if index==0 else 'generation'
            try:
                budget.admit(str(index),role)
                return role
            except ModelBudgetExceeded: return None
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(admit,range(5)))
        assert results.count('repair_verification')==1 and results.count('generation')==2
        assert budget_snapshot(ledger.store.conn)['reserved']==3
    finally: ledger.store.close()


def test_hold_release_is_not_a_refund_and_cannot_be_reactivated(tmp_path):
    ledger=Ledger(tmp_path/'run')
    try:
        budget=ModelCallBudget(ledger.store,3)
        assert budget.hold_verification('tail')
        with pytest.raises(RecoveryError): budget.hold_verification('different')
        budget.admit('request','generation')
        budget.finish('request','failed')
        budget.release_verification('tail')
        assert not budget.hold_verification('tail')
        info=budget_snapshot(ledger.store.conn)
        assert info['reserved']==1 and info['remaining']==2 and info['verification_slots_held']==0
    finally: ledger.store.close()


def test_crash_after_fallback_intent_and_release_cannot_restart_generation(source_run,tmp_path,monkeypatch):
    source,_,_,_,_=source_run
    enable(source,3)
    checker=Checker()
    generated=[]
    def graph(ledger,*args,**kwargs):
        generated.append(1)
        budget=model_budget_for(ledger)
        budget.admit('first','generation')
        budget.finish('first','completed')
        budget.admit('blocked','generation')
    original_release=ModelCallBudget.release_verification
    def crash(self,key):
        original_release(self,key)
        raise KeyboardInterrupt()
    dest=tmp_path/'candidate'
    with monkeypatch.context() as context:
        context.setattr(ModelCallBudget,'release_verification',crash)
        with pytest.raises(KeyboardInterrupt): repair_run(source,dest,checker=checker,graph_runner=graph)
    assert json.loads((dest/'repair-state.json').read_text())['budget_fallback_reason']
    state=repair_run(None,dest,checker=checker,graph_runner=graph,resume=True)
    assert state['status']=='completed' and state['selection']['selected']=='original'
    assert generated==[1] and checker.calls==1 and snapshot(dest)['reserved']==2


def test_existing_candidate_can_use_last_slot_for_verification(source_run,tmp_path):
    source,sid,eid,_,_=source_run
    enable(source,2)
    dest=tmp_path/'candidate'
    fork_run(source,dest)
    legacy=json.loads((dest/'repair-state.json').read_text())
    del legacy['verification_tail_policy']
    atomic_text(dest/'repair-state.json',json.dumps(legacy))
    ledger=Ledger(dest,resume=True)
    candidate=content(ledger,sid,eid)
    ledger.store.close()
    atomic_text(dest/'revisions/candidate.md',candidate)
    checker=Checker()
    state=repair_run(None,dest,resume=True,checker=checker,graph_runner=lambda *a,**k:pytest.fail('Candidate already exists'))
    assert state['selection']['accepted'] and checker.calls==2 and snapshot(dest)['remaining']==0


def test_unknown_tail_policy_cannot_invoke_models_on_resume(source_run,tmp_path):
    source,_,_,_,_=source_run
    enable(source,3)
    dest=tmp_path/'candidate'
    fork_run(source,dest)
    path=dest/'repair-state.json'
    state=json.loads(path.read_text())
    state['verification_tail_policy']='different-policy'
    atomic_text(path,json.dumps(state))
    checker=Checker()
    with pytest.raises(RecoveryError,match='verification-tail policy'):
        repair_run(None,dest,resume=True,checker=checker,graph_runner=lambda *a,**k:pytest.fail('No generation allowed'))
    assert checker.calls==0


def test_real_deepagents_callback_stops_before_second_model_invocation(source_run,tmp_path,monkeypatch):
    from test_agent import ScriptedToolModel
    source,_,_,_,_=source_run
    enable(source,3)
    invoked=[]
    class Model(ScriptedToolModel):
        def _generate(self,*args,**kwargs):
            invoked.append(1)
            return super()._generate(*args,**kwargs)
    model=Model(messages=iter([AIMessage(content='',tool_calls=[dict(name='write_todos',
        args={'todos':[{'content':'Revise report','status':'in_progress'}]},id='todo',type='tool_call')]),
        AIMessage(content='Must not be invoked')]))
    monkeypatch.setattr('researchdesk.agent.ChatDeepSeek',lambda **kwargs:model)
    dest=tmp_path/'candidate'
    state=repair_run(source,dest,checker=Checker())
    assert state['status']=='completed' and state['selection']['selected']=='original'
    assert state['budget_fallback_reason']=='repair_generation_stopped_for_verification_budget'
    assert invoked==[1] and snapshot(dest)['reserved']==2


def test_hard_kill_preserves_hold_and_uncertain_generation_slot(tmp_path):
    directory=tmp_path/'run'
    code="""import sys,time
from pathlib import Path
from researchdesk.ledger import Ledger
from researchdesk.model_budget import ModelCallBudget
from researchdesk.storage import atomic_text
ledger=Ledger(Path(sys.argv[1]))
budget=ModelCallBudget(ledger.store,2)
assert budget.hold_verification('tail')
budget.admit('uncertain-generation','generation')
atomic_text(ledger.directory/'ready','ready')
time.sleep(60)
"""
    options=dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW) if os.name=='nt' else dict(start_new_session=True)
    process=subprocess.Popen([sys.executable,'-c',code,str(directory)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
    try:
        deadline=time.monotonic()+20
        while not (directory/'ready').exists() and process.poll() is None and time.monotonic()<deadline:
            time.sleep(0.05)
        assert (directory/'ready').exists()
        stop_owned(process)
    finally:
        if process.poll() is None: stop_owned(process)
    ledger=Ledger(directory,resume=True)
    try:
        restored=ModelCallBudget(ledger.store,2)
        assert restored.hold_verification('tail')
        with pytest.raises(VerificationTailProtected): restored.admit('new-generation','generation')
        restored.admit('verify','repair_verification')
        restored.finish('verify','completed')
        assert budget_snapshot(ledger.store.conn)['states']=={'reserved':1,'completed':1}
    finally: ledger.store.close()


@pytest.mark.parametrize('maximum',[3,4])
def test_workflow_finishes_review_and_completed_resume_never_regenerates(workflow,maximum):
    from researchdesk.workflow import run_workflow
    root,research,calls=workflow
    path=root/'workflow.json'
    state=json.loads(path.read_text(encoding='utf-8'))
    state['plan']['max_model_calls']=maximum
    atomic_text(path,json.dumps(state))
    checker=WorkflowChecker('unsupported')
    generations=[]
    def graph(ledger,*args,**kwargs):
        generations.append(1)
        budget=model_budget_for(ledger)
        budget.admit('first-repair','generation')
        budget.finish('first-repair','completed')
        budget.admit('blocked-repair','generation')
        pytest.fail('Tail must remain available')
    def repair(source,destination,*,resume=False):
        return repair_run(source,destination,checker=checker,graph_runner=graph,resume=resume)
    result=run_workflow(root,research_runner=research,repair_runner=repair)
    assert result['status']=='needs_review' and result['phase']=='done' and result['last_returncode']==3
    assert result['result']['selected']=='original' and result['human_review']=='pending'
    assert len(generations)==(0 if maximum==3 else 1)
    assert result['result']['repair_rounds']==(0 if maximum==3 else 1)
    candidate=root/'candidate'/result['candidate_run_id']
    assert snapshot(candidate)['remaining']==1
    assert run_workflow(root,research_runner=research,repair_runner=repair)==result
    assert len(calls)==checker.calls==1
