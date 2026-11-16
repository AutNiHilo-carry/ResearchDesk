import json
from dataclasses import asdict
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from researchdesk.ledger import Ledger
from researchdesk.policy import ToolLimits
from researchdesk.repair import frozen_feedback, repair_run, selection
from researchdesk.storage import RecoveryError
from researchdesk.tools import ResearchTools
from researchdesk.verification import VerificationError, assess, prepare, split_units


class Checker:
    usage = {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    prompt_version = "fixture-v2"
    prompt_sha256 = "fixture-prompt"
    def __init__(self):
        self.calls = 0
    def __call__(self, payload):
        self.calls += 1
        return {"units": [dict(unit_id=row["unit_id"], factual=True, reason="测试桩",
            links=[dict(evidence_id=eid, verdict="unsupported" if "没有依据" in row["text"] else "supported",
                        reason="测试桩") for eid in row["citations"]]) for row in payload["units"]]}


@pytest.fixture
def source_run(tmp_path):
    source = tmp_path / "source"
    ledger = Ledger(source)
    saved = ledger.add_source("Fixture", "fixture://facts", "事实A。事实B。", "local")
    runtime = ResearchTools(ledger, "", limits=ToolLimits(max_calls=4, max_provider_requests=0))
    evidence = runtime.executor.execute("record_evidence",
        {"claim": "事实A", "source_id": saved["id"], "snippet": "事实A。"}, operation_id="original-record")
    report = f"事实A。[{evidence['id']}]\n没有依据的断言。[{evidence['id']}]"
    ledger.save_report(report)
    metadata = dict(format_version=2, status="completed", mode="live", model="fixture",
        question="Question", prepared=True, requires_search=False, allowed_tools=["read_source", "record_evidence"],
        tool_scope="local_documents_only", tool_policy=asdict(ToolLimits(max_calls=4, max_provider_requests=0)))
    (source / "run.json").write_text(json.dumps(metadata))
    (source / "input.json").write_text(json.dumps({"messages": [{"role": "user", "content": "Question"}]}))
    payload, _ = prepare(split_units(report), ledger.evidence, ledger.sources)
    feedback = assess(payload, Checker())["units"]
    ledger.store.close()
    return source, saved["id"], evidence["id"], report, feedback


def test_single_round_repairs_without_mutating_source_or_resetting_budget(source_run, tmp_path):
    source, sid, eid, original, feedback = source_run
    before = (source / "report.md").read_bytes()
    checker, calls = Checker(), []
    def graph(ledger, initial, limits, model, *, thread_id, allowed_tools):
        calls.append(thread_id)
        assert thread_id.endswith(":repair-1")
        assert allowed_tools == ["read_source", "record_evidence"]
        assert ledger.store.counters()["calls"] == 1
        runtime = ResearchTools(ledger, "", limits=limits, allowed_tools=allowed_tools)
        fresh = runtime.executor.execute("record_evidence",
            {"claim": "事实B", "source_id": sid, "snippet": "事实B。"}, operation_id="repair-record")
        assert "error" not in fresh
        return {"messages": [AIMessage(content=f"事实A。[{eid}]\n事实B。[{fresh['id']}]")]}
    destination = tmp_path / "candidate"
    state = repair_run(source, destination, feedback_rows=feedback, checker=checker, graph_runner=graph)
    assert state["rounds_used"] == 1 and state["selection"]["accepted"]
    assert len(calls) == 1 and checker.calls == 1  # Frozen feedback saves original checker call.
    assert (source / "report.md").read_bytes() == before
    assert (destination / "revisions" / "original.md").read_text(encoding="utf-8") == original
    assert (destination / "revisions" / "changes.diff").is_file()
    restored = Ledger(destination, resume=True)
    assert restored.store.counters()["calls"] == 2
    restored.store.close()
    again = repair_run(None, destination, checker=checker, graph_runner=graph, resume=True)
    assert again == state and len(calls) == checker.calls == 1


@pytest.mark.parametrize("candidate", ["编造引用。[Efake]", "```text\n隐藏的未验证断言\n```"])
def test_invalid_candidate_falls_back_without_extra_checker_request(source_run, tmp_path, candidate):
    source, _, _, _, feedback = source_run
    checker = Checker()
    def graph(*args, **kwargs):
        return {"messages": [AIMessage(content=candidate)]}
    destination = tmp_path / "candidate"
    state = repair_run(source, destination, feedback_rows=feedback, checker=checker, graph_runner=graph)
    assert not state["selection"]["accepted"]
    assert state["selection"]["selected"] == "original"
    assert checker.calls == 0
    assert candidate == (destination / "revisions" / "candidate.md").read_text(encoding="utf-8")
    assert (destination / "report.md").read_bytes() == (source / "report.md").read_bytes()


def test_wide_candidate_is_archived_then_bound_and_assessed_canonically(source_run, tmp_path):
    from researchdesk.verification import digest
    source, _, eid, _, feedback = source_run
    original_bytes = (source / "report.md").read_bytes()
    raw = f"事实A。［{eid}］\n事实A。［{eid}］"
    canonical = raw.replace("［", "[").replace("］", "]")
    checker, graph_calls = Checker(), []
    def graph(*args, **kwargs):
        graph_calls.append(1)
        return {"messages": [AIMessage(content=raw)]}
    destination = tmp_path / "candidate"
    state = repair_run(source, destination, feedback_rows=feedback, checker=checker, graph_runner=graph)
    assert state["selection"]["accepted"]
    assert state["candidate_sha256"] == digest(canonical)
    assert state["candidate_raw_sha256"] == digest(raw)
    assert (destination / "revisions" / "candidate.md").read_text(encoding="utf-8") == canonical
    assert (destination / "report-inputs" / (digest(raw) + ".md")).read_bytes() == raw.encode()
    assert (source / "report.md").read_bytes() == original_bytes
    again = repair_run(None, destination, checker=checker, graph_runner=graph, resume=True)
    assert again == state and graph_calls == [1] and checker.calls == 1


def test_started_candidate_cannot_be_reformatted_on_resume(source_run, tmp_path):
    from researchdesk.verification import digest
    source, _, eid, _, feedback = source_run
    def graph(*args, **kwargs):
        return {"messages": [AIMessage(content=f"事实A。[{eid}]\n事实A。[{eid}]")]}
    class Broken(Checker):
        def __call__(self, payload):
            raise RuntimeError("uncertain checker outcome")
    broken = Broken()
    destination = tmp_path / "candidate"
    with pytest.raises(RuntimeError):
        repair_run(source, destination, feedback_rows=feedback, checker=broken, graph_runner=graph)
    # Recreate a legacy fullwidth candidate whose exact input was already sent.
    path = destination / "revisions" / "candidate.md"
    raw = path.read_text(encoding="utf-8").replace("[", "［").replace("]", "］")
    path.write_text(raw, encoding="utf-8")
    state_path = destination / "repair-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["candidate_sha256"] = digest(raw)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    checker = Checker()
    with pytest.raises(RecoveryError, match="already assessed"):
        repair_run(None, destination, checker=checker, graph_runner=graph, resume=True)
    assert checker.calls == 0 and path.read_text(encoding="utf-8") == raw


def test_vacuous_or_fact_dropping_candidate_cannot_pass_support_gate():
    before = {"metrics": dict(factual_units=2, citation_links=2, supported_links=2, covered_units=2,
        citation_precision=1.0, evidence_coverage=1.0, invalid_links=0, conflicted_units=0)}
    empty = {"metrics": dict(factual_units=0, citation_links=0, supported_links=0, covered_units=0,
        citation_precision=None, evidence_coverage=None, invalid_links=0, conflicted_units=0)}
    assert not selection(before, empty)["accepted"]
    one = {"metrics": dict(before["metrics"], factual_units=1, covered_units=1)}
    assert "fewer_supported_units" in selection(before, one)["reasons"]


def test_uncertain_checker_result_is_not_automatically_retried(source_run, tmp_path):
    source, *_ = source_run
    calls = []
    def broken(payload):
        calls.append("called")
        raise RuntimeError("fixture failure")
    destination = tmp_path / "candidate"
    with pytest.raises(RuntimeError):
        repair_run(source, destination, checker=broken)
    with pytest.raises(RecoveryError, match="uncertain"):
        repair_run(None, destination, checker=broken, resume=True)
    assert calls == ["called"]


def test_frozen_feedback_must_cover_exact_original_report(source_run):
    _, _, _, report, rows = source_run
    with pytest.raises(VerificationError):
        frozen_feedback(report, rows[:-1])
    changed = [dict(row) for row in rows]
    changed[0]["text"] = "另一份报告"
    with pytest.raises(VerificationError):
        frozen_feedback(report, changed)


def test_named_repair_checkpoint_does_not_reinvoke_completed_graph(tmp_path, monkeypatch):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from researchdesk.recovery import run_graph
    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self
    created = []
    def factory(**kwargs):
        created.append("model")
        return Model(messages=iter([AIMessage(content=f"Report {len(created)}")]))
    monkeypatch.setattr("researchdesk.agent.ChatDeepSeek", factory)
    ledger = Ledger(tmp_path / "run")
    initial = {"messages": [{"role": "user", "content": "Question"}]}
    first = run_graph(ledger, initial, ToolLimits(), "fixture")
    second = run_graph(ledger, initial, ToolLimits(), "fixture", thread_id="repair-1")
    resumed = run_graph(ledger, initial, ToolLimits(), "fixture", thread_id="repair-1")
    assert first["messages"][-1].content == "Report 1"
    assert second["messages"][-1].content == resumed["messages"][-1].content == "Report 2"
    events = [json.loads(line) for line in (ledger.directory / "tool_events.jsonl").read_text().splitlines()]
    assert sum(event["event"] == "model_started" for event in events) == 2
    ledger.store.close()

