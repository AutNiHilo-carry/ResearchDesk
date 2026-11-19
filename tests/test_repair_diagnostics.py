"""Diagnostics revalidate cached labels but never infer paraphrase support or copy citations."""
import json
import sqlite3
import sys
import pytest
from langchain_core.messages import AIMessage
from researchdesk import repair,repair_diagnostics as diagnostic
from researchdesk.ledger import Ledger
from researchdesk.verification import SYSTEM_PROMPT_V2,VerificationError,assess,digest,prepare,split_units
from researchdesk.storage import RecoveryError
from test_repair import source_run,Checker
from test_repair_budget_context import enable


def checker():
    judge=Checker()
    judge.prompt_sha256=digest(SYSTEM_PROMPT_V2)
    judge.prompt_version='report-quote-v2'
    return judge


def build(source_run,tmp_path,body=None,**options):
    source,sid,eid,_,_=source_run
    ledger=Ledger(source,resume=True)
    try:
        second=ledger.add_evidence('事实B',sid,'事实B。')['id']
        report=f'事实A。[{eid}]\n事实B。[{second}]\n没有依据的断言。[{eid}]'
        ledger.save_report(report)
        payload,_=prepare(split_units(report),ledger.evidence,ledger.sources)
        feedback=assess(payload,checker())['units']
    finally:
        ledger.store.close()
    candidate=body(eid,second) if body else f'事实A。\n事实B。[{second}]\n没有依据的断言。'
    directory=tmp_path/'repair'
    repair.repair_run(source,directory,feedback_rows=feedback,checker=checker(),
        graph_runner=lambda *a,**k:dict(messages=[AIMessage(content=candidate)]),**options)
    return directory,eid,second


def snapshot(directory):
    return {str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}


def test_literal_loss_and_saved_label_gaps_are_readonly_and_do_not_invent_support(source_run,tmp_path,monkeypatch):
    directory,eid,second=build(source_run,tmp_path)
    before=snapshot(directory)
    monkeypatch.setattr(repair,'ModelJudge',lambda *a,**k:pytest.fail('No models'))
    result=diagnostic.diagnose(directory)
    assert result['status']=='diagnostic_only' and result['api_requests']==0
    assert result['source_run_directory']==str(directory.resolve())
    assert result['inventory']['disappeared_ids']==[eid]
    assert result['inventory']['retained_ids']==[second]
    first=result['original_units'][0]
    assert first['saved_label']['covered'] and first['relation']=='literal_match'
    assert first['literal_matches'][0]['removed_reference_ids']==[eid]
    assert first['candidate_reference_locations'][eid]==[]
    assert result['candidate_units'][0]['saved_label']['covered'] is False
    assert result['semantic_rejudgment']=='not_performed' and not result['automatic_citation_transfer']
    assert result['human_review']=='pending' and not result['affects_original_scores']
    assert snapshot(directory)==before


@pytest.mark.parametrize('text',['并非事实A。','事实A！','事实 A。','事实A成立。'])
def test_negation_spacing_punctuation_and_paraphrases_do_not_become_exact_matches(source_run,tmp_path,text):
    directory,eid,_=build(source_run,tmp_path,lambda a,b:text+f'\n事实B。[{b}]')
    result=diagnostic.diagnose(directory)
    assert result['original_units'][0]['relation']=='unmatched'
    assert result['original_units'][0]['literal_matches']==[]
    assert eid in result['inventory']['disappeared_ids']


def test_ambiguous_literal_matches_and_shared_ids_remain_explicit(source_run,tmp_path):
    directory,eid,_=build(source_run,tmp_path,lambda a,b:f'事实A。\n事实A。[{a}]\n事实B。[{b}]')
    result=diagnostic.diagnose(directory)
    row=result['original_units'][0]
    assert len(row['literal_matches'])==2
    assert [r['removed_reference_ids'] for r in row['literal_matches']]==[[eid],[]]
    assert row['candidate_reference_locations'][eid]==['U2']
    assert eid not in result['inventory']['disappeared_ids']


@pytest.mark.parametrize('mutation',['text','metrics','verdict','input_hash','prompt'])
def test_inconsistent_cached_labels_are_refused(source_run,tmp_path,mutation):
    directory,_,_=build(source_run,tmp_path)
    path=directory/'revisions/original-quality.json'
    cache=json.loads(path.read_text(encoding='utf-8'))
    if mutation=='text':
        cache['result']['units'][0]['text']='Invented body'
    elif mutation=='metrics':
        cache['result']['metrics']['covered_units']+=1
    elif mutation=='verdict':
        cache['result']['units'][0]['links'][0]['verdict']='unsupported'
    elif mutation=='input_hash':
        cache['input_sha256']='0'*64
    else:
        cache['prompt_sha256']='unknown'
    path.write_text(json.dumps(cache),encoding='utf-8')
    with pytest.raises(VerificationError):
        diagnostic.diagnose(directory)


@pytest.mark.parametrize('revision',['original','candidate'])
def test_changed_revision_body_is_refused(source_run,tmp_path,revision):
    directory,_,_=build(source_run,tmp_path)
    path=directory/'revisions'/f'{revision}.md'
    path.write_text(path.read_text(encoding='utf-8')+' changed',encoding='utf-8')
    with pytest.raises((VerificationError,RecoveryError)):
        diagnostic.diagnose(directory)


def test_budget_stop_without_candidate_is_not_reported_as_lost_references(source_run,tmp_path):
    source,_,_,_,feedback=source_run
    enable(source,1)
    directory=tmp_path/'repair'
    repair.repair_run(source,directory,feedback_rows=feedback,checker=checker(),body_limit=200,
        graph_runner=lambda *a,**k:pytest.fail('No generation budget'))
    result=diagnostic.diagnose(directory)
    assert result['candidate_status']=='not_generated' and result['inventory'] is None
    assert result['candidate_metrics'] is None and result['candidate_units']==[]
    assert all(r['relation']=='not_compared_no_candidate' for r in result['original_units'])


def test_length_rejection_without_candidate_labels_stays_unscored(source_run,tmp_path):
    directory,_,_=build(source_run,tmp_path,lambda a,b:'长'*201+f'[{a}]',body_limit=200)
    result=diagnostic.diagnose(directory)
    assert result['candidate_status']=='generated' and result['candidate_quality_status']=='not_available'
    assert result['candidate_metrics'] is None
    assert all(r['saved_label'] is None for r in result['candidate_units'])
    assert result['candidate_body_constraints']['status']=='failed'


def test_cli_creates_new_view_without_credentials_or_overwrite(source_run,tmp_path,monkeypatch):
    directory,_,_=build(source_run,tmp_path)
    monkeypatch.setattr(repair,'load_dotenv',lambda:pytest.fail('No credentials'))
    output=tmp_path/'view'
    monkeypatch.setattr(sys,'argv',['repair_diagnostics',str(directory),'--output',str(output)])
    assert diagnostic.main()==0
    before=snapshot(output)
    assert '不证明语义继承' in (output/'summary.md').read_text(encoding='utf-8')
    assert diagnostic.main()==2 and snapshot(output)==before


def test_committed_wal_policy_changes_are_not_silently_ignored(source_run,tmp_path):
    directory,_,_=build(source_run,tmp_path,body_limit=200)
    writer=sqlite3.connect(directory/'application.sqlite3')
    try:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("UPDATE records SET payload=? WHERE kind='repair_body_policy'",(json.dumps(dict(version='drift')),))
        writer.commit()  # Keep the connection open so this committed frame remains in WAL.
        wal=directory/'application.sqlite3-wal'
        assert wal.is_file() and wal.stat().st_size>0
        before=snapshot(directory)
        with pytest.raises(RecoveryError,match='durable state'):
            diagnostic.diagnose(directory)
        assert snapshot(directory)==before
    finally:
        writer.close()


def test_source_change_during_copy_is_refused(source_run,tmp_path,monkeypatch):
    directory,_,_=build(source_run,tmp_path)
    original=diagnostic.shutil.copyfile
    def changed(source,destination):
        if source.name=='candidate.md':
            source.write_text(source.read_text(encoding='utf-8')+' concurrent change',encoding='utf-8')
        return original(source,destination)
    monkeypatch.setattr(diagnostic.shutil,'copyfile',changed)
    with pytest.raises(VerificationError,match='changed while copying'):
        diagnostic.diagnose(directory)


def test_cli_refuses_to_write_a_view_into_the_original_run(source_run,tmp_path,monkeypatch):
    directory,_,_=build(source_run,tmp_path)
    before=snapshot(directory)
    monkeypatch.setattr(sys,'argv',['repair_diagnostics',str(directory),'--output',str(directory/'new-view')])
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code==2 and snapshot(directory)==before
