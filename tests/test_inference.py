import copy
import json
import pytest
from researchdesk.inference import check_trace, create_trace
from researchdesk.ledger import Ledger
from researchdesk.storage import atomic_text
from researchdesk.verification import VerificationError


@pytest.fixture
def run(tmp_path):
    directory = tmp_path / "run"
    ledger = Ledger(directory)
    atomic_text(directory / "run.json", json.dumps(dict(status="completed")))
    a = ledger.add_source("Guide", "fixture://guide", "Guide says recovery is supported. Date 2026-11-01.", "local")
    b = ledger.add_source("Release", "fixture://release", "Release says recovery is unsupported. Date 2026-11-01.", "local")
    first = ledger.add_evidence("Guide statement", a["id"], "Guide says recovery is supported.")
    second = ledger.add_evidence("Release statement", b["id"], "Release says recovery is unsupported.")
    ledger.save_report(f"# Report\nGuide says recovery is supported.[{first['id']}]\nThe documents conflict.")
    ledger.store.close()
    return directory, first, second


def classify(trace):
    for row in trace["claims"]:
        row.update(kind="nonfactual", rationale="Test-only declaration; not semantic truth.")
    return trace


def test_export_preserves_all_lines_and_is_only_a_draft(run, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("No network allowed"))
    directory, _, _ = run
    trace = create_trace(directory)
    assert len(trace["claims"]) == 3
    result = check_trace(directory, trace)
    assert result["status"] == "draft" and len(result["pending"]) == 3
    assert result["semantic_verification"] == "not_performed" and result["human_review"] == "pending"


def test_joint_premises_bind_two_sources_without_rewriting_report_or_scores(run):
    directory, first, second = run
    before = (directory / "draft.md").read_bytes()
    trace = classify(create_trace(directory))
    claim = trace["claims"][-1]
    claim.update(kind="synthesis", rationale="Opposing source statements require scope review.", premises=[
        dict(id="P1", statement="Guide says recovery is supported.", evidence_ids=[first["id"]]),
        dict(id="P2", statement="Release says recovery is unsupported.", evidence_ids=[second["id"]])])
    result = check_trace(directory, trace)
    assert result["status"] == "structure_checked" and result["premise_count"] == 2
    assert len({b["source_id"] for b in result["bindings"]}) == 2
    assert all(b["cited_on_original_line"] is False for b in result["bindings"])
    assert result["semantic_verification"] == "not_performed" and result["human_review"] == "pending"
    assert (directory / "draft.md").read_bytes() == before
    assert "citation_precision" not in result and "success" not in result


@pytest.mark.parametrize("change", ["omit", "duplicate", "invent", "rewrite"])
def test_conclusions_cannot_omit_duplicate_invent_or_replace_report_units(run, change):
    directory, _, _ = run
    trace = create_trace(directory)
    if change == "omit":
        trace["claims"].pop()
    elif change == "duplicate":
        trace["claims"][1] = copy.deepcopy(trace["claims"][0])
    elif change == "invent":
        trace["claims"][0]["unit_id"] = "invented"
    else:
        trace["claims"][0]["text"] = "Changed report"
    with pytest.raises(VerificationError):
        check_trace(directory, trace)


def test_uncited_source_changes_invalidate_the_trace_snapshot(run):
    directory, _, second = run
    trace = create_trace(directory)
    ledger = Ledger(directory, resume=True)
    source = ledger.sources[second["source_id"]]
    source["content"] += " New context."
    with ledger.store.transaction() as connection:
        connection.execute("UPDATE records SET payload=? WHERE kind='sources' AND id=?",
                           (json.dumps(source), source["id"]))
    ledger.store.close()
    with pytest.raises(VerificationError, match="changed after"):
        check_trace(directory, trace)


def test_missing_premises_and_evidence_remain_explicitly_pending(run):
    directory, _, _ = run
    trace = classify(create_trace(directory))
    trace["claims"][-1].update(kind="abstention", premises=[])
    result = check_trace(directory, trace)
    assert result["status"] == "draft" and result["pending"][0]["reason"] == "missing_premises"
    trace["claims"][-1]["premises"] = [dict(id="P1", statement="Unconfirmed premise", evidence_ids=[])]
    assert check_trace(directory, trace)["pending"][0]["reason"] == "missing_evidence"


@pytest.mark.parametrize("change", ["unknown_evidence", "duplicate_premise", "duplicate_evidence", "blank"])
def test_invalid_premise_links_rejected(run, change):
    directory, first, _ = run
    trace = classify(create_trace(directory))
    premise = dict(id="P1", statement="Declared fact", evidence_ids=[first["id"]])
    trace["claims"][-1].update(kind="synthesis", premises=[premise])
    if change == "unknown_evidence":
        premise["evidence_ids"] = ["Eunknown"]
    elif change == "duplicate_premise":
        trace["claims"][-1]["premises"].append(copy.deepcopy(premise))
    elif change == "duplicate_evidence":
        premise["evidence_ids"] *= 2
    else:
        premise["statement"] = "   "
    with pytest.raises(VerificationError):
        check_trace(directory, trace)


def test_intact_but_irrelevant_quote_does_not_become_semantic_approval(run):
    directory, first, _ = run
    trace = classify(create_trace(directory))
    trace["claims"][-1].update(kind="synthesis", premises=[
        dict(id="P1", statement="Unrelated assertion about unlimited throughput", evidence_ids=[first["id"]])])
    result = check_trace(directory, trace)
    assert result["status"] == "structure_checked"
    assert result["semantic_verification"] == "not_performed" and result["human_review"] == "pending"


def test_forged_semantic_labels_are_not_accepted_as_trace_input(run):
    directory, _, _ = run
    trace = create_trace(directory)
    trace["semantic_verification"] = "passed"
    with pytest.raises(ValueError):
        check_trace(directory, trace)


@pytest.mark.parametrize("text", ["```\nhidden\n```", "## Evidence Ledger\nhidden"])
def test_omitted_report_content_cannot_get_a_partial_trace(run, text):
    directory, _, _ = run
    with (directory / "draft.md").open("a", encoding="utf-8") as out:
        out.write("\n" + text)
    with pytest.raises(VerificationError):
        create_trace(directory)
