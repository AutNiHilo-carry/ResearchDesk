import json
import pytest
from researchdesk.judge_consistency import compare, fingerprint


@pytest.fixture
def data(tmp_path):
    paths = [tmp_path / name for name in ["left.json", "right.json", "cases.json"]]
    for path, factual in zip(paths, [False, True]):
        path.write_text(json.dumps(dict(prompt_sha256="a"*64, result=dict(status="complete",
            judge_prompt_sha256="a"*64, prompt_version="fixture-v2",
            units=[dict(unit_id="U1", text="Candidate", factual=factual)]))))
    cases = dict(version="judge-consistency-cases-v1", relation="candidate_paraphrase_not_verified_equivalence",
        left_sha256=fingerprint(paths[0]), right_sha256=fingerprint(paths[1]),
        pairs=[dict(id="P1", left=dict(unit_id="U1", text="Candidate"), right=dict(unit_id="U1", text="Candidate"))])
    paths[2].write_text(json.dumps(cases))
    return paths, cases


def test_difference_is_observation_not_accuracy_and_files_unchanged(data):
    paths, _ = data
    before = [p.read_bytes() for p in paths]
    result = compare(*paths)
    assert result["factual_disagreements"] == 1 and result["compared_pairs"] == 1
    assert result["accuracy"] is None and result["human_review"] == "pending" and result["api_calls"] == 0
    assert [p.read_bytes() for p in paths] == before


def test_changed_cache_is_rejected(data):
    paths, _ = data
    paths[0].write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        compare(*paths)


@pytest.mark.parametrize("issue", ["anchor", "duplicate", "same_pair"])
def test_bad_pairs_rejected(data, issue):
    paths, cases = data
    if issue == "anchor":
        cases["pairs"][0]["left"]["text"] = "Paraphrased by tool"
    else:
        cases["pairs"].append(dict(cases["pairs"][0], id="P2" if issue == "same_pair" else "P1"))
    paths[2].write_text(json.dumps(cases))
    with pytest.raises(ValueError):
        compare(*paths)


@pytest.mark.parametrize("issue", ["mixed_prompt", "incomplete", "non_boolean", "duplicate_units"])
def test_invalid_assessments_rejected_even_with_rebound_file_hash(data, issue):
    paths, cases = data
    cache = json.loads(paths[1].read_text())
    if issue == "mixed_prompt":
        cache["prompt_sha256"] = cache["result"]["judge_prompt_sha256"] = "b"*64
    elif issue == "incomplete":
        cache["result"]["status"] = "failed"
    elif issue == "non_boolean":
        cache["result"]["units"][0]["factual"] = "true"
    else:
        cache["result"]["units"] *= 2
    paths[1].write_text(json.dumps(cache))
    cases["right_sha256"] = fingerprint(paths[1])
    paths[2].write_text(json.dumps(cases))
    with pytest.raises(ValueError):
        compare(*paths)
