import json
from pathlib import Path

import pytest

from researchdesk import task_grading_replay as replay
from researchdesk.task_eval import TASK_SUFFIX
from researchdesk.task_grading_calibration import compile_cases, prepare
from researchdesk.verification import VerificationError, digest


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def fixture(tmp_path):
    pilot = tmp_path / 'pilot'
    run = pilot / 'execution/v1/tasks/example/research/run'
    run.mkdir(parents=True)
    body = 'One writer at a time.\n'
    (run / 'report.md').write_text(body + '\n## Evidence Ledger\nAppendix only.', encoding='utf-8')
    write(run / 'run.json', dict(status='completed', model='fixture', report_policy={'version': 'v1'}))
    (run / 'sources.jsonl').write_text('', encoding='utf-8')
    (run / 'evidence.jsonl').write_text('', encoding='utf-8')
    task = dict(id='example', question='Explain writers.', documents=['notes.md'],
                criteria=[dict(id='limit', description='States writer limit.')])
    plan = dict(version=1, data={}, tasks=[task], documents={'notes.md': dict(content='One writer.', sha256=digest('One writer.'))},
                model='fixture', max_model_calls=6, timeout=180)
    frozen = {k: plan[k] for k in ('data', 'tasks', 'documents', 'model', 'max_model_calls', 'timeout')}
    plan['controls'] = dict(input_sha256=digest(json.dumps(frozen, ensure_ascii=False, sort_keys=True)),
        task_suffix_sha256=digest(TASK_SUFFIX), report_policies={'v1': {'version': 'v1'}},
        source_code_sha256={name: digest(Path(replay.__file__).with_name(name).read_text(encoding='utf-8'))
                            for name in ('task_eval.py', 'verification.py')})
    record = dict(task_id='example', generation_status='completed', run_directory=str(run), model='fixture',
                  question_sha256=digest(task['question'] + TASK_SUFFIX))
    result = dict(controls=plan['controls'], arms={'v1': {'tasks': [record]}})
    write(pilot / 'plan.json', plan)
    write(pilot / 'execution/result.json', result)
    targets = tmp_path / 'targets.json'
    write(targets, dict(version='task-grading-replay-targets-v1', targets=[dict(
        arm='v1', task_id='example', criterion_id='limit', candidate_passed=True)]))
    return pilot, targets, run, body


def test_export_freezes_exact_body_and_omits_appendix_and_references_from_request(tmp_path):
    pilot, targets, run, body = fixture(tmp_path)
    before = (run / 'report.md').read_bytes()
    data = replay.export(pilot, targets, tmp_path / 'export')
    assert data['cases'][0]['report'] == body
    assert 'Appendix only.' not in data['cases'][0]['report']
    assert (run / 'report.md').read_bytes() == before
    origin = data['provenance']['reports'][0]
    assert origin['report_body_sha256'] == digest(body)
    assert origin['report_file_sha256'] == replay.file_digest(run / 'report.md')
    plan = prepare(tmp_path / 'export/dataset.json', tmp_path / 'prepared', model='fixture')
    assert plan['payload'] == compile_cases(data)[0]
    assert 'candidate_passed' not in json.dumps(plan['payload'])
    assert plan['controls']['max_sdk_requests'] == 2


@pytest.mark.parametrize('change', ['document', 'question', 'model', 'policy', 'path', 'extractor'])
def test_drift_or_wrong_origin_blocks_export(tmp_path, change):
    pilot, targets, run, _ = fixture(tmp_path)
    plan = replay.read_json(pilot / 'plan.json')
    result = replay.read_json(pilot / 'execution/result.json')
    if change == 'document':
        plan['documents']['notes.md']['content'] += 'Changed'
    elif change == 'extractor':
        plan['controls']['source_code_sha256']['verification.py'] = 'changed'
        result['controls'] = plan['controls']
    elif change == 'question':
        result['arms']['v1']['tasks'][0]['question_sha256'] = 'changed'
    elif change == 'path':
        result['arms']['v1']['tasks'][0]['run_directory'] = str(tmp_path)
    else:
        metadata = replay.read_json(run / 'run.json')
        metadata['model' if change == 'model' else 'report_policy'] = 'changed'
        write(run / 'run.json', metadata)
    write(pilot / 'plan.json', plan)
    write(pilot / 'execution/result.json', result)
    with pytest.raises(VerificationError):
        replay.export(pilot, targets, tmp_path / 'export')
    assert not (tmp_path / 'export').exists()


def test_export_does_not_truncate_oversized_report(tmp_path):
    pilot, targets, run, _ = fixture(tmp_path)
    (run / 'report.md').write_text('x' * 4001, encoding='utf-8')
    with pytest.raises(ValueError):
        replay.export(pilot, targets, tmp_path / 'export')
    assert not (tmp_path / 'export').exists()


def test_duplicate_targets_and_implicit_candidate_labels_are_rejected(tmp_path):
    pilot, targets, _, _ = fixture(tmp_path)
    selection = replay.read_json(targets)
    selection['targets'] *= 2
    write(targets, selection)
    with pytest.raises(VerificationError):
        replay.export(pilot, targets, tmp_path / 'export')
    selection['targets'] = [selection['targets'][0]]
    selection['targets'][0]['candidate_passed'] = 'true'
    write(targets, selection)
    with pytest.raises(ValueError):
        replay.export(pilot, targets, tmp_path / 'export')


def test_export_is_one_shot_and_preserves_previous_dataset(tmp_path):
    pilot, targets, _, _ = fixture(tmp_path)
    replay.export(pilot, targets, tmp_path / 'export')
    before = (tmp_path / 'export/dataset.json').read_bytes()
    with pytest.raises(FileExistsError):
        replay.export(pilot, targets, tmp_path / 'export')
    assert (tmp_path / 'export/dataset.json').read_bytes() == before
