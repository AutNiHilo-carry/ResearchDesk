from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from researchdesk.agent import build_agent
from researchdesk.ledger import Ledger


class ScriptedToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        assert all(tool.name != "task" for tool in tools)
        assert {tool.name for tool in tools} <= {"write_todos", "search_sources", "read_source", "record_evidence"}
        return self


def test_real_deepagents_graph_with_scripted_model(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "run")
    source = ledger.add_source("Fixture", "fixture://graph", "A saved observation.")
    import hashlib
    eid = "E" + hashlib.sha256(("Observation\0" + source["id"] + "\0A saved observation.").encode()).hexdigest()[:16]
    model = ScriptedToolModel(messages=iter([
        AIMessage(content="", tool_calls=[dict(name="record_evidence",
            args=dict(claim="Observation", source_id=source["id"], snippet="A saved observation."),
            id="evidence-call", type="tool_call")]),
        AIMessage(content=f"An observation.[{eid}]"),
    ]))
    monkeypatch.setattr("researchdesk.agent.ChatDeepSeek", lambda **kwargs: model)
    graph = build_agent(ledger)
    result = graph.invoke({"messages": [{"role": "user", "content": "Research fixture"}]})
    assert ledger.save_report(result["messages"][-1].content).exists()


def test_graph_policy_blocks_builtin_bypass_and_validates_raw_args(tmp_path, monkeypatch):
    import json
    ledger = Ledger(tmp_path / "run")
    model = ScriptedToolModel(messages=iter([
        AIMessage(content="", tool_calls=[dict(name="write_file", args={"file_path": "/bypass", "content": "bad"},
                                              id="denied-call", type="tool_call")]),
        AIMessage(content="", tool_calls=[dict(name="search_sources", args={"query": 123},
                                              id="invalid-call", type="tool_call")]),
        AIMessage(content="No supported research evidence."),
    ]))
    monkeypatch.setattr("researchdesk.agent.ChatDeepSeek", lambda **kwargs: model)
    result = build_agent(ledger).invoke({"messages": [{"role": "user", "content": "Test policy"}]})
    rows = [json.loads(m.content) for m in result["messages"] if m.type == "tool"]
    assert [row["error_code"] for row in rows] == ["permission_denied", "invalid_arguments"]
    assert not ledger.sources
    events = [json.loads(line) for line in (ledger.directory / "tool_events.jsonl").read_text().splitlines()]
    tool_events = [event for event in events if "tool" in event]
    assert len(tool_events) == 2


def test_model_usage_events_record_every_call_without_report_content(tmp_path, monkeypatch):
    from researchdesk.run_metrics import run_metrics
    ledger = Ledger(tmp_path / "run")
    source = ledger.add_source("Fixture", "file://fixture", "An observation.", "local")
    messages = [AIMessage(content="", usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        tool_calls=[dict(name="read_source", args={"source_id": source["id"]}, id="read1", type="tool_call")]),
        AIMessage(content="private-report-body", usage_metadata={"input_tokens": 7, "output_tokens": 11, "total_tokens": 18})]
    monkeypatch.setattr("researchdesk.agent.ChatDeepSeek", lambda **kwargs: ScriptedToolModel(messages=iter(messages)))
    build_agent(ledger).invoke({"messages": [{"role": "user", "content": "Test usage"}]})
    metrics = run_metrics(ledger.directory)
    assert metrics["model_calls"] == 2 and metrics["total_tokens"] == 23
    assert "private-report-body" not in (ledger.directory / "tool_events.jsonl").read_text()
    ledger.store.close()


def test_local_only_cli_scope_is_saved_for_resume(tmp_path, monkeypatch):
    from researchdesk import cli, recovery
    import json
    import sys
    document = tmp_path / "doc.md"
    document.write_text("A saved fact.")
    output = tmp_path / "runs"
    def graph(ledger, initial, limits, model_name, *, allowed_tools):
        assert set(allowed_tools) == {"read_source", "record_evidence"}
        source = next(iter(ledger.sources.values()))
        evidence = ledger.add_evidence("A saved fact.", source["id"], "A saved fact.")
        return {"messages": [AIMessage(content=f"A saved fact.[{evidence['id']}]")]}
    monkeypatch.setattr(recovery, "run_graph", graph)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["researchdesk", "Question", "--local-only", "--document", str(document),
                                     "--output", str(output)])
    assert cli.main() == 0
    directory = next(output.iterdir())
    manifest = json.loads((directory / "run.json").read_text())
    assert manifest["tool_scope"] == "local_documents_only"
    assert manifest["allowed_tools"] == ["read_source", "record_evidence"]
    monkeypatch.setattr(sys, "argv", ["researchdesk", "--resume", directory.name, "--output", str(output)])
    assert cli.main() == 0


def test_local_scope_hides_and_rejects_external_search(tmp_path, monkeypatch):
    import json
    ledger = Ledger(tmp_path / "run")
    class LocalModel(ScriptedToolModel):
        def bind_tools(self, tools, **kwargs):
            assert "search_sources" not in {tool.name for tool in tools}
            return super().bind_tools(tools, **kwargs)
    model = LocalModel(messages=iter([
        AIMessage(content="", tool_calls=[dict(name="search_sources", args={"query": "forbidden"},
                                              id="search1", type="tool_call")]),
        AIMessage(content="No external search allowed."),
    ]))
    monkeypatch.setattr("researchdesk.agent.ChatDeepSeek", lambda **kwargs: model)
    result = build_agent(ledger, allowed_tools=["read_source", "record_evidence"]).invoke(
        {"messages": [{"role": "user", "content": "Test scope"}]})
    tool = next(message for message in result["messages"] if message.type == "tool")
    assert json.loads(tool.content)["error_code"] == "permission_denied"
    assert ledger.store.counters()["requests"] == 0
    ledger.store.close()

