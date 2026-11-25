"""Recover an overflow fallback after actual CLI process termination."""
import json
import os
import subprocess
import sys
import time

import pytest

from researchdesk.final_body_gate import verify_completed
from researchdesk.run_metrics import run_metrics
from researchdesk.verification import digest


CHILD = '''
import os
from pathlib import Path
import sys
from threading import Event

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from researchdesk import agent, cli
from researchdesk.verification import digest


def offline(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('Network disabled in overflow publication probe')


sys.addaudithook(offline)
cli.load_dotenv = lambda: None
os.environ['DEEPSEEK_API_KEY'] = 'fixture'
root = Path.cwd()
output = root / 'runs'
args = sys.argv[1:]


class Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


if args[0] == '--resume':
    class ResumeModel(Model):
        def _generate(self, *args, **kwargs):
            raise AssertionError('Resume invoked the model after the graph had finished')

    agent.ChatDeepSeek = lambda **kwargs: ResumeModel(messages=iter(()))
    raise SystemExit(cli.main([*args, '--output', str(output)]))

boundary = args[0]
note = root / 'source.md'
quote = 'Fixture fact.'
source_id = 'S' + digest(str(note.resolve()) + chr(0) + quote)[:16]
evidence_id = 'E' + digest(quote + chr(0) + source_id + chr(0) + quote)[:16]
report = f'{quote} [{evidence_id}]'
responses = [
    AIMessage(content='', tool_calls=[dict(name='record_evidence', id='record', args={
        'claim': quote, 'source_id': source_id, 'snippet': quote})]),
    AIMessage(content='', tool_calls=[dict(name='inspect_report', id='inspect', args={'report': report})]),
    AIMessage(content='更' * 210 + f' [{evidence_id}]'),
]


agent.ChatDeepSeek = lambda **kwargs: Model(messages=iter(responses))
replace = os.replace


def pause_after_publish(source, destination):
    replace(source, destination)
    target = Path(destination)
    if target.name == boundary and target.parent.parent == output:
        (root / 'kill-ready').write_bytes(b'ready')
        Event().wait()


os.replace = pause_after_publish
raise SystemExit(cli.main([
    'Fixture', '--document', str(note), '--local-only', '--draft-inspection-limit', '200',
    '--draft-inspection-final-turn', '--final-body-limit', '200', '--max-model-calls', '3',
    '--reuse-checked-draft-on-overflow', '--output', str(output),
]))
'''


@pytest.mark.parametrize('boundary', ['overflow-selection.json', 'report.md'])
def test_overflow_publication_resumes_after_process_kill(tmp_path, boundary):
    (tmp_path / 'source.md').write_text('Fixture fact.', encoding='utf-8')
    command = [sys.executable, '-I', '-X', 'utf8', '-c', CHILD]
    environment = os.environ.copy()
    for key in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY'):
        environment.pop(key, None)
    ready = tmp_path / 'kill-ready'
    process = subprocess.Popen(
        [*command, boundary], cwd=tmp_path, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not (ready.is_file() and ready.read_bytes() == b'ready'):
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                pytest.fail(f'Probe exited before {boundary}: {stdout!r}; {stderr!r}')
            if time.monotonic() > deadline:
                pytest.fail(f'Probe did not reach {boundary} within 30 seconds')
            time.sleep(0.025)
        assert process.poll() is None
        process.kill()
        process.communicate(timeout=15)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=15)

    run, = (tmp_path / 'runs').iterdir()
    manifest = run / 'run.json'
    report = run / 'report.md'
    assert json.loads(manifest.read_text(encoding='utf-8'))['status'] == 'running'
    assert report.is_file() == (boundary == 'report.md')
    published = [run / 'overflow-selection.json', run / 'overflow-candidate.md']
    if report.is_file():
        published.extend([report, run / 'final-body-check.json', run / 'draft.md'])
    before = {path: path.read_bytes() for path in published}
    selection = json.loads(before[run / 'overflow-selection.json'])
    assert selection['reason'] == 'body_too_long'
    assert selection['candidate_sha256'] == digest((run / 'overflow-candidate.md').read_text(encoding='utf-8'))
    metrics = run_metrics(run)
    assert (metrics['model_calls'], metrics['tool_calls']) == (3, 2)

    resumed = subprocess.run(
        [*command, '--resume', run.name], cwd=tmp_path, env=environment, capture_output=True, timeout=30,
    )
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    metadata = json.loads(manifest.read_text(encoding='utf-8'))
    assert metadata['status'] == 'completed'
    assert metadata['overflow_selection'] == selection
    assert {path: path.read_bytes() for path in published} == before
    selected = (run / 'draft.md').read_text(encoding='utf-8')
    assert selected.startswith('Fixture fact. [')
    assert selection['selected_sha256'] == digest(selected)
    verify_completed(run)
    resumed_metrics = run_metrics(run)
    for count in ('model_calls', 'tool_calls'):
        assert resumed_metrics[count] == metrics[count]
