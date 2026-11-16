import copy
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from researchdesk.evaluation import benchmark_inputs, evaluate
from researchdesk.ledger import Ledger
from researchdesk.verification import (VerificationError, ModelJudge, assess, digest, load_run,
                                        prepare, split_units)


@pytest.fixture
def sample(tmp_path):
    ledger = Ledger(tmp_path / "run")
    source = ledger.add_source("Fixture", "fixture://doc", "系统支持日志。", "fixture")
    evidence = ledger.add_evidence("系统支持日志。", source["id"], "系统支持日志。")
    yield ledger, evidence
    ledger.store.close()


def reply(unit, verdict="supported", factual=True):
    return dict(unit_id=unit["unit_id"], factual=factual, reason="测试摘要",
                links=[dict(evidence_id=eid, verdict=verdict, reason="测试依据") for eid in unit["citations"]])


def test_actual_report_statement_is_verified_not_original_ledger_claim(sample):
    ledger, evidence = sample
    units = split_units(f"系统支持日志和自动备份。[{evidence['id']}]")
    payload, _ = prepare(units, ledger.evidence, ledger.sources)
    assert payload["units"][0]["text"].startswith("系统支持日志和自动备份")
    assert "claim" not in payload["evidence"][0]  # Ledger claim cannot substitute report assertion.
    result = assess(payload, lambda p: {"units": [reply(p["units"][0], "partial")]})
    assert result["metrics"]["citation_precision"] == 0
    assert result["metrics"]["evidence_coverage"] == 0


def test_tampered_quote_cannot_be_marked_supported(sample):
    ledger, evidence = sample
    altered = copy.deepcopy(ledger.evidence)
    altered[evidence["id"]]["snippet"] = "系统支持自动备份。"
    units = split_units(f"系统支持自动备份。[{evidence['id']}]")
    payload, _ = prepare(units, altered, ledger.sources)
    result = assess(payload, lambda p: {"units": [reply(p["units"][0])]})
    assert result["units"][0]["links"][0]["verdict"] == "invalid_evidence"
    assert result["metrics"]["invalid_links"] == 1
    assert result["metrics"]["evidence_coverage"] == 0


def test_unknown_citation_is_invalid(sample):
    ledger, _ = sample
    payload, _ = prepare(split_units("有事实。[Eunknown]"), ledger.evidence, ledger.sources)
    result = assess(payload, lambda p: {"units": [reply(p["units"][0])]})
    assert result["metrics"]["citation_precision"] == 0
    assert result["metrics"]["invalid_links"] == 1


def test_uncited_fact_counts_in_coverage_denominator(sample):
    ledger, evidence = sample
    units = split_units(f"有支持的事实。[{evidence['id']}]\n未引用的事实。")
    payload, _ = prepare(units, ledger.evidence, ledger.sources)
    result = assess(payload, lambda p: {"units": [reply(unit) for unit in p["units"]]})
    assert result["metrics"]["factual_units"] == 2
    assert result["metrics"]["citation_precision"] == 1
    assert result["metrics"]["evidence_coverage"] == 0.5


def test_zero_denominators_are_na():
    payload, _ = prepare(split_units("建议先做实验。"), {}, {})
    result = assess(payload, lambda p: {"units": [reply(p["units"][0], factual=False)]})
    assert result["metrics"]["citation_precision"] is None
    assert result["metrics"]["evidence_coverage"] is None


def test_conflicting_sources_prevent_coverage(sample):
    ledger, first = sample
    source = ledger.add_source("Other", "fixture://other", "系统不支持日志。", "fixture")
    second = ledger.add_evidence("系统不支持日志。", source["id"], source["content"])
    units = split_units(f"系统支持日志。[{first['id']}][{second['id']}]")
    payload, _ = prepare(units, ledger.evidence, ledger.sources)
    def judge(p):
        row = reply(p["units"][0])
        for link in row["links"]:
            if link["evidence_id"] == second["id"]:
                link["verdict"] = "contradicted"
        return {"units": [row]}
    result = assess(payload, judge)
    assert result["metrics"]["citation_precision"] == 0.5
    assert result["metrics"]["evidence_coverage"] == 0
    assert result["metrics"]["conflicted_units"] == 1


@pytest.mark.parametrize("bad", ["missing_unit", "extra_unit", "duplicate_unit", "missing_link", "extra_link"])
def test_incomplete_or_invented_judge_output_is_rejected(sample, bad):
    ledger, evidence = sample
    payload, _ = prepare(split_units(f"事实。[{evidence['id']}]"), ledger.evidence, ledger.sources)
    row = reply(payload["units"][0])
    data = {"units": [row]}
    if bad == "missing_unit":
        data["units"] = []
    elif bad == "extra_unit":
        data["units"].append(dict(row, unit_id="invented"))
    elif bad == "duplicate_unit":
        data["units"].append(copy.deepcopy(row))
    elif bad == "missing_link":
        row["links"] = []
    else:
        row["links"].append(dict(evidence_id="Efake", verdict="supported", reason="bad"))
    with pytest.raises(VerificationError):
        assess(payload, lambda p: data)


def test_strict_output_types(sample):
    ledger, evidence = sample
    payload, _ = prepare(split_units(f"事实。[{evidence['id']}]"), ledger.evidence, ledger.sources)
    with pytest.raises(ValidationError):
        assess(payload, lambda p: {"units": [dict(reply(p["units"][0]), factual="true")]})


def test_budget_rejects_without_silently_dropping_text():
    units = split_units("事实一\n事实二")
    with pytest.raises(VerificationError, match="Unit budget"):
        prepare(units, {}, {}, max_units=1)
    with pytest.raises(VerificationError, match="input budget"):
        prepare(units, {}, {}, max_chars=1)


def test_headings_are_not_silently_excluded():
    units = split_units("# 系统支持恢复。\n\n```python\nignored_code()\n```\n| --- | --- |\n另一条事实。")
    assert len(units) == 2
    assert units[0]["line"] == 1 and units[1]["line"] == 7


def test_loader_prefers_authoritative_sqlite_over_tampered_export(sample):
    ledger, evidence = sample
    report = f"系统支持日志。[{evidence['id']}]"
    ledger.save_report(report)
    (ledger.directory / "run.json").write_text(json.dumps({"status": "completed", "mode": "fixture"}))
    (ledger.directory / "evidence.jsonl").write_text("broken export")
    loaded, restored, _, _ = load_run(ledger.directory)
    assert loaded == report
    assert restored[evidence["id"]] == evidence


def test_benchmark_labels_not_sent_to_judge_and_false_support_fails_gate():
    dataset = json.loads((Path(__file__).parents[1] / "evals" / "evidence-v1.json").read_text(encoding="utf-8"))
    units, evidence, sources, gold = benchmark_inputs(dataset)
    def reference_judge(payload):
        assert "expected_verdicts" not in json.dumps(payload)
        rows = []
        for unit in payload["units"]:
            expected = gold[unit["unit_id"]]
            links = []
            for eid in unit["citations"]:
                target = expected["verdicts"][eid]
                links.append(dict(evidence_id=eid, verdict="insufficient" if target == "invalid_evidence" else target,
                                  reason="固定标签测试桩"))
            rows.append(dict(unit_id=unit["unit_id"], factual=expected["factual"], reason="固定标签测试桩", links=links))
        return {"units": rows}
    result = evaluate(dataset, reference_judge)
    assert result["benchmark"]["semantic_link_accuracy"] == 1
    assert result["benchmark"]["gate_passed"]
    assert result["benchmark"]["exact_match_baseline_link_accuracy"] < 0.5
    def overconfident(payload):
        rows = [reply(unit, factual=gold[unit["unit_id"]]["factual"]) for unit in payload["units"]]
        return {"units": rows}
    bad = evaluate(dataset, overconfident)
    assert bad["benchmark"]["false_support_count"] > 0
    assert not bad["benchmark"]["gate_passed"]


@pytest.mark.parametrize("content,finish,valid", [
    ('{"units":[]}', "stop", True), (" ", "stop", False),
    ('{"units":', "length", False), ("not json", "stop", False),
])
def test_model_json_failure_modes(monkeypatch, content, finish, valid):
    from types import SimpleNamespace
    class Model:
        def bind(self, **kwargs):
            assert kwargs == {"response_format": {"type": "json_object"}}
            return self
        def invoke(self, messages):
            assert "json" in messages[0]["content"].lower()
            return SimpleNamespace(content=content, response_metadata={"finish_reason": finish},
                                   usage_metadata={"total_tokens": 1})
    def create(**kwargs):
        assert kwargs["max_retries"] == 0
        assert kwargs["extra_body"]["thinking"]["type"] == "disabled"
        return Model()
    monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", create)
    judge = ModelJudge("fixture-model")
    if valid:
        assert judge({"units": []}) == {"units": []}
    else:
        with pytest.raises((VerificationError, ValueError)):
            judge({"units": []})


@pytest.mark.parametrize("fails", [False, True])
def test_quality_cli_preserves_report_and_rejects_bad_scores(tmp_path, monkeypatch, fails):
    from researchdesk import quality
    run_id = "f" * 32
    ledger = Ledger(tmp_path / run_id)
    source = ledger.add_source("Fixture", "fixture://cli", "A fact.", "fixture")
    evidence = ledger.add_evidence("A fact.", source["id"], "A fact.")
    ledger.save_report(f"A fact.[{evidence['id']}]")
    (ledger.directory / "run.json").write_text(json.dumps({"status": "completed", "mode": "fixture"}))
    ledger.store.close()
    before = (ledger.directory / "report.md").read_bytes()
    class Judge:
        usage = {"total_tokens": 0}
        def __call__(self, payload):
            return {"units": [] if fails else [reply(row) for row in payload["units"]]}
    monkeypatch.setattr(quality, "ModelJudge", lambda model: Judge())
    monkeypatch.setattr(quality, "load_dotenv", lambda: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    import sys
    monkeypatch.setattr(sys, "argv", ["quality", "verify", run_id, "--output", str(tmp_path)])
    assert quality.main() == (1 if fails else 0)
    result = json.loads(next((ledger.directory / "verification").glob("*/result.json")).read_text(encoding="utf-8"))
    assert result["status"] == ("failed" if fails else "complete")
    if fails:
        assert result["metrics"] is None
    assert (ledger.directory / "report.md").read_bytes() == before

