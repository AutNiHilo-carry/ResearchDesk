import pytest
from researchdesk.ledger import Ledger
from researchdesk.cli import main


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "run")


def test_rejects_fabricated_quote_and_source(ledger):
    source = ledger.add_source("Doc", "https://example.org", "Transactions are atomic.")
    with pytest.raises(ValueError, match="exactly match"):
        ledger.add_evidence("Claim", source["id"], "Transactions are always fast.")
    with pytest.raises(ValueError, match="Unknown source"):
        ledger.add_evidence("Claim", "missing", "Transactions")
    assert not ledger.evidence


def test_deduplicates_and_keeps_changed_snapshots(ledger):
    first = ledger.add_source("Doc", "https://example.org", "Version one.")
    assert ledger.add_source("Doc", "https://example.org", "Version one.") == first
    changed = ledger.add_source("Doc", "https://example.org", "Version two.")
    assert first["id"] != changed["id"]
    evidence = ledger.add_evidence("First version", first["id"], "Version one.")
    assert ledger.add_evidence("First version", first["id"], "Version one.") == evidence
    assert len((ledger.directory / "sources.jsonl").read_text().splitlines()) == 2
    assert len((ledger.directory / "evidence.jsonl").read_text().splitlines()) == 1


def test_report_rejects_missing_and_unknown_citations(ledger):
    with pytest.raises(ValueError):
        ledger.save_report("No evidence.")
    source = ledger.add_source("Doc", "file://doc", "Saved exact text.")
    evidence = ledger.add_evidence("Claim", source["id"], "exact text")
    with pytest.raises(ValueError, match="Unknown citations"):
        ledger.save_report("[Efake]")
    path = ledger.save_report(f"A claim.[{evidence['id']}]")
    assert "file://doc" in path.read_text(encoding="utf-8")


def test_offline_cli(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["researchdesk", "--demo", "--output", str(tmp_path)])
    assert main() == 0
    assert len(list(tmp_path.glob("*/report.md"))) == 1


def test_invalid_report_keeps_draft_but_no_published_report(ledger):
    with pytest.raises(ValueError):
        ledger.save_report("Unsupported draft [Efake]")
    assert (ledger.directory / "draft.md").read_text() == "Unsupported draft [Efake]"
    assert not (ledger.directory / "report.md").exists()

