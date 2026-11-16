import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import pytest
from researchdesk.ledger import Ledger
from researchdesk.policy import ToolExecutor, ToolLimits, ToolSpec
from researchdesk.storage import RecoveryError, RunLock
from researchdesk.tools import ResearchTools, SearchArgs

WORKER = Path(__file__).with_name("recovery_worker.py")


@pytest.mark.parametrize("point", ["receipt", "evidence"])
def test_process_kill_and_restart_preserves_effects(tmp_path, point):
    directory = tmp_path / "run"
    error_path = tmp_path / "worker-error.log"
    error_handle = error_path.open("wb")
    process = subprocess.Popen([sys.executable, str(WORKER), str(directory), "start", point],
                               stdout=subprocess.DEVNULL, stderr=error_handle)
    marker = directory / "fault-ready.json"
    actual_pid = None
    try:
        deadline = time.monotonic() + 40
        while not marker.exists():
            if process.poll() is not None:
                pytest.fail("Worker exited before fault boundary: " + error_path.read_text(errors="replace"))
            if time.monotonic() > deadline:
                pytest.fail("Worker did not reach fault boundary")
            time.sleep(0.05)
        actual_pid = json.loads(marker.read_text())["pid"]
        # Target only the PID reported by our newly spawned worker. On Windows,
        # the venv launcher may be a different PID from the Python interpreter.
        os.kill(actual_pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        process.communicate(timeout=10)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            if actual_pid is None and (directory / "worker-started.json").exists():
                actual_pid = json.loads((directory / "worker-started.json").read_text())["pid"]
            if actual_pid is not None:
                try:
                    os.kill(actual_pid, signal.SIGTERM)
                except OSError:
                    pass
            process.kill()
            process.communicate(timeout=10)
        error_handle.close()
    resumed = subprocess.run([sys.executable, str(WORKER), str(directory), "resume", "none"],
                             capture_output=True, timeout=40)
    assert resumed.returncode == 0, resumed.stderr.decode(errors="replace")
    assert (directory / "provider_calls.log").read_text().splitlines() == ["request"]
    summary = json.loads((directory / "recovery-check.json").read_text())
    assert summary["sources"] == summary["evidence"] == 1
    assert summary["budgets"] == {"calls": 2, "requests": 1}
    assert len((directory / "evidence.jsonl").read_text().splitlines()) == 1
    assert (directory / "report.md").is_file()
    before = (directory / "model_calls.log").read_text()
    again = subprocess.run([sys.executable, str(WORKER), str(directory), "resume", "none"],
                           capture_output=True, timeout=40)
    assert again.returncode == 0, again.stderr.decode(errors="replace")
    assert (directory / "model_calls.log").read_text() == before
    assert (directory / "provider_calls.log").read_text().splitlines() == ["request"]


def test_durable_receipt_binding_budget_and_export_repair(tmp_path):
    directory = tmp_path / "run"
    ledger = Ledger(directory)
    source = ledger.add_source("Doc", "file://fixture", "Saved quote.", "local")
    runtime = ResearchTools(ledger, "", ToolLimits(max_calls=1))
    args = {"claim": "Fact", "source_id": source["id"], "snippet": "quote"}
    original = runtime.executor.execute("record_evidence", args, operation_id="same-operation")
    ledger.store.close()
    (directory / "evidence.jsonl").write_text('{"torn')
    restored = Ledger(directory, resume=True)
    try:
        runtime = ResearchTools(restored, "", ToolLimits(max_calls=1))
        assert runtime.executor.execute("record_evidence", args, operation_id="same-operation") == original
        assert runtime.executor.calls == 1
        assert len((directory / "evidence.jsonl").read_text().splitlines()) == 1
        changed = dict(args, claim="Changed")
        assert runtime.executor.execute("record_evidence", changed, operation_id="same-operation")["error_code"] == "operation_id_conflict"
        assert runtime.executor.execute("record_evidence", args, operation_id="new-operation")["error_code"] == "call_budget_exceeded"
    finally:
        restored.store.close()


def test_unknown_pending_side_effect_requires_reconciliation(tmp_path):
    calls = []
    executor = ToolExecutor(tmp_path, {"unsafe": ToolSpec(SearchArgs,
        lambda query: calls.append(query) or {"ok": True}, read_only=False)})
    args = {"query": "x"}
    fingerprint = hashlib.sha256(json.dumps(["unsafe", args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    executor.store.begin_call("pending", "unsafe", fingerprint, 20)
    result = executor.execute("unsafe", args, operation_id="pending")
    assert result["error_code"] == "ambiguous_operation"
    assert not calls
    executor.store.close()


def test_run_lock_rejects_second_owner(tmp_path):
    with RunLock(tmp_path):
        with pytest.raises(RecoveryError, match="Another process"):
            with RunLock(tmp_path):
                pass


def test_legacy_run_has_no_recovery_database(tmp_path):
    directory = tmp_path / "legacy"
    directory.mkdir()
    with pytest.raises(RecoveryError, match="legacy runs"):
        Ledger(directory, resume=True)


def test_resume_cli_rejects_paths(monkeypatch):
    from researchdesk.cli import main
    monkeypatch.setattr(sys, "argv", ["researchdesk", "--resume", "../other"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_resume_cli_restores_input_and_exports_then_reuses_completed_report(tmp_path, monkeypatch):
    from dataclasses import asdict
    from langchain_core.messages import AIMessage
    from researchdesk.cli import main
    run_id = "a" * 32
    directory = tmp_path / run_id
    ledger = Ledger(directory)
    source = ledger.add_source("Fixture", "file://fixture", "Exact quote.", "local")
    evidence = ledger.add_evidence("A fact", source["id"], "Exact quote.")
    ledger.store.close()
    initial = {"messages": [{"role": "user", "content": "Saved original input"}]}
    (directory / "input.json").write_text(json.dumps(initial))
    (directory / "run.json").write_text(json.dumps(dict(
        format_version=2, mode="live", status="running", prepared=True, requires_search=False,
        tool_policy=asdict(ToolLimits(max_calls=3)), model="saved-model", resume_count=0)))
    calls = []
    def graph(restored, saved_input, limits, model_name):
        assert restored.evidence[evidence["id"]] == evidence
        assert saved_input == initial and model_name == "saved-model" and limits.max_calls == 3
        calls.append("invoked")
        return {"messages": [AIMessage(content=f"Supported claim.[{evidence['id']}]")]}
    monkeypatch.setattr("researchdesk.recovery.run_graph", graph)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    monkeypatch.setattr(sys, "argv", ["researchdesk", "--resume", run_id, "--output", str(tmp_path)])
    assert main() == 0
    assert main() == 0
    assert calls == ["invoked"]
    (directory / "report.md").unlink()
    assert main() == 0
    assert calls == ["invoked", "invoked"]
    assert json.loads((directory / "run.json").read_text())["status"] == "completed"

