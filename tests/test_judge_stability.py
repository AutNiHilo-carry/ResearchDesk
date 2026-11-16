import copy
import json
from pathlib import Path
import pytest
from researchdesk.judge_stability import frozen_input, payload_hash, run_stability
from researchdesk.verification import VerificationError


def inputs():
    payload = dict(units=[dict(unit_id="task:U1", task_id="task", line=1, text="A fact.[E1]", citations=["E1"])],
                   evidence=[dict(evidence_id="E1", integrity="valid", snippet="A fact.", location="fixture://a")])
    return payload, dict(input_sha256=payload_hash(payload), prompt_sha256="fixture", prompt_version="fixture-v2")


class Judge:
    prompt_sha256 = "fixture"
    prompt_version = "fixture-v2"
    usage = dict(input_tokens=1, output_tokens=2, total_tokens=3)
    def __init__(self, label="supported"):
        self.label = label
    def __call__(self, payload):
        if self.label == "invalid":
            return {"units": []}
        return {"units": [dict(unit_id="task:U1", factual=True, reason="Fixture",
            links=[dict(evidence_id="E1", verdict=self.label, reason="Fixture")])]}


def test_identical_repeats_and_exact_pairwise_disagreement_without_majority(tmp_path):
    payload, controls = inputs()
    requests, labels = [], iter(["supported", "partial", "supported"])
    def factory():
        delegate = Judge(next(labels))
        class Observed(Judge):
            def __call__(self, request):
                requests.append(copy.deepcopy(request))
                return delegate(request)
        return Observed()
    result = run_stability(payload, controls, tmp_path / "audit", factory)
    assert all(request == payload for request in requests) and len(requests) == 3
    assert result["successful_trial_pairs"] == 3
    assert result["link_pairwise_agreement"] == 1/3
    assert result["factual_pairwise_agreement"] == 1
    assert result["unstable_unit_ids"] == ["task:U1"]
    assert result["units"][0]["link_verdict_counts"]["E1"] == {"supported": 2, "partial": 1}
    assert result["total_tokens"] == 9 and result["majority_label_applied"] is False
    assert result["trials"][0]["quality"]["judge_prompt_sha256"] == "fixture"
    assert result["trials"][0]["quality"]["prompt_version"] == "fixture-v2"
    assert json.loads((tmp_path / "audit" / "input.json").read_text()) == payload


def test_schema_failure_is_preserved_and_not_retried_or_counted_as_agreement(tmp_path):
    payload, controls = inputs()
    labels = iter(["invalid", "supported"])
    result = run_stability(payload, controls, tmp_path / "audit", lambda: Judge(next(labels)), repeats=2)
    assert result["status"] == "partial" and result["schema_completion_rate"] == 0.5
    assert result["link_pairwise_agreement"] is None and result["stable_unit_fraction"] is None
    assert result["trials"][0]["unaccepted_output"] == {"units": []}
    assert len(result["trials"]) == 2 and result["total_tokens"] == 6


def test_uncertain_request_usage_stays_unknown_and_failed_round_not_retried(tmp_path):
    payload, controls = inputs()
    calls = []
    class Timeout(Judge):
        usage = None
        def __call__(self, request):
            calls.append("request")
            raise RuntimeError("private provider details must not leak")
    result = run_stability(payload, controls, tmp_path / "audit", Timeout, repeats=2)
    assert len(calls) == 2 and result["valid_trials"] == 0
    assert result["total_tokens"] is None and result["link_pairwise_agreement"] is None
    assert "private provider" not in (tmp_path / "audit" / "result.json").read_text()


@pytest.mark.parametrize("count", [True, 1, 6])
def test_invalid_repeat_budget_fails_before_any_request(tmp_path, count):
    payload, controls = inputs()
    def never():
        raise AssertionError("No request")
    with pytest.raises(VerificationError, match="2..5"):
        run_stability(payload, controls, tmp_path / "audit", never, repeats=count)
    assert not (tmp_path / "audit").exists()


def test_changed_input_is_rejected_and_existing_artifacts_are_not_overwritten(tmp_path):
    payload, controls = inputs()
    changed = copy.deepcopy(payload)
    changed["units"][0]["text"] = "Changed."
    with pytest.raises(VerificationError, match="fingerprint"):
        run_stability(changed, controls, tmp_path / "rejected", Judge)
    run_stability(payload, controls, tmp_path / "saved", Judge, repeats=2)
    with pytest.raises(FileExistsError):
        run_stability(payload, controls, tmp_path / "saved", Judge, repeats=2)


def test_prompt_change_blocks_requests_and_does_not_claim_usage(tmp_path):
    payload, controls = inputs()
    class Changed(Judge):
        prompt_sha256 = "different"
        def __call__(self, request):
            raise AssertionError("No request")
    result = run_stability(payload, controls, tmp_path / "audit", Changed, repeats=2)
    assert result["valid_trials"] == 0 and result["total_tokens"] is None
    assert all(row["request_started"] is False for row in result["trials"])


def test_request_mutation_is_not_treated_as_a_frozen_repeat(tmp_path):
    payload, controls = inputs()
    class Mutated(Judge):
        def __call__(self, request):
            request["units"][0]["text"] = "Mutated."
            return super().__call__(request)
    result = run_stability(payload, controls, tmp_path / "audit", Mutated, repeats=2)
    assert result["valid_trials"] == 0
    assert payload["units"][0]["text"] == "A fact.[E1]"
