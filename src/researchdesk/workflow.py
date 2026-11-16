"""Resumable application orchestration over the existing CLI, repair and diagnostics."""
import argparse
import hashlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from .cli import main as research_main, POLICY_OPTIONS
from .policy import ToolLimits
from .inspect_run import inspect_run, markdown as diagnostic_markdown
from .ledger import now
from .model_budget import ModelBudgetExceeded, VERSION as MODEL_POLICY_VERSION
from .repair import repair_run, selection
from .report_policy import policy_spec
from .search_context import policy_spec as search_context_spec,validate_spec as validate_search_context
from .storage import RecoveryError, RunLock, atomic_text
from .verification import SYSTEM_PROMPT_V2, assess, digest, load_run, prepare, split_units


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def owned_run(directory, category, run_id):
    if not isinstance(run_id, str) or not re.fullmatch("[0-9a-f]{32}", run_id):
        raise RecoveryError("Workflow contains an invalid run identity.")
    result = (directory / category / run_id).resolve()
    if not result.is_relative_to(directory.resolve()):
        raise RecoveryError("Workflow run escapes its own directory.")
    return result


def create_workflow(directory, question, documents, *, model, local_only=False, context_mode="full", maximum=12, report_policy="v1", tool_limits=None, search_context='full'):
    policy_spec(report_policy)
    search_policy = search_context_spec(search_context)
    if local_only and search_context != 'full':
        raise RecoveryError('Search preview requires web-enabled research.')
    limits = tool_limits if tool_limits is not None else ToolLimits()
    if not isinstance(limits, ToolLimits):
        raise RecoveryError("Workflow tool limits must be a validated ToolLimits instance.")
    if not question.strip() or type(maximum) is not int or not 0 <= maximum <= 1000:
        raise RecoveryError("Workflow needs a question and a model call limit of 0..1000.")
    if context_mode not in {"full", "catalog"} or (local_only and not documents) or (context_mode == "catalog" and not local_only):
        raise RecoveryError("Catalog needs local-only imported sources.")
    if len(documents) > 4:
        raise RecoveryError("Workflow currently supports at most four imported documents.")
    snapshots = []
    for path in documents:
        path = path.resolve(strict=True)
        raw = path.read_bytes()
        if path.suffix.lower() not in {".md", ".txt"} or len(raw) > 200000:
            raise RecoveryError("Documents must be UTF-8 .md/.txt files no larger than 200 KB.")
        raw.decode("utf-8")
        snapshots.append((path.name, raw))
    directory.mkdir(parents=True, exist_ok=False)
    frozen = []
    if snapshots:
        (directory / "corpus").mkdir()
    for number, (name, raw) in enumerate(snapshots, 1):
        relative = "corpus/" + f"{number:02d}-{name}"
        (directory / relative).write_bytes(raw)
        frozen.append(dict(path=relative, sha256=hashlib.sha256(raw).hexdigest()))
    state = dict(version=1, created_at=now(), status="ready", phase="research", source_run_id=None,
        candidate_run_id=uuid4().hex, human_review="pending",
        plan=dict(question=question, documents=frozen, model=model, local_only=local_only,
                  context_mode=context_mode, max_model_calls=maximum, report_policy_version=report_policy,
                  tool_policy=asdict(limits),search_context_policy=search_policy))
    atomic_text(directory / "workflow.json", json.dumps(state, ensure_ascii=False, indent=2))
    return state


def load_quality(path, body, evidence, sources):
    cached = read_json(path)
    payload, fingerprint = prepare(split_units(body), evidence, sources)
    if cached.get("input_sha256") != fingerprint or cached.get("prompt_sha256") != digest(SYSTEM_PROMPT_V2):
        raise RecoveryError("Cached verification does not match current report/evidence or the required judge prompt.")
    result = cached.get("result", {})
    if result.get("status") != "complete":
        raise RecoveryError("Cached verification is incomplete.")
    # Revalidate saved labels and recompute metrics without invoking any model.
    rows = [dict(unit_id=row["unit_id"], factual=row["factual"], reason=row["reason"], links=[
        dict(evidence_id=link["evidence_id"], verdict="insufficient" if link["verdict"] == "invalid_evidence" else link["verdict"],
             reason=link["reason"]) for link in row["links"]]) for row in result["units"]]
    return assess(payload, lambda _: {"units": rows})


def validate_manifest(manifest, plan):
    expected_policy = dict(version=MODEL_POLICY_VERSION, max_calls=plan["max_model_calls"])
    if manifest.get("question") != plan["question"] or manifest.get("model") != plan["model"] or manifest.get("model_policy") != expected_policy:
        raise RecoveryError("Workflow question, model or quota changed.")
    if (manifest.get("context_policy", {}).get("strategy") != plan["context_mode"]
            or manifest.get("allowed_tools") != (["read_source", "record_evidence"] if plan["local_only"] else None)):
        raise RecoveryError("Workflow context strategy or tool scope changed.")
    expected_report = policy_spec(plan.get("report_policy_version", "v1"))
    actual_report = manifest.get("report_policy")
    if actual_report != expected_report and not (actual_report is None and expected_report["version"] == "v1"):
        raise RecoveryError("Workflow report-writing policy changed.")
    if "tool_policy" in plan and manifest.get("tool_policy") != plan["tool_policy"]:
        raise RecoveryError("Workflow tool policy differs from its frozen limits.")
    if 'search_context_policy' in plan:
        expected_search = plan['search_context_policy']
        validate_search_context(expected_search)
        actual_search = manifest.get('search_context_policy')
        if actual_search != expected_search and not (actual_search is None and expected_search['mode']=='full'):
            raise RecoveryError('Workflow search context policy changed.')


def require_assessable(directory):
    body = load_run(directory)[0]
    draft = directory / "draft.md"
    if "```" in body or "~~~" in body or (draft.is_file() and draft.read_text(encoding="utf-8").strip() != body.strip()):
        raise RecoveryError("Report contains fenced or omitted sections outside the line verifier's coverage; separate review is required.")


def assemble(directory, state):
    source = owned_run(directory, "research", state["source_run_id"])
    candidate = owned_run(directory, "candidate", state["candidate_run_id"])
    require_assessable(source)
    require_assessable(candidate)
    original, _, _, source_meta = load_run(source)
    body, evidence, sources, metadata = load_run(candidate)
    repair = read_json(candidate / "repair-state.json")
    if repair.get("status") != "completed" or repair.get("rounds_used") not in {0, 1}:
        raise RecoveryError("Repair phase is not complete or exceeded its one-round limit.")
    for manifest in (source_meta, metadata):
        validate_manifest(manifest, state["plan"])
    if digest(original) != repair["original_report_sha256"]:
        raise RecoveryError("Original report changed after repair began.")
    chosen = repair["selection"]["selected"]
    if chosen not in {"original", "candidate"}:
        raise RecoveryError("Unknown repair selection.")
    expected_hash = repair["original_report_sha256"] if chosen == "original" else repair["candidate_sha256"]
    if digest(body) != expected_hash:
        raise RecoveryError("Selected report changed after verification.")
    before_path = candidate / "revisions" / "original-quality.json"
    selected_path = candidate / "revisions" / (chosen + "-quality.json")
    before = load_quality(before_path, original, evidence, sources)
    current = before if chosen == "original" else load_quality(selected_path, body, evidence, sources)
    gate = selection(before, current)
    if gate["accepted"] != repair["selection"]["accepted"]:
        raise RecoveryError("Saved selection disagrees with the recomputed support gate.")
    return dict(status="model_support_gate_passed" if gate["accepted"] else "needs_review",
        selected=chosen, selected_report=str(candidate / "report.md"), report_sha256=file_hash(candidate / "report.md"),
        selected_body_sha256=digest(body), quality=current["metrics"], support_gate=gate,
        repair_selection=repair["selection"], repair_rounds=repair["rounds_used"], human_review="pending",
        assessment_hashes={"original": file_hash(before_path), "selected": file_hash(selected_path)})


def write_diagnostics(directory, state):
    destination = directory / "diagnostics" / uuid4().hex
    exported = False
    for name, category, run_id in (("research", "research", state.get("source_run_id")),
                                   ("selected", "candidate", state["candidate_run_id"])):
        if run_id is None:
            continue
        run = owned_run(directory, category, run_id)
        if (run / "run.json").is_file() and (run / "application.sqlite3").is_file():
            diagnosis = inspect_run(run)
            destination.mkdir(parents=True, exist_ok=True)
            atomic_text(destination / (name + ".json"), json.dumps(diagnosis, ensure_ascii=False, indent=2))
            atomic_text(destination / (name + ".md"), diagnostic_markdown(diagnosis))
            exported = True
    return str(destination) if exported else None


def run_workflow(directory, *, research_runner=research_main, repair_runner=repair_run):
    directory = directory.resolve()
    with RunLock(directory):
        path = directory / "workflow.json"
        state = read_json(path)
        if state.get("version") != 1 or state.get("phase") not in {"research", "repair", "diagnostics", "done"}:
            raise RecoveryError("Unsupported workflow state.")
        def save():
            atomic_text(path, json.dumps(state, ensure_ascii=False, indent=2))
        def finish_attempt(status, code, **fields):
            state.update(status=status, last_returncode=code, **fields)
            if state["phase"] != "done":
                try:
                    state["diagnostics"] = write_diagnostics(directory, state)
                except Exception as error:
                    state["diagnostic_error_type"] = type(error).__name__
            save()
            atomic_text(directory / "summary.md", summary(state))
            return state
        try:
            plan = state["plan"]
            documents = []
            for row in plan["documents"]:
                frozen = (directory / row["path"]).resolve()
                if not frozen.is_relative_to(directory) or file_hash(frozen) != row["sha256"]:
                    raise RecoveryError("Frozen workflow document changed.")
                documents.append(frozen)
            if state["phase"] == "done":
                current = assemble(directory, state)
                if current != state["result"]:
                    raise RecoveryError("Completed workflow artifacts changed; refusing to reuse stale approval.")
                if not (directory / "summary.md").is_file():
                    atomic_text(directory / "summary.md", summary(state))
                return state
            if state["phase"] == "research":
                root = directory / "research"
                if state["source_run_id"] is None and root.exists():
                    runs = [p for p in root.iterdir() if p.is_dir() and re.fullmatch("[0-9a-f]{32}", p.name)]
                    if len(runs) > 1:
                        raise RecoveryError("Workflow research root has multiple runs.")
                    if runs:
                        state["source_run_id"] = runs[0].name
                        save()
                source = owned_run(directory, "research", state["source_run_id"]) if state["source_run_id"] else None
                if source is not None and (source / "run.json").is_file():
                    validate_manifest(read_json(source / "run.json"), plan)
                completed = source is not None and (source / "report.md").is_file() and (source / "run.json").is_file() and read_json(source / "run.json").get("status") == "completed"
                if not completed:
                    state.update(status="research_running")
                    save()
                    args = (["--resume", source.name, "--output", str(root)] if source else [
                        "--output", str(root), "--context-mode", plan["context_mode"], "--max-model-calls", str(plan["max_model_calls"])])
                    if source is None:
                        if "tool_policy" in plan:
                            for field, option in POLICY_OPTIONS.items():
                                args.extend(["--"+option.replace("_", "-"), str(plan["tool_policy"][field])])
                        args.extend(["--report-policy", plan.get("report_policy_version", "v1")])
                        if 'search_context_policy' in plan and not plan['local_only']:
                            validate_search_context(plan['search_context_policy'])
                            args.extend(['--search-context',plan['search_context_policy']['mode']])
                        if plan["local_only"]:
                            args.append("--local-only")
                        for document in documents:
                            args.extend(["--document", str(document)])
                        args.extend(["--", plan["question"]])  # A question starting with -- is data, not CLI syntax.
                    try:
                        code = research_runner(args, model_name=plan["model"] if source is None else None)
                    except SystemExit as error:
                        code = error.code if type(error.code) is int else 2
                    if state["source_run_id"] is None and root.exists():
                        runs = [p for p in root.iterdir() if p.is_dir() and re.fullmatch("[0-9a-f]{32}", p.name)]
                        if len(runs) == 1:
                            state["source_run_id"] = runs[0].name
                    if code != 0:
                        return finish_attempt("budget_exhausted" if code == 4 else "interrupted" if code == 130 else "research_failed", code)
                state.update(phase="repair", status="research_completed")
                save()
            source = owned_run(directory, "research", state["source_run_id"])
            candidate = owned_run(directory, "candidate", state["candidate_run_id"])
            validate_manifest(read_json(source / "run.json"), plan)
            if state["phase"] == "repair":
                require_assessable(source)
                repair_path = candidate / "repair-state.json"
                if candidate.exists() and not repair_path.is_file():
                    raise RecoveryError("Partial repair fork needs inspection; workflow will not create another paid branch.")
                if not repair_path.is_file() or read_json(repair_path).get("status") != "completed":
                    if (candidate / "run.json").is_file():
                        validate_manifest(read_json(candidate / "run.json"), plan)
                    state.update(status="repair_running")
                    save()
                    repair_runner(source if not candidate.exists() else None, candidate, resume=candidate.exists())
                state.update(phase="diagnostics", status="repair_completed")
                save()
            result = assemble(directory, state)
            diagnostics = write_diagnostics(directory, state)
            state.update(phase="done", result=result, diagnostics=diagnostics, completed_at=now())
            return finish_attempt(result["status"], 0 if result["status"] == "model_support_gate_passed" else 3)
        except KeyboardInterrupt:
            return finish_attempt("interrupted", 130)
        except ModelBudgetExceeded:
            return finish_attempt("budget_exhausted", 4)
        except Exception as error:
            candidate = owned_run(directory, "candidate", state["candidate_run_id"])
            repair = read_json(candidate / "repair-state.json") if (candidate / "repair-state.json").is_file() else {}
            uncertain = any(label in {"original", "candidate"} and not (candidate / "revisions" / (label + "-quality.json")).is_file()
                            for label in repair.get("checks_started", []))
            fields = dict(error_type=type(error).__name__)
            if isinstance(error, RecoveryError):
                fields["reason"] = str(error)
            return finish_attempt("verification_outcome_uncertain" if uncertain else "needs_inspection", 3 if uncertain else 1, **fields)


def summary(state):
    result = state.get("result") or {}
    lines = ["# ResearchDesk 受检工作流", "", f"状态：{state['status']}；阶段：{state['phase']}。",
        f"研究Run：{state.get('source_run_id')}；候选Run：{state['candidate_run_id']}。",
        f"模型调用上限：{state['plan']['max_model_calls']}；人工复核：pending。", ""]
    tool_policy = state['plan'].get('tool_policy')
    if tool_policy is not None:
        lines += [f"工具调用上限：{tool_policy['max_calls']}；外部请求上限（含重试）：{tool_policy['max_provider_requests']}；额外重试上限：{tool_policy['max_retries']}。", ""]
    if 'search_context_policy' in state['plan']:
        lines += [f"搜索工具上下文：{state['plan']['search_context_policy']['mode']}。",'']
    if state.get("reason"):
        lines += [f"待检查原因：{state['reason']}", ""]
    if state.get("diagnostics"):
        lines += [f"运行诊断：{state['diagnostics']}", ""]
    if result:
        lines += [f"选择：{result['selected']}；修订轮数：{result['repair_rounds']}。",
                  f"可供复核的报告：{result['selected_report']}",
                  f"引用指标：{result['quality']}", f"门槛判定：{result['support_gate']}", ""]
    lines += ["model_support_gate_passed仅表示本次模型辅助证据检查通过，不代表独立人工审核或业务任务成功。",
              "needs_review保留原稿与候选供复核，不把生成文件等同验收成功。恢复复用同一运行，不自动重试到通过。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Research, one-round evidence repair and diagnosis in a resumable workflow")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--document", type=Path, action="append", default=[])
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--context-mode", choices=["full", "catalog"])
    parser.add_argument("--report-policy", choices=["v1", "v2"])
    parser.add_argument('--search-context',choices=['full','preview'])
    parser.add_argument("--max-model-calls", type=int)
    parser.add_argument("--output", type=Path)
    for field, option in POLICY_OPTIONS.items():
        parser.add_argument("--"+option.replace("_", "-"), type=float if field in {"timeout_seconds", "max_duration_seconds"} else int)
    args = parser.parse_args()
    if args.resume and (args.question or args.document or args.local_only or args.context_mode is not None or args.report_policy is not None or args.search_context is not None or args.max_model_calls is not None or args.output is not None or any(getattr(args, option) is not None for option in POLICY_OPTIONS.values())):
        parser.error("Resume accepts only the existing workflow directory; no replacement inputs or quota.")
    if not args.resume and (not args.question or not args.question.strip()):
        parser.error("Supply a question or --resume WORKFLOW_DIRECTORY")
    load_dotenv()
    directory = args.resume or (args.output or Path("runs/workflows")) / uuid4().hex
    try:
        if not args.resume:
            defaults = asdict(ToolLimits())
            limits = ToolLimits(**{field: getattr(args, option) if getattr(args, option) is not None else defaults[field]
                                   for field, option in POLICY_OPTIONS.items()})
            create_workflow(directory, args.question, args.document, model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                local_only=args.local_only, context_mode=args.context_mode or "full",
                maximum=args.max_model_calls if args.max_model_calls is not None else 12, report_policy=args.report_policy or "v1",tool_limits=limits,
                search_context=args.search_context or 'full')
        print(f"Workflow: {directory}", flush=True)
        state = run_workflow(directory)
        print(f"Workflow status: {state['status']}; summary: {directory / 'summary.md'}")
        return state["last_returncode"]
    except Exception as error:
        print(f"Workflow failed ({type(error).__name__}); artifacts: {directory}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
