"""Dataset integrity and offline integration; no report quality assertions."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from researchdesk import report_eval
from researchdesk.task_eval import load_taskset

DATASET = Path(__file__).parents[1] / 'evals/technical-selection-v1.json'


def test_technical_notes_have_complete_pinned_source_manifest():
    data, tasks, documents = load_taskset(DATASET)
    manifest = json.loads((DATASET.parent / data['source_manifest']).read_text(encoding='utf-8'))
    rows = manifest['sources']
    assert len(rows) == len({row['document'] for row in rows})
    assert set(documents) == {row['document'] for row in rows}
    for row in rows:
        raw = (DATASET.parent / row['document']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row['sha256'] == documents[row['document']]['sha256']
        url = urlparse(row['url'])
        assert url.scheme == 'https' and url.hostname in {'www.sqlite.org', 'www.postgresql.org'}
        assert row['url'] in documents[row['document']]['content']
        if url.hostname == 'www.postgresql.org':
            assert url.path.startswith('/docs/18/')
        assert row['version_scope']
    assert 'pending' in data['label_status']
    assert manifest['review_status'] == 'pending_independent_human_review'
    assert tasks  # Parsing above also validates document paths, IDs and criterion bounds.


@pytest.mark.parametrize('start', [0, 3, 6])
def test_every_technical_task_can_enter_existing_offline_frozen_plan(tmp_path, monkeypatch, start):
    monkeypatch.setattr(report_eval, 'load_dotenv', lambda: pytest.fail('No credentials during preparation'))
    monkeypatch.setattr(report_eval, 'evaluate_batch', lambda *a, **k: pytest.fail('No model during preparation'))
    data = json.loads(DATASET.read_text(encoding='utf-8'))
    data['tasks'] = data['tasks'][start:start + 3]
    # Retain relative source paths without copying or rewriting note bytes.
    selected = tmp_path / 'evals'
    selected.mkdir()
    (selected / 'fixtures').mkdir()
    for name in {name for task in data['tasks'] for name in task['documents']}:
        (selected / name).write_bytes((DATASET.parent / name).read_bytes())
    path = selected / 'tasks.json'
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    output = tmp_path / 'plan'
    plan = report_eval.create_plan(path, output, model='fixture-not-executed', limit=len(data['tasks']), maximum=6)
    assert [task['id'] for task in plan['tasks']] == [task['id'] for task in data['tasks']]
    assert plan['controls'] == report_eval.controls(plan)
    assert plan['controls']['max_sdk_requests'] == 2 * len(data['tasks']) * 6 + 4
    _, _, documents = load_taskset(path)
    assert plan['documents'] == documents
    assert (output / 'plan.json').exists() and not (output / 'execution').exists()
