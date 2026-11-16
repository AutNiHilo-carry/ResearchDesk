import copy
import json
import pytest
from researchdesk.scope_review import packet, template, import_review
from researchdesk.scope_check import PROMPT_V3, fingerprint
from researchdesk.verification import digest


@pytest.fixture
def data(tmp_path):
    payload = dict(parents=[dict(unit_id="U1", text="Fact")], segments=[dict(id="A1", parent_id="U1",
        role="assertion", conditions=[], bindings=[dict(evidence_id="E1")])], evidence={"E1":dict(snippet="Fact")},
        integrity=dict(pending=[]))
    plan = dict(payload=payload, payload_sha256=fingerprint(payload), judge_prompt_version="v3",
        prompt_sha256=digest(PROMPT_V3), cases=[dict(engineering_reference="SECRET_EXPECTED_LABEL")])
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    p = packet(path)
    form = dict(packet_sha256=fingerprint(p), reviewer="Fixture reviewer", reviewed_at="2026-11-16T12:00:00+08:00",
        confirmed=True, judgment=dict(parents=[dict(parent_id="U1", declarations="complete", reason="Fixture")],
        assertions=[dict(segment_id="A1", conditions="preserved", support="supported",
        anchors=[dict(evidence_id="E1", quote="Fact")], reason="Fixture")]))
    return p, form


def test_export_does_not_prefill_labels_or_reveal_engineering_references(data):
    p, _ = data
    form = template(p)
    assert "SECRET_EXPECTED_LABEL" not in json.dumps(p)
    assert form["reviewer"] is None and not form["confirmed"]
    assert form["judgment"]["assertions"][0]["support"] is None
    with pytest.raises(ValueError):
        import_review(p, form)


def test_import_is_only_human_declared_not_gold_or_identity_proof(data):
    p, form = data
    before = copy.deepcopy((p, form))
    result = import_review(p, form)
    assert result["label_status"] == "human_declared_not_independently_verified"
    assert not result["identity_verified"] and not result["affects_original_scores"]
    assert (p, form) == before


@pytest.mark.parametrize("fault", ["packet", "confirmation", "reviewer", "timestamp", "missing_id", "quote"])
def test_invalid_reference_rejected(data, fault):
    p, f = data
    if fault == "packet": f["packet_sha256"] = "changed"
    elif fault == "confirmation": f["confirmed"] = False
    elif fault == "reviewer": f["reviewer"] = " "
    elif fault == "timestamp": f["reviewed_at"] = "2026-11-16T12:00:00"
    elif fault == "missing_id": f["judgment"]["parents"] = []
    else: f["judgment"]["assertions"][0]["anchors"][0]["quote"] = "Invented"
    with pytest.raises(ValueError):
        import_review(p, f)
