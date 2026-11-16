import pytest
from researchdesk.scope_check import PROMPT, PROMPT_V2, prompt_text, assess, fingerprint
from researchdesk.verification import VerificationError, digest


def data(prompt):
    payload = dict(parents=[], segments=[], evidence={}, integrity=dict(pending=[]))
    envelope = dict(payload_sha256=fingerprint(payload), prompt_sha256=digest(prompt),
                    judgment=dict(parents=[], assertions=[]))
    return payload, envelope


def test_v2_explicitly_requests_json_and_retains_v1_contract():
    assert "json" not in PROMPT.lower()
    assert "valid json only" in PROMPT_V2.lower()
    assert PROMPT_V2.startswith(PROMPT)
    assert prompt_text() == PROMPT and digest(PROMPT_V2) != digest(PROMPT)


def test_v1_import_remains_bound_to_v1():
    payload, envelope = data(PROMPT)
    assert assess(payload, envelope)["judge_prompt_version"] == "v1"
    with pytest.raises(VerificationError, match="binding"):
        assess(payload, envelope, prompt_version="v2")


def test_v2_import_is_explicit_and_cannot_be_reused_as_v1():
    payload, envelope = data(PROMPT_V2)
    result = assess(payload, envelope, prompt_version="v2")
    assert result["prompt_sha256"] == digest(PROMPT_V2)
    assert result["human_review"] == "pending" and not result["affects_original_scores"]
    with pytest.raises(VerificationError, match="binding"):
        assess(payload, envelope)


def test_unknown_prompt_refused():
    with pytest.raises(VerificationError):
        prompt_text("v4")
