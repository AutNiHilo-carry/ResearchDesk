"""Offline crash worker used by subprocess tests: no network or paid model."""
import json
import os
import sys
import time
from pathlib import Path
import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from researchdesk.ledger import Ledger
from researchdesk.policy import ToolExecutor, ToolLimits
from researchdesk.recovery import run_graph
from researchdesk.storage import RunLock, atomic_text
import researchdesk.agent as agent_module

directory, mode, point = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
os.environ["TAVILY_API_KEY"] = "offline-fixture-not-a-real-key"
TEXT = "SQLite permits a single writer per database file."


def append(filename, text):
    with (directory / filename).open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def park():
    atomic_text(directory / "fault-ready.json", json.dumps({"pid": os.getpid(), "point": point}))
    while True:
        time.sleep(0.1)


class FixtureModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "offline-recovery-fixture"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        append("model_calls.log", "call")
        replies = {m.tool_call_id: json.loads(m.content) for m in messages if m.type == "tool"}
        if "search1" not in replies:
            answer = AIMessage(content="", tool_calls=[dict(name="search_sources",
                args={"query": "fixture single writer"}, id="search1", type="tool_call")])
        elif "record1" not in replies:
            source = replies["search1"]["sources"][0]
            answer = AIMessage(content="", tool_calls=[dict(name="record_evidence",
                args={"claim": "Fixture statement", "source_id": source["id"], "snippet": TEXT},
                id="record1", type="tool_call")])
        else:
            answer = AIMessage(content="# Offline fault-injection fixture\n\n"
                               f"Saved fixture statement.[{replies['record1']['id']}]")
        return ChatResult(generations=[ChatGeneration(message=answer)])


def provider(request):
    append("provider_calls.log", "request")
    return httpx.Response(200, json={"results": [
        {"title": "Offline fixture", "url": "https://example.org/fixture", "content": TEXT}]})


original_client = httpx.Client
httpx.Client = lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs)
agent_module.ChatDeepSeek = lambda **kwargs: FixtureModel()
if point == "receipt":
    original_finish = ToolExecutor._finish
    def finish(self, fields, result, success):
        output = original_finish(self, fields, result, success)
        if fields["tool"] == "search_sources" and success:
            park()
        return output
    ToolExecutor._finish = finish
elif point == "evidence":
    original_record = Ledger.add_evidence
    def record(self, *args, **kwargs):
        output = original_record(self, *args, **kwargs)
        park()
        return output
    Ledger.add_evidence = record

ledger = Ledger(directory, resume=mode == "resume")
atomic_text(directory / "worker-started.json", json.dumps({"pid": os.getpid()}))
try:
    with RunLock(directory):
        result = run_graph(ledger, {"messages": [{"role": "user", "content": "Offline fixture"}]},
                           ToolLimits(max_calls=3, max_provider_requests=1), "offline-fixture")
        report = ledger.save_report(result["messages"][-1].content)
        ledger.store.export()
        summary = dict(mode="offline_fault_probe", report=str(report), sources=len(ledger.sources),
                       evidence=len(ledger.evidence), budgets=ledger.store.counters())
        atomic_text(directory / "recovery-check.json", json.dumps(summary, indent=2))
finally:
    ledger.store.close()

