"""Opt-in draft feedback. Structural checks are never semantic acceptance."""
import json
import sqlite3
from .report_constraints import check_body, policy
from .verification import prepare, split_units, digest, VerificationError
from .storage import RecoveryError

HINT = """本次可调用 inspect_report(report) 检查最终正文草稿。
记录证据后，在剩余模型／工具预算允许时检查一次完整候选正文，再按返回的精确计数与逐行引用摘录调整。
只提交正文，不包含自行生成的证据附录。检查消耗应用工具额度，不发放额外模型调用；余额不足不要循环检查。
检查通过仅说明长度与引用结构合规，绝不证明摘录支持整句。逐行保留版本和条件，拆开未被同一摘录支持的事实。
不要为缩短正文删除用户需求或必要限制；不要把事实改标“建议／推断”来逃避引用。
最终仍直接输出完整报告，工具结果不是最终报告，也不会替你保存正文。
"""

FINAL_TURN_VERSION = 'draft-inspection-final-turn-v1'
FINAL_TURN_HINT = (
    '这是模型预算允许的最后一轮。现在直接输出完整 Markdown 报告正文。'
    '只使用已记录的证据及其完整引用编号。资料不足时保留结论。'
)


def policy_spec(max_chars, *, final_turn=False):
    spec = dict(version='draft-inspection-v1', max_chars=policy(max_chars)['max_chars'], hint_sha256=digest(HINT))
    if final_turn:
        spec.update(version=FINAL_TURN_VERSION, final_turn_hint_sha256=digest(FINAL_TURN_HINT))
    return spec


def manifest_policy(directory):
    path=directory/'run.json'
    spec=json.loads(path.read_text(encoding='utf-8')).get('draft_inspection_policy') if path.is_file() else None
    if spec is not None:
        if not isinstance(spec, dict) or spec != policy_spec(
                spec.get('max_chars'), final_turn=spec.get('version') == FINAL_TURN_VERSION):
            raise RecoveryError('Draft inspection policy changed or is invalid.')
    return spec


def bind_policy(ledger):
    spec=manifest_policy(ledger.directory)
    with ledger.store.transaction() as connection:
        row=connection.execute("SELECT payload FROM records WHERE kind='draft_inspection_policy' AND id='singleton'").fetchone()
        if row is not None:
            if json.loads(row[0])!=spec:
                raise RecoveryError('Draft inspection differs from its persisted policy.')
        elif spec is not None:
            historical=any(ledger.store.counters().values()) or any(
                json.loads(event[0]).get('event')=='model_started' for event in connection.execute('SELECT payload FROM events'))
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_call_reservations'").fetchone():
                historical=historical or connection.execute('SELECT 1 FROM model_call_reservations LIMIT 1').fetchone()
            if historical:
                raise RecoveryError('Cannot enable draft inspection after model or tool calls.')
            connection.execute("INSERT INTO records VALUES ('draft_inspection_policy','singleton',?)",(json.dumps(spec,sort_keys=True),))
    return spec


def verify_completed_policy(directory):
    spec=manifest_policy(directory)
    with sqlite3.connect((directory/'application.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as connection:
        row=connection.execute("SELECT payload FROM records WHERE kind='draft_inspection_policy' AND id='singleton'").fetchone()
    if (row is not None and json.loads(row[0])!=spec) or (row is None and spec is not None):
        raise RecoveryError('Completed draft inspection policy has no matching binding.')


def inspect(report,evidence,sources,max_chars):
    if '```' in report or '~~~' in report:
        raise VerificationError('Draft inspection requires unfenced prose; no omitted fenced content.')
    body=check_body(report,max_chars=max_chars)
    payload,_=prepare(split_units(report),evidence,sources)
    structural=bool(payload['evidence']) and all(row['integrity']=='valid' for row in payload['evidence'])
    result=dict(body_constraints=body,citation_structure='passed' if structural else 'failed',
                units=payload['units'],evidence=payload['evidence'],semantic_support='not_assessed',
                business_requirements='not_assessed',report_saved=False,api_requests=0)
    if len(json.dumps(result,ensure_ascii=False))>60000:
        raise VerificationError('Draft feedback exceeds budget; refusing truncated quotes.')
    return result
