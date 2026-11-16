"""Optional search-return presentation; saved snapshots and evidence identities stay intact."""
import json
import sqlite3
from .storage import RecoveryError
from .verification import digest

VERSION='search-return-v1'
PREVIEW_CHARS=600
PREVIEW_HINT=('搜索结果的preview只是搜索摘要片段，不是网页正文。'
    'read_source会尝试提取网页正文并保留独立快照，受外部请求预算约束；'
    '不要把preview称为已读网页正文，不要把片段截断处当作完整结论。引用前核对证据支持范围，资料不足就保留结论。')


def policy_spec(mode='full'):
    if not isinstance(mode,str) or mode not in {'full','preview'}:
        raise RecoveryError('Unknown search context presentation mode.')
    return dict(version=VERSION,mode=mode,preview_chars=PREVIEW_CHARS,
                instruction_sha256=digest(PREVIEW_HINT if mode=='preview' else ''))


def validate_spec(spec):
    if not isinstance(spec,dict) or spec!=policy_spec(spec.get('mode')):
        raise RecoveryError('Search context policy does not match its versioned settings.')
    return spec['mode']


def bind_policy(ledger):
    path=ledger.directory/'run.json'
    manifest=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    spec=manifest.get('search_context_policy')
    if spec is not None: validate_spec(spec)
    with ledger.store.transaction() as connection:
        row=connection.execute("SELECT payload FROM records WHERE kind='search_context_policy' AND id='singleton'").fetchone()
        if spec is None:
            if row is not None:
                raise RecoveryError('Pinned search context policy cannot be removed.')
            return policy_spec()  # Old runs keep full responses without invented history.
        if row is None:
            historical=connection.execute('SELECT 1 FROM dispatches LIMIT 1').fetchone() or any(
                json.loads(e[0]).get('event')=='model_started' for e in connection.execute('SELECT payload FROM events'))
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_call_reservations'").fetchone():
                historical = historical or connection.execute('SELECT 1 FROM model_call_reservations LIMIT 1').fetchone()
            if historical:
                raise RecoveryError('Cannot assign a search context policy after historical model/tool calls.')
            connection.execute("INSERT INTO records VALUES ('search_context_policy','singleton',?)",(json.dumps(spec,sort_keys=True),))
        elif json.loads(row[0])!=spec:
            raise RecoveryError('Search context policy differs from the persisted run policy.')
    return spec


def render_search_sources(sources,spec):
    if validate_spec(spec)=='full':
        return {'sources':sources}
    result=[]
    for source in sources:
        entry={key:value for key,value in source.items() if key!='content'}
        entry.update(preview=source['content'][:PREVIEW_CHARS],saved_snippet_chars=len(source['content']),
                     preview_truncated=len(source['content'])>PREVIEW_CHARS)
        result.append(entry)
    return dict(sources=result,presentation=VERSION,source_text_kind='search_snippet_preview',
                body_read_action='read_source_fetches_web_body_subject_to_request_budget')


def verify_completed_policy(directory):
    """Read-only guard for CLI's completed-report shortcut; never invent policy history."""
    manifest=json.loads((directory/'run.json').read_text(encoding='utf-8'))
    spec=manifest.get('search_context_policy')
    if spec is not None: validate_spec(spec)
    with sqlite3.connect((directory/'application.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as connection:
        row=connection.execute("SELECT payload FROM records WHERE kind='search_context_policy' AND id='singleton'").fetchone()
    if row is not None and (spec is None or json.loads(row[0])!=spec):
        raise RecoveryError('Completed run search context policy differs from its persisted binding.')
    if row is None and spec is not None and spec['mode']=='preview':
        raise RecoveryError('Completed preview run has no persisted search context policy.')
