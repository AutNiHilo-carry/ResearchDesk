import copy
import json
import pytest
from researchdesk.ledger import Ledger
from researchdesk.scope_trace import create, check
from researchdesk.verification import CITATION, VerificationError


@pytest.fixture
def run(tmp_path):
    ledger = Ledger(tmp_path / "run")
    source = ledger.add_source("Fixture", "fixture://scope", "只有内存足够时才运行。")
    eid = ledger.add_evidence("Conditional fact", source["id"], source["content"])["id"]
    body = f"只有内存足够时，系统才运行[{eid}]。"
    ledger.save_report(body)
    (ledger.directory / "run.json").write_text(json.dumps(dict(status="completed")))
    raw = create(ledger.directory)
    cut = body.index("，") + 1
    m = CITATION.search(body)
    raw["segments"] = [dict(id="C1", parent_id="U1", start=0, end=cut, text=body[:cut],
        role="condition", condition_review="pending", conditions=[], bindings=[]),
        dict(id="A1", parent_id="U1", start=cut, end=len(body), text=body[cut:], role="assertion",
        condition_review="linked", conditions=["C1"], bindings=[dict(evidence_id=eid,
        start=m.start(), end=m.end(), scope="declared_direct")])]
    yield ledger, raw
    ledger.store.close()


def test_export_stays_pending_and_does_not_invent_scope(run):
    ledger, _ = run
    result = check(ledger.directory, create(ledger.directory))
    assert result["status"] == "draft"
    assert {p["reason"] for p in result["pending"]} == {"unclassified", "unassigned_citation"}


def test_exact_scope_can_pass_integrity_without_semantic_approval(run):
    ledger, raw = run
    before = (ledger.directory / "report.md").read_bytes()
    result = check(ledger.directory, raw)
    assert result["status"] == "structure_checked" and not result["pending"]
    assert result["semantic_verification"] == "not_performed" and result["human_review"] == "pending"
    assert (ledger.directory / "report.md").read_bytes() == before


@pytest.mark.parametrize("fault", ["text", "gap", "overlap", "tail", "duplicate", "missing_parent",
                                    "bad_condition", "wrong_role", "citation_anchor", "citation_id"])
def test_broken_scope_rejected(run, fault):
    ledger, raw = run
    raw = copy.deepcopy(raw)
    a, b = raw["segments"]
    if fault == "text": a["text"] = "Invented condition"
    elif fault == "gap": b["start"] += 1; b["text"] = b["text"][1:]
    elif fault == "overlap": b["start"] -= 1; b["text"] = a["text"][-1] + b["text"]
    elif fault == "tail": b["end"] -= 1; b["text"] = b["text"][:-1]
    elif fault == "duplicate": b["id"] = a["id"]
    elif fault == "missing_parent": raw["segments"] = []
    elif fault == "bad_condition": b["conditions"] = ["missing"]
    elif fault == "wrong_role": a["role"] = "context"
    elif fault == "citation_anchor": b["bindings"][0]["start"] += 1
    elif fault == "citation_id": b["bindings"][0]["evidence_id"] = "Eunknown"
    with pytest.raises(ValueError):
        check(ledger.directory, raw)


def test_unlinked_condition_and_unreviewed_scope_remain_pending(run):
    ledger, raw = run
    raw["segments"][1].update(conditions=[], condition_review="pending")
    result = check(ledger.directory, raw)
    assert result["status"] == "draft"
    assert {p["reason"] for p in result["pending"]} == {"orphan_condition", "condition_scope_unreviewed"}


@pytest.mark.parametrize("scope", ["candidate_pool", "unresolved"])
def test_candidate_binding_is_not_confirmed_by_integrity(run, scope):
    ledger, raw = run
    raw["segments"][1]["bindings"][0]["scope"] = scope
    assert "citation_scope_unresolved" in {p["reason"] for p in check(ledger.directory, raw)["pending"]}


def test_changed_corpus_invalidates_trace(run):
    ledger, raw = run
    ledger.add_source("New", "fixture://new", "Additional snapshot")
    with pytest.raises(VerificationError, match="changed"):
        check(ledger.directory, raw)
