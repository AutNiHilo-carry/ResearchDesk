"""Offline mechanism acceptance using fixed fixtures, not research quality validation."""
import argparse
import json
from pathlib import Path
from uuid import uuid4
from .ledger import Ledger, CitationValidationError
from .policy import ToolExecutor, ToolLimits, ToolSpec
from .storage import atomic_text
from .tools import ResearchTools, SearchArgs
from .verification import assess, prepare, split_units


def run(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    run_dir = directory / "research"
    limits = ToolLimits(max_calls=2, max_provider_requests=0, max_retries=0)
    ledger = Ledger(run_dir)
    try:
        source = ledger.add_source("Fixed fictional fixture", "fixture://acceptance/logs",
                                   "Fixture supports logs.", "local")
        runtime = ResearchTools(ledger, "", limits, allowed_tools={"record_evidence"})
        denied = runtime.executor.execute("search_sources", {"query": "anything"}, operation_id="denied")
        forged = runtime.executor.execute("record_evidence", dict(claim="Backups", source_id=source["id"],
            snippet="Fixture supports automatic backups."), operation_id="forged")
        args = dict(claim="Logs", source_id=source["id"], snippet="Fixture supports logs.")
        evidence = runtime.executor.execute("record_evidence", args, operation_id="record")
        before = ledger.store.counters()
    finally:
        ledger.store.close()
    # Reopen the actual SQLite store, rather than retaining an in-memory receipt.
    ledger = Ledger(run_dir, resume=True)
    try:
        runtime = ResearchTools(ledger, "", limits, allowed_tools={"record_evidence"})
        replay = runtime.executor.execute("record_evidence", args, operation_id="record")
        after = ledger.store.counters()
        exhausted = runtime.executor.execute("record_evidence", args, operation_id="extra")
        unknown_rejected = False
        try:
            ledger.validate_report("Unknown reference [E0000000000000000]")
        except CitationValidationError:
            unknown_rejected = True
        body = f"Fixture supports logs.[{evidence['id']}]\nFixture performs automatic backups."
        ledger.save_report(body)
        payload, fingerprint = prepare(split_units(body), ledger.evidence, ledger.sources)
        class ScriptedJudge:
            prompt_version = "scripted-fixture-not-a-model"
            def __call__(self, payload):
                return dict(units=[dict(unit_id=u["unit_id"], factual=True, reason="Fixed fixture label",
                    links=[dict(evidence_id=eid, verdict="supported", reason="Fixed fixture label")
                           for eid in u["citations"]]) for u in payload["units"]])
        quality = assess(payload, ScriptedJudge())
        quality.update(label_origin="scripted_fixture", input_sha256=fingerprint,
                       proves_model_accuracy=False, human_review="not_performed")
        atomic_text(run_dir / "run.json", json.dumps(dict(status="completed", mode="offline_acceptance_fixture")))
        evidence_count = len(ledger.evidence)
    finally:
        ledger.store.close()
    # Exercise request admission directly; there is no HTTP adapter or credentials.
    provider_dir = directory / "provider-admission"
    ledger = Ledger(provider_dir)
    transport_attempts = [0]
    try:
        def lookup(query):
            executor.provider_timeout()
            transport_attempts[0] += 1
            raise AssertionError("Zero provider quota should prevent transport admission")
        executor = ToolExecutor(provider_dir, {"fixture_lookup": ToolSpec(SearchArgs, lookup, read_only=True)},
            ToolLimits(max_calls=1, max_provider_requests=0, max_retries=0), store=ledger.store)
        provider_denial = executor.execute("fixture_lookup", {"query": "fixture"}, operation_id="provider-zero")
        provider_counts = ledger.store.counters()
    finally:
        ledger.store.close()
    checks = dict(permission_denied=denied.get("error_code") == "permission_denied",
        forged_quote_rejected=forged.get("error_code") == "invalid_evidence",
        durable_receipt_reused=replay == evidence and before == after and evidence_count == 1,
        persisted_call_budget_enforced=exhausted.get("error_code") == "call_budget_exceeded",
        unknown_citation_rejected=unknown_rejected,
        provider_admission_denied=provider_denial.get("error_code") == "request_budget_exceeded"
            and transport_attempts[0] == 0 and provider_counts["requests"] == 0,
        uncited_fact_retained_in_denominator=quality["metrics"]["evidence_coverage"] == 0.5)
    result = dict(version="offline-acceptance-demo-v1", status="mechanism_checks_passed" if all(checks.values()) else "failed",
        checks=checks, counter_before_reopen=before, counter_after_reopen=after,
        provider_counters=provider_counts, model_requests=0, external_requests=0,
        fixture_quality_metrics=quality["metrics"], label_origin="scripted_fixture_not_human_or_model",
        scope="application receipt reopen; not process-kill graph recovery or research task success")
    atomic_text(directory / "fixture-quality.json", json.dumps(quality, ensure_ascii=False, indent=2))
    atomic_text(directory / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
    lines = ["# ResearchDesk 离线机制验收演示", "", "固定虚构资料与固定标签，无模型、网络或密钥加载。", "",
             f"状态：{result['status']}。", ""]
    lines += [f"- {name}: {'通过' if passed else '失败'}" for name, passed in checks.items()]
    lines += ["", "[报告与证据台账](research/report.md) · [固定标签质量结果](fixture-quality.json)", "",
        "覆盖率50%是固定标签下的预期缺口，不是真实模型准确率。关闭/重开SQLite验证回执复用，不等同进程kill或LangGraph恢复。", ""]
    atomic_text(directory / "summary.md", "\n".join(lines))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None, help="New directory only")
    args = parser.parse_args()
    directory = args.output or Path("runs/offline-acceptance") / uuid4().hex
    result = run(directory)
    print(f"Offline acceptance: {result['status']}; {directory / 'summary.md'}")
    return 0 if result["status"] == "mechanism_checks_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
