import json

import pytest
from langchain_core.messages import AIMessage

from researchdesk import cli, recovery
from researchdesk.final_body_gate import verify_completed, verify_rejection
from researchdesk.tools import ResearchTools


@pytest.mark.parametrize('scenario', ['reuse', 'normal', 'disabled', 'missing', 'tight_limit', 'bad_citation', 'newer_overflow'])
def test_cli_overflow_selection_uses_only_latest_eligible_receipt(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(cli, 'load_dotenv', lambda: None)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fixture')
    note = tmp_path / 'source.md'
    note.write_text('Fixture fact.', encoding='utf-8')
    output = tmp_path / 'runs'
    bodies = {}

    def graph(ledger, *args, **kwargs):
        source = next(iter(ledger.sources.values()))
        item = ledger.add_evidence('Fixture fact.', source['id'], 'Fixture fact.')
        current = ResearchTools(ledger, '', allowed_tools=kwargs['allowed_tools'])
        suffix = f"[{item['id']}]"
        bodies['checked'] = 'Fixture fact.' + suffix
        if scenario != 'missing':
            result = current.executor.execute('inspect_report', {'report': bodies['checked']})
            assert result['body_constraints']['passed']
        if scenario == 'newer_overflow':
            result = current.executor.execute('inspect_report', {'report': '长'*201 + suffix})
            assert not result['body_constraints']['passed']
        cid = '[E0000000000000000]' if scenario == 'bad_citation' else suffix
        bodies['overflow'] = ('合规最终稿' if scenario == 'normal' else '更'*210) + cid
        return {'messages': [AIMessage(content=bodies['overflow'])]}

    monkeypatch.setattr(recovery, 'run_graph', graph)
    args = ['Fixture', '--document', str(note), '--local-only', '--draft-inspection-limit', '200',
            '--final-body-limit', '10' if scenario == 'tight_limit' else '200', '--output', str(output)]
    if scenario != 'disabled':
        args.append('--reuse-checked-draft-on-overflow')
    status = cli.main(args)
    run = next(output.iterdir())
    if scenario == 'reuse':
        assert status == 0
        assert (run/'draft.md').read_text(encoding='utf-8') == bodies['checked']
        assert (run/'overflow-candidate.md').read_text(encoding='utf-8') == bodies['overflow']
        verify_completed(run)
        (run/'overflow-candidate.md').write_text('tampered', encoding='utf-8')
        assert cli.main(['--resume', run.name, '--output', str(output)]) == 1
    elif scenario == 'normal':
        assert status == 0
        assert (run/'draft.md').read_text(encoding='utf-8') == bodies['overflow']
        assert not (run/'overflow-selection.json').exists()
    else:
        assert status == 1 and not (run/'report.md').exists()
        assert not (run/'overflow-selection.json').exists()
        assert verify_rejection(run)['status'] == 'rejected'
