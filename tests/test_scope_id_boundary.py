import copy
import pytest
from researchdesk.scope_check import PROMPT_V2, PROMPT_V3, prompt_text, assess, fingerprint
from researchdesk.verification import digest, VerificationError


def fixture():
    # Deliberately wrong nonfactual role: reviewer must flag the parent, not add IDs.
    payload = dict(parents=[dict(unit_id="U1", text="Fact")], segments=[
        dict(id="A1", role="assertion", parent_id="U1", conditions=[], bindings=[dict(evidence_id="E1")]),
        dict(id="A5", role="nonfactual", parent_id="U1", conditions=[], bindings=[])],
        evidence={"E1": dict(snippet="Fact")}, integrity=dict(pending=[]))
    judgment = dict(parents=[dict(parent_id="U1", declarations="incomplete", reason="Misclassified fact")],
        assertions=[dict(segment_id="A1", conditions="preserved", support="supported",
        anchors=[dict(evidence_id="E1", quote="Fact")], reason="Fixture")])
    envelope = dict(payload_sha256=fingerprint(payload), prompt_sha256=digest(PROMPT_V3), judgment=judgment)
    return payload, envelope


def test_misclassification_is_reported_without_reclassifying_input():
    payload, envelope = fixture()
    result = assess(payload, envelope, prompt_version="v3")
    assert result["parents"][0]["declarations"] == "incomplete"
    assert len(result["assertions"]) == 1
    assert result["assertions"][0]["assessment"] == "model_support_not_established"


def test_extra_assertion_for_nonfactual_segment_rejected_not_filtered():
    payload, envelope = fixture()
    before = copy.deepcopy(envelope)
    extra = dict(envelope["judgment"]["assertions"][0], segment_id="A5")
    envelope["judgment"]["assertions"].append(extra)
    with pytest.raises(VerificationError, match="unexpected=1"):
        assess(payload, envelope, prompt_version="v3")
    assert envelope["judgment"]["assertions"] == before["judgment"]["assertions"] + [extra]


def test_version_is_distinct_and_old_v2_prompt_is_retained():
    assert prompt_text("v2") == PROMPT_V2
    assert PROMPT_V3.startswith(PROMPT_V2) and "JSON" in PROMPT_V3
    payload, envelope = fixture()
    with pytest.raises(VerificationError, match="binding"):
        assess(payload, envelope, prompt_version="v2")


def test_unknown_version_still_rejected():
    with pytest.raises(VerificationError):
        prompt_text("v4")
