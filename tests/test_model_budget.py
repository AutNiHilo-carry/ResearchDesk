import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from researchdesk import cli
from researchdesk.inspect_run import inspect_run
from researchdesk.ledger import Ledger
from researchdesk.model_budget import ModelCallBudget, ModelBudgetExceeded, VERSION, budget_snapshot, model_budget_for
from researchdesk.repair import AuditedJudge, fork_run, repair_run
from researchdesk.storage import RecoveryError, atomic_text
from researchdesk.task_eval import stop_owned
from test_agent import ScriptedToolModel


def policy(directory, maximum):
    atomic_text(directory / "run.json", json.dumps(dict(mode="live", model_policy=dict(version=VERSION, max_calls=maximum))))


def test_failed_and_uncertain_requests_are_never_refunded(tmp_path):
    ledger = Ledger(tmp_path / "run")
    try:
        policy(ledger.directory, 2)
        budget = model_budget_for(ledger)
        budget.admit("one", "generation")
        budget.finish("one", "failed")
        budget.admit("uncertain", "generation")
        restored = model_budget_for(ledger)
        with pytest.raises(ModelBudgetExceeded):
            restored.admit("third", "generation")
        assert budget_snapshot(ledger.store.conn)["states"] == {"failed": 1, "reserved": 1}
        assert ledger.store.counters() == {"calls": 0, "requests": 0}
    finally:
        ledger.store.close()


def test_budget_limit_is_pinned_and_cannot_be_disabled_or_replayed(tmp_path):
    ledger = Ledger(tmp_path / "run")
    try:
        policy(ledger.directory, 2)
        budget = model_budget_for(ledger)
        budget.admit("one", "generation")
        with pytest.raises(RecoveryError, match="already reserved"):
            budget.admit("one", "generation")
        policy(ledger.directory, 3)
        with pytest.raises(RecoveryError, match="persisted policy"):
            model_budget_for(ledger)
        atomic_text(ledger.directory / "run.json", "{}")
        with pytest.raises(RecoveryError, match="cannot be disabled"):
            model_budget_for(ledger)
    finally:
        ledger.store.close()


def test_quota_admission_is_atomic_under_concurrent_requests(tmp_path):
    ledger = Ledger(tmp_path / "run")
    try:
        budget = ModelCallBudget(ledger.store, 1)
        def reserve(number):
            try:
                budget.admit(str(number), "generation")
                return True
            except ModelBudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=4) as executor:
            outcomes = list(executor.map(reserve, range(4)))
        assert sum(outcomes) == 1 and budget_snapshot(ledger.store.conn)["reserved"] == 1
    finally:
        ledger.store.close()


def test_legacy_model_history_cannot_be_given_a_fresh_quota(tmp_path):
    ledger = Ledger(tmp_path / "run")
    try:
        assert model_budget_for(ledger) is None
        ledger.store.add_event(dict(event="model_started", model_call_id="historical"))
        with pytest.raises(RecoveryError, match="unbudgeted"):
            ModelCallBudget(ledger.store, 5)
        assert budget_snapshot(ledger.store.conn) is None  # failed initialization rolled back its tables.
    finally:
        ledger.store.close()


def test_repair_verifier_shares_the_remaining_run_budget(tmp_path):
    ledger = Ledger(tmp_path / "run")
    calls = []
    class Checker:
        usage = dict(input_tokens=1, output_tokens=2, total_tokens=3)
        def __call__(self, payload):
            calls.append(payload)
            return {"units": []}
    try:
        policy(ledger.directory, 2)
        budget = model_budget_for(ledger)
        budget.admit("generation", "generation")
        budget.finish("generation", "completed")
        judge = AuditedJudge(ledger, Checker())
        judge({"fixture": True})
        with pytest.raises(ModelBudgetExceeded):
            judge({"must_not_invoke": True})
        assert len(calls) == 1
        assert budget_snapshot(ledger.store.conn)["roles"] == {"generation": 1, "repair_verification": 1}
    finally:
        ledger.store.close()


def test_cli_graph_cap_survives_resume_and_preserves_evidence(tmp_path, monkeypatch):
    observed = []
    class Model(ScriptedToolModel):
        def _generate(self, messages, *args, **kwargs):
            observed.append("request")
            # Use the imported source from the actual prepared input.
            text = next(message.content for message in messages if message.type == "human" and "Imported sources:\n" in message.content)
            source = json.loads(text.split("Imported sources:\n", 1)[1])[0]
            return super()._generate(messages, *args, **kwargs)
    doc = tmp_path / "source.md"
    doc.write_text("A fact.", encoding="utf-8")
    output = tmp_path / "runs"
    # Need source ID before graph call, so the model factory reads the actual ledger source record.
    def factory(**kwargs):
        directory = next(output.iterdir())
        rows = [json.loads(line) for line in (directory / "sources.jsonl").read_text(encoding="utf-8").splitlines()]
        return Model(messages=iter([AIMessage(content="", tool_calls=[dict(name="record_evidence", id="record1",
            type="tool_call", args=dict(claim="A fact", source_id=rows[0]["id"], snippet="A fact."))])]))
    monkeypatch.setattr("researchdesk.agent.ChatDeepSeek", factory)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture")
    monkeypatch.setattr(sys, "argv", ["researchdesk", "Question", "--local-only", "--document", str(doc),
        "--max-model-calls", "1", "--output", str(output)])
    assert cli.main() == 4
    directory = next(output.iterdir())
    original_evidence = [json.loads(line) for line in (directory / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    monkeypatch.setattr(sys, "argv", ["researchdesk", "--resume", directory.name, "--output", str(output)])
    assert cli.main() == 4
    assert observed == ["request"] and [json.loads(line) for line in (directory / "evidence.jsonl").read_text(encoding="utf-8").splitlines()] == original_evidence
    diagnosis = inspect_run(directory)
    assert diagnosis["run"]["status"] == "budget_exhausted"
    assert diagnosis["model_budget"]["reserved"] == 1 and diagnosis["budget"]["model_calls"]["remaining"] == 0
    assert sum(row["event"] == "model_budget_denied" for row in diagnosis["timeline"]) == 2


def test_hard_kill_after_reservation_keeps_the_slot_spent(tmp_path):
    directory = tmp_path / "run"
    code = """import sys,time
from pathlib import Path
from researchdesk.ledger import Ledger
from researchdesk.model_budget import ModelCallBudget
from researchdesk.storage import atomic_text
ledger=Ledger(Path(sys.argv[1]))
budget=ModelCallBudget(ledger.store,1)
budget.admit('interrupted','generation')
atomic_text(ledger.directory/'ready','ready')
time.sleep(60)
"""
    options = dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else dict(start_new_session=True)
    process = subprocess.Popen([sys.executable, "-c", code, str(directory)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, **options)
    try:
        deadline = time.monotonic()+20
        while not (directory / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (directory / "ready").exists()
        stop_owned(process)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            stop_owned(process)
    ledger = Ledger(directory, resume=True)
    try:
        budget = ModelCallBudget(ledger.store, 1)
        with pytest.raises(ModelBudgetExceeded):
            budget.admit("after-restart", "generation")
        assert budget_snapshot(ledger.store.conn)["states"] == {"reserved": 1}
    finally:
        ledger.store.close()


def test_resume_cannot_supply_a_replacement_limit(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["researchdesk", "--resume", "a"*32, "--max-model-calls", "5"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def complete_fixture(directory, maximum):
    ledger = Ledger(directory)
    atomic_text(directory / "run.json", json.dumps(dict(format_version=2, status="completed", mode="live", model="fixture",
        model_policy=dict(version=VERSION, max_calls=maximum), tool_policy=dict(max_calls=3, max_provider_requests=0))))
    atomic_text(directory / "input.json", json.dumps({"messages": [{"role": "user", "content": "Fixture"}]}))
    source = ledger.add_source("Fixture", "fixture://source", "A fact.", "local")
    evidence = ledger.add_evidence("Fact", source["id"], "A fact.")
    ledger.save_report(f"A fact.[{evidence['id']}]")
    model_budget_for(ledger)
    return ledger


def test_repair_fork_inherits_quota_without_spending_parent_quota(tmp_path):
    source = tmp_path / "source"
    ledger = complete_fixture(source, 2)
    budget = model_budget_for(ledger)
    budget.admit("original", "generation")
    budget.finish("original", "completed")
    ledger.store.close()
    destination = tmp_path / "candidate"
    fork_run(source, destination)
    child = Ledger(destination, resume=True)
    try:
        budget = model_budget_for(child)
        assert budget_snapshot(child.store.conn)["remaining"] == 1
        budget.admit("candidate-generation", "generation")
        with pytest.raises(ModelBudgetExceeded):
            budget.admit("candidate-check", "repair_verification")
    finally:
        child.store.close()
    connection = sqlite3.connect((source / "application.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert budget_snapshot(connection)["reserved"] == 1
    finally:
        connection.close()


def test_denied_repair_check_is_not_an_uncertain_remote_outcome(tmp_path):
    source = tmp_path / "source"
    ledger = complete_fixture(source, 0)
    ledger.store.close()
    class Never:
        def __call__(self, payload):
            raise AssertionError("Verifier must not be invoked")
    destination = tmp_path / "candidate"
    with pytest.raises(ModelBudgetExceeded):
        repair_run(source, destination, checker=Never())
    state = json.loads((destination / "repair-state.json").read_text(encoding="utf-8"))
    assert state["checks_started"] == []
    findings = {row["code"] for row in inspect_run(destination)["findings"]}
    assert "model_call_budget_exhausted" in findings and "verification_outcome_uncertain" not in findings


def test_framework_summarization_cannot_bypass_the_model_quota(tmp_path, monkeypatch):
    from researchdesk.agent import build_agent
    ledger = Ledger(tmp_path / "run")
    observed = []
    class Model(ScriptedToolModel):
        def _generate(self, *args, **kwargs):
            observed.append("request")
            return super()._generate(*args, **kwargs)
    def factory(**kwargs):
        return Model(messages=iter([AIMessage(content="A compact summary.")]), profile={"max_input_tokens": 10000})
    monkeypatch.setattr("researchdesk.agent.ChatDeepSeek", factory)
    try:
        policy(ledger.directory, 1)
        history = []
        for number in range(30):
            history.extend([HumanMessage(content=f"Context {number}: " + "Earlier research context " * 1000),
                            AIMessage(content=f"Answer {number}: " + "Earlier research answer " * 1000)])
        history.append(HumanMessage(content="Continue this research."))
        graph = build_agent(ledger, allowed_tools=["read_source", "record_evidence"])
        with pytest.raises(ModelBudgetExceeded):
            graph.invoke({"messages": history})
        assert observed == ["request"]
        state = budget_snapshot(ledger.store.conn)
        assert state["reserved"] == 1 and state["roles"] == {"context_summarization": 1}
    finally:
        ledger.store.close()
