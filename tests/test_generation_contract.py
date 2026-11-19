"""Generator-counting contract bindings; offline fixtures are not quality experiments."""
import json
from pathlib import Path
import sys
import pytest
from researchdesk import task_eval, report_eval, task_grading_replay as replay
from researchdesk.report_constraints import policy
from researchdesk.verification import digest, VerificationError
from researchdesk.storage import RecoveryError
from test_task_eval import task_data, fixture_runner, RubricJudge, EvidenceJudge
from test_task_grading_replay import fixture as replay_fixture, write
from test_review import review_case, comparison_fixture

DATASET=Path(__file__).parents[1]/'evals/research-tasks-v1.json'


def test_legacy_suffix_bytes_and_default_questions_are_unchanged():
    assert digest(task_eval.TASK_SUFFIX)=='2efc1128f369918ed390da59a0e80f5a69c91c7df408a39f4e477c9af44fce5a'
    assert task_eval.task_suffix({})==task_eval.task_suffix(dict(generation_contract='legacy'))==task_eval.TASK_SUFFIX


def test_counting_instruction_is_opt_in_and_binds_the_validator_policy():
    task=dict(generation_contract='body-length-v1')
    spec=task_eval.generation_contract_spec(task)
    suffix=task_eval.task_suffix(task)
    assert suffix.startswith(task_eval.TASK_SUFFIX+'\n') and suffix.endswith(spec['instruction'])
    assert spec['suffix_sha256']==digest(suffix) and spec['constraint_policy']==policy()
    assert '200' in suffix and 'Unicode' in suffix and '16位小写十六进制' in suffix
    assert task_eval.common_suffix_hash([{},task]) is None


@pytest.mark.parametrize('version',['future',None,{},True])
def test_unknown_contract_is_rejected_before_creating_a_run(tmp_path,version):
    with pytest.raises(VerificationError):
        task_eval.run_task(dict(id='x',question='q',generation_contract=version),[],tmp_path/'run',30)
    assert not (tmp_path/'run').exists()


def test_actual_generation_command_and_record_bind_the_complete_question_without_rubric_leak(tmp_path,monkeypatch):
    task=dict(id='case',question='Question',generation_contract='body-length-v1',runtime_model='fixture',
              criteria=[dict(description='PRIVATE_RUBRIC_MARKER')])
    captured=[]
    def command(args,cwd,log,timeout,environment):
        captured.append(args)
        return dict(returncode=1,timed_out=False,elapsed_seconds=0)
    monkeypatch.setattr(task_eval,'run_command',command)
    result=task_eval.run_task(task,[],tmp_path/'task',30)
    assert captured[0][3]==task['question']+task_eval.task_suffix(task)
    assert 'PRIVATE_RUBRIC_MARKER' not in json.dumps(captured[0])
    assert result['question_sha256']==digest(captured[0][3])
    assert result['generation_contract']==task_eval.generation_contract_spec(task)


@pytest.mark.parametrize('mutation',['task','binding'])
def test_opt_in_report_plan_freezes_same_contract_for_both_arms_and_refuses_drift(tmp_path,monkeypatch,mutation):
    monkeypatch.setattr(report_eval,'load_dotenv',lambda:pytest.fail('No credentials'))
    root=tmp_path/'plan'
    plan=report_eval.create_plan(DATASET,root,model='fixture',limit=1,generation_contract='body-length-v1')
    task=plan['tasks'][0]
    assert 'generation_contract' not in plan['data']['tasks'][0]
    assert plan['controls']['generation_contracts'][task['id']]==task_eval.generation_contract_spec(task)
    assert plan['controls']['task_suffix_sha256']==digest(task_eval.task_suffix(task))
    assert plan['controls']['max_sdk_requests']==16
    if mutation=='task':
        task['generation_contract']='legacy'
    else:
        plan['controls']['generation_contracts'][task['id']]['instruction']='Changed'
    (root/'plan.json').write_text(json.dumps(plan),encoding='utf-8')
    with pytest.raises(RecoveryError):
        report_eval.execute_plan(root,evaluator=lambda *a,**k:pytest.fail('No paid request on drift'))
    assert not (root/'execution').exists()


@pytest.mark.parametrize('bind',[True,False])
def test_evaluation_refuses_uninformed_opt_in_generator_and_records_actual_suffix(tmp_path,bind):
    data,tasks,docs=task_data()
    selected=[dict(tasks[0],generation_contract='body-length-v1')]
    def runner(task,paths,directory,timeout):
        result=fixture_runner(task,paths,directory,timeout)
        if bind:
            result['generation_contract']=task_eval.generation_contract_spec(task)
            result['question_sha256']=digest(task['question']+task_eval.task_suffix(task))
            manifest=Path(result['run_directory'])/'run.json'
            raw=json.loads(manifest.read_text(encoding='utf-8'))
            raw['question']=task['question']+task_eval.task_suffix(task)
            manifest.write_text(json.dumps(raw),encoding='utf-8')
        return result
    def no_grade(_):
        pytest.fail('Unbound generator must not reach either grader')
    result=task_eval.evaluate_batch(data,selected,docs,tmp_path/'eval',runner=runner,model_name='fixture',
        task_judge=RubricJudge() if bind else no_grade,evidence_judge=EvidenceJudge() if bind else no_grade)
    assert result['task_prompt_suffix_sha256']==digest(task_eval.task_suffix(selected[0]))
    assert result['generation_contracts'][selected[0]['id']]==task_eval.generation_contract_spec(selected[0])
    assert result['tasks'][0]['generation_status']==('completed' if bind else 'invalid_artifacts')


@pytest.mark.parametrize('remove_binding',[False,True])
def test_replay_reconstructs_saved_opt_in_question_and_rejects_missing_binding(tmp_path,remove_binding):
    pilot,targets,run,body=replay_fixture(tmp_path)
    plan=replay.read_json(pilot/'plan.json')
    result=replay.read_json(pilot/'execution/result.json')
    task=plan['tasks'][0]
    task['generation_contract']='body-length-v1'
    plan['controls']['generation_contracts']={task['id']:task_eval.generation_contract_spec(task)}
    plan['controls']['task_suffix_sha256']=digest(task_eval.task_suffix(task))
    frozen={k:plan[k] for k in ('data','tasks','documents','model','max_model_calls','timeout')}
    plan['controls']['input_sha256']=digest(json.dumps(frozen,ensure_ascii=False,sort_keys=True))
    result['controls']=plan['controls']
    record=result['arms']['v1']['tasks'][0]
    record.update(question_sha256=digest(task['question']+task_eval.task_suffix(task)),
                  generation_contract=task_eval.generation_contract_spec(task))
    metadata=replay.read_json(run/'run.json')
    metadata['question']=task['question']+task_eval.task_suffix(task)
    write(run/'run.json',metadata)
    if remove_binding:
        plan['controls'].pop('generation_contracts')
    write(pilot/'plan.json',plan)
    write(pilot/'execution/result.json',result)
    if remove_binding:
        with pytest.raises(VerificationError):
            replay.export(pilot,targets,tmp_path/'export')
        assert not (tmp_path/'export').exists()
    else:
        assert replay.export(pilot,targets,tmp_path/'export')['cases'][0]['report']==body


@pytest.mark.parametrize('mode',['--check-result','--summarize'])
def test_offline_commands_cannot_replace_the_generation_contract(monkeypatch,mode):
    monkeypatch.setattr(sys,'argv',['task_eval',mode,'result.json','--generation-contract','body-length-v1'])
    with pytest.raises(SystemExit) as error:
        task_eval.main()
    assert error.value.code==2


def test_counting_instruction_is_included_in_preflight_generation_budget(tmp_path):
    data,_,_=task_data()
    task=dict(id='case',question='q',documents=['notes.md'],criteria=[],generation_contract='body-length-v1')
    content='x'*(20000-len('q'+task_eval.TASK_SUFFIX))
    documents={'notes.md':dict(content=content,sha256=digest(content))}
    with pytest.raises(VerificationError,match='generation character budget'):
        task_eval.evaluate_batch(data,[task],documents,tmp_path/'eval',
            runner=lambda *a,**k:pytest.fail('No generation before preflight'))
    assert not (tmp_path/'eval').exists()


def test_controlled_comparison_rejects_changed_generation_metadata(review_case,tmp_path):
    from researchdesk.evaluation_audit import compare_evaluations
    baseline,candidate=comparison_fixture(review_case,tmp_path)
    result=json.loads((candidate/'result.json').read_text(encoding='utf-8'))
    next(iter(result['generation_contracts'].values()))['version']='body-length-v1'
    (candidate/'result.json').write_text(json.dumps(result),encoding='utf-8')
    with pytest.raises(VerificationError,match='Generation contracts differ'):
        compare_evaluations(baseline,candidate)


def test_report_cli_prepares_opt_in_without_credentials_or_api(tmp_path,monkeypatch):
    monkeypatch.setattr(report_eval,'load_dotenv',lambda:pytest.fail('No credentials during plan preparation'))
    monkeypatch.setattr(sys,'argv',['report_eval','--dataset',str(DATASET),'--limit','1',
        '--generation-contract','body-length-v1','--output',str(tmp_path/'plans')])
    assert report_eval.main()==0
    root=next((tmp_path/'plans').iterdir())
    plan=json.loads((root/'plan.json').read_text(encoding='utf-8'))
    assert all(spec['version']=='body-length-v1' for spec in plan['controls']['generation_contracts'].values())
    assert not (root/'execution').exists()


@pytest.mark.parametrize('mode',['--execute-plan','--summarize-result'])
def test_saved_report_modes_cannot_override_generation_contract(monkeypatch,mode):
    monkeypatch.setattr(sys,'argv',['report_eval',mode,'saved','--generation-contract','body-length-v1'])
    with pytest.raises(SystemExit) as error:
        report_eval.main()
    assert error.value.code==2
