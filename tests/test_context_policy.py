import copy
import json
from pathlib import Path
import sys
import pytest
from langchain_core.messages import AIMessage
from researchdesk import cli
from researchdesk.context_policy import prepare_input, verify_saved_input
from researchdesk.ledger import Ledger
from researchdesk.policy import ToolLimits
from researchdesk.storage import RecoveryError
from researchdesk.tools import ResearchTools
from researchdesk.verification import VerificationError


def document(ledger):
    return ledger.add_source("Fixture", "fixture://source", "PRIVATE_DOCUMENT_BODY", "local")


def test_full_mode_preserves_exact_existing_initial_input(tmp_path):
    ledger = Ledger(tmp_path / "run")
    try:
        docs = [document(ledger)]
        initial, policy = prepare_input("Question", docs)
        assert initial == {"messages": [{"role": "user", "content": "Question\nImported sources:\n" + json.dumps(docs, ensure_ascii=False)}]}
        assert policy["strategy"] == "full"
        verify_saved_input(initial, policy)
    finally:
        ledger.store.close()


def test_catalog_omits_body_but_existing_read_and_evidence_tools_retain_it(tmp_path):
    ledger = Ledger(tmp_path / "run")
    try:
        source = document(ledger)
        initial, policy = prepare_input("Question", [source], "catalog")
        assert "PRIVATE_DOCUMENT_BODY" not in json.dumps(initial)
        assert source["id"] in json.dumps(initial) and "read_source" in json.dumps(initial)
        tools = ResearchTools(ledger, "", limits=ToolLimits(max_provider_requests=0), allowed_tools=["read_source", "record_evidence"])
        found = tools.executor.execute("read_source", {"source_id": source["id"]}, operation_id="read")
        assert found["content"] == "PRIVATE_DOCUMENT_BODY"
        evidence = tools.executor.execute("record_evidence", dict(claim="A fact", source_id=source["id"], snippet=found["content"]), operation_id="record")
        assert evidence["snippet"] == source["content"] and tools.executor.provider_requests == 0
        assert policy["source_content_chars"] == len(source["content"])
    finally:
        ledger.store.close()


def test_saved_context_hash_rejects_changed_input_and_allows_legacy_resume():
    initial, policy = prepare_input("Question", [])
    changed = copy.deepcopy(initial)
    changed["messages"][0]["content"] = "Changed"
    with pytest.raises(RecoveryError):
        verify_saved_input(changed, policy)
    verify_saved_input(initial, None)
    with pytest.raises(VerificationError):
        prepare_input("Question", [], "catalog")


def test_catalog_cli_resume_reuses_saved_input_without_rebuilding_context(tmp_path, monkeypatch):
    doc = tmp_path / "document.md"
    doc.write_text("Saved exact fact.", encoding="utf-8")
    output = tmp_path / "runs"
    calls = []
    def graph(ledger, initial, limits, model, **kwargs):
        calls.append(copy.deepcopy(initial))
        assert "Saved exact fact." not in json.dumps(initial)
        if len(calls) == 1:
            raise RuntimeError("Injected model failure")
        source = next(iter(ledger.sources.values()))
        evidence = ledger.add_evidence("Fact", source["id"], source["content"])
        return {"messages": [AIMessage(content=f"Fact.[{evidence['id']}]")]}
    monkeypatch.setattr("researchdesk.recovery.run_graph", graph)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture")
    monkeypatch.setattr(sys, "argv", ["researchdesk", "Question", "--local-only", "--context-mode", "catalog",
        "--document", str(doc), "--output", str(output)])
    assert cli.main() == 1
    directory = next(output.iterdir())
    saved = json.loads((directory / "run.json").read_text(encoding="utf-8"))["context_policy"]
    doc.write_text("Changed external file.", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["researchdesk", "--resume", directory.name, "--output", str(output)])
    assert cli.main() == 0 and calls[0] == calls[1]
    assert json.loads((directory / "run.json").read_text(encoding="utf-8"))["context_policy"] == saved


def test_resume_cannot_override_strategy(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["researchdesk", "--resume", "a"*32, "--context-mode", "full"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_catalog_requires_local_document_scope(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["researchdesk", "Question", "--context-mode", "catalog"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
