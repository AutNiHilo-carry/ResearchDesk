"""Scripted full-batch stability preflight; no real provider or human labels."""
import copy
import json
import sys
from pathlib import Path
import pytest
from researchdesk import judge_stability as stability, task_eval
from researchdesk.task_grading import TASK_PROMPT
from researchdesk.verification import VerificationError, SYSTEM_PROMPT_V2, digest
from test_task_eval import task_data, fixture_runner, RubricJudge, EvidenceJudge


@pytest.fixture(params=['legacy', 'body-length-v1'])
def completed_batch(tmp_path, request):
    data, tasks, documents = task_data()
    task = dict(tasks[0], generation_contract=request.param)
    def runner(task, paths, directory, timeout):
        record = fixture_runner(task, paths, directory, timeout)
        manifest = Path(record['run_directory']) / 'run.json'
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        metadata.update(model='fixture', question=task['question'] + task_eval.task_suffix(task))
        manifest.write_text(json.dumps(metadata), encoding='utf-8')
        record.update(question_sha256=digest(metadata['question']))
        if request.param != 'legacy':
            record['generation_contract'] = task_eval.generation_contract_spec(task)
        return record
    rubric = RubricJudge()
    rubric.prompt_sha256 = digest(TASK_PROMPT)
    rubric.prompt_version = 'research-task-rubric-v1'
    judge = EvidenceJudge()
    judge.prompt_sha256 = digest(SYSTEM_PROMPT_V2)
    judge.prompt_version = 'report-quote-v2'
    directory = tmp_path / 'evaluation'
    result = task_eval.evaluate_batch(data, [task], documents, directory, runner=runner,
        model_name='fixture', task_judge=rubric, evidence_judge=judge)
    assert result['status'] == 'complete'
    dataset = Path(__file__).parents[1] / 'evals/research-tasks-v1.json'
    return directory, dataset, task, result


def test_full_batch_stability_accepts_recorded_generation_contract(completed_batch):
    directory, dataset, task, result = completed_batch
    payload, controls = stability.frozen_input(directory, dataset)
    assert stability.payload_hash(payload) == result['quality_input_sha256']
    assert controls['generation_contracts'] == result['generation_contracts']
    assert controls['prompt_sha256'] == digest(SYSTEM_PROMPT_V2)
    assert {u['task_id'] for u in payload['units']} == {task['id']}


@pytest.mark.parametrize('mutation', ['generation', 'question', 'contract', 'prompt', 'labels', 'corpus'])
def test_stability_preflight_rejects_drift(completed_batch, mutation):
    directory, dataset, task, result = completed_batch
    if mutation == 'generation':
        result['task_prompt_suffix_sha256'] = '0' * 64
        (directory / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    elif mutation == 'question':
        path = Path(result['tasks'][0]['run_directory']) / 'run.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['question'] += ' changed'
        path.write_text(json.dumps(manifest), encoding='utf-8')
    elif mutation == 'contract':
        result['generation_contracts'][task['id']]['version'] = 'future-unknown'
        (directory / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    elif mutation == 'prompt':
        result['citation_judge_prompt_sha256'] = '0' * 64
        (directory / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    elif mutation == 'labels':
        path = directory / 'citation-quality.json'
        quality = json.loads(path.read_text(encoding='utf-8'))
        quality['metrics']['citation_precision'] = 0
        path.write_text(json.dumps(quality), encoding='utf-8')
    else:
        next((directory / 'corpus').glob('*.md')).write_text('Changed source', encoding='utf-8')
    with pytest.raises(VerificationError):
        stability.frozen_input(directory, dataset)


def test_stability_does_not_impose_unrelated_business_grader_prompt(completed_batch):
    from researchdesk.task_grading import TASK_PROMPT_V2
    directory, dataset, _, result = completed_batch
    result['task_grader_prompt_sha256'] = digest(TASK_PROMPT_V2)
    (directory / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    payload, _ = stability.frozen_input(directory, dataset)
    assert stability.payload_hash(payload) == result['quality_input_sha256']


def test_stability_cli_uses_exact_frozen_batch_without_real_api(completed_batch, tmp_path, monkeypatch):
    directory, dataset, _, result = completed_batch
    before = {p: p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    calls = []
    class ScriptedJudge(EvidenceJudge):
        prompt_sha256 = digest(SYSTEM_PROMPT_V2)
        prompt_version = 'report-quote-v2'
        usage = dict(input_tokens=1, output_tokens=2, total_tokens=3)
        def __call__(self, payload):
            calls.append(copy.deepcopy(payload))
            return super().__call__(payload)
    monkeypatch.setattr(stability, 'load_dotenv', lambda: None)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fixture-not-a-real-key')
    monkeypatch.setenv('DEEPSEEK_MODEL', 'fixture')
    monkeypatch.setattr(stability, 'ModelJudge', lambda *a, **k: ScriptedJudge())
    output = tmp_path / 'stability'
    monkeypatch.setattr(sys, 'argv', ['judge_stability', str(directory), '--dataset', str(dataset),
                                    '--repeats', '2', '--output', str(output)])
    assert stability.main() == 0
    assert len(calls) == 2 and calls[0] == calls[1]
    assert stability.payload_hash(calls[0]) == result['quality_input_sha256']
    assert {p: p.read_bytes() for p in directory.rglob('*') if p.is_file()} == before
