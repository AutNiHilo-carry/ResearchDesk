import json
import pytest
from researchdesk.judge_consistency import compare, fingerprint
from researchdesk.verification import assess, digest


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


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_cases_hash_binds_parsed_snapshot_when_file_changes(data, monkeypatch, change):
    paths, _ = data
    original_hash = fingerprint(paths[2])
    original_read = type(paths[0]).read_bytes

    def read_and_change_cases(path):
        raw = original_read(path)
        if path == paths[0]:
            if change == "replace":
                paths[2].write_text("{}")
            else:
                paths[2].unlink()
        return raw

    monkeypatch.setattr(type(paths[0]), "read_bytes", read_and_change_cases)
    result = compare(*paths)
    assert result["cases_sha256"] == original_hash
    assert result["compared_pairs"] == 1
    assert result["pairs"][0]["id"] == "P1"
    assert result["factual_disagreements"] == 1


@pytest.fixture
def grouped(tmp_path):
    paths = [tmp_path / name for name in ("left.json", "right.json", "groups.json")]
    left = ["备份无阻塞暂难保证；摘记未做停电试验。"]
    right = ["备份无阻塞暂难保证。", "摘记未做停电试验。"]
    prompt_hash = "b" * 64

    def freeze(path, texts, labels):
        evidence_id = "E" + "1" * 16
        units = [dict(unit_id=f"U{i}", line=i, text=text, citations=[evidence_id])
                 for i, text in enumerate(texts, 1)]
        payload = dict(units=units, evidence=[{"evidence_id": evidence_id, "integrity": "valid"}])

        def judge(_):
            return dict(units=[dict(unit_id=unit["unit_id"], factual=factual, reason="fixture",
                         links=[dict(evidence_id=evidence_id, verdict=verdict, reason="fixture")])
                         for unit, (factual, verdict) in zip(units, labels, strict=True)])

        judge.prompt_sha256 = prompt_hash
        judge.prompt_version = "fixture"
        cache = dict(prompt_sha256=prompt_hash, payload=payload, result=assess(payload, judge),
                     input_sha256=digest(json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        path.write_text(json.dumps(cache), encoding="utf-8")
        return [{key: unit[key] for key in ("unit_id", "text")} for unit in units]

    left_anchors = freeze(paths[0], left, [(False, "not_applicable")])
    right_anchors = freeze(paths[1], right, [(True, "partial"), (False, "not_applicable")])
    cases = dict(version="judge-consistency-groups-v1", relation="declared_rewrite_not_verified_equivalence",
                 left_sha256=fingerprint(paths[0]), right_sha256=fingerprint(paths[1]),
                 groups=[dict(id="G1", left=left_anchors, right=right_anchors)])
    paths[2].write_text(json.dumps(cases), encoding="utf-8")
    return paths, cases


def test_group_retains_one_to_many_labels_and_denominator_change(grouped):
    paths, _ = grouped
    before = [path.read_bytes() for path in paths]
    result = compare(*paths)
    group, = result["groups"]
    assert [row["factual"] for row in group["left"]] == [False]
    assert [row["factual"] for row in group["right"]] == [True, False]
    assert group["right"][0]["links"][0]["verdict"] == "partial"
    assert (group["factual_units_delta"], group["covered_units_delta"]) == (1, 0)
    assert result["compared_groups"] == 1 and result["accuracy"] is None and result["api_calls"] == 0
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize("mutation", ["anchor", "duplicate_unit", "unbound_payload", "tampered_metrics"])
def test_group_rejects_inconsistent_bindings(grouped, mutation):
    paths, cases = grouped
    group = cases["groups"][0]
    if mutation == "anchor":
        group["right"][0]["text"] = "Changed text"
    elif mutation == "duplicate_unit":
        group["right"].append(group["right"][0])
    else:
        cache = json.loads(paths[1].read_text(encoding="utf-8"))
        if mutation == "unbound_payload":
            cache["payload"]["units"][0]["text"] = "Mismatched input"
        else:
            cache["result"]["metrics"]["covered_units"] = 1
        paths[1].write_text(json.dumps(cache), encoding="utf-8")
        cases["right_sha256"] = fingerprint(paths[1])
    paths[2].write_text(json.dumps(cases), encoding="utf-8")
    with pytest.raises(ValueError):
        compare(*paths)
