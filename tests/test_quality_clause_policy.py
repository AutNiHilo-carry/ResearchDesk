import json
import sys
import pytest
from researchdesk import quality
from researchdesk.ledger import Ledger
from researchdesk.verification import digest


def setup_run(tmp_path, text):
    run_id = "a" * 32
    ledger = Ledger(tmp_path / run_id)
    source = ledger.add_source("Fixture", "fixture://fact", "只有一个 WAL 文件。")
    eid = ledger.add_evidence("Fact", source["id"], source["content"])["id"]
    ledger.save_report(text.replace("EID", eid))
    (ledger.directory / "run.json").write_text(json.dumps(dict(status="completed", mode="fixture")))
    ledger.store.close()
    return ledger.directory, run_id


def environment(monkeypatch):
    monkeypatch.setattr(quality, "load_dotenv", lambda: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")


@pytest.mark.parametrize("policy,temporal,count", [
    (None, False, 1), ("clause-spans-v1", False, 1), ("clause-spans-v2", False, 2),
    ("clause-spans-v3", False, 2), ("clause-spans-v2", True, 2), ("clause-spans-v3", True, 1),
])
def test_cli_uses_requested_policy_and_saves_exact_input(tmp_path, monkeypatch, policy, temporal, count):
    text = "只有一个连接可用时，执行写入[EID]；第二阶段开始。" if temporal else "只有一个 WAL 文件[EID]。另一个事实。"
    directory, run_id = setup_run(tmp_path, text)
    environment(monkeypatch)
    before = (directory / "report.md").read_bytes()
    calls = []
    class Judge:
        usage = dict(total_tokens=0)
        prompt_version = "report-quote-v2"
        prompt_sha256 = "fixture"
        def __call__(self, payload):
            calls.append(payload)
            assert len(payload["units"]) == count
            return dict(units=[dict(unit_id=u["unit_id"], factual=True, reason="fixture",
                links=[dict(evidence_id=e, verdict="supported", reason="fixture") for e in u["citations"]])
                for u in payload["units"]])
    monkeypatch.setattr(quality, "ModelJudge", lambda *a, **k: Judge())
    argv = ["quality", "verify", run_id, "--output", str(tmp_path), "--granularity", "clause", "--judge-prompt", "v2"]
    if policy:
        argv += ["--clause-policy", policy]
    monkeypatch.setattr(sys, "argv", argv)
    assert quality.main() == 0 and len(calls) == 1
    artifact = next((directory / "verification").iterdir())
    result = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
    payload = json.loads((artifact / "input.json").read_text(encoding="utf-8"))
    assert result["unitization_version"] == (policy or "clause-spans-v1")
    assert result["unitization_version"] in (artifact / "summary.md").read_text(encoding="utf-8")
    assert all(u["unitization_version"] == result["unitization_version"] for u in payload["units"])
    assert digest(json.dumps(payload, ensure_ascii=False, sort_keys=True)) == result["input_sha256"]
    assert (directory / "report.md").read_bytes() == before


@pytest.mark.parametrize("extra", [
    ["--clause-policy", "clause-spans-v3"],
    ["--granularity", "clause", "--clause-policy", "clause-spans-v3"],
    ["--granularity", "clause", "--clause-policy", "unknown"],
])
def test_invalid_combinations_rejected_before_environment_or_model(monkeypatch, extra):
    monkeypatch.setattr(quality, "load_dotenv", lambda: pytest.fail("Invalid request reached environment"))
    monkeypatch.setattr(sys, "argv", ["quality", "verify", "a"*32] + extra)
    with pytest.raises(SystemExit):
        quality.main()


def test_budget_checked_before_model_creation(tmp_path, monkeypatch):
    directory, run_id = setup_run(tmp_path, "只有一个 WAL 文件[EID]。第二个事实。")
    environment(monkeypatch)
    monkeypatch.setattr(quality, "ModelJudge", lambda *a, **k: pytest.fail("Budget check must precede model"))
    monkeypatch.setattr(sys, "argv", ["quality", "verify", run_id, "--output", str(tmp_path),
        "--granularity", "clause", "--clause-policy", "clause-spans-v3", "--judge-prompt", "v2", "--max-units", "1"])
    assert quality.main() == 1
    assert not (directory / "verification").exists()


def test_failed_model_keeps_frozen_input_and_no_scores(tmp_path, monkeypatch):
    directory, run_id = setup_run(tmp_path, "只有一个 WAL 文件[EID]。第二个事实。")
    environment(monkeypatch)
    class Broken:
        usage = None
        def __call__(self, payload):
            raise RuntimeError("Fixture failure")
    monkeypatch.setattr(quality, "ModelJudge", lambda *a, **k: Broken())
    monkeypatch.setattr(sys, "argv", ["quality", "verify", run_id, "--output", str(tmp_path),
        "--granularity", "clause", "--clause-policy", "clause-spans-v3", "--judge-prompt", "v2"])
    assert quality.main() == 1
    artifact = next((directory / "verification").iterdir())
    result = json.loads((artifact / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed" and result["metrics"] is None
    payload = json.loads((artifact / "input.json").read_text(encoding="utf-8"))
    assert digest(json.dumps(payload, ensure_ascii=False, sort_keys=True)) == result["input_sha256"]
