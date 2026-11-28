import json

import pytest
from langchain_core.messages import AIMessage

from researchdesk import citation_repair, cli, recovery
from researchdesk.final_body_gate import verify_completed
from researchdesk.ledger import Ledger
from researchdesk.model_budget import model_budget_for


@pytest.mark.parametrize("scenario", [
    "valid", "invalid_mapping", "no_budget", "resume", "interrupted_request", "default", "fenced",
])
def test_cli_publication_repair(tmp_path, monkeypatch, scenario):
    source_file = tmp_path / "source.md"
    source_file.write_text("The system supports audit logs.", encoding="utf-8")
    output = tmp_path / "runs"
    unknown = "E0000000000000000"
    requests = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)

    def fake_graph(ledger, *_args, **_kwargs):
        source, = ledger.sources.values()
        evidence = ledger.add_evidence("Audit logs", source["id"], source["content"])
        budget = model_budget_for(ledger)
        if not budget.store.records("fixture_generation"):
            budget.admit("initial", "generation")
            budget.finish("initial", "completed")
            budget.store.save_record("fixture_generation", {"id": "initial"})
        body = f"Audit logs are supported.[{unknown}]"
        if scenario == "fenced":
            body = "```\n" + body + "\n```"
        return {"messages": [AIMessage(content=body)]}

    def fake_mapping(_model, raw):
        payload = json.loads(raw)
        assert payload["unknown_ids"] == [unknown]
        known, = payload["evidence"]
        requests.append(payload)
        if scenario == "interrupted_request":
            raise KeyboardInterrupt
        replacement = unknown if scenario == "invalid_mapping" else known
        return AIMessage(content=json.dumps({unknown: replacement}))

    monkeypatch.setattr(recovery, "run_graph", fake_graph)
    monkeypatch.setattr(citation_repair, "request_mapping", fake_mapping)

    if scenario == "resume":
        save_report = Ledger.save_report
        interrupted = False

        def interrupted_save(self, report):
            nonlocal interrupted
            if unknown not in report and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return save_report(self, report)

        monkeypatch.setattr(Ledger, "save_report", interrupted_save)

    limit = "1" if scenario == "no_budget" else "2"
    args = ["Does it support logs?", "--document", str(source_file), "--local-only",
            "--output", str(output), "--final-body-limit", "200", "--max-model-calls", limit]
    if scenario != "default":
        args.append("--repair-unknown-citations")
    code = cli.main(args)
    run, = output.iterdir()
    if scenario in {"resume", "interrupted_request"}:
        assert code == 130
        assert not (run / "report.md").exists()
        code = cli.main(["--resume", run.name, "--output", str(output)])

    if scenario in {"valid", "resume"}:
        assert code == 0 and len(requests) == 1
        report = (run / "report.md").read_text(encoding="utf-8")
        assert report.startswith("Audit logs are supported.[E")
        assert unknown not in report
        verify_completed(run)
    else:
        assert code == 1 and not (run / "report.md").exists()
        assert unknown in (run / "draft.md").read_text(encoding="utf-8")
        assert len(requests) == (1 if scenario in {"invalid_mapping", "interrupted_request"} else 0)
