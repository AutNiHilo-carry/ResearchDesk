"""Offline run diagnosis from a read-only SQLite snapshot. Never invokes a model."""
from .citations import CitationSyntaxError, validate_citation_syntax
import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from uuid import uuid4
from .run_metrics import metrics_from_rows
from .model_budget import budget_snapshot
from .storage import atomic_text
from .verification import CITATION, VerificationError, digest, evidence_context

TOOLS = {"search_sources", "read_source", "record_evidence", "write_todos"}
EVENTS = {"model_started", "model_completed", "model_failed", "started", "completed", "failed",
          "rejected", "attempt_failed", "cache_hit", "replayed", "model_budget_denied"}
ERRORS = {"invalid_arguments", "permission_denied", "call_budget_exceeded", "operation_id_conflict",
          "ambiguous_operation", "deadline_exceeded", "retry_budget_exceeded", "request_budget_exceeded",
          "execution_failed", "invalid_result", "timeout", "provider_timeout", "provider_http_error",
          "missing_credentials", "network_error", "unknown_source", "invalid_evidence", "model_call_budget_exceeded", "verification_tail_protected"}
REPAIR_REASONS = {"precision_below_target", "coverage_below_target", "citation_precision_regression",
    "evidence_coverage_regression", "fewer_supported_units", "invalid_or_conflicted_evidence",
    "no_scored_facts_or_citations", "already_meets_support_gate", "candidate_citation_structure_invalid",
    "insufficient_budget_for_repair_and_verification", "repair_generation_stopped_for_verification_budget"}


def safe_code(value):
    if isinstance(value, str) and (value in ERRORS or re.fullmatch(r"http_[1-5][0-9]{2}", value)):
        return value
    return "other_error"


def safe_event(row, event_id):
    result = dict(event_id=event_id, event=row.get("event") if row.get("event") in EVENTS else "other_event")
    # No argument dictionaries, adapter result bodies, report text or free-form errors.
    for key in ("timestamp", "model_call_id", "call_id"):
        if isinstance(row.get(key), str):
            result[key] = row[key]
    if "tool" in row:
        result["tool"] = row["tool"] if row["tool"] in TOOLS else "unrecognized_tool"
    for key in ("elapsed_ms", "calls_used", "provider_requests_used", "attempt", "retry_count", "budget_used", "budget_limit"):
        if type(row.get(key)) is int and row[key] >= 0:
            result[key] = row[key]
    for key in ("recovery_replay", "will_retry"):
        if type(row.get(key)) is bool:
            result[key] = row[key]
    if "error_code" in row:
        result["error_code"] = safe_code(row["error_code"])
    if isinstance(row.get("error_type"), str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", row["error_type"]):
        result["error_type"] = row["error_type"]
    if row.get("model_role") in {"repair_verification", "context_summarization"}:
        result["model_role"] = row["model_role"]
    return result


def model_calls(events):
    groups = defaultdict(list)
    for index, event in events:
        if event.get("event") in {"model_started", "model_completed", "model_failed"}:
            groups[event.get("model_call_id") or f"missing-id-event-{index}"].append((index, event))
    calls = []
    for call_id, rows in groups.items():
        starts = [(i, r) for i, r in rows if r["event"] == "model_started"]
        ends = [(i, r) for i, r in rows if r["event"] != "model_started"]
        valid = len(starts) == len(ends) == 1 and starts[0][0] < ends[0][0]
        status = ("completed" if ends[0][1]["event"] == "model_completed" else "failed") if valid else (
            "inflight_or_interrupted" if len(starts) == 1 and not ends else "ambiguous_events")
        terminal = ends[0][1] if valid else {}
        usage = terminal.get("usage")
        known = status == "completed" and isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
            for key in ("input_tokens", "output_tokens", "total_tokens"))
        role = next((row["model_role"] for _, row in rows if row.get("model_role") in {"repair_verification", "context_summarization"}), "generation")
        calls.append(dict(model_call_id=call_id, status=status, first_event_id=rows[0][0], model_role=role,
            usage={key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")} if known else None,
            elapsed_ms=terminal.get("elapsed_ms") if type(terminal.get("elapsed_ms")) is int and terminal["elapsed_ms"] >= 0 else None))
    return calls


def inspect_run(directory):
    directory = directory.resolve()
    manifest_path = directory / "run.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    database = directory / "application.sqlite3"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise VerificationError("Unsupported application schema.")
        events = [(eid, json.loads(payload)) for eid, payload in connection.execute("SELECT id,payload FROM events ORDER BY id")]
        operations = list(connection.execute("SELECT id,tool,fingerprint,attempts,result FROM dispatches ORDER BY rowid"))
        counters = dict(connection.execute("SELECT name,value FROM counters"))
        records = {kind: {rid: json.loads(payload) for rid, payload in connection.execute(
            "SELECT id,payload FROM records WHERE kind=?", (kind,))} for kind in ("sources", "evidence")}
        receipt_count = connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        receipt_keys = {row[0] for row in connection.execute("SELECT fingerprint FROM receipts")}
        model_budget = budget_snapshot(connection)
    finally:
        connection.close()
    raw_events = [event for _, event in events]
    metrics = metrics_from_rows(raw_events, [(rid, attempts, result) for rid, _, _, attempts, result in operations], counters)
    findings = []
    def issue(code, severity="warning", **fields):
        findings.append(dict(code=code, severity=severity, **fields))
    same_manifest = manifest_text == manifest_path.read_text(encoding="utf-8")
    if not same_manifest:
        issue("manifest_changed_during_snapshot")
    if manifest.get("status") in {"running", "preparing", "repairing"}:
        issue("active_or_interrupted_snapshot", "info")
    if not metrics["token_usage_complete"]:
        issue("total_token_usage_unknown")
    calls = model_calls(events)
    for call in calls:
        if call["status"] != "completed":
            issue("model_" + call["status"], model_call_id=call["model_call_id"])
        elif call["usage"] is None:
            issue("model_usage_missing", model_call_id=call["model_call_id"])
    budget = {}
    if model_budget is not None:
        budget["model_calls"] = dict(used=model_budget["reserved"], limit=model_budget["limit"], remaining=model_budget["remaining"])
        if any(row.get("event") == "model_budget_denied" and row.get('error_code') != 'verification_tail_protected' for row in raw_events):
            issue("model_call_budget_exhausted")
        if any(row.get('error_code') == 'verification_tail_protected' for row in raw_events):
            issue('verification_tail_preserved','info')
        if model_budget.get('verification_slots_held',0):
            issue('candidate_verification_slot_held','info')
        if model_budget["states"].get("reserved", 0):
            issue("model_request_outcome_unrecorded")
    policy = manifest.get("tool_policy") or {}
    for name, key in (("calls", "max_calls"), ("requests", "max_provider_requests")):
        used, limit = counters[name], policy.get(key)
        valid_limit = type(limit) is int and limit >= 0
        budget[name] = dict(used=used, limit=limit if valid_limit else None,
                            remaining=max(0, limit-used) if valid_limit else None)
        if used < 0 or (valid_limit and used > limit):
            issue("budget_out_of_range", "error", budget=name)
    if counters["calls"] != len(operations):
        issue("call_counter_dispatch_count_mismatch", "error")
    by_call = defaultdict(list)
    for _, event in events:
        if event.get("call_id"):
            by_call[event["call_id"]].append(event)
    tools = []
    for rid, tool, fingerprint, attempts, encoded in operations:
        result = json.loads(encoded) if encoded is not None else None
        status = "pending" if result is None else "failed" if "error" in result else "completed"
        history = by_call[rid]
        replayed = sum(event.get("event") == "replayed" for event in history)
        reexecutions = sum(event.get("event") == "started" and event.get("recovery_replay") is True for event in history)
        row = dict(call_id=rid, tool=tool if tool in TOOLS else "unrecognized_tool", status=status, attempts=attempts,
            extra_executions=max(0, attempts-1), receipt_replays=replayed, recovery_reexecutions=reexecutions,
            cache_hits=sum(event.get("event") == "cache_hit" for event in history), receipt_present=fingerprint in receipt_keys)
        if status == "pending":
            issue("tool_pending_or_interrupted", "error" if manifest.get("status") == "completed" else "warning", call_id=rid)
        elif status == "failed":
            row["error_code"] = safe_code(result.get("error_code"))
            issue("tool_failure", call_id=rid, error_code=row["error_code"])
        elif not any(event.get("event") in {"completed", "replayed", "cache_hit"} for event in history):
            issue("durable_result_without_completion_event", "info", call_id=rid)
        if reexecutions:
            issue("recovery_operation_reexecuted", "info", call_id=rid)
        if replayed:
            issue("durable_result_replayed", "info", call_id=rid)
        tools.append(row)
    source_count, evidence_count = len(records["sources"]), len(records["evidence"])
    draft, report_path = directory / "draft.md", directory / "report.md"
    body_path = draft if draft.is_file() else report_path
    citation = dict(report_present=report_path.is_file(), draft_present=draft.is_file(),
                    structural_valid=None, semantic_assessment="not_evaluated", issues=[])
    if body_path.is_file():
        body = body_path.read_text(encoding="utf-8").split("\n## Evidence Ledger\n", 1)[0]
        citation["body_sha256"] = digest(body)
        try:
            validate_citation_syntax(body)
        except CitationSyntaxError:
            citation["issues"].append(dict(evidence_id="malformed_reference"))
        matches = list(CITATION.finditer(body))
        for match in matches:
            eid = match.group(1)
            if evidence_context(eid, records["evidence"], records["sources"])["integrity"] != "valid":
                citation["issues"].append(dict(line=body[:match.start()].count("\n")+1,
                    evidence_id=eid if re.fullmatch("E[0-9a-f]{16}", eid) else "malformed_reference"))
        citation["structural_valid"] = bool(matches) and evidence_count > 0 and not citation["issues"]
        if not citation["structural_valid"]:
            issue("report_citation_integrity_gap", "error")
    elif manifest.get("status") == "completed":
        issue("completed_run_report_missing", "error")
    if manifest.get("status") == "completed" and not report_path.is_file() and body_path.is_file():
        issue("completed_run_report_missing", "error")
    slowest = sorted([row for row in calls if row["elapsed_ms"] is not None], key=lambda row: row["elapsed_ms"], reverse=True)[:5]
    repair = None
    repair_path = directory / "repair-state.json"
    if repair_path.is_file():
        state = json.loads(repair_path.read_text(encoding="utf-8"))
        selection = state.get("selection", {})
        repair = dict(rounds_used=state.get("rounds_used"),
            accepted=selection.get("accepted") if type(selection.get("accepted")) is bool else None,
            selected=selection.get("selected") if selection.get("selected") in {"original", "candidate"} else None,
            reasons=[reason if reason in REPAIR_REASONS else "other_reason" for reason in selection.get("reasons", [])])
        if repair["accepted"] is False:
            issue("repair_candidate_rejected")
        for label in state.get("checks_started", []):
            if label in {"original", "candidate"} and not (directory / "revisions" / (label + "-quality.json")).is_file():
                issue("verification_outcome_uncertain")
    return dict(schema_version=1, run_id=directory.name, manifest_sha256=digest(manifest_text),
        manifest_stable_during_read=same_manifest, database_snapshot="single_read_transaction",
        cross_file_snapshot="manifest/report are outside the database transaction; active runs may change",
        run=dict(status=manifest.get("status"), model=manifest.get("model"), mode=manifest.get("mode"),
                 resume_count=manifest.get("resume_count", 0), includes_parent_history=bool(manifest.get("parent_run_directory"))),
        metrics=metrics, budget=budget, model_budget=model_budget, source_count=source_count, evidence_count=evidence_count,
        receipt_count=receipt_count, event_count=len(events), findings=findings, model_calls=calls, repair=repair,
        slowest_model_calls=slowest, tool_operations=tools, citations=citation,
        timeline=[safe_event(event, event_id) for event_id, event in events],
        export_scope="metadata only; no model prompts, arguments, tool response bodies, evidence quotes or report body")


def markdown(result):
    counts = Counter(row["severity"] for row in result["findings"])
    lines = ["# ResearchDesk 运行诊断", "", f"Run：{result['run_id']}；状态：{result['run']['status']}。",
        f"模型调用：{result['metrics']['model_calls']}；Token：{result['metrics']['total_tokens']}。",
        f"发现：{dict(counts)}；来源：{result['source_count']}；证据：{result['evidence_count']}。", "",
        "| 预算 | 已用 | 上限 | 剩余 |", "|---|---:|---:|---:|"]
    for key, budget in result["budget"].items():
        lines.append(f"| {key} | {budget['used']} | {budget['limit']} | {budget['remaining']} |")
    model_budget = result.get('model_budget') or {}
    if 'verification_slots_held' in model_budget:
        lines += ['', f"待发验证预留：{model_budget['verification_slots_held']}；生成可用：{model_budget['generation_remaining']}。预留不是已发请求，失败/未知请求仍按已用计数。"]
    lines += ["", "| 诊断代码 | 严重度 | 调用定位 |", "|---|---|---|"]
    for row in result["findings"]:
        lines.append(f"| {row['code']} | {row['severity']} | {row.get('call_id', row.get('model_call_id', ''))} |")
    lines += ["", "## 模型调用耗时（已知值，非整次任务墙钟耗时）", "",
              "| 调用 | 角色 | 状态 | 毫秒 | Token |", "|---|---|---|---:|---:|"]
    for row in result["model_calls"]:
        lines.append(f"| {row['model_call_id']} | {row['model_role']} | {row['status']} | {row['elapsed_ms']} | {(row['usage'] or {}).get('total_tokens')} |")
    lines += ["", f"引用结构有效：{result['citations']['structural_valid']}；语义支持：本命令未评估。",
              "详细工具状态与按数据库顺序排列的事件时间线见result.json。未知值保留null，不据此断言远程操作未发生。",
              "数据库来自单次只读事务；运行中的manifest、报告与数据库不是跨文件原子快照。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Offline, read-only diagnosis of an existing run")
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, default=Path("runs/diagnostics"))
    args = parser.parse_args()
    destination = args.output / uuid4().hex
    try:
        if args.output.resolve().is_relative_to(args.run.resolve()):
            raise VerificationError("Write diagnostic output outside the source run directory.")
        result = inspect_run(args.run)
        destination.mkdir(parents=True, exist_ok=False)
        atomic_text(destination / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
        atomic_text(destination / "summary.md", markdown(result))
        print(f"Run diagnosis: {destination / 'summary.md'}")
        print(f"Findings: {len(result['findings'])}; no model requests.")
        return 0
    except Exception as error:
        print(f"Diagnosis failed ({type(error).__name__}); no accepted diagnostic report written.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
