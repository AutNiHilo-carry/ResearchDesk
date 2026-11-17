import copy
import json
from pathlib import Path

import pytest

from researchdesk import task_grading_calibration as calibration
from researchdesk.task_grading import TASK_PROMPT, TaskJudge, grade_tasks, task_prompt
from researchdesk.verification import VerificationError, digest

DATASET = Path(__file__).parents[1] / 'evals/task-grading-calibration-v1.json'


def prepare(tmp_path):
    root = tmp_path / 'calibration'
    plan = calibration.prepare(DATASET, root, model='fixture')
    return root, plan


def response(payload, passed=True):
    return {'tasks': [dict(task_id=t['task_id'], criteria=[dict(
        criterion_id=c['id'], passed=passed, report_quote=t['report'] if passed else '', reason='fixture')
        for c in t['criteria']]) for t in payload['tasks']]}


def test_existing_prompt_and_default_remain_unchanged():
    assert task_prompt('v1') == TASK_PROMPT
    assert digest(TASK_PROMPT) == 'c7230fb8c00b261a9dccb65d24a3a05a9139fc2d1a588f76c194bcbc4b3ad0b5'
    assert task_prompt('v2') != task_prompt('v1')
    with pytest.raises(VerificationError):
        task_prompt('unknown')


@pytest.mark.parametrize('passed', [True, False])
def test_document_quote_cannot_substitute_for_report_quote_even_on_failure(passed):
    payload = {'tasks': [dict(task_id='t', report='Report body.', documents=['Document only fact.'],
                             criteria=[dict(id='c', description='criterion')])]}
    answer = response(payload, passed)
    answer['tasks'][0]['criteria'][0]['report_quote'] = 'Document only fact.'
    with pytest.raises(VerificationError, match='invented a report quote'):
        grade_tasks(payload, lambda _: answer)


def test_positive_semantic_paraphrase_still_requires_verbatim_report_span():
    data = json.loads(DATASET.read_text(encoding='utf-8'))
    case = next(c for c in data['cases'] if c['id'] == 'semantic_paraphrase')
    payload, _ = calibration.compile_cases(dict(data, cases=[case]))
    assert grade_tasks(payload, lambda p: response(p))[case['id']]['rubric_passed']
    answer = response(payload)
    answer['tasks'][0]['criteria'][0]['report_quote'] = case['documents'][0]
    with pytest.raises(VerificationError):
        grade_tasks(payload, lambda _: answer)


def test_prepare_never_loads_credentials_or_constructs_judge(tmp_path, monkeypatch):
    monkeypatch.setattr(calibration, 'load_dotenv', lambda: pytest.fail('No credentials'))
    monkeypatch.setattr(calibration, 'TaskJudge', lambda *a, **k: pytest.fail('No API'))
    root, plan = prepare(tmp_path)
    assert plan['controls']['max_sdk_requests'] == 2
    assert set(plan['references'].values()) == {True, False}
    assert (root / 'plan.json').exists() and not (root / 'execution').exists()
    assert 'candidate_passed' not in json.dumps(plan['payload'])


@pytest.mark.parametrize('change', ['report', 'references', 'model', 'prompt'])
def test_drift_stops_before_constructing_model(tmp_path, monkeypatch, change):
    root, plan = prepare(tmp_path)
    if change == 'report':
        plan['payload']['tasks'][0]['report'] += 'Changed'
    elif change == 'references':
        key = next(iter(plan['references']))
        plan['references'][key] = not plan['references'][key]
    elif change == 'model':
        plan['model'] = 'changed'
    else:
        monkeypatch.setattr(calibration, 'task_prompt', lambda _: 'changed prompt')
    (root / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(VerificationError):
        calibration.execute(root, factory=lambda *a, **k: pytest.fail('No model allowed'))
    assert not (root / 'execution').exists()


def test_failed_arm_preserves_output_unknowns_and_does_not_retry(tmp_path):
    root, plan = prepare(tmp_path)
    calls = []
    class Judge:
        usage = {'total_tokens': 5}
        last_output = None
        def __init__(self, model, *, prompt_version):
            calls.append(prompt_version)
            self.version = prompt_version
        def __call__(self, payload):
            assert payload == plan['payload']
            self.last_output = response(payload)
            if self.version == 'v1':
                self.last_output['tasks'][0]['criteria'][0]['report_quote'] = 'Not in any report'
            return self.last_output
    result = calibration.execute(root, factory=Judge)
    assert calls == ['v1', 'v2']
    assert result['mode'] == 'injected_offline' and result['status'] == 'incomplete_scoring'
    assert result['arms']['v1']['candidate_agreement_rate'] is None
    assert result['arms']['v1']['raw_output'] and result['arms']['v1']['usage']['total_tokens'] == 5
    assert result['arms']['v2']['candidate_agreement_rate'] == sum(plan['references'].values()) / len(plan['references'])
    assert result['promotion'] == 'none' and result['default_prompt'] == 'v1'
    assert result['human_review'] == 'pending'
    with pytest.raises(FileExistsError):
        calibration.execute(root, factory=lambda *a, **k: pytest.fail('No duplicate calls'))


def test_interrupt_preserves_result_and_blocks_reexecution(tmp_path):
    root, _ = prepare(tmp_path)
    class Judge:
        def __init__(self, *a, **k):
            pass
        def __call__(self, payload):
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        calibration.execute(root, factory=Judge)
    result = json.loads((root / 'execution/result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'interrupted'
    with pytest.raises(FileExistsError):
        calibration.execute(root, factory=Judge)


def test_candidate_labels_are_required_and_strict():
    data = json.loads(DATASET.read_text(encoding='utf-8'))
    changed = copy.deepcopy(data)
    changed['cases'][0]['candidate_passed'] = 'true'
    with pytest.raises(ValueError):
        calibration.compile_cases(changed)
    changed = copy.deepcopy(data)
    changed['cases'].append(changed['cases'][0])
    with pytest.raises(VerificationError):
        calibration.compile_cases(changed)


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_model_request_uses_selected_prompt_and_no_sdk_retries(monkeypatch, version):
    import langchain_deepseek
    from types import SimpleNamespace
    captured = {}
    class Model:
        def __init__(self, **kwargs):
            captured['settings'] = kwargs
        def bind(self, **kwargs):
            captured['binding'] = kwargs
            return self
        def invoke(self, messages):
            captured['messages'] = messages
            return SimpleNamespace(usage_metadata={'total_tokens': 1}, response_metadata={}, content='{"tasks":[]}')
    monkeypatch.setattr(langchain_deepseek, 'ChatDeepSeek', Model)
    judge = TaskJudge('fixture', prompt_version=version)
    assert judge({'tasks': []}) == {'tasks': []}
    assert captured['settings']['max_retries'] == 0
    assert captured['messages'][0]['content'] == task_prompt(version)
    assert json.loads(captured['messages'][1]['content']) == {'tasks': []}
    assert judge.prompt_sha256 == digest(task_prompt(version))


def test_between_arm_drift_stops_second_request_and_keeps_first(tmp_path, monkeypatch):
    root, _ = prepare(tmp_path)
    calls = []
    class Judge:
        def __init__(self, model, *, prompt_version):
            calls.append(prompt_version)
        def __call__(self, payload):
            monkeypatch.setattr(calibration, 'task_prompt', lambda _: 'drifted')
            return response(payload)
    with pytest.raises(VerificationError):
        calibration.execute(root, factory=Judge)
    saved = json.loads((root / 'execution/result.json').read_text(encoding='utf-8'))
    assert calls == ['v1'] and saved['status'] == 'failed'
    assert saved['arms']['v1']['status'] == 'validated' and 'v2' not in saved['arms']
