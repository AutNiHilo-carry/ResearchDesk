"""Demo preparation and injected execution tests; never independent human quality labels."""
import importlib.util
import json
from pathlib import Path
import pytest
from researchdesk import report_eval
from researchdesk.storage import RecoveryError
from researchdesk.task_eval import evaluate_batch
from researchdesk.task_eval import task_suffix,generation_contract_spec
from researchdesk.verification import digest
from test_task_eval import fixture_runner,RubricJudge,EvidenceJudge


def script():
    path=Path(__file__).parents[1]/'scripts/portfolio_demo.py'
    spec=importlib.util.spec_from_file_location('portfolio_demo_for_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_five_cases_have_bound_public_sources_and_explicit_scope():
    module=script()
    data,tasks,path=module.check_sources()
    assert len(tasks)==5 and len(module.read(path)['sources'])==6
    assert data['label_status']=='project-authored candidate rubrics; pending independent human review'
    assert 'not an independent held-out benchmark' in data['scope']
    assert [t['id'] for t in tasks[:3]]==module.SELECTED


def test_prepare_has_no_credentials_or_calls_and_freezes_28_request_cap(tmp_path,monkeypatch):
    module=script()
    monkeypatch.setattr(module,'load_dotenv',lambda *a:pytest.fail('Preparation must not load credentials'))
    monkeypatch.setattr(report_eval,'evaluate_batch',lambda *a,**k:pytest.fail('No model evaluation'))
    root=tmp_path/'plan'
    metadata=module.prepare(root,model='fixture')
    assert metadata['max_sdk_requests']==28 and len(metadata['not_executed'])==2
    assert not (root/'execution').exists()
    assert module.validate_plan(root)==metadata
    with pytest.raises(RecoveryError): module.prepare(root)


@pytest.mark.parametrize('mutation',['dataset_sha256','registry_sha256','runner_sha256','selected','plan'])
def test_modified_preparation_cannot_load_keys_or_dispatch(tmp_path,monkeypatch,mutation):
    module=script()
    root=tmp_path/'plan'
    module.prepare(root,model='fixture')
    if mutation=='plan':
        path=root/'plan.json'
        value=module.read(path)
        value['tasks'][0]['question']='Changed question'
    else:
        path=root/'portfolio.json'
        value=module.read(path)
        value[mutation]=[] if mutation=='selected' else '0'*64
    path.write_text(json.dumps(value),encoding='utf-8')
    monkeypatch.setattr(module,'load_dotenv',lambda *a:pytest.fail('Reject before keys'))
    with pytest.raises(RecoveryError): module.execute(root)
    assert not (root/'execution').exists()


def test_existing_execution_is_refused_before_loading_credentials(tmp_path,monkeypatch):
    module=script()
    root=tmp_path/'plan'
    module.prepare(root,model='fixture')
    (root/'execution').mkdir()
    monkeypatch.setattr(module,'load_dotenv',lambda *a:pytest.fail('No repeated paid execution'))
    with pytest.raises(RecoveryError): module.execute(root)


def test_incomplete_scoring_stays_unknown_and_reserved_cases_stay_unexecuted(tmp_path):
    module=script()
    root=tmp_path/'plan'
    module.prepare(root,model='fixture')
    (root/'execution').mkdir()
    (root/'execution/result.json').write_text(json.dumps(dict(status='failed',arms={},execution_mode='injected_offline')),encoding='utf-8')
    result=module.package(root,tmp_path/'showcase')
    assert all(r['application_gate']=='unknown' and r['status']=='not_packaged' for r in result['packages'].values())
    assert len(result['not_executed'])==2 and result['api_requests']==0
    assert 'not_executed' in (tmp_path/'showcase/index.md').read_text(encoding='utf-8')


def test_injected_complete_batches_are_packaged_without_new_scoring(tmp_path,monkeypatch):
    module=script()
    root=tmp_path/'plan'
    module.prepare(root,model='fixture')
    def runner(task,paths,directory,timeout):
        record=fixture_runner(task,paths,directory,timeout)
        path=Path(record['run_directory'])/'run.json'
        manifest=json.loads(path.read_text(encoding='utf-8'))
        question=task['question']+task_suffix(task)
        manifest.update(model='fixture',question=question)
        path.write_text(json.dumps(manifest),encoding='utf-8')
        record.update(generation_contract=generation_contract_spec(task),question_sha256=digest(question))
        return record
    def injected(data,tasks,documents,artifact,**kwargs):
        return evaluate_batch(data,tasks,documents,artifact,runner=runner,model_name='fixture',
                              task_judge=RubricJudge(),evidence_judge=EvidenceJudge())
    report_eval.execute_plan(root,evaluator=injected)
    monkeypatch.setattr(module,'load_dotenv',lambda *a:pytest.fail('No credentials during packaging'))
    result=module.package(root,tmp_path/'showcase')
    assert result['execution_mode']=='injected_offline' and result['api_requests']==0
    assert all(r['status']=='packaged' for r in result['packages'].values())
    assert all(r['application_gate']=='passed' for r in result['packages'].values())
    with pytest.raises(RecoveryError): module.package(root,tmp_path/'showcase')
