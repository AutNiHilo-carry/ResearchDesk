import copy
import json
import pytest
from researchdesk.ledger import Ledger
from researchdesk.scope_trace import create
from researchdesk.scope_check import prepare, assess, fingerprint, PROMPT
from researchdesk.verification import CITATION, digest, VerificationError


@pytest.fixture
def sample(tmp_path):
    ledger = Ledger(tmp_path / "run")
    source = ledger.add_source("Fixture", "fixture://conditional", "只有内存足够时才运行。")
    eid = ledger.add_evidence("Fact", source["id"], source["content"])["id"]
    body = f"只有内存足够时，系统才运行[{eid}]。"
    ledger.save_report(body)
    (ledger.directory / "run.json").write_text(json.dumps(dict(status="completed")))
    trace = create(ledger.directory)
    cut = body.index("，") + 1
    ref = CITATION.search(body)
    trace["segments"] = [dict(id="C", parent_id="U1", start=0, end=cut, text=body[:cut],
        role="condition", condition_review="pending", conditions=[], bindings=[]),
        dict(id="A", parent_id="U1", start=cut, end=len(body), text=body[cut:], role="assertion",
        condition_review="linked", conditions=["C"], bindings=[dict(evidence_id=eid,
        start=ref.start(), end=ref.end(), scope="declared_direct")])]
    payload = prepare(ledger.directory, trace)
    judgment = dict(parents=[dict(parent_id="U1", declarations="complete", reason="fixture")],
        assertions=[dict(segment_id="A", conditions="preserved", support="supported",
        anchors=[dict(evidence_id=eid, quote=source["content"])], reason="fixture")])
    envelope = dict(payload_sha256=fingerprint(payload), prompt_sha256=digest(PROMPT), judgment=judgment)
    yield ledger, trace, payload, envelope
    ledger.store.close()


def test_model_candidate_is_never_human_approval_or_score_change(sample):
    ledger, _, payload, envelope = sample
    before = (ledger.directory / "report.md").read_bytes()
    result = assess(payload, envelope)
    assert result["assertions"][0]["assessment"] == "model_support_candidate"
    assert result["human_review"] == "pending" and not result["affects_original_scores"]
    assert result["api_calls"] == 0 and (ledger.directory / "report.md").read_bytes() == before


@pytest.mark.parametrize("target,value", [("conditions", "missing"), ("conditions", "unnecessary"),
    ("conditions", "uncertain"), ("support", "partial"), ("support", "contradicted"), ("support", "insufficient")])
def test_negative_dimensions_block_candidate(sample, target, value):
    _, _, payload, envelope = sample
    envelope["judgment"]["assertions"][0][target] = value
    assert assess(payload, envelope)["assertions"][0]["assessment"] == "model_support_not_established"


def test_incomplete_parent_blocks_even_positive_assertion(sample):
    _, _, payload, envelope = sample
    envelope["judgment"]["parents"][0]["declarations"] = "incomplete"
    assert assess(payload, envelope)["assertions"][0]["assessment"] == "model_support_not_established"


@pytest.mark.parametrize("fault", ["parent_missing", "parent_duplicate", "assertion_missing", "unknown_id",
    "anchor_missing", "invented_quote", "empty_support", "input_hash", "prompt_hash"])
def test_invalid_review_cannot_be_imported(sample, fault):
    _, _, payload, e = sample
    e = copy.deepcopy(e)
    a = e["judgment"]["assertions"][0]
    if fault == "parent_missing": e["judgment"]["parents"] = []
    elif fault == "parent_duplicate": e["judgment"]["parents"] *= 2
    elif fault == "assertion_missing": e["judgment"]["assertions"] = []
    elif fault == "unknown_id": a["segment_id"] = "fake"
    elif fault == "anchor_missing": a["anchors"] = []
    elif fault == "invented_quote": a["anchors"][0]["quote"] = "Unconditional capability"
    elif fault == "empty_support": a["anchors"][0]["quote"] = ""
    elif fault == "input_hash": e["payload_sha256"] = "changed"
    elif fault == "prompt_hash": e["prompt_sha256"] = "changed"
    with pytest.raises(ValueError):
        assess(payload, e)


def test_orphan_condition_cannot_be_rescued_by_positive_model(sample):
    ledger, trace, _, envelope = sample
    trace["segments"][1].update(conditions=[], condition_review="none_declared")
    payload = prepare(ledger.directory, trace)
    envelope["payload_sha256"] = fingerprint(payload)
    assert assess(payload, envelope)["assertions"][0]["assessment"] == "model_support_not_established"


def test_candidate_pool_cannot_be_rescued_by_positive_model(sample):
    ledger, trace, _, envelope = sample
    trace["segments"][1]["bindings"][0]["scope"] = "candidate_pool"
    payload = prepare(ledger.directory, trace)
    envelope["payload_sha256"] = fingerprint(payload)
    assert assess(payload, envelope)["assertions"][0]["assessment"] == "model_support_not_established"


def test_changed_source_refuses_preparation(sample):
    ledger, trace, _, _ = sample
    ledger.add_source("New", "fixture://new", "Changed corpus")
    with pytest.raises(VerificationError):
        prepare(ledger.directory, trace)
