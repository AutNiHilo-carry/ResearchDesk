"""Local delivery and portable validation; all scores are injected, not human gold."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import pytest
from researchdesk import delivery
from test_judge_stability_inputs import completed_batch
from test_task_eval import task_data, fixture_runner, RubricJudge, EvidenceJudge


@pytest.mark.parametrize('scenario,model,application',[
    ('quality_failed','failed','failed'),('missing_evidence','failed','failed'),('overlong','passed','failed'),
    ('legacy_constraints','passed','unknown'),('unknown_usage','passed','passed')])
def test_delivery_preserves_failed_unknown_and_unmeasured_states(tmp_path,scenario,model,application):
    from researchdesk.task_eval import evaluate_batch
    data,tasks,documents=task_data()
    def runner(*args):
        record=fixture_runner(*args)
        if scenario=='overlong':
            path=Path(record['run_directory'])/'draft.md'
            path.write_text(path.read_text(encoding='utf-8')+'X'*220,encoding='utf-8')
        if scenario=='missing_evidence':
            path=Path(record['run_directory'])/'draft.md'
            path.write_text(path.read_text(encoding='utf-8')+'\nMissing fact.[E0000000000000000]',encoding='utf-8')
        if scenario=='unknown_usage':
            record['generation_metrics'].update(token_usage_complete=False,total_tokens=None)
        return record
    rubric=RubricJudge()
    judge=EvidenceJudge('unsupported' if scenario=='quality_failed' else 'supported')
    if scenario=='unknown_usage': rubric.usage=judge.usage=None
    evaluation=tmp_path/'evaluation'
    result=evaluate_batch(data,tasks[:1],documents,evaluation,runner=runner,model_name='fixture',
                          task_judge=rubric,evidence_judge=judge)
    if scenario=='legacy_constraints':
        result.pop('constraint_policy',None)
        for row in result['tasks']: row.pop('constraints',None)
        (evaluation/'result.json').write_text(json.dumps(result),encoding='utf-8')
    dataset=Path(__file__).parents[1]/'evals/research-tasks-v1.json'
    output=tmp_path/'delivery'
    created=delivery.create_delivery(evaluation,dataset,output)
    assert created['exit_code']==(0 if application=='passed' else 3)
    verified=delivery.verify_delivery(output)
    assert verified['model_gate']==model and verified['application_gate']==application
    if scenario=='missing_evidence':
        assert json.loads((output/'quality.json').read_text(encoding='utf-8'))['metrics']['invalid_links']==1
    if scenario=='unknown_usage':
        stats=json.loads((output/'statistics.json').read_text(encoding='utf-8'))
        assert stats['total_tokens'] is stats['generation_total_tokens'] is stats['evaluator_total_tokens'] is None
        assert stats['estimated_fee'] is stats['recovery_rate'] is None


def hashes(root):
    return {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}


def rehash(root):
    path=root/'manifest.json'
    manifest=json.loads(path.read_text(encoding='utf-8'))
    for name in manifest['files']:
        manifest['files'][name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest),encoding='utf-8')


def test_create_portable_delivery_preserves_sources_and_pending_review(completed_batch,tmp_path,monkeypatch):
    evaluation,dataset,_,_=completed_batch
    # Fields outside the approved export are not included just because they are in a manifest.
    run=next((evaluation/'tasks').glob('*/research/*/run.json'))
    metadata=json.loads(run.read_text(encoding='utf-8'))
    metadata['private_response']='NOT_AN_EXPORT_FIELD'
    run.write_text(json.dumps(metadata),encoding='utf-8')
    (evaluation/'.env').write_text('DO_NOT_READ_OR_EXPORT',encoding='utf-8')
    before=hashes(evaluation)
    original=Path.read_bytes
    def no_secret_read(path):
        if path==evaluation/'.env': pytest.fail('Delivery must not read .env')
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',no_secret_read)
    import dotenv
    monkeypatch.setattr(dotenv,'load_dotenv',lambda:pytest.fail('No credentials'))
    output=tmp_path/'delivery'
    result=delivery.create_delivery(evaluation,dataset,output)
    assert result['exit_code']==0 and result['api_requests']==0
    portable=tmp_path/'portable'
    shutil.copytree(output,portable)
    monkeypatch.setattr(delivery,'build_packet',lambda *a,**k:pytest.fail('Verify must not access original evaluation'))
    monkeypatch.setattr(delivery,'check_saved_result',lambda *a,**k:pytest.fail('Verify must not re-open source'))
    checked=delivery.verify_delivery(portable)
    assert checked['status']=='integrity_verified' and checked['application_gate']=='passed'
    assert checked['human_review']==checked['final_acceptance']=='pending' and checked['promotion']=='none'
    text='\n'.join(p.read_text(encoding='utf-8') for p in output.rglob('*') if p.is_file())
    assert 'NOT_AN_EXPORT_FIELD' not in text and 'DO_NOT_READ_OR_EXPORT' not in text
    monkeypatch.setattr(Path,'read_bytes',original)
    assert hashes(evaluation)==before
    template=json.loads((output/'review/review.json').read_text(encoding='utf-8'))
    assert not template['reviewer'] and all(u['factual'] is None for t in template['tasks'] for u in t['units'])


@pytest.mark.parametrize('mutation',['report','missing','extra','quality','gate','evidence','review','statistics','index','reading'])
def test_tampered_bundles_are_rejected_even_with_rehashed_manifest(completed_batch,tmp_path,mutation):
    evaluation,dataset,_,_=completed_batch
    output=tmp_path/'delivery'
    delivery.create_delivery(evaluation,dataset,output)
    if mutation=='report': (output/'reports/001.md').write_text('Changed report',encoding='utf-8')
    elif mutation=='missing': (output/'quality.json').unlink()
    elif mutation=='extra': (output/'unlisted.txt').write_text('Extra',encoding='utf-8')
    elif mutation in {'index','reading'}:
        (output/('index.md' if mutation=='index' else 'review/reading.md')).write_text('Misleading reading material',encoding='utf-8')
    else:
        path=output/({'quality':'quality.json','gate':'assessment.json','evidence':'evidence.json',
                     'review':'review/review.json','statistics':'statistics.json'}[mutation])
        value=json.loads(path.read_text(encoding='utf-8'))
        if mutation=='quality': value['metrics']['covered_units']=999
        elif mutation=='gate': value['final_acceptance']='passed'
        elif mutation=='evidence': value['units'][0]['text']='Changed ledger'
        elif mutation=='review': value['tasks'][0]['units'][0]['factual']=True
        else: value['total_tokens']=999999
        path.write_text(json.dumps(value),encoding='utf-8')
    if mutation not in {'missing','extra'}: rehash(output)
    with pytest.raises(ValueError): delivery.verify_delivery(output)


@pytest.mark.parametrize('name',['../outside.json','/absolute.json','reports\\bad.md'])
def test_manifest_paths_cannot_request_external_reads(completed_batch,tmp_path,name):
    evaluation,dataset,_,_=completed_batch
    output=tmp_path/'delivery'
    delivery.create_delivery(evaluation,dataset,output)
    path=output/'manifest.json'
    value=json.loads(path.read_text(encoding='utf-8'))
    value['files'][name]='0'*64
    path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ValueError): delivery.verify_delivery(output)


def test_source_drift_and_existing_outputs_are_not_silently_accepted(completed_batch,tmp_path,monkeypatch):
    evaluation,dataset,_,_=completed_batch
    output=tmp_path/'delivery'
    original=delivery.statistics
    calls=[]
    def drift(*args):
        value=original(*args)
        calls.append(1)
        if len(calls)==2:
            path=evaluation/'result.json'
            path.write_bytes(path.read_bytes()+b' ')
        return value
    monkeypatch.setattr(delivery,'statistics',drift)
    with pytest.raises(ValueError): delivery.create_delivery(evaluation,dataset,output)
    assert not output.exists()
    monkeypatch.setattr(delivery,'statistics',original)
    delivery.create_delivery(evaluation,dataset,output)
    before=hashes(output)
    with pytest.raises(ValueError): delivery.create_delivery(evaluation,dataset,output)
    assert hashes(output)==before


def test_failed_publication_has_no_manifest_marker(completed_batch,tmp_path,monkeypatch):
    evaluation,dataset,_,_=completed_batch
    output=tmp_path/'delivery'
    original=delivery.atomic_text
    def fail(path,text):
        if path==output/'quality.json': raise OSError('Injected delivery publication failure')
        return original(path,text)
    monkeypatch.setattr(delivery,'atomic_text',fail)
    with pytest.raises(OSError): delivery.create_delivery(evaluation,dataset,output)
    assert not (output/'manifest.json').exists()
    with pytest.raises(ValueError): delivery.verify_delivery(output)


def test_cli_returns_separate_creation_and_integrity_status(completed_batch,tmp_path,monkeypatch):
    evaluation,dataset,_,_=completed_batch
    output=tmp_path/'delivery'
    monkeypatch.setattr(sys,'argv',['delivery','create',str(evaluation),'--dataset',str(dataset),'--output',str(output)])
    assert delivery.main()==0
    monkeypatch.setattr(sys,'argv',['delivery','verify',str(output)])
    assert delivery.main()==0
    (output/'index.md').write_text('Changed',encoding='utf-8')
    assert delivery.main()==1
