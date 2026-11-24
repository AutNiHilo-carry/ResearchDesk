"""Explicit prepublication body gate; no semantic scoring or model calls."""
import json
import sqlite3
import hashlib
import shutil
import tempfile
from pathlib import Path
from contextlib import closing
from .draft_inspection import inspect
from .report_constraints import policy, check_body
from .storage import RecoveryError,atomic_text
from .verification import digest,load_run
from .citations import normalize_citation_delimiters,CitationSyntaxError

HINT='最终正文发布前由应用检查长度和引用结构；超限会拒绝发布，不自动补轮。该检查不证明语义或业务要求满足。'


class FinalBodyRejected(RecoveryError):
    pass


def policy_spec(max_chars, *, reuse_checked_draft=False):
    spec = dict(version='final-body-gate-v1', constraint_policy=policy(max_chars), hint_sha256=digest(HINT))
    if reuse_checked_draft:
        spec['overflow_policy'] = 'reuse-checked-draft-v1'
    return spec


def manifest_policy(directory):
    path=directory/'run.json'
    spec=json.loads(path.read_text(encoding='utf-8')).get('final_body_policy') if path.is_file() else None
    if spec is not None:
        try: valid=spec==policy_spec(spec['constraint_policy']['max_chars'],
                                    reuse_checked_draft=spec.get('overflow_policy')=='reuse-checked-draft-v1')
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


def checked_fallback(candidate, evidence, sources, spec, rows):
    """Select only the newest completed preflight and recheck it against current sources."""
    limit = spec['constraint_policy']['max_chars']
    check = evaluate(candidate, evidence, sources, limit)
    if check['body_constraints']['reasons'] != ['body_too_long'] or check['citation_structure'] != 'passed':
        return None
    for (raw,) in rows:
        receipt = json.loads(raw)
        if 'body_constraints' not in receipt:
            continue
        draft = receipt.get('checked_draft')
        if not isinstance(draft, str):
            return None
        # The receipt must still agree with the exact draft and its quoted evidence.
        try:
            preflight = inspect(draft, evidence, sources, receipt['body_constraints']['policy']['max_chars'])
            normalized = normalize_citation_delimiters(draft)
        except (ValueError, TypeError, KeyError):
            return None
        if (receipt != dict(preflight, checked_draft=draft) or not preflight['body_constraints']['passed']
                or evaluate(normalized, evidence, sources, limit)['status'] != 'passed'):
            return None
        return normalized
    return None


def select_body(ledger, candidate):
    spec = bind_policy(ledger)
    if not spec or 'overflow_policy' not in spec:
        return candidate, None
    try:
        normalized = normalize_citation_delimiters(candidate)
    except CitationSyntaxError:
        return candidate, None
    with ledger.store.lock:
        rows = ledger.store.conn.execute("SELECT result FROM dispatches WHERE tool='inspect_report' AND result IS NOT NULL ORDER BY rowid DESC").fetchall()
    selected = checked_fallback(normalized, ledger.evidence, ledger.sources, spec, rows)
    if selected is None:
        return candidate, None
    original_path = ledger.directory/'overflow-candidate.md'
    selection = selection_receipt(candidate, selected)
    atomic_text(original_path, candidate)
    atomic_text(ledger.directory/'overflow-selection.json', json.dumps(selection, indent=2))
    return selected, selection


def selection_receipt(candidate, selected):
    return dict(policy='reuse-checked-draft-v1', reason='body_too_long',
                candidate_sha256=digest(candidate), selected_sha256=digest(selected))


def verify_completed(directory):
    spec=manifest_policy(directory)
    with closing(sqlite3.connect((directory/'application.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as connection:
        row=connection.execute("SELECT payload FROM records WHERE kind='final_body_policy' AND id='singleton'").fetchone()
        checks=connection.execute("SELECT result FROM dispatches WHERE tool='inspect_report' AND result IS NOT NULL ORDER BY rowid DESC").fetchall()
    if row is None and spec is None: return
    if row is None or json.loads(row[0])!=spec: raise RecoveryError('Completed final body policy binding changed.')
    report,evidence,sources,metadata=load_run(directory)
    saved=json.loads((directory/'final-body-check.json').read_text(encoding='utf-8'))
    expected=evaluate(report,evidence,sources,spec['constraint_policy']['max_chars'])
    attach_preflight(expected,spec,checks)
    if (expected['status']!='passed' or saved!=expected
            or not (directory/'report.md').read_text(encoding='utf-8').startswith(report+'\n\n## Evidence Ledger\n')):
        raise RecoveryError('Completed final body or its receipt changed.')
    selection_path = directory/'overflow-selection.json'
    overflow_path = directory/'overflow-candidate.md'
    if metadata.get('overflow_selection') is not None or selection_path.exists() or overflow_path.exists():
        candidate = overflow_path.read_text(encoding='utf-8')
        selection = json.loads(selection_path.read_text(encoding='utf-8'))
        if ('overflow_policy' not in spec
                or checked_fallback(normalize_citation_delimiters(candidate), evidence, sources, spec, checks) != report
                or selection != selection_receipt(candidate, report) or metadata.get('overflow_selection') != selection):
            raise RecoveryError('Completed draft selection differs from its receipt.')


def verify_rejection(directory):
    """Read stopped rejected candidates from a private WAL-aware SQLite snapshot."""
    metadata=json.loads((directory/'run.json').read_text(encoding='utf-8'))
    spec=manifest_policy(directory)
    if (metadata.get('status')!='failed' or metadata.get('error_type')!='FinalBodyRejected'
            or spec is None or (directory/'report.md').exists()):
        raise RecoveryError('Not a stopped prepublication gate rejection.')
    paths=[directory/('application.sqlite3'+s) for s in ('','-wal','-journal')]
    def signatures():
        result={}
        for p in paths:
            if p.is_symlink() or (p.exists() and not p.resolve().is_relative_to(directory.resolve())):
                raise RecoveryError('Gate database escapes its run.')
            result[p.name]=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        return result
    before=signatures()
    if before['application.sqlite3'] is None: raise RecoveryError('Missing gate database.')
    with tempfile.TemporaryDirectory(prefix='researchdesk-rejected-gate-') as temporary:
        target=Path(temporary)
        for p in paths:
            if before[p.name] is not None:
                shutil.copyfile(p,target/p.name)
                if hashlib.sha256((target/p.name).read_bytes()).hexdigest()!=before[p.name]:
                    raise RecoveryError('Gate database changed during copy.')
        if signatures()!=before: raise RecoveryError('Gate database changed during copy.')
        with closing(sqlite3.connect(target/'application.sqlite3')) as connection:
            if connection.execute('PRAGMA user_version').fetchone()[0]!=1:
                raise RecoveryError('Unsupported gate database version.')
            row=connection.execute("SELECT payload FROM records WHERE kind='final_body_policy' AND id='singleton'").fetchone()
            if row is None or json.loads(row[0])!=spec: raise RecoveryError('Rejected gate policy binding changed.')
            sources={r['id']:r for (raw,) in connection.execute("SELECT payload FROM records WHERE kind='sources'") for r in [json.loads(raw)]}
            evidence={r['id']:r for (raw,) in connection.execute("SELECT payload FROM records WHERE kind='evidence'") for r in [json.loads(raw)]}
            checks=connection.execute("SELECT result FROM dispatches WHERE tool='inspect_report' AND result IS NOT NULL ORDER BY rowid DESC").fetchall()
    if signatures()!=before: raise RecoveryError('Gate database changed during reading.')
    report=(directory/'draft.md').read_text(encoding='utf-8')
    try: report=normalize_citation_delimiters(report)
    except CitationSyntaxError: pass  # Malformed input is checked in its original form by enforce().
    expected=evaluate(report,evidence,sources,spec['constraint_policy']['max_chars'])
    attach_preflight(expected,spec,checks)
    raw=(directory/'final-body-check.json').read_bytes()
    if expected['status']!='failed' or json.loads(raw)!=expected:
        raise RecoveryError('Rejected final body or its receipt changed.')
    return dict(status='rejected',check=expected,receipt_sha256=hashlib.sha256(raw).hexdigest(),
                semantic_scoring='not_performed',publication='blocked')
