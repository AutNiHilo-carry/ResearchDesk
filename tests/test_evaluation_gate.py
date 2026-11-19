"""Automation consumes explicit gates; fixtures never establish semantic quality."""
import json
from pathlib import Path
import sys
import pytest
from researchdesk import task_eval
from researchdesk.verification import VerificationError
from test_task_eval import task_data, fixture_runner, RubricJudge, EvidenceJudge
from test_report_constraints import long_runner

DATASET = Path(__file__).parents[1]/'evals/research-tasks-v1.json'


def batch(tmp_path, *, long=False):
    data, tasks, docs = task_data()
    directory = tmp_path/'evaluation'
    result = task_eval.evaluate_batch(data, tasks[:1], docs, directory,
        runner=long_runner if long else fixture_runner, model_name='fixture',
        task_judge=RubricJudge(), evidence_judge=EvidenceJudge())
    return directory, result


def forbid_models(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail('No model or credentials during offline acceptance')
    monkeypatch.setattr(task_eval, 'load_dotenv', forbidden)
    monkeypatch.setattr(task_eval, 'ModelJudge', forbidden)
    monkeypatch.setattr(task_eval, 'TaskJudge', forbidden)


@pytest.mark.parametrize('long,gate,expected', [(False,'application',0), (True,'application',3), (True,'model',0)])
def test_bound_offline_cli_uses_selected_gate_and_leaves_inputs_unchanged(tmp_path, monkeypatch, long, gate, expected):
    directory, result = batch(tmp_path, long=long)
    before = {str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    forbid_models(monkeypatch)
    monkeypatch.setattr(sys, 'argv', ['task_eval', '--check-result', str(directory/'result.json'),
        '--dataset',str(DATASET),'--output',str(tmp_path/'gates'),'--gate',gate])
    assert task_eval.main() == expected
    output = next((tmp_path/'gates').iterdir())
    verdict = json.loads((output/'gate.json').read_text(encoding='utf-8'))
    provenance = json.loads((output/'provenance.json').read_text(encoding='utf-8'))
    assert verdict['exit_code'] == expected and verdict['gate'] == gate
    assert provenance['api_requests'] == 0 and provenance['source_sha256']
    assert verdict['human_review'] == verdict['final_acceptance'] == 'pending'
    assert before == {str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}


def test_legacy_success_cannot_be_promoted_to_application_pass(tmp_path):
    directory, result = batch(tmp_path)
    result.pop('constraint_policy')
    result['tasks'][0].pop('constraints')
    (directory/'result.json').write_text(json.dumps(result),encoding='utf-8')
    app = task_eval.check_saved_result(directory/'result.json', DATASET, tmp_path/'app')
    model = task_eval.check_saved_result(directory/'result.json', DATASET, tmp_path/'model', gate='model')
    assert app['status'] == 'unknown' and app['exit_code'] == 3
    assert app['tasks'][0]['constraint_gate'] == 'not_checked'
    assert model['status'] == 'passed' and model['exit_code'] == 0


def test_cached_badges_and_batch_rate_do_not_override_actual_constraint_failure(tmp_path):
    _, result = batch(tmp_path, long=True)
    result['tasks'][0]['application_assessment'] = dict(application_gate='passed')
    result['task_success_rate'] = 1
    assert task_eval.result_gate(result)['status'] == 'failed'


@pytest.mark.parametrize('mutation', ['constraints', 'success', 'rubric', 'quote', 'task_grades', 'body'])
def test_offline_gate_rejects_inconsistent_saved_inputs_before_creating_verdict(tmp_path, mutation):
    directory, result = batch(tmp_path)
    record = result['tasks'][0]
    if mutation == 'constraints':
        record['constraints']['counted_chars'] += 1
    elif mutation == 'success':
        record['success'] = False
    elif mutation == 'rubric':
        record['rubric']['rubric_passed'] = False
    elif mutation == 'quote':
        record['rubric']['criteria'][0]['report_quote'] = 'Invented report text.'
    elif mutation == 'task_grades':
        result['task_grades'] = {}
    else:
        run = Path(record['run_directory'])
        (run/'draft.md').write_text('Changed body.',encoding='utf-8')
    (directory/'result.json').write_text(json.dumps(result),encoding='utf-8')
    with pytest.raises((VerificationError, ValueError)):
        task_eval.check_saved_result(directory/'result.json', DATASET, tmp_path/'rejected')
    assert not (tmp_path/'rejected').exists()


def test_gate_rejects_empty_or_duplicate_batches_and_preserves_unknown(tmp_path):
    _, result = batch(tmp_path)
    empty = dict(result, tasks=[])
    with pytest.raises(VerificationError):
        task_eval.result_gate(empty)
    duplicate = dict(result, tasks=result['tasks']*2)
    with pytest.raises(VerificationError):
        task_eval.result_gate(duplicate)
    result['tasks'][0]['success'] = None
    assert task_eval.result_gate(result)['status'] == 'unknown'
    assert task_eval.result_gate(result)['exit_code'] == 3


def test_completed_gate_output_cannot_be_overwritten(tmp_path):
    directory, _ = batch(tmp_path)
    output = tmp_path/'gate'
    task_eval.check_saved_result(directory/'result.json',DATASET,output)
    before = {p:p.read_bytes() for p in output.iterdir()}
    with pytest.raises(FileExistsError):
        task_eval.check_saved_result(directory/'result.json',DATASET,output)
    assert before == {p:p.read_bytes() for p in output.iterdir()}


@pytest.mark.parametrize('gate,expected', [('application',3), ('model',0), (None,0)])
def test_new_evaluation_cli_can_enforce_application_gate_without_changing_model_metric(tmp_path, monkeypatch, gate, expected):
    original = task_eval.evaluate_batch
    def offline_evaluate(data,tasks,documents,artifact,**kwargs):
        return original(data,tasks,documents,artifact,runner=long_runner,model_name='fixture',
            task_judge=RubricJudge(),evidence_judge=EvidenceJudge())
    monkeypatch.setattr(task_eval,'evaluate_batch',offline_evaluate)
    monkeypatch.setattr(task_eval,'load_dotenv',lambda:None)
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture-only')
    args = ['task_eval','--dataset',str(DATASET),'--limit','1','--output',str(tmp_path/'runs')]
    if gate is not None:
        args += ['--gate',gate]
    monkeypatch.setattr(sys,'argv',args)
    assert task_eval.main() == expected
    output = next((tmp_path/'runs').iterdir())
    assert json.loads((output/'result.json').read_text(encoding='utf-8'))['task_success_rate'] == 1
    verdict = json.loads((output/'cli-gate.json').read_text(encoding='utf-8'))
    assert verdict['gate'] == (gate or 'model') and verdict['exit_code'] == expected


def test_summarize_cannot_be_mistaken_for_an_acceptance_command(monkeypatch):
    monkeypatch.setattr(sys,'argv',['task_eval','--summarize','result.json','--gate','application'])
    with pytest.raises(SystemExit) as error:
        task_eval.main()
    assert error.value.code == 2


@pytest.mark.parametrize('controls', [['--limit','1'], ['--timeout','1']])
def test_saved_gate_rejects_generation_controls_instead_of_silently_filtering_tasks(monkeypatch, controls):
    monkeypatch.setattr(sys,'argv',['task_eval','--check-result','result.json']+controls)
    with pytest.raises(SystemExit) as error:
        task_eval.main()
    assert error.value.code == 2
