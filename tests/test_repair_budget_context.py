import json
from pathlib import Path
import sys

import pytest
from langchain_core.messages import AIMessage

from researchdesk import repair
from researchdesk.ledger import Ledger
from researchdesk.model_budget import VERSION, budget_snapshot, model_budget_for
from researchdesk.storage import RecoveryError, atomic_text
from test_repair import source_run, Checker


def enable(source, maximum=4):
    path = source / 'run.json'
    metadata = json.loads(path.read_text(encoding='utf-8'))
    metadata['model_policy'] = dict(version=VERSION, max_calls=maximum)
    atomic_text(path, json.dumps(metadata))


def data(prepared):
    return json.loads(prepared['messages'][-1]['content'].rsplit('\n', 1)[-1])


def fixed_report(ledger, sid, eid):
    second = ledger.add_evidence('Fact B', sid, '事实B。')
    return f"事实A。[{eid}]\n事实B。[{second['id']}]"


def test_snapshot_excludes_verification_hold_and_keeps_source_unchanged(source_run, tmp_path):
    source, sid, eid, _, _ = source_run
    enable(source)
    before = (source / 'run.json').read_bytes()
    seen = []
    def graph(ledger, prepared, *a, **k):
        hint = data(prepared)['budget_context']
        seen.append(hint)
        assert hint['model'] == dict(limit=4, admitted_calls=1, remaining=3, verification_reserved=1, generation_remaining=2)
        assert hint['tools']['used'] == 1 and hint['tools']['remaining'] == 3
        assert hint['tools']['provider_requests_remaining'] == 0
        budget = model_budget_for(ledger)
        budget.admit('generation', 'generation')
        budget.finish('generation', 'completed')
        return {'messages': [AIMessage(content=fixed_report(ledger, sid, eid))]}
    destination = tmp_path / 'candidate'
    state = repair.repair_run(source, destination, checker=Checker(), graph_runner=graph, budget_context='snapshot-v1')
    assert state['selection']['accepted'] and len(seen) == 1
    assert state['budget_context_policy']['version'] == 'snapshot-v1' and state['repair_input_sha256']
    assert (source / 'run.json').read_bytes() == before
    assert repair.repair_run(None, destination, checker=Checker(), graph_runner=lambda *a, **k: pytest.fail('No repeat'), resume=True) == state


def test_resume_reuses_start_snapshot_instead_of_refreshing_remaining_quota(source_run, tmp_path):
    source, sid, eid, _, _ = source_run
    enable(source)
    destination = tmp_path / 'candidate'
    inputs = []
    def interrupted(ledger, prepared, *a, **k):
        inputs.append(prepared)
        budget = model_budget_for(ledger)
        budget.admit('first', 'generation')
        budget.finish('first', 'completed')
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        repair.repair_run(source, destination, checker=Checker(), graph_runner=interrupted, budget_context='snapshot-v1')
    frozen = (destination / 'repair-input.json').read_bytes()
    def resumed(ledger, prepared, *a, **k):
        assert prepared == inputs[0]
        assert data(prepared)['budget_context']['model']['generation_remaining'] == 2
        assert budget_snapshot(ledger.store.conn)['generation_remaining'] == 1
        budget = model_budget_for(ledger)
        budget.admit('second', 'generation')
        budget.finish('second', 'completed')
        return {'messages': [AIMessage(content=fixed_report(ledger, sid, eid))]}
    state = repair.repair_run(None, destination, checker=Checker(), graph_runner=resumed, resume=True)
    assert state['selection']['accepted']
    assert (destination / 'repair-input.json').read_bytes() == frozen


def test_frozen_feedback_does_not_spend_original_check_before_snapshot(source_run, tmp_path):
    source, sid, eid, _, feedback = source_run
    enable(source, 6)
    ledger = Ledger(source, resume=True)
    try:
        budget = model_budget_for(ledger)
        for index in range(3):
            budget.admit(f'prior-{index}', 'generation')
            budget.finish(f'prior-{index}', 'completed')
    finally:
        ledger.store.close()
    def graph(ledger, prepared, *a, **k):
        model = data(prepared)['budget_context']['model']
        assert model['admitted_calls'] == 3 and model['generation_remaining'] == 2
        assert model['verification_reserved'] == 1
        budget = model_budget_for(ledger)
        budget.admit('repair-generation', 'generation')
        budget.finish('repair-generation', 'completed')
        return {'messages': [AIMessage(content=fixed_report(ledger, sid, eid))]}
    checker = Checker()
    state = repair.repair_run(source, tmp_path / 'cached', feedback_rows=feedback, checker=checker,
                              graph_runner=graph, budget_context='snapshot-v1')
    assert state['selection']['accepted'] and checker.calls == 1  # Only the candidate is checked.
    ledger = Ledger(source, resume=True)
    try:
        assert budget_snapshot(ledger.store.conn)['reserved'] == 3
    finally:
        ledger.store.close()


@pytest.mark.parametrize('mutation', ['input', 'version', 'prompt', 'downgrade', 'missing_input', 'unbound'])
def test_changed_or_partial_budget_input_cannot_resume_model_calls(source_run, tmp_path, mutation):
    source, _, _, _, _ = source_run
    enable(source)
    destination = tmp_path / 'candidate'
    def interrupted(*a, **k):
        raise KeyboardInterrupt()
    checker = Checker()
    with pytest.raises(KeyboardInterrupt):
        repair.repair_run(source, destination, checker=checker, graph_runner=interrupted, budget_context='snapshot-v1')
    state_path = destination / 'repair-state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    if mutation == 'input':
        path = destination / 'repair-input.json'
        path.write_text(path.read_text(encoding='utf-8') + ' ', encoding='utf-8')
    elif mutation == 'version':
        state['budget_context_policy']['version'] = 'unknown'
    elif mutation == 'prompt':
        state['budget_context_policy']['prompt_sha256'] = 'changed'
    elif mutation == 'downgrade':
        state.pop('budget_context_policy')
        path = destination / 'run.json'
        metadata = json.loads(path.read_text(encoding='utf-8'))
        metadata.pop('budget_context_policy')
        atomic_text(path, json.dumps(metadata))
    elif mutation == 'missing_input':
        (destination / 'repair-input.json').unlink()
    else:
        state.pop('repair_input_sha256')
        state['rounds_used'] = 0
    atomic_text(state_path, json.dumps(state))
    calls = checker.calls
    with pytest.raises(RecoveryError):
        repair.repair_run(None, destination, checker=checker, graph_runner=lambda *a, **k: pytest.fail('No model'), resume=True)
    assert checker.calls == calls


def test_default_keeps_legacy_input_and_cannot_be_overridden_on_resume(source_run, tmp_path):
    source, sid, eid, _, _ = source_run
    def graph(ledger, prepared, *a, **k):
        assert 'budget_context' not in data(prepared)
        return {'messages': [AIMessage(content=fixed_report(ledger, sid, eid))]}
    destination = tmp_path / 'legacy'
    state = repair.repair_run(source, destination, checker=Checker(), graph_runner=graph)
    assert 'budget_context_policy' not in state and 'repair_input_sha256' not in state
    with pytest.raises(RecoveryError):
        repair.repair_run(None, destination, resume=True, budget_context='snapshot-v1')
    with pytest.raises(RecoveryError):
        repair.repair_run(source, tmp_path / 'unknown', budget_context='unknown')
    assert not (tmp_path / 'unknown').exists()


def test_missing_model_budget_stays_unknown_and_zero_tool_balance_stays_zero(source_run):
    source, _, _, _, _ = source_run
    ledger = Ledger(source, resume=True)
    try:
        metadata = json.loads((source / 'run.json').read_text(encoding='utf-8'))
        metadata['tool_policy']['max_calls'] = 1
        hint = repair.budget_context_snapshot(ledger, metadata)
        assert hint['model'] is None
        assert hint['tools']['remaining'] == hint['tools']['provider_requests_remaining'] == 0
    finally:
        ledger.store.close()


def test_cli_rejects_resume_policy_override_before_configuration(monkeypatch):
    monkeypatch.setattr(repair, 'load_dotenv', lambda: pytest.fail('No config on invalid override'))
    monkeypatch.setattr(sys, 'argv', ['repair', '--resume', 'existing', '--budget-context', 'snapshot-v1'])
    with pytest.raises(SystemExit) as error:
        repair.main()
    assert error.value.code == 2


def test_actual_deepagents_model_receives_snapshot_and_budget_is_still_enforced(source_run, tmp_path, monkeypatch):
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    import researchdesk.agent as agent_module
    source, sid, eid, _, _ = source_run
    enable(source, 3)
    ledger = Ledger(source, resume=True)
    try:
        report = fixed_report(ledger, sid, eid)
    finally:
        ledger.store.close()
    seen = []
    class Scripted(BaseChatModel):
        @property
        def _llm_type(self):
            return 'budget-context-fixture'
        def bind_tools(self, tools, **kwargs):
            return self
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            content = next(m.content for m in reversed(messages) if m.type == 'human')
            hint = json.loads(content.rsplit('\n', 1)[-1])['budget_context']
            assert hint['model']['generation_remaining'] == 1
            seen.append(hint)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=report,
                usage_metadata=dict(input_tokens=1, output_tokens=1, total_tokens=2)))])
    monkeypatch.setattr(agent_module, 'ChatDeepSeek', lambda **k: Scripted())
    destination = tmp_path / 'actual'
    state = repair.repair_run(source, destination, checker=Checker(), budget_context='snapshot-v1')
    assert state['selection']['accepted'] and len(seen) == 1
    ledger = Ledger(destination, resume=True)
    try:
        info = budget_snapshot(ledger.store.conn)
        assert info['reserved'] == 3 and info['remaining'] == 0 and info['verification_slots_held'] == 0
    finally:
        ledger.store.close()
