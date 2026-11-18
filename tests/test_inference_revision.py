"""Revision selection is snapshot integrity, never semantic acceptance."""
import json
import pytest
from researchdesk import inference_check
from researchdesk.inference import create_trace, check_trace
from researchdesk.storage import atomic_text
from researchdesk.verification import VerificationError, digest, load_run
from test_inference import run


@pytest.fixture
def repair(run):
    directory, first, second = run
    original = load_run(directory)[0]
    candidate = f"# Rejected candidate\nThe sources conflict.[{first['id']}][{second['id']}]"
    (directory / 'revisions').mkdir()
    atomic_text(directory / 'revisions/original.md', original)
    atomic_text(directory / 'revisions/candidate.md', candidate)
    atomic_text(directory / 'run.json', json.dumps(dict(status='completed', mode='repair_candidate')))
    state = dict(version=1, status='completed', original_report_sha256=digest(original),
                 candidate_sha256=digest(candidate), selection=dict(selected='original', accepted=False))
    atomic_text(directory / 'repair-state.json', json.dumps(state))
    return directory, first, second


def declared(repair):
    directory, first, second = repair
    trace = create_trace(directory, revision='candidate')
    trace['claims'][1].update(kind='synthesis', rationale='Fixture declaration, not a human judgment.', premises=[
        dict(id='P1', statement='Guide says recovery is supported.', evidence_ids=[first['id']]),
        dict(id='P2', statement='Release says recovery is unsupported.', evidence_ids=[second['id']])])
    return trace


def test_rejected_revision_keeps_all_units_and_leaves_every_source_file_unchanged(repair, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('No network'))
    directory, _, _ = repair
    before = {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    default = create_trace(directory)
    candidate = create_trace(directory, revision='candidate')
    original = create_trace(directory, revision='original')
    assert default['claims'] == original['claims']
    assert candidate['claims'][0]['text'] == '# Rejected candidate'
    assert len(candidate['claims']) == 2
    assert len({t['input_sha256'] for t in (default, candidate, original)}) == 3
    result = check_trace(directory, candidate, revision='candidate')
    assert result['status'] == 'draft' and len(result['pending']) == 2
    assert result['semantic_verification'] == 'not_performed'
    assert result['human_review'] == 'pending'
    assert before == {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()}


@pytest.mark.parametrize('target', [None, 'original'])
def test_revision_trace_cannot_be_reused_for_selected_or_original(repair, target):
    directory, _, _ = repair
    with pytest.raises(VerificationError, match='changed after'):
        check_trace(directory, create_trace(directory, revision='candidate'), revision=target)


@pytest.mark.parametrize('change', ['body', 'hash', 'state', 'missing', 'unfinished', 'mode', 'unknown'])
def test_changed_or_unavailable_revision_fails_closed(repair, change):
    directory, _, _ = repair
    trace = create_trace(directory, revision='candidate')
    path = directory / 'revisions/candidate.md'
    state = json.loads((directory / 'repair-state.json').read_text())
    revision = 'candidate'
    if change == 'body':
        atomic_text(path, 'Changed candidate')
    elif change == 'hash':
        state['candidate_sha256'] = '0' * 64
    elif change == 'state':
        state['selection']['accepted'] = True
    elif change == 'missing':
        path.rename(directory / 'revisions/hidden.md')
    elif change == 'unfinished':
        state['status'] = 'repair_started'
    elif change == 'mode':
        atomic_text(directory / 'run.json', json.dumps(dict(status='completed')))
    else:
        revision = '../candidate'
    atomic_text(directory / 'repair-state.json', json.dumps(state))
    with pytest.raises((VerificationError, FileNotFoundError)):
        check_trace(directory, trace, revision=revision)


@pytest.mark.parametrize('body', ['```hidden```', '~~~hidden~~~', 'Invalid evidence.[Eunknown]'])
def test_saved_hash_does_not_bypass_content_validation(repair, body):
    directory, _, _ = repair
    state = json.loads((directory / 'repair-state.json').read_text())
    state['candidate_sha256'] = digest(body)
    atomic_text(directory / 'revisions/candidate.md', body)
    atomic_text(directory / 'repair-state.json', json.dumps(state))
    with pytest.raises(VerificationError):
        create_trace(directory, revision='candidate')


def test_semantic_request_freezes_revision_and_rechecks_before_any_execution(repair, tmp_path):
    directory, _, _ = repair
    request = inference_check.create_request(directory, declared(repair), ['U2'], 'fixture', revision='candidate')
    assert request['repair_revision'] == request['payload']['repair_revision'] == 'candidate'
    assert len(request['payload']['report_units']) == 2
    inference_check.verify_request(request)
    plan = tmp_path / 'plan'
    plan.mkdir()
    atomic_text(plan / 'request.json', json.dumps(request))
    atomic_text(directory / 'revisions/candidate.md', 'Changed before execution')
    with pytest.raises(VerificationError):
        inference_check.execute(plan, judge=lambda _: pytest.fail('Must reject before judge'))
    assert not (plan / 'execution').exists()


def test_removing_request_revision_cannot_downgrade_snapshot_binding(repair):
    directory, _, _ = repair
    request = inference_check.create_request(directory, declared(repair), ['U2'], 'fixture', revision='candidate')
    request.pop('repair_revision')
    with pytest.raises(VerificationError):
        inference_check.verify_request(request)


def test_default_snapshot_keeps_legacy_hash(run):
    directory, _, _ = run
    report, evidence, sources, _ = load_run(directory)
    expected = digest(json.dumps(dict(report=report, evidence=evidence, sources=sources), ensure_ascii=False, sort_keys=True))
    assert create_trace(directory)['input_sha256'] == expected
