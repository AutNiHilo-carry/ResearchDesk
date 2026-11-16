import copy
import json
from pathlib import Path
import pytest
from researchdesk import inference_check as module
from researchdesk.inference import create_trace
from researchdesk.ledger import Ledger
from researchdesk.storage import atomic_text
from researchdesk.verification import VerificationError


@pytest.fixture
def case(tmp_path):
    run = tmp_path/'run'
    ledger = Ledger(run)
    atomic_text(run/'run.json', json.dumps(dict(status='completed')))
    a = ledger.add_source('Guide', 'fixture://guide', 'Guide says recovery is supported.', 'local')
    b = ledger.add_source('Release', 'fixture://release', 'Release says recovery is unsupported.', 'local')
    ea = ledger.add_evidence('Guide statement', a['id'], a['content'])
    eb = ledger.add_evidence('Release statement', b['id'], b['content'])
    ledger.save_report(f"# Fixture\nThe two sources conflict.[{ea['id']}][{eb['id']}]")
    ledger.store.close()
    trace = create_trace(run)
    trace['claims'][1].update(kind='synthesis', rationale='Candidate linkage, not authenticated labels.', premises=[
        dict(id='P1', statement=a['content'], evidence_ids=[ea['id']]),
        dict(id='P2', statement=b['content'], evidence_ids=[eb['id']])])
    payload = module.prepare(run, trace, ['U2'])
    return run, trace, payload


def judgment(payload):
    c = payload['conclusions'][0]
    return dict(units=[dict(unit_id=c['unit_id'], premises=[dict(premise_id=p['id'], support='supported',
        anchors=[dict(evidence_id=eid, quote=payload['evidence'][eid]['snippet']) for eid in p['evidence_ids']],
        reason='Scripted exact support') for p in c['premises']], scope='consistent', inference='valid',
        strength='within_evidence', reason='Scripted result, not model or human quality validation')])


def request_dir(tmp_path, case):
    run, trace, _ = case
    request = module.create_request(run, trace, ['U2'], 'fixture')
    directory = tmp_path/'request'
    directory.mkdir()
    atomic_text(directory/'request.json', json.dumps(request))
    return directory


def test_selected_complete_claim_can_be_checked_without_omitting_rest_of_report(case):
    run, trace, payload = case
    assert trace['claims'][0]['kind'] == 'unclassified'
    assert payload['selected_units'] == ['U2'] and len(payload['report_units']) == 2
    assert len(payload['sources']) == 2
    result = module.assess(payload, judgment(payload))
    assert result['units'][0]['assessment'] == 'model_support_established'
    assert result['human_review'] == 'pending' and result['affects_original_scores'] is False
    assert 'citation_precision' not in result and 'task_success_rate' not in result


@pytest.mark.parametrize('field,value', [('scope','inconsistent'), ('scope','uncertain'),
    ('inference','invalid'), ('inference','uncertain'), ('strength','overstated'), ('strength','uncertain')])
def test_supported_quotes_cannot_override_scope_inference_or_strength_failure(case, field, value):
    _, _, payload = case
    output = judgment(payload)
    output['units'][0][field] = value
    assert module.assess(payload, output)['units'][0]['assessment'] == 'model_support_not_established'


@pytest.mark.parametrize('support', ['partial','contradicted','insufficient'])
def test_supported_conclusion_cannot_override_missing_premise_support(case, support):
    _, _, payload = case
    output = judgment(payload)
    output['units'][0]['premises'][0]['support'] = support
    assert module.assess(payload, output)['units'][0]['assessment'] == 'model_support_not_established'


@pytest.mark.parametrize('change', ['unit_omitted','unit_duplicate','premise_omitted','premise_invented',
    'anchor_invented','anchor_duplicate','quote_invented','supported_without_anchor','blank_reason','extra_score'])
def test_strict_semantic_output_rejects_missing_ids_fabricated_anchors_and_scores(case, change):
    _, _, payload = case
    output = judgment(payload)
    unit = output['units'][0]
    premise = unit['premises'][0]
    if change == 'unit_omitted': output['units'] = []
    elif change == 'unit_duplicate': output['units'].append(copy.deepcopy(unit))
    elif change == 'premise_omitted': unit['premises'].pop()
    elif change == 'premise_invented': premise['premise_id'] = 'P99'
    elif change == 'anchor_invented': premise['anchors'][0]['evidence_id'] = 'Eunknown'
    elif change == 'anchor_duplicate': premise['anchors'].append(copy.deepcopy(premise['anchors'][0]))
    elif change == 'quote_invented': premise['anchors'][0]['quote'] = 'Invented quote'
    elif change == 'supported_without_anchor': premise['anchors'][0]['quote'] = ''
    elif change == 'blank_reason': unit['reason'] = '   '
    else: output['task_success_rate'] = 1
    with pytest.raises(ValueError):
        module.assess(payload, output)


@pytest.mark.parametrize('selected', [[], ['U2','U2'], ['unknown'], ['U1']])
def test_invalid_or_incomplete_selection_stops_offline(case, selected):
    run, trace, _ = case
    with pytest.raises(VerificationError):
        module.prepare(run, trace, selected)


def test_frozen_request_drift_stops_before_any_judge_or_output(case, tmp_path, monkeypatch):
    directory = request_dir(tmp_path, case)
    monkeypatch.setattr(module, 'code_hashes', lambda: {'changed': 'yes'})
    with pytest.raises(VerificationError):
        module.execute(directory, judge=lambda _: pytest.fail('No request allowed'))
    assert not (directory/'execution').exists()


def test_frozen_payload_cannot_be_replaced_before_execution(case, tmp_path):
    directory = request_dir(tmp_path, case)
    path = directory/'request.json'
    request = json.loads(path.read_text())
    request['payload']['conclusions'][0]['text'] = 'Substituted claim'
    atomic_text(path, json.dumps(request))
    with pytest.raises(VerificationError): module.execute(directory, judge=lambda _: pytest.fail('No request allowed'))
    assert not (directory/'execution').exists()


def test_user_scenario_question_is_bound_separately_from_product_evidence(case, tmp_path):
    run, trace, _ = case
    metadata = json.loads((run/'run.json').read_text())
    metadata['question'] = 'Assume a fully offline user scenario.'
    atomic_text(run/'run.json', json.dumps(metadata))
    directory = request_dir(tmp_path, case)
    request = json.loads((directory/'request.json').read_text())
    assert request['payload']['task_question'] == metadata['question']
    metadata['question'] = 'A different scenario'
    atomic_text(run/'run.json', json.dumps(metadata))
    with pytest.raises(VerificationError): module.execute(directory, judge=lambda _: pytest.fail('No request allowed'))


def test_full_source_budget_is_checked_without_truncation(case):
    run, trace, _ = case
    ledger = Ledger(run, resume=True)
    ledger.add_source('Large unreferenced context', 'fixture://large', 'x'*60001, 'local')
    ledger.store.close()
    trace = create_trace(run)
    # A new trace retains the candidate binding, but binds the entire changed snapshot.
    _, old_trace, _ = case
    trace['claims'][1].update({key: old_trace['claims'][1][key] for key in ('kind','premises','rationale')})
    with pytest.raises(VerificationError, match='60000'): module.prepare(run, trace, ['U2'])


def test_single_request_is_marked_before_call_and_success_is_separate(case, tmp_path):
    directory = request_dir(tmp_path, case)
    calls = []
    def judge(payload):
        state = json.loads((directory/'execution/request-state.json').read_text())
        assert state['status'] == 'request_started'
        calls.append(1)
        return judgment(payload)
    result = module.execute(directory, judge=judge)
    assert result['execution_mode'] == 'injected_offline' and len(calls) == 1
    state = json.loads((directory/'execution/request-state.json').read_text())
    assert state['status'] == 'complete' and state['usage'] is None
    with pytest.raises(FileExistsError): module.execute(directory, judge=judge)
    assert len(calls) == 1


def test_bad_output_retained_and_not_retried_or_accepted(case, tmp_path):
    directory = request_dir(tmp_path, case)
    with pytest.raises(ValueError): module.execute(directory, judge=lambda _: {'units': []})
    assert (directory/'execution/unaccepted-output.json').is_file()
    assert not (directory/'execution/result.json').exists()
    assert json.loads((directory/'execution/request-state.json').read_text())['status'] == 'failed'
    with pytest.raises(FileExistsError): module.execute(directory, judge=lambda _: pytest.fail('No paid retry'))


def test_interruption_remains_unknown_and_is_not_refunded(case, tmp_path):
    directory = request_dir(tmp_path, case)
    def stop(payload): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt): module.execute(directory, judge=stop)
    state = json.loads((directory/'execution/request-state.json').read_text())
    assert state['status'] == 'interrupted' and state['usage'] is None
    with pytest.raises(FileExistsError): module.execute(directory, judge=stop)


def test_model_adapter_has_no_sdk_retries_and_rejects_truncation(monkeypatch):
    import langchain_deepseek
    from types import SimpleNamespace
    settings = {}
    class Client:
        def __init__(self, **kwargs): settings.update(kwargs)
        def bind(self, **kwargs): return self
        def invoke(self, messages):
            assert messages[0]['content'] == module.PROMPT
            return SimpleNamespace(content='{}', usage_metadata={'total_tokens': 7}, response_metadata={'finish_reason':'length'})
    monkeypatch.setattr(langchain_deepseek, 'ChatDeepSeek', Client)
    judge = module.ModelJudge('fixture')
    with pytest.raises(VerificationError, match='truncated'): judge({})
    assert settings['max_retries'] == 0 and settings['timeout'] == 90 and judge.usage['total_tokens'] == 7


def test_invalid_json_response_is_retained_with_usage_for_inspection(case, tmp_path):
    directory = request_dir(tmp_path, case)
    class BadJSON:
        raw_content = None
        usage = None
        def __call__(self, payload):
            self.raw_content = 'not valid JSON'
            self.usage = {'total_tokens': 7}
            return json.loads(self.raw_content)
    with pytest.raises(ValueError): module.execute(directory, judge=BadJSON())
    assert json.loads((directory/'execution/raw-response.json').read_text()) == 'not valid JSON'
    assert json.loads((directory/'execution/request-state.json').read_text())['usage']['total_tokens'] == 7
    assert not (directory/'execution/result.json').exists()
