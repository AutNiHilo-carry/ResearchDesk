import argparse
import json
import os
import re
import sys
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from .ledger import CitationValidationError, Ledger, now
from .policy import ToolLimits
from .storage import RecoveryError, RunLock, atomic_text
from .context_policy import prepare_input, verify_saved_input
from .model_budget import ModelBudgetExceeded, VERSION as MODEL_POLICY_VERSION
from .report_policy import policy_spec
from .search_context import policy_spec as search_context_spec,verify_completed_policy
from .draft_inspection import policy_spec as draft_inspection_spec, verify_completed_policy as verify_completed_inspection
from .final_body_gate import policy_spec as final_body_spec,bind_policy as bind_final_body,verify_completed as verify_final_body

POLICY_OPTIONS = {"max_calls": "max_tool_calls", "max_provider_requests": "max_provider_requests",
                  "timeout_seconds": "tool_timeout", "max_retries": "tool_retries",
                  "max_duration_seconds": "tool_deadline"}


def run_demo(ledger):
    text = "ResearchDesk records immutable source snapshots and exact evidence quotes."
    source = ledger.add_source("Offline fixture", "fixture://researchdesk", text, "fixture")
    evidence = ledger.add_evidence("演示记录支持将引用追溯到保存的原文。", source["id"], text)
    return ledger.save_report(
        "# 离线数据流程演示\n\n本报告由固定样例生成，未调用模型或网络。\n\n"
        f"证据记录关联保存的原文。[{evidence['id']}]\n\n"
        "此演示仅验证资料、证据和报告的落盘流程，不能验证研究质量。")


def main(argv=None, *, model_name=None):
    parser = argparse.ArgumentParser(description="ResearchDesk")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--resume", help="resume a run ID under --output")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--document", action="append", default=[], type=Path)
    parser.add_argument("--local-only", action="store_true", help="allow only imported documents and evidence tools")
    parser.add_argument("--context-mode", choices=["full", "catalog"], help="imported source presentation (default: full)")
    parser.add_argument("--report-policy", choices=["v1", "v2"], help="writing contract (default: unchanged v1)")
    parser.add_argument('--draft-inspection-limit',type=int,help='Opt-in inspect_report feedback limit; no semantic acceptance')
    parser.add_argument('--draft-inspection-final-turn', action='store_true',
                        help='Use the last budgeted model turn to write the report without tools')
    parser.add_argument('--final-body-limit',type=int,help='Opt-in final publication length and citation-structure gate')
    parser.add_argument('--reuse-checked-draft-on-overflow', action='store_true',
                        help='Publish the latest valid inspected draft when the final candidate only exceeds its length limit')
    parser.add_argument('--search-context',choices=['full','preview'],help='search tool response presentation; default full')
    parser.add_argument("--output", type=Path, default=Path("runs"))
    parser.add_argument("--max-tool-calls", type=int)
    parser.add_argument("--max-model-calls", type=int, help="durable model request limit; default: legacy graph limits only")
    parser.add_argument("--max-provider-requests", type=int)
    parser.add_argument("--tool-timeout", type=float)
    parser.add_argument("--tool-retries", type=int)
    parser.add_argument("--tool-deadline", type=float)
    args = parser.parse_args(argv)
    if args.max_model_calls is not None and not 0 <= args.max_model_calls <= 1000:
        parser.error("--max-model-calls must be 0..1000")
    if args.draft_inspection_final_turn and (args.resume or args.demo or args.draft_inspection_limit is None
                                           or not args.max_model_calls):
        parser.error('--draft-inspection-final-turn requires a new live run with --draft-inspection-limit and a positive --max-model-calls')
    if args.draft_inspection_limit is not None:
        try: draft_inspection_spec(args.draft_inspection_limit)
        except ValueError: parser.error('--draft-inspection-limit must be 1..100000')
        if args.resume or args.demo:
            parser.error('Draft inspection is only configured on new live runs.')
    if args.final_body_limit is not None:
        try: final_body_spec(args.final_body_limit)
        except ValueError: parser.error('--final-body-limit must be 1..100000')
        if args.resume or args.demo: parser.error('Final body gate is only configured on new live runs.')
    if args.reuse_checked_draft_on_overflow and (args.resume or args.demo or args.final_body_limit is None
                                                or args.draft_inspection_limit is None):
        parser.error('--reuse-checked-draft-on-overflow requires a new live run with both draft and final body limits')
    load_dotenv()
    if args.doctor:
        present = {key: bool(os.getenv(key, "").strip()) for key in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY")}
        for key, value in present.items():
            print(f"{key}: {'configured' if value else 'missing'}")
        return 0 if all(present.values()) else 1
    if args.resume:
        if not re.fullmatch(r"[0-9a-f]{32}", args.resume):
            parser.error("--resume requires a 32-character run ID, not a path")
        if args.question or args.document or args.demo or args.local_only or args.context_mode is not None or args.report_policy is not None or args.search_context is not None or args.max_model_calls is not None or any(
                getattr(args, option) is not None for option in POLICY_OPTIONS.values()):
            parser.error("Resume uses the saved input/model/policy; do not supply replacements")
    elif not args.demo and (not args.question or not args.question.strip()):
        parser.error("provide a research question, --demo or --resume RUN_ID")
    if args.local_only and (not args.document or args.demo):
        parser.error("--local-only requires --document and cannot be used with --demo")
    if args.context_mode == "catalog" and not args.local_only:
        parser.error("--context-mode catalog requires --local-only and imported documents")
    if args.demo and args.context_mode is not None:
        parser.error("--context-mode is for live research, not --demo")
    if args.demo and args.max_model_calls is not None:
        parser.error("--max-model-calls is for live research, not --demo")
    if args.demo and args.report_policy is not None:
        parser.error("--report-policy is for live research, not --demo")
    if args.search_context is not None and (args.demo or args.local_only):
        parser.error('--search-context is for web-enabled research only')
    directory = args.output / (args.resume or uuid4().hex)
    metadata = None
    stack = ExitStack()
    try:
        if args.resume:
            if not (directory / "run.json").is_file():
                raise RecoveryError("Run does not exist or has no manifest.")
            stack.enter_context(RunLock(directory))
            metadata = json.loads((directory / "run.json").read_text(encoding="utf-8"))
            if metadata.get("format_version") != 2 or metadata.get("mode") != "live":
                metadata = None
                raise RecoveryError("This legacy/demo run cannot be resumed.")
            if metadata.get("status") == "completed" and (directory / "report.md").is_file():
                verify_completed_policy(directory)
                verify_completed_inspection(directory)
                verify_final_body(directory)
                print(f"Report: {directory / 'report.md'} (already completed)")
                return 0
            if not metadata.get("prepared"):
                raise RecoveryError("Initial input was not prepared before interruption; start a new run.")
            limits = ToolLimits(**metadata["tool_policy"])
        else:
            defaults = asdict(ToolLimits())
            limits = ToolLimits(**{key: getattr(args, option) if getattr(args, option) is not None
                                   else defaults[key] for key, option in POLICY_OPTIONS.items()})
        if not args.demo:
            required = ["DEEPSEEK_API_KEY"]
            if (metadata["requires_search"] if args.resume else not args.document):
                required.append("TAVILY_API_KEY")
            missing = [key for key in required if not os.getenv(key, "").strip()]
            if missing:
                print("Missing configuration: " + ", ".join(missing), file=sys.stderr)
                return 2
        ledger = Ledger(directory, resume=bool(args.resume))
        if not args.resume:
            stack.enter_context(RunLock(directory))
        stack.callback(ledger.store.close)
        if not args.resume:
            metadata = dict(format_version=2, question=args.question,
                            mode="offline_fixture" if args.demo else "live",
                            started_at=now(), status="preparing", prepared=False,
                            requires_search=not args.document,
                            model=model_name or os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                            allowed_tools=["read_source", "record_evidence"] if args.local_only else None,
                            tool_scope="local_documents_only" if args.local_only else "default",
                            model_settings=dict(temperature=0, timeout=60, max_retries=0, max_tokens=4096),
                            tool_policy=asdict(limits), resume_count=0)
            metadata["model_policy"] = (dict(version=MODEL_POLICY_VERSION, max_calls=args.max_model_calls)
                                        if args.max_model_calls is not None else None)
            if not args.demo:
                metadata["report_policy"] = policy_spec(args.report_policy or "v1")
                metadata['search_context_policy'] = search_context_spec(args.search_context or 'full')
            if args.draft_inspection_limit is not None:
                metadata['draft_inspection_policy'] = draft_inspection_spec(
                    args.draft_inspection_limit, final_turn=args.draft_inspection_final_turn)
                if metadata['allowed_tools'] is not None:
                    metadata['allowed_tools'].append('inspect_report')
            if args.final_body_limit is not None:
                metadata['final_body_policy'] = final_body_spec(
                    args.final_body_limit, reuse_checked_draft=args.reuse_checked_draft_on_overflow)
            atomic_text(directory / "run.json", json.dumps(metadata, ensure_ascii=False, indent=2))
            documents = []
            for path in args.document:
                resolved = path.resolve(strict=True)
                if resolved.suffix.lower() not in {".txt", ".md"} or resolved.stat().st_size > 200000:
                    raise RecoveryError("Documents must be UTF-8 .txt/.md files smaller than 200 KB")
                documents.append(ledger.add_source(resolved.name, str(resolved),
                                                  resolved.read_text(encoding="utf-8"), "local"))
            initial, metadata["context_policy"] = prepare_input(args.question, documents, args.context_mode or "full")
            atomic_text(directory / "input.json", json.dumps(initial, ensure_ascii=False))
            metadata["prepared"] = True
        else:
            initial = json.loads((directory / "input.json").read_text(encoding="utf-8"))
            verify_saved_input(initial, metadata.get("context_policy"))
            metadata["resume_count"] += 1
        print(f"Run ID: {directory.name}", flush=True)
        metadata.update(status="running", last_started_at=now())
        metadata.pop("error_type", None)
        metadata.pop("validation_error", None)
        atomic_text(directory / "run.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        if args.demo:
            report_path = run_demo(ledger)
        else:
            from .recovery import run_graph
            bind_final_body(ledger)
            options = {} if metadata.get("allowed_tools") is None else {"allowed_tools": metadata["allowed_tools"]}
            result = run_graph(ledger, initial, limits, metadata["model"], **options)
            message = result["messages"][-1]
            metadata["response"] = {"finish_reason": message.response_metadata.get("finish_reason"),
                                    "usage": message.usage_metadata}
            content = message.content
            if isinstance(content, list):
                content = "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
            from .final_body_gate import select_body
            selected, selection = select_body(ledger, content)
            report_path = ledger.save_report(selected)
            if selection is not None:
                metadata['overflow_selection'] = selection
        metadata.update(status="completed", finished_at=now(), report=str(report_path))
        atomic_text(directory / "run.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        if metadata.get('overflow_selection') is not None:
            print(f"Published the checked draft. Oversized candidate: {directory / 'overflow-candidate.md'}")
        print(f"Report: {report_path}")
        return 0
    except (Exception, KeyboardInterrupt) as error:
        # Report failure without provider response bodies, credentials or chain-of-thought.
        if metadata is not None:
            metadata.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                            error_type=type(error).__name__, finished_at=now())
            if isinstance(error, ModelBudgetExceeded):
                metadata["status"] = "budget_exhausted"
            if isinstance(error, (CitationValidationError, RecoveryError)):
                metadata["validation_error"] = str(error)
            atomic_text(directory / "run.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        if isinstance(error, (CitationValidationError, RecoveryError)):
            print(str(error), file=sys.stderr)
        print(f"Run failed ({type(error).__name__}). Artifacts: {directory}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 4 if isinstance(error, ModelBudgetExceeded) else 1
    finally:
        stack.close()


if __name__ == "__main__":
    raise SystemExit(main())

