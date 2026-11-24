"""Explicit prepublication body gate; no semantic scoring or model calls."""
import json
import sqlite3
from contextlib import closing
from .draft_inspection import inspect
from .report_constraints import policy, check_body
from .storage import RecoveryError,atomic_text
from .verification import digest,load_run

HINT='最终正文发布前由应用检查长度和引用结构；超限会拒绝发布，不自动补轮。该检查不证明语义或业务要求满足。'


class FinalBodyRejected(RecoveryError):
    pass


def policy_spec(max_chars):
    return dict(version='final-body-gate-v1',constraint_policy=policy(max_chars),hint_sha256=digest(HINT))


def manifest_policy(directory):
    path=directory/'run.json'
    spec=json.loads(path.read_text(encoding='utf-8')).get('final_body_policy') if path.is_file() else None
    if spec is not None:
        try: valid=spec==policy_spec(spec['constraint_policy']['max_chars'])
        except (TypeError,KeyError,ValueError): valid=False
        if not valid: raise RecoveryError('Invalid final body policy.')
    return spec


def bind_policy(ledger):
    spec=manifest_policy(ledger.directory)
    with ledger.store.transaction() as connection:
        row=connection.execute("SELECT payload FROM records WHERE kind='final_body_policy' AND id='singleton'").fetchone()
        if row is not None:
            if json.loads(row[0])!=spec: raise RecoveryError('Final body policy differs from its persisted binding.')
        elif spec is not None:
            historical=any(ledger.store.counters().values()) or any(json.loads(r[0]).get('event')=='model_started' for r in connection.execute('SELECT payload FROM events'))
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_call_reservations'").fetchone():
                historical=historical or connection.execute('SELECT 1 FROM model_call_reservations LIMIT 1').fetchone()
            if historical: raise RecoveryError('Cannot enable final body policy after model/tool calls.')
            connection.execute("INSERT INTO records VALUES ('final_body_policy','singleton',?)",(json.dumps(spec,sort_keys=True),))
    return spec


def evaluate(report,evidence,sources,max_chars):
    body=check_body(report,max_chars=max_chars)
    structure='failed'
    try:
        structure=inspect(report,evidence,sources,max_chars)['citation_structure']
    except ValueError:
        pass  # Invalid/fenced/oversized input is rejected, never silently omitted.
    return dict(version='final-body-check-v1',status='passed' if body['passed'] and structure=='passed' else 'failed',
                body_constraints=body,citation_structure=structure,semantic_support='not_assessed',
                business_requirements='not_assessed',publication='not_claimed',api_requests=0)


def enforce(ledger,report):
    spec=bind_policy(ledger)
    if spec is None: return
    result=evaluate(report,ledger.evidence,ledger.sources,spec['constraint_policy']['max_chars'])
    with ledger.store.lock:
        rows=ledger.store.conn.execute("SELECT result FROM dispatches WHERE tool='inspect_report' AND result IS NOT NULL ORDER BY rowid DESC").fetchall()
    attach_preflight(result,spec,rows)
    atomic_text(ledger.directory/'final-body-check.json',json.dumps(result,ensure_ascii=False,indent=2))
    if result['status']!='passed':
        raise FinalBodyRejected('Final body rejected before report publication; candidate and local check retained.')


def attach_preflight(result,spec,rows):
    preflight=next((json.loads(r[0])['body_constraints'] for r in rows if 'body_constraints' in json.loads(r[0])),None)
    result.update(policy=spec,last_preflight_body_sha256=preflight['body_sha256'] if preflight else None,
                  matches_last_preflight=preflight['body_sha256']==result['body_constraints']['body_sha256'] if preflight else None)


def verify_completed(directory):
    spec=manifest_policy(directory)
    with closing(sqlite3.connect((directory/'application.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as connection:
        row=connection.execute("SELECT payload FROM records WHERE kind='final_body_policy' AND id='singleton'").fetchone()
        checks=connection.execute("SELECT result FROM dispatches WHERE tool='inspect_report' AND result IS NOT NULL ORDER BY rowid DESC").fetchall()
    if row is None and spec is None: return
    if row is None or json.loads(row[0])!=spec: raise RecoveryError('Completed final body policy binding changed.')
    report,evidence,sources,_=load_run(directory)
    saved=json.loads((directory/'final-body-check.json').read_text(encoding='utf-8'))
    expected=evaluate(report,evidence,sources,spec['constraint_policy']['max_chars'])
    attach_preflight(expected,spec,checks)
    if (expected['status']!='passed' or saved!=expected
            or not (directory/'report.md').read_text(encoding='utf-8').startswith(report+'\n\n## Evidence Ledger\n')):
        raise RecoveryError('Completed final body or its receipt changed.')
