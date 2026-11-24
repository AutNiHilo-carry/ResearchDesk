"""Runner wiring tests, not report-quality experiments."""
from pathlib import Path
import pytest
from researchdesk import task_eval
from researchdesk.draft_inspection import policy_spec


@pytest.mark.parametrize('limit,final_turn',[(None,False),(200,False),(200,True)])
def test_optional_inspection_reaches_cli_without_changing_question_or_other_budgets(tmp_path,monkeypatch,limit,final_turn):
    commands=[]
    def run(command,cwd,log,timeout,environment):
        commands.append(command)
        return dict(returncode=1,timed_out=False,elapsed_seconds=0)
    monkeypatch.setattr(task_eval,'run_command',run)
    task=dict(id='sample',question='固定问题',generation_contract='body-length-v1',max_model_calls=5,report_policy='v2')
    if limit is not None: task['draft_inspection_limit']=limit
    if final_turn: task['draft_inspection_final_turn']=True
    record=task_eval.run_task(task,[],tmp_path/'task',240)
    command=commands[0]
    assert command[3]=='固定问题'+task_eval.task_suffix(task)
    assert command[command.index('--max-model-calls')+1]=='5'
    assert command[command.index('--max-tool-calls')+1]=='10'
    assert ('--draft-inspection-final-turn' in command) is final_turn
    if limit is None:
        assert '--draft-inspection-limit' not in command and 'draft_inspection_policy' not in record
    else:
        assert command[command.index('--draft-inspection-limit')+1]=='200'
        assert record['draft_inspection_policy']==policy_spec(200, final_turn=final_turn)


@pytest.mark.parametrize('limit',[True,0,100001,'200'])
def test_invalid_limit_refused_before_creating_task_directory(tmp_path,limit):
    with pytest.raises(ValueError): task_eval.run_task(dict(id='sample',question='q',draft_inspection_limit=limit),[],tmp_path/'task',240)
    assert not (tmp_path/'task').exists()


@pytest.mark.parametrize('options',[
    {'draft_inspection_final_turn': 'true', 'draft_inspection_limit': 200, 'max_model_calls': 5},
    {'draft_inspection_final_turn': True, 'max_model_calls': 5},
    {'draft_inspection_final_turn': True, 'draft_inspection_limit': 200, 'max_model_calls': 0},
])
def test_invalid_final_turn_is_refused_before_creating_task_directory(tmp_path,options):
    with pytest.raises(ValueError, match='Draft inspection final turn'):
        task_eval.run_task(dict(id='sample', question='q', **options), [], tmp_path/'task', 240)
    assert not (tmp_path/'task').exists()
