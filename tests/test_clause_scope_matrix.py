import json
from pathlib import Path
import pytest
from researchdesk.claims import split_clauses, validate_anchors
from researchdesk.verification import split_units

DATA = json.loads((Path(__file__).parents[1] / "evals/clause-scope-structural-v1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["id"])
@pytest.mark.parametrize("policy", ["clause-spans-v1", "clause-spans-v2", "clause-spans-v3"])
def test_scope_matrix_preserves_source_and_declared_binding(case, policy):
    parents = split_units(case["text"])
    rows = split_clauses(parents, policy_version=policy)
    validate_anchors(rows, parents)
    assert len(rows) == case["expected_units"][policy]
    assert "".join(r["text"] for r in rows) == case["text"]
    assert set(eid for r in rows for eid in r["citations"]) == set(parents[0]["citations"])
    assert all(r["unitization_version"] == policy for r in rows)
    if policy == "clause-spans-v3" and "expected_v3_citations" in case:
        assert [r["citations"] for r in rows] == case["expected_v3_citations"]
    if policy == "clause-spans-v3" and "expected_v3_direct_citations" in case:
        assert [r["direct_citations"] for r in rows] == case["expected_v3_direct_citations"]
        assert [r["citation_binding"] for r in rows] == case["expected_v3_bindings"]


def test_reference_is_not_semantic_ground_truth():
    assert DATA["human_review"] == "pending"
    assert DATA["reference_status"] == "engineering_structural_reference_not_semantic_gold"
    assert len({c["id"] for c in DATA["cases"]}) == len(DATA["cases"])
