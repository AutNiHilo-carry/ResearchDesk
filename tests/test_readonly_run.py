"""Filesystem and committed-WAL regressions; no model or human reference labels."""
import json
import sqlite3
from pathlib import Path
import pytest
from researchdesk import verification
from researchdesk.ledger import Ledger


def files(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}


@pytest.fixture
def completed_run(tmp_path):
    directory = tmp_path / 'run'
    ledger = Ledger(directory)
    try:
        source = ledger.add_source('Fixture', 'fixture://source', 'Fixture fact.', 'fixture')
        evidence = ledger.add_evidence('Fixture fact.', source['id'], 'Fixture fact.')
        ledger.save_report(f"Fixture fact.[{evidence['id']}]")
        (directory / 'run.json').write_text(json.dumps(dict(status='completed')), encoding='utf-8')
    finally:
        ledger.store.close()
    return directory, source, evidence


def test_load_run_creates_no_source_sidecars(completed_run):
    directory, source, evidence = completed_run
    assert not (directory / 'application.sqlite3-wal').exists()
    assert not (directory / 'application.sqlite3-shm').exists()
    before = files(directory)
    body, found_evidence, found_sources, _ = verification.load_run(directory)
    assert evidence['id'] in body
    assert found_evidence[evidence['id']] == evidence
    assert found_sources[source['id']] == source
    assert files(directory) == before


def test_load_run_includes_committed_wal_without_touching_source(completed_run):
    directory, source, _ = completed_run
    writer = sqlite3.connect(directory / 'application.sqlite3')
    try:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        changed = dict(source, title='Committed in WAL')
        writer.execute("UPDATE records SET payload=? WHERE kind='sources' AND id=?",
                       (json.dumps(changed), source['id']))
        writer.commit()
        assert (directory / 'application.sqlite3-wal').stat().st_size > 0
        before = files(directory)
        assert verification.load_run(directory)[2][source['id']] == changed
        assert files(directory) == before
    finally:
        writer.close()


def test_load_run_refuses_source_change_during_copy(completed_run, monkeypatch):
    import shutil
    directory, _, _ = completed_run
    original_copy = shutil.copyfile

    def copy_and_change(source, target):
        result = original_copy(source, target)
        if Path(source) == directory / 'application.sqlite3':
            (directory / 'application.sqlite3-wal').write_bytes(b'concurrent change')
        return result

    monkeypatch.setattr(shutil, 'copyfile', copy_and_change)
    with pytest.raises(verification.VerificationError, match='changed'):
        verification.load_run(directory)


def test_load_run_refuses_source_change_during_private_read(completed_run, monkeypatch):
    directory, _, _ = completed_run
    original_connect = sqlite3.connect

    def connect_and_change(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        (directory / 'application.sqlite3-journal').write_bytes(b'concurrent change')
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect_and_change)
    with pytest.raises(verification.VerificationError, match='changed'):
        verification.load_run(directory)


def test_legacy_jsonl_run_still_loads_without_database(tmp_path):
    source = dict(id='legacy-source', content='Legacy fact.')
    evidence = dict(id='legacy-evidence', snippet='Legacy fact.')
    (tmp_path / 'run.json').write_text(json.dumps(dict(status='completed')), encoding='utf-8')
    (tmp_path / 'report.md').write_text('Legacy fact.', encoding='utf-8')
    for name, record in [('sources', source), ('evidence', evidence)]:
        (tmp_path / f'{name}.jsonl').write_text(json.dumps(record) + '\n', encoding='utf-8')
    before = files(tmp_path)
    report, found_evidence, found_sources, _ = verification.load_run(tmp_path)
    assert report == 'Legacy fact.'
    assert found_sources == {'legacy-source': source}
    assert found_evidence == {'legacy-evidence': evidence}
    assert files(tmp_path) == before
