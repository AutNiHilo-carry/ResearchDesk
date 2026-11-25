import json
from dataclasses import asdict
from pathlib import Path
import sys
import pytest
from langchain_core.messages import AIMessage
from researchdesk.context_policy import prepare_input
from researchdesk.ledger import Ledger
from researchdesk.model_budget import VERSION, model_budget_for
from researchdesk.policy import ToolLimits
from researchdesk.repair import repair_run
from researchdesk.storage import RecoveryError, atomic_text
from researchdesk.verification import SYSTEM_PROMPT_V2, digest
from researchdesk.workflow import create_workflow, run_workflow
from researchdesk import workflow as orchestration


class Checker:
    prompt_sha256 = digest(SYSTEM_PROMPT_V2)
    prompt_version = "report-quote-v2"
    usage = dict(input_tokens=1, output_tokens=1, total_tokens=2)
    def __init__(self, verdict="supported"):
        self.verdict = verdict
        self.calls = 0
    def __call__(self, payload):
        self.calls += 1
        return {"units": [dict(unit_id=row["unit_id"], factual=True, reason="Fixture reference",
            links=[dict(evidence_id=eid, verdict=self.verdict, reason="Fixture decision") for eid in row["citations"]])
            for row in payload["units"]]}


@pytest.fixture
def workflow(tmp_path):
    document = tmp_path / "source.md"
    document.write_text("A fact.", encoding="utf-8")
    root = tmp_path / "workflow"
    create_workflow(root, "Question", [document], model="fixture", local_only=True)
    calls = []
    def research(args, *, model_name=None):
        calls.append(list(args))
        output = Path(args[args.index("--output")+1])
        if "--resume" in args:
            run = output / args[args.index("--resume")+1]
            ledger = Ledger(run, resume=True)
            metadata = json.loads((run / "run.json").read_text(encoding="utf-8"))
            evidence = next(iter(ledger.evidence.values()))
        else:
            run = output / ("a" * 32)
            ledger = Ledger(run)
            doc = Path(args[args.index("--document")+1])
            source = ledger.add_source(doc.name, str(doc), doc.read_text(encoding="utf-8"), "local")
            question = args[args.index("--")+1]
            initial, context = prepare_input(question, [source])
            maximum = int(args[args.index("--max-model-calls")+1])
            metadata = dict(format_version=2, status="running", prepared=True, requires_search=False,
                mode="live", model=model_name, question=question, model_policy=dict(version=VERSION, max_calls=maximum),
                context_policy=context, allowed_tools=["read_source", "record_evidence"],
                tool_scope="local_documents_only", tool_policy=asdict(ToolLimits()))
            atomic_text(run / "run.json", json.dumps(metadata))
            atomic_text(run / "input.json", json.dumps(initial, ensure_ascii=False))
            budget = model_budget_for(ledger)
            budget.admit("fixture-generation", "generation")
            budget.finish("fixture-generation", "completed", dict(input_tokens=1, output_tokens=1, total_tokens=2))
            evidence = ledger.add_evidence("A fact", source["id"], "A fact.")
        try:
            ledger.save_report(f"A fact.[{evidence['id']}]")
            metadata["status"] = "completed"
            atomic_text(run / "run.json", json.dumps(metadata))
        finally:
            ledger.store.close()
        return 0
    return root, research, calls


def repair_fixture(checker, calls):
    def repair(source, destination, *, resume=False):
        calls.append("repair")
        def graph(ledger, initial, limits, model, **kwargs):
            eid = next(iter(ledger.evidence))
            return {"messages": [AIMessage(content=f"A fact.[{eid}]")]}
        return repair_run(source, destination, checker=checker, graph_runner=graph, resume=resume)
    return repair


def test_single_command_pipeline_and_completed_resume_do_not_repeat_models(workflow):
    root, research, calls = workflow
    checker, repairs = Checker(), []
    repair = repair_fixture(checker, repairs)
    state = run_workflow(root, research_runner=research, repair_runner=repair)
    assert state["status"] == "model_support_gate_passed" and state["last_returncode"] == 0
    assert state["human_review"] == state["result"]["human_review"] == "pending"
    assert state["result"]["repair_rounds"] == 0 and state["result"]["selected"] == "original"
    assert Path(state["diagnostics"], "selected.json").is_file()
    cached = run_workflow(root, research_runner=research, repair_runner=repair)
    assert cached == state and len(calls) == len(repairs) == checker.calls == 1
    (root / "summary.md").unlink()
    assert run_workflow(root, research_runner=research, repair_runner=repair) == state
    assert (root / "summary.md").is_file() and checker.calls == len(calls) == 1


def test_rejected_candidate_remains_needs_review_and_is_not_retried_until_pass(workflow):
    root, research, calls = workflow
    checker, repairs = Checker("unsupported"), []
    repair = repair_fixture(checker, repairs)
    state = run_workflow(root, research_runner=research, repair_runner=repair)
    assert state["status"] == "needs_review" and state["last_returncode"] == 3
    assert state["result"]["selected"] == "original" and state["result"]["repair_rounds"] == 1
    assert run_workflow(root, research_runner=research, repair_runner=repair) == state
    assert len(calls) == len(repairs) == 1 and checker.calls == 2


@pytest.mark.parametrize("verdict, expected_exit", [("supported", 0), ("unsupported", 3)])
def test_offline_result_checks_fallback_to_original_without_rewriting_state(workflow, tmp_path, verdict, expected_exit):
    root, research, _ = workflow
    state = run_workflow(root, research_runner=research, repair_runner=repair_fixture(Checker(verdict), []))
    raw = (root / "workflow.json").read_bytes()
    result = orchestration.check_result(root, tmp_path / "offline")
    assert result["exit_code"] == expected_exit == state["last_returncode"]
    assert result["selected"] == "original" and result["factuality_gate"]["status"] == "not_applicable"
    assert result["final_acceptance"] == "pending" and result["api_requests"] == 0
    assert (root / "workflow.json").read_bytes() == raw


@pytest.mark.parametrize("candidate_label, status, exit_code", [
    ("supported", "passed", 0), ("not_applicable", "review_required", 3),
    ("unmatched", "unknown", 3),
])
def test_offline_workflow_cli_rechecks_selected_candidate_factuality(
        workflow, tmp_path, monkeypatch, candidate_label, status, exit_code):
    root, research, _ = workflow

    class ChangingChecker(Checker):
        def __call__(self, payload):
            result = super().__call__(payload)
            first = result["units"][0]
            if self.calls == 1:
                first["links"][0]["verdict"] = "unsupported"
            elif candidate_label == "not_applicable":
                first["factual"] = False
                first["links"][0]["verdict"] = "not_applicable"
            return result

    def repair(source, destination, *, resume=False):
        def graph(ledger, *args, **kwargs):
            evidence_id = next(iter(ledger.evidence))
            text = "A different line." if candidate_label == "unmatched" else "A fact."
            return {"messages": [AIMessage(content=f"{text}[{evidence_id}]\nAnother line.[{evidence_id}]")]}
        return repair_run(source, destination, checker=ChangingChecker(), graph_runner=graph, resume=resume)

    state = run_workflow(root, research_runner=research, repair_runner=repair)
    assert state["status"] == "model_support_gate_passed" and state["result"]["selected"] == "candidate"
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("The offline check must not load credentials or resume a workflow")

    monkeypatch.setattr(orchestration, "load_dotenv", forbidden)
    monkeypatch.setattr(orchestration, "run_workflow", forbidden)
    output = tmp_path / "check"
    monkeypatch.setattr(sys, "argv", ["workflow", "--check-result", str(root), "--output", str(output)])
    assert orchestration.main() == exit_code
    gate = json.loads((output / "gate.json").read_bytes())
    assert gate["status"] == ("passed" if exit_code == 0 else "review_required")
    assert gate["factuality_gate"]["status"] == status
    assert gate["model_support_gate"]["accepted"]
    assert gate["factuality_gate"]["matched_units"] == (0 if status == "unknown" else 1)
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert orchestration.main() == 1  # Existing results cannot be overwritten.


@pytest.mark.parametrize("mutation", ["report", "corpus", "unfinished"])
def test_workflow_check_rejects_changed_or_unfinished_inputs(workflow, tmp_path, mutation):
    root, research, _ = workflow
    if mutation != "unfinished":
        state = run_workflow(root, research_runner=research, repair_runner=repair_fixture(Checker(), []))
        path = (Path(state["result"]["selected_report"]) if mutation == "report"
                else root / state["plan"]["documents"][0]["path"])
        path.write_bytes(path.read_bytes() + b" changed")
    output = tmp_path / "check"
    with pytest.raises(RecoveryError):
        orchestration.check_result(root, output)
    assert not output.exists()


def test_report_changed_after_completion_cannot_reuse_cached_gate(workflow):
    root, research, calls = workflow
    state = run_workflow(root, research_runner=research, repair_runner=repair_fixture(Checker(), []))
    report = Path(state["result"]["selected_report"])
    report.write_text(report.read_text(encoding="utf-8")+"Changed appendix.", encoding="utf-8")
    state = run_workflow(root, research_runner=research, repair_runner=repair_fixture(Checker(), []))
    assert state["status"] == "needs_inspection" and state["last_returncode"] == 1 and len(calls) == 1


def test_interruption_between_phases_does_not_restart_research(workflow):
    root, research, calls = workflow
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    state = run_workflow(root, research_runner=research, repair_runner=interrupted)
    assert state["status"] == "interrupted" and state["phase"] == "repair"
    state = run_workflow(root, research_runner=research, repair_runner=repair_fixture(Checker(), []))
    assert state["status"] == "model_support_gate_passed" and len(calls) == 1


def test_interrupted_research_resumes_the_existing_run_id(workflow):
    root, research, calls = workflow
    def interrupted(args, **kwargs):
        code = research(args, **kwargs)
        if len(calls) == 1:
            run = root / "research" / ("a" * 32)
            metadata = json.loads((run / "run.json").read_text(encoding="utf-8"))
            metadata["status"] = "interrupted"
            atomic_text(run / "run.json", json.dumps(metadata))
            return 130
        return code
    state = run_workflow(root, research_runner=interrupted, repair_runner=repair_fixture(Checker(), []))
    original = state["source_run_id"]
    assert state["phase"] == "research" and state["status"] == "interrupted"
    state = run_workflow(root, research_runner=interrupted, repair_runner=repair_fixture(Checker(), []))
    assert state["status"] == "model_support_gate_passed" and state["source_run_id"] == original
    assert calls[1][:2] == ["--resume", original] and len(list((root / "research").iterdir())) == 1


def test_cached_verification_for_other_input_is_rejected_without_regrading(workflow):
    root, research, calls = workflow
    checker = Checker()
    repair = repair_fixture(checker, [])
    state = run_workflow(root, research_runner=research, repair_runner=repair)
    path = root / "candidate" / state["candidate_run_id"] / "revisions" / "original-quality.json"
    cached = json.loads(path.read_text(encoding="utf-8"))
    cached["input_sha256"] = "different-input"
    atomic_text(path, json.dumps(cached))
    state = run_workflow(root, research_runner=research, repair_runner=repair)
    assert state["status"] == "needs_inspection" and checker.calls == len(calls) == 1


@pytest.mark.parametrize("unscored", ["\n```text\nUnverified assertion\n```", "\n## Evidence Ledger\nUnverified assertion"])
def test_unscored_sections_cannot_pass_by_hiding_from_line_verification(workflow, unscored):
    root, research, calls = workflow
    checker = Checker()
    def hidden(args, **kwargs):
        code = research(args, **kwargs)
        draft = root / "research" / ("a" * 32) / "draft.md"
        atomic_text(draft, draft.read_text(encoding="utf-8") + unscored)
        return code
    state = run_workflow(root, research_runner=hidden, repair_runner=repair_fixture(checker, []))
    assert state["status"] == "needs_inspection" and checker.calls == 0


def test_uncertain_verification_is_not_automatically_paid_again(workflow):
    root, research, calls = workflow
    class Broken(Checker):
        def __call__(self, payload):
            self.calls += 1
            raise RuntimeError("Fixture network outcome unknown")
    checker, repairs = Broken(), []
    repair = repair_fixture(checker, repairs)
    state = run_workflow(root, research_runner=research, repair_runner=repair)
    assert state["status"] == "verification_outcome_uncertain"
    state = run_workflow(root, research_runner=research, repair_runner=repair)
    assert state["status"] == "verification_outcome_uncertain" and checker.calls == 1 and len(calls) == 1


def test_frozen_document_changes_stop_before_model_execution(workflow):
    root, research, calls = workflow
    next((root / "corpus").iterdir()).write_text("Changed fact.", encoding="utf-8")
    state = run_workflow(root, research_runner=research)
    assert state["status"] == "needs_inspection" and calls == []


def test_unprepared_partial_fork_does_not_create_another_candidate(workflow):
    root, research, calls = workflow
    state = json.loads((root / "workflow.json").read_text(encoding="utf-8"))
    candidate = root / "candidate" / state["candidate_run_id"]
    candidate.mkdir(parents=True)
    state = run_workflow(root, research_runner=research)
    assert state["status"] == "needs_inspection"
    assert list((root / "candidate").iterdir()) == [candidate] and len(calls) == 1


def test_zero_model_budget_stops_workflow_and_resume_without_requests(tmp_path, monkeypatch):
    from test_agent import ScriptedToolModel
    doc = tmp_path / "source.md"
    doc.write_text("A fact.", encoding="utf-8")
    root = tmp_path / "workflow"
    create_workflow(root, "--doctor", [doc], model="fixture", local_only=True, maximum=0)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture")
    monkeypatch.setattr("researchdesk.cli.load_dotenv", lambda: None)
    monkeypatch.setattr("researchdesk.agent.ChatDeepSeek", lambda **kwargs: ScriptedToolModel(messages=iter([])))
    state = run_workflow(root)
    assert state["status"] == "budget_exhausted" and state["last_returncode"] == 4
    first = state["source_run_id"]
    state = run_workflow(root)
    assert state["status"] == "budget_exhausted" and state["source_run_id"] == first
    assert list((root / "research").iterdir()) == [root / "research" / first]
    assert state["diagnostics"] is not None
