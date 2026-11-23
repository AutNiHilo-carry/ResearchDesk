"""Injected opinions test mechanics only; never independent human reference labels."""
import json
import pytest
from researchdesk.ledger import Ledger
from researchdesk.context_experiment import prepare_plan, execute_plan
from researchdesk.context_reference import build_packet, export_reference, import_reference
from researchdesk.storage import RecoveryError
from researchdesk.verification import VerificationError
from test_context_experiment import InjectedJudge


@pytest.fixture
def experiment(tmp_path):
    run=tmp_path/'run'
    ledger=Ledger(run)
    source=ledger.add_source('Fixture','fixture://18','# Version 18\n系统支持日志。','fixture')
    evidence=ledger.add_evidence('日志',source['id'],'系统支持日志。')
    ledger.save_report(f"建议：系统支持日志。[{evidence['id']}]\n系统自动恢复，但资料不足。")
    (run/'run.json').write_text('{"status":"completed"}',encoding='utf-8')
    root=tmp_path/'experiment'
    prepare_plan([dict(name='fixture',run=str(run),body='selected')],root)
    def factory(model,arm):
        judge=InjectedJudge()
        judge.prompt_version='context-factorial-v1'
        judge.prompt_sha256=arm['prompt_sha256']
        return judge
    execute_plan(root,judge_factory=factory)
    yield root
    ledger.store.close()


def write_form(experiment,tmp_path):
    bundle=tmp_path/'bundle'
    form=export_reference(experiment,bundle)
    form.update(reviewer='Injected test fixture, not a human',reviewed_at='2026-11-23T20:00:00+08:00')
    for case in form['cases']:
        for unit in case['units']:
            unit.update(factual=True,atomic_claims=['Fixture assertion'],reason='Injected reference for mechanics')
            for link in unit['links']:
                link.update(quote_verdict='partial',source_verdict='supported',reason='Injected only')
    path=tmp_path/'opinion.json'
    path.write_text(json.dumps(form),encoding='utf-8')
    return bundle,path,form


def test_export_has_no_model_labels_and_all_templates_unknown(experiment,tmp_path,monkeypatch):
    import socket
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('Offline only'))
    bundle=tmp_path/'bundle'
    form=export_reference(experiment,bundle)
    packet=json.loads((bundle/'packet.json').read_text(encoding='utf-8'))
    assert 'metrics' not in packet and 'quality' not in packet
    assert all(set(c)=={'case_id','report','units','evidence','source_context','evidence_source_ids'} for c in packet['cases'])
    assert packet['cases'][0]['report'].endswith('但资料不足。')
    assert form['reviewer']==form['reviewed_at']==''
    assert all(u['factual'] is None for c in form['cases'] for u in c['units'])
    assert all(l['quote_verdict'] is None and l['source_verdict'] is None for c in form['cases'] for u in c['units'] for l in u['links'])
    with pytest.raises(RecoveryError): export_reference(experiment,bundle)


def test_recorded_fixture_stays_separate_and_identity_self_declared(experiment,tmp_path):
    bundle,path,form=write_form(experiment,tmp_path)
    original=(experiment/'execution/result.json').read_bytes()
    result=import_reference(experiment,bundle,path,tmp_path/'accepted')
    assert result['identity']=='self_declared_not_authenticated' and result['promotion']=='none'
    assert len(result['comparisons'])==4
    assert all(len(r['differences'])==(0 if r['context'] else 1) for r in result['comparisons'])
    assert (experiment/'execution/result.json').read_bytes()==original
    assert json.loads((tmp_path/'accepted/submitted-review.json').read_text())==form


@pytest.mark.parametrize('change',['null','duplicate','missing_link','no_zone','no_reason','no_claim','applicability'])
def test_invalid_opinions_rejected_before_output(experiment,tmp_path,change):
    bundle,path,form=write_form(experiment,tmp_path)
    unit=form['cases'][0]['units'][0]
    if change=='null': unit['factual']=None
    elif change=='duplicate': form['cases'].append(form['cases'][0])
    elif change=='missing_link': unit['links']=[]
    elif change=='no_zone': form['reviewed_at']='2026-11-23T20:00:00'
    elif change=='no_reason': unit['reason']=' '
    elif change=='no_claim': unit['atomic_claims']=[]
    else: unit['links'][0]['quote_verdict']='not_applicable'
    path.write_text(json.dumps(form),encoding='utf-8')
    with pytest.raises(ValueError): import_reference(experiment,bundle,path,tmp_path/'accepted')
    assert not (tmp_path/'accepted').exists()


def test_empty_template_never_accepted(experiment,tmp_path):
    bundle=tmp_path/'bundle'
    export_reference(experiment,bundle)
    with pytest.raises(ValueError): import_reference(experiment,bundle,bundle/'review.json',tmp_path/'accepted')
    assert not (tmp_path/'accepted').exists()


def test_model_metrics_tampering_rejected(experiment,tmp_path):
    path=experiment/'execution/result.json'
    result=json.loads(path.read_text())
    result['rows'][0]['quality']['metrics']['evidence_coverage']=1
    path.write_text(json.dumps(result),encoding='utf-8')
    with pytest.raises(VerificationError): export_reference(experiment,tmp_path/'bundle')
    assert not (tmp_path/'bundle').exists()


def test_review_bound_to_unchanged_model_bytes(experiment,tmp_path):
    bundle,path,_=write_form(experiment,tmp_path)
    result=experiment/'execution/result.json'
    result.write_bytes(result.read_bytes()+b'\n')
    with pytest.raises(VerificationError): import_reference(experiment,bundle,path,tmp_path/'accepted')
    assert not (tmp_path/'accepted').exists()


def test_protected_output_refused(experiment,tmp_path):
    with pytest.raises(RecoveryError): export_reference(experiment,experiment/'review')
    with pytest.raises(RecoveryError): export_reference(experiment,tmp_path/'run/review')


def test_readonly_review_survives_code_upgrade_when_contract_inputs_unchanged(experiment,monkeypatch):
    from researchdesk import context_experiment
    original=build_packet(experiment)
    hashes=context_experiment.code_hashes()
    hashes['verification.py']='0'*64
    monkeypatch.setattr(context_experiment,'code_hashes',lambda:hashes)
    assert build_packet(experiment)==original
    with pytest.raises(RecoveryError): context_experiment.validate_plan(experiment)


def test_malformed_historical_code_metadata_rejected(experiment):
    path=experiment/'plan.json'
    plan=json.loads(path.read_text(encoding='utf-8'))
    plan['source_code_sha256']={'verification.py':'not-a-hash'}
    path.write_text(json.dumps(plan),encoding='utf-8')
    with pytest.raises(RecoveryError): build_packet(experiment)
