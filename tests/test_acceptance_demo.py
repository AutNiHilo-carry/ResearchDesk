import json
import pytest
from researchdesk.acceptance_demo import run


def test_demo_uses_real_policy_and_receipts_without_network_or_model(tmp_path, monkeypatch):
    monkeypatch.setattr("researchdesk.tools.ResearchTools.request", lambda *a, **k: pytest.fail("Network adapter invoked"))
    monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", lambda *a, **k: pytest.fail("Model instantiated"))
    monkeypatch.setenv("TAVILY_API_KEY", "ignored-fixture")
    root = tmp_path / "demo"
    result = run(root)
    assert result["status"] == "mechanism_checks_passed" and all(result["checks"].values())
    assert result["model_requests"] == result["external_requests"] == 0
    assert result["counter_before_reopen"] == result["counter_after_reopen"]
    assert result["fixture_quality_metrics"]["evidence_coverage"] == 0.5
    quality = json.loads((root / "fixture-quality.json").read_text(encoding="utf-8"))
    assert quality["label_origin"] == "scripted_fixture" and not quality["proves_model_accuracy"]
    assert (root / "research/report.md").is_file() and (root / "summary.md").is_file()


def test_existing_output_is_never_overwritten(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    marker = root / "marker"
    marker.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        run(root)
    assert marker.read_bytes() == b"preserve"
