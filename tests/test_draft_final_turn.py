"""Regression for a draft tool call consuming the last available model turn."""
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage

from researchdesk import agent, cli
from researchdesk.draft_inspection import FINAL_TURN_HINT, bind_policy, policy_spec
from researchdesk.final_body_gate import verify_completed
from researchdesk.ledger import Ledger
from researchdesk.model_budget import VERSION, model_budget_for
from researchdesk.run_metrics import run_metrics
from researchdesk.storage import RecoveryError
from researchdesk.verification import digest


def scripted_model(responses, turns):
    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self.bind(scripted_tools=tuple(tool.name for tool in tools))

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            system = '\n'.join(message.text for message in messages if isinstance(message, SystemMessage))
            turns.append((kwargs.pop('scripted_tools', ()), system))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    return Model(messages=iter(responses))


def test_final_turn_after_draft_check_publishes_within_the_original_budget(tmp_path, monkeypatch):
    note = tmp_path / 'source.md'
    quote = 'Fixture fact.'
    note.write_text(quote, encoding='utf-8')
    source_id = 'S' + digest(str(note.resolve()) + '\0' + quote)[:16]
    evidence_id = 'E' + digest(quote + '\0' + source_id + '\0' + quote)[:16]
    report = f'Fixture fact. [{evidence_id}]'
    turns = []
    responses = [
        AIMessage(content='', tool_calls=[dict(name='record_evidence', id='record', args={
            'claim': quote, 'source_id': source_id, 'snippet': quote})]),
        AIMessage(content='', tool_calls=[dict(name='inspect_report', id='inspect', args={'report': report})]),
        AIMessage(content=report),
    ]
    monkeypatch.setattr(agent, 'ChatDeepSeek', lambda **kwargs: scripted_model(responses, turns))
    monkeypatch.setattr(cli, 'load_dotenv', lambda: None)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fixture')
    output = tmp_path / 'runs'
    args = ['Fixture', '--document', str(note), '--local-only', '--draft-inspection-limit', '200',
            '--draft-inspection-final-turn', '--final-body-limit', '200', '--max-model-calls', '3',
            '--output', str(output)]
    assert cli.main(args) == 0
    run = next(output.iterdir())
    assert 'inspect_report' in turns[0][0] and 'inspect_report' in turns[1][0]
    assert turns[2][0] == () and FINAL_TURN_HINT in turns[2][1]
    assert FINAL_TURN_HINT not in turns[1][1]
    assert run_metrics(run)['model_calls'] == 3
    assert run_metrics(run)['tool_calls'] == 2
    assert json.loads((run / 'final-body-check.json').read_text(encoding='utf-8'))['matches_last_preflight'] is True
    verify_completed(run)
    assert cli.main(['--resume', run.name, '--output', str(output)]) == 0
    assert len(turns) == 3


def test_last_turn_uses_persisted_balance_and_rejects_unexpected_tool_calls(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / 'run')
    try:
        manifest = ledger.directory / 'run.json'
        manifest.write_text(json.dumps(dict(draft_inspection_policy=policy_spec(200, final_turn=True),
            model_policy=dict(version=VERSION, max_calls=5))), encoding='utf-8')
        bind_policy(ledger)
        budget = model_budget_for(ledger)
        for index in range(4):
            budget.admit(f'prior-{index}', 'generation')
        turns = []
        unexpected = AIMessage(content='', tool_calls=[dict(name='inspect_report', id='late', args={'report': 'draft'})])
        monkeypatch.setattr(agent, 'ChatDeepSeek', lambda **kwargs: scripted_model([unexpected], turns))
        graph = agent.build_agent(ledger, model_name='fixture')
        with pytest.raises(RecoveryError, match='returned a tool call'):
            graph.invoke({'messages': [{'role': 'user', 'content': 'Write a report.'}]})
        assert len(turns) == 1 and turns[0][0] == ()
        assert FINAL_TURN_HINT in turns[0][1]
        assert ledger.store.counters()['calls'] == 0
        assert not (ledger.directory / 'report.md').exists()
        manifest.write_text(json.dumps(dict(draft_inspection_policy=policy_spec(200),
            model_policy=dict(version=VERSION, max_calls=5))), encoding='utf-8')
        with pytest.raises(RecoveryError, match='persisted policy'):
            agent.build_agent(ledger, model_name='fixture')
    finally:
        ledger.store.close()


@pytest.mark.parametrize('extra', [[], ['--draft-inspection-limit', '200'],
    ['--draft-inspection-limit', '200', '--max-model-calls', '0']])
def test_final_turn_requires_draft_inspection_and_positive_quota(extra, monkeypatch):
    monkeypatch.setattr(cli, 'load_dotenv', lambda: pytest.fail('Invalid configuration loaded credentials'))
    with pytest.raises(SystemExit) as error:
        cli.main(['Fixture', '--draft-inspection-final-turn', *extra])
    assert error.value.code == 2
