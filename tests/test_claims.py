import copy
import json
from pathlib import Path
import pytest
from researchdesk.claims import split_clauses, validate_anchors
from researchdesk.comparison import compare, reference_inputs
from researchdesk.verification import (CITATION, SYSTEM_PROMPT, SYSTEM_PROMPT_V2,
                                        VerificationError, digest, split_units)


def parts(text):
    units = split_clauses(split_units(text))
    validate_anchors(units)
    return units


def test_exact_spans_preserve_all_characters_and_parent_context():
    text = "  - 系统支持日志；系统支持自动备份。[E123]  "
    rows = parts(text)
    assert len(rows) == 2
    assert "".join(row["text"] for row in rows) == text.strip()
    for row in rows:
        assert row["source_line_text"][row["span_start"]:row["span_end"]] == row["text"]
    assert rows[0]["citations"] == rows[1]["citations"] == ["E123"]
    assert rows[0]["direct_citations"] == []
    assert rows[0]["citation_binding"] == "parent_final_candidate_pool"


@pytest.mark.parametrize("text", [
    "服务URL为https://example.org/v1.2?a=1;b=2；上限为8。",
    "示例文本是“A；B”；支持日志。",
    "代码为`A;B`；支持日志。",
    "比例为3.14；上限为8。",
    "测试（A；B）；支持日志。",
])
def test_protected_punctuation_does_not_split_inside_values(text):
    assert len(parts(text)) == 2


def test_conditional_scope_is_preserved():
    text = "只有在内存不少于4GB时，模式A才可用；模式B可用。"
    rows = parts(text)
    assert len(rows) == 1
    assert rows[0]["text"] == text
    assert rows[0]["partition_reason"] == "conditional_scope_preserved"


def test_explicit_citations_are_not_inherited_into_an_uncited_clause():
    rows = parts("事实A[E111]；事实B[E222]；事实C。")
    assert [row["citations"] for row in rows] == [["E111"], ["E222"], []]
    assert all(row["citation_binding"] == "explicit_span_only" for row in rows)


def test_conjunction_is_not_claimed_to_be_fully_atomic():
    rows = parts("系统支持日志和自动备份。")
    assert len(rows) == 1
    assert rows[0]["text"] == "系统支持日志和自动备份。"


@pytest.mark.parametrize("fault", ["text", "gap", "overlap", "tail"])
def test_bad_or_incomplete_spans_are_rejected(fault):
    rows = parts("事实A；事实B。")
    if fault == "text":
        rows[0]["text"] = "改写后的内容"
    elif fault == "gap":
        rows[1]["span_start"] += 1
        rows[1]["text"] = rows[1]["source_line_text"][rows[1]["span_start"]:rows[1]["span_end"]]
    elif fault == "overlap":
        rows[1]["span_start"] -= 1
        rows[1]["text"] = rows[1]["source_line_text"][rows[1]["span_start"]:rows[1]["span_end"]]
    else:
        rows.pop()
    with pytest.raises(VerificationError):
        validate_anchors(rows)


def dataset():
    return json.loads((Path(__file__).parents[1] / "evals" / "claims-v2.json").read_text(encoding="utf-8"))


def test_partitions_match_fixed_reference_texts_without_gold_in_payload():
    data = dataset()
    units, _, _, gold = reference_inputs(data, "clause")
    actual = [CITATION.sub("", row["text"]).strip() for row in units]
    expected = [clause["text"] for case in data["cases"] for clause in case["clauses"]]
    assert actual == expected
    assert gold["compound_missing_fact.C1"]["factual"]
    assert list(gold["compound_missing_fact.C1"]["verdicts"].values()) == ["supported"]
    assert list(gold["compound_missing_fact.C2"]["verdicts"].values()) == ["unsupported"]


def test_comparison_controls_prompt_then_unitization_without_automatic_promotion():
    calls = []
    class Judge:
        usage = {"total_tokens": 0}
        def __init__(self, version):
            self.prompt_version = "report-quote-" + version
        def __call__(self, payload):
            calls.append((self.prompt_version, copy.deepcopy(payload)))
            assert "expected_factual" not in json.dumps(payload)
            return {"units": [dict(unit_id=row["unit_id"], factual=True, reason="测试桩",
                links=[dict(evidence_id=eid, verdict="unsupported", reason="测试桩")
                       for eid in row["citations"]]) for row in payload["units"]]}
    result = compare(dataset(), Judge)
    assert len(calls) == 3
    assert calls[0][0] == "report-quote-v1"
    assert calls[1][0] == calls[2][0] == "report-quote-v2"
    assert calls[0][1] == calls[1][1]  # Prompt-only experiment uses identical input.
    assert len(calls[2][1]["units"]) > len(calls[1][1]["units"])
    assert result["promotion"] == "manual_review_required"
    assert result["arms"][0]["input_sha256"] == result["arms"][1]["input_sha256"]
    assert not result["arms"][-1]["benchmark"]["gate_passed"]


def test_all_arm_budgets_are_checked_before_first_model_call():
    calls = []
    def factory(version):
        calls.append(version)
        raise AssertionError("Model must not be constructed")
    with pytest.raises(VerificationError, match="Unit budget"):
        compare(dataset(), factory, max_units=len(dataset()["cases"]))
    assert not calls


def test_v1_dataset_and_prompt_remain_available_and_unchanged():
    data = json.loads((Path(__file__).parents[1] / "evals" / "evidence-v1.json").read_text(encoding="utf-8"))
    assert digest(json.dumps(data, sort_keys=True, ensure_ascii=False)) == "492251d3db78f681035d036d1b5e6e34934d4f414d08a42295e0c78e26350ca0"
    assert SYSTEM_PROMPT_V2.startswith(SYSTEM_PROMPT)
    assert "版本" not in SYSTEM_PROMPT  # Original English v1 prompt has not been replaced.


def test_omitting_a_whole_parent_cannot_pass_anchor_validation():
    parents = split_units("事实A；事实B。\n事实C。")
    rows = split_clauses(parents)
    with pytest.raises(VerificationError, match="whole source line"):
        validate_anchors([row for row in rows if row["parent_id"] != "U2"], parents)


def test_candidate_cli_records_version_offsets_and_keeps_original_report(tmp_path, monkeypatch):
    import sys
    from researchdesk import quality
    from researchdesk.ledger import Ledger
    run_id = "e" * 32
    ledger = Ledger(tmp_path / run_id)
    source = ledger.add_source("Fixture", "fixture://candidate", "系统支持日志。", "fixture")
    evidence = ledger.add_evidence("系统支持日志。", source["id"], "系统支持日志。")
    ledger.save_report(f"系统支持日志；系统支持自动备份。[{evidence['id']}]")
    (ledger.directory / "run.json").write_text(json.dumps({"status": "completed", "mode": "fixture"}))
    ledger.store.close()
    before = (ledger.directory / "report.md").read_bytes()
    class Judge:
        usage = {"total_tokens": 0}
        prompt_version = "report-quote-v2"
        def __call__(self, payload):
            assert len(payload["units"]) == 2
            return {"units": [dict(unit_id=row["unit_id"], factual=True, reason="测试桩",
                links=[dict(evidence_id=eid,
                            verdict="supported" if "日志" in row["text"] else "unsupported",
                            reason="测试桩") for eid in row["citations"]]) for row in payload["units"]]}
    def factory(model, *, prompt_version):
        assert prompt_version == "v2"
        return Judge()
    monkeypatch.setattr(quality, "ModelJudge", factory)
    monkeypatch.setattr(quality, "load_dotenv", lambda: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    monkeypatch.setattr(sys, "argv", ["quality", "verify", run_id, "--output", str(tmp_path),
                                     "--granularity", "clause", "--judge-prompt", "v2"])
    assert quality.main() == 0
    result = json.loads(next((ledger.directory / "verification").glob("*/result.json")).read_text(encoding="utf-8"))
    assert result["prompt_version"] == "report-quote-v2"
    assert result["unitization_version"] == "clause-spans-v1"
    assert result["metrics"]["evidence_coverage"] == 0.5
    assert (ledger.directory / "report.md").read_bytes() == before


def test_schema_failed_arm_is_unscored_and_other_arms_continue():
    from researchdesk.comparison import comparison_markdown
    saved = []
    class Judge:
        usage = {"total_tokens": 0}
        last_output = None
        def __init__(self, version):
            self.version = version
        def __call__(self, payload):
            if self.version == "v1":
                self.last_output = {"units": [{"unit_id": row["unit_id"], "factual": True,
                                               "links": []} for row in payload["units"]]}
            else:
                self.last_output = {"units": [dict(unit_id=row["unit_id"], factual=True,
                    reason="测试桩", links=[dict(evidence_id=eid, verdict="unsupported", reason="测试桩")
                                            for eid in row["citations"]]) for row in payload["units"]]}
            return self.last_output
    result = compare(dataset(), Judge, on_arm=saved.append)
    assert result["status"] == "partial"
    assert [arm["status"] for arm in saved] == ["failed", "complete", "complete"]
    assert saved[0]["metrics"] is None and saved[0]["benchmark"] is None
    assert saved[0]["schema_errors"][0]["location"] == ["units", 0, "reason"]
    assert "unaccepted_judge_output" in saved[0]
    assert "input" not in saved[0]["schema_errors"][0]
    assert "N/A" in comparison_markdown(result)


def test_compare_cli_persists_each_arm_even_when_one_is_invalid(tmp_path, monkeypatch):
    import sys
    from researchdesk import quality
    class Judge:
        usage = {"total_tokens": 0}
        def __init__(self, version):
            self.version = version
        def __call__(self, payload):
            return {"units": [] if self.version == "v1" else [dict(unit_id=row["unit_id"],
                factual=True, reason="测试桩", links=[dict(evidence_id=eid, verdict="unsupported", reason="测试桩")
                                                   for eid in row["citations"]]) for row in payload["units"]]}
    monkeypatch.setattr(quality, "ModelJudge", lambda model, prompt_version="v1": Judge(prompt_version))
    monkeypatch.setattr(quality, "load_dotenv", lambda: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    data_path = Path(__file__).parents[1] / "evals" / "claims-v2.json"
    monkeypatch.setattr(sys, "argv", ["quality", "compare", "--dataset", str(data_path), "--output", str(tmp_path)])
    assert quality.main() == 3
    result_path = next((tmp_path / "comparisons").glob("*/result.json"))
    assert json.loads(result_path.read_text(encoding="utf-8"))["status"] == "partial"
    assert (result_path.parent / "baseline_line_v1.json").is_file()
    assert (result_path.parent / "candidate_clause_v2.json").is_file()

