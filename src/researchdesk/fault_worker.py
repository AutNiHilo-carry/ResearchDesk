"""Dedicated experiment subprocess. Fault hooks never affect ordinary CLI execution."""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from .ledger import Ledger, now
from .policy import ToolExecutor
from .storage import atomic_text


def install_fault(point, marker, root):
    def park(directory):
        directory = directory.resolve()
        if directory.parent != root.resolve() or not re.fullmatch("[0-9a-f]{32}", directory.name):
            raise ValueError("Fault boundary is outside this worker's run root.")
        atomic_text(marker, json.dumps(dict(point=point, run_id=directory.name, pid=os.getpid(), reached_at=now())))
        while True:
            time.sleep(0.1)
    if point == "model_response":
        from .model_budget import BudgetCallbacks
        original = BudgetCallbacks.on_llm_end
        def returned(self, response, *, run_id, **kwargs):
            original(self, response, run_id=run_id, **kwargs)
            # Inline callback: model result has not returned to the graph node yet.
            park(self.ledger.directory)
        BudgetCallbacks.on_llm_end = returned
    elif point == "receipt":
        original = ToolExecutor._finish
        def finish(self, fields, result, success):
            output = original(self, fields, result, success)
            if fields["tool"] == "record_evidence" and success:
                park(self.directory)
            return output
        ToolExecutor._finish = finish
    elif point == "evidence":
        original = Ledger.add_evidence
        def record(self, *args, **kwargs):
            output = original(self, *args, **kwargs)
            park(self.directory)
            return output
        Ledger.add_evidence = record
    elif point == "report":
        original = Ledger.save_report
        def report(self, text):
            output = original(self, text)
            park(self.directory)
            return output
        Ledger.save_report = report


def scripted_model():
    """Explicit offline simulation of the runtime, not research-quality validation."""
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    import researchdesk.agent as agent_module
    class Scripted(BaseChatModel):
        @property
        def _llm_type(self):
            return "scripted-recovery-fixture"
        def bind_tools(self, tools, **kwargs):
            return self
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            replies = {message.tool_call_id: json.loads(message.content) for message in messages if message.type == "tool"}
            if "fault-fixture-record" in replies:
                eid = replies["fault-fixture-record"]["id"]
                answer = AIMessage(content=f"Offline fixture; structural verification only.[{eid}]",
                    usage_metadata=dict(input_tokens=1, output_tokens=1, total_tokens=2))
            else:
                text = next(message.content for message in messages if message.type == "human"
                            and isinstance(message.content, str) and "Imported sources:\n" in message.content)
                source = json.loads(text.split("Imported sources:\n", 1)[1])[0]
                answer = AIMessage(content="", tool_calls=[dict(name="record_evidence", id="fault-fixture-record",
                    type="tool_call", args=dict(claim="Offline fixture source", source_id=source["id"],
                                               snippet=source["content"][:1000]))],
                    usage_metadata=dict(input_tokens=1, output_tokens=1, total_tokens=2))
            return ChatResult(generations=[ChatGeneration(message=answer)])
    agent_module.ChatDeepSeek = lambda **kwargs: Scripted()
    os.environ["DEEPSEEK_API_KEY"] = "offline-fixture-not-a-real-key"
    os.environ["DEEPSEEK_MODEL"] = "scripted-recovery-fixture"


def main():
    parser = argparse.ArgumentParser(description="Owned fault-evaluation subprocess")
    parser.add_argument("--point", choices=["none", "evidence", "receipt", "report", "model_response"], required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--scripted", action="store_true")
    parser.add_argument("cli", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.scripted:
        scripted_model()
    install_fault(args.point, args.marker, args.root)
    from .cli import main as cli_main
    cli_args = args.cli[1:] if args.cli[:1] == ["--"] else args.cli
    sys.argv = ["researchdesk"] + cli_args
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
