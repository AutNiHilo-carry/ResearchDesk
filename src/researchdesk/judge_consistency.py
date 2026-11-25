"""Offline comparison of saved factual and citation labels for declared rewrites."""
import argparse
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from .verification import digest, replay_assessment


class Anchor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    unit_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    left: Anchor
    right: Anchor


class BoundCases(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    left_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    right_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Cases(BoundCases):
    version: Literal["judge-consistency-cases-v1"]
    relation: Literal["candidate_paraphrase_not_verified_equivalence"]
    pairs: list[Pair] = Field(min_length=1, max_length=100)


class RewriteGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    left: list[Anchor] = Field(min_length=1, max_length=8)
    right: list[Anchor] = Field(min_length=1, max_length=8)


class GroupCases(BoundCases):
    version: Literal["judge-consistency-groups-v1"]
    relation: Literal["declared_rewrite_not_verified_equivalence"]
    groups: list[RewriteGroup] = Field(min_length=1, max_length=100)


CASE_INPUT = TypeAdapter(Annotated[Cases | GroupCases, Field(discriminator="version")])


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compare(left_path, right_path, case_path):
    # Parse and bind the same bytes even if the path is replaced during comparison.
    raw_cases = Path(case_path).read_bytes()
    cases = CASE_INPUT.validate_json(raw_cases)
    if isinstance(cases, Cases):
        if len({pair.id for pair in cases.pairs}) != len(cases.pairs):
            raise ValueError("Duplicate pair IDs")
        anchors = [(p.left.unit_id, p.right.unit_id) for p in cases.pairs]
        if len(set(anchors)) != len(anchors):
            raise ValueError("Duplicate unit pairs")
    else:
        if len({group.id for group in cases.groups}) != len(cases.groups):
            raise ValueError("Duplicate group IDs")
        for side in ("left", "right"):
            ids = [anchor.unit_id for group in cases.groups for anchor in getattr(group, side)]
            if len(set(ids)) != len(ids):
                raise ValueError("A unit can only appear once on each side of the rewrite comparison")
    caches, hashes, indexed = [], [], []
    for path, expected in [(left_path, cases.left_sha256), (right_path, cases.right_sha256)]:
        raw = Path(path).read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise ValueError("Frozen assessment file changed")
        cache = json.loads(raw)
        result = cache["result"]
        prompt = cache.get("prompt_sha256")
        if (result.get("status") != "complete" or not isinstance(prompt, str) or len(prompt) != 64
                or result.get("judge_prompt_sha256") != prompt or not result.get("prompt_version")):
            raise ValueError("Require complete assessments with bound judge prompts")
        if isinstance(cases, GroupCases):
            payload = cache.get("payload")
            if (not isinstance(payload, dict)
                    or cache.get("input_sha256") != digest(json.dumps(payload, ensure_ascii=False, sort_keys=True))):
                raise ValueError("Require bound scoring inputs for a grouped comparison")
            replay_assessment(payload, result)
        units = result["units"]
        if any(type(row.get("factual")) is not bool or not isinstance(row.get("text"), str) for row in units):
            raise ValueError("Invalid factual labels or text")
        rows = {row["unit_id"]: row for row in units}
        if len(rows) != len(units):
            raise ValueError("Duplicate assessment unit IDs")
        caches.append(cache)
        hashes.append(actual)
        indexed.append(rows)
    if (caches[0]["prompt_sha256"] != caches[1]["prompt_sha256"]
            or caches[0]["result"]["prompt_version"] != caches[1]["result"]["prompt_version"]):
        raise ValueError("Mixed judge prompts cannot be treated as a consistency comparison")
    binding = dict(left_sha256=hashes[0], right_sha256=hashes[1],
                   cases_sha256=hashlib.sha256(raw_cases).hexdigest(), prompt_sha256=caches[0]["prompt_sha256"])
    if isinstance(cases, GroupCases):
        return compare_groups(cases.groups, indexed, binding)
    rows = []
    for pair in cases.pairs:
        labels = [anchored_row(anchor, units)["factual"] for anchor, units in zip((pair.left, pair.right), indexed)]
        rows.append(dict(id=pair.id, left=pair.left.model_dump(), right=pair.right.model_dump(),
                         left_factual=labels[0], right_factual=labels[1], disagreement=labels[0] != labels[1]))
    return dict(version="judge-consistency-v1", **binding,
        pairs=rows, compared_pairs=len(rows), factual_disagreements=sum(r["disagreement"] for r in rows),
        scope="declared candidate paraphrases; equivalence and correctness not verified; contexts may differ",
        human_review="pending", accuracy=None, api_calls=0)


def anchored_row(anchor, units):
    row = units.get(anchor.unit_id)
    if row is None or row["text"] != anchor.text:
        raise ValueError("Candidate anchor does not match saved assessment text")
    return row


def compare_groups(groups, indexed, binding):
    rows = []
    for group in groups:
        left, right = [[anchored_row(anchor, units) for anchor in anchors]
                       for anchors, units in zip((group.left, group.right), indexed)]
        deltas = {label: sum(row[label] for row in right) - sum(row[label] for row in left)
                  for label in ("factual", "covered")}
        rows.append(dict(id=group.id, left=left, right=right,
                         factual_units_delta=deltas["factual"], covered_units_delta=deltas["covered"]))
    return dict(version="judge-consistency-group-comparison-v1", **binding, groups=rows,
                compared_groups=len(rows), scope="saved labels for declared rewrites; equivalence unverified",
                human_review="pending", accuracy=None, api_calls=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("left", "right", "cases", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.suffix != ".json":
        parser.error("Output must be a new .json file")
    result = compare(args.left, args.right, args.cases)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    if "compared_groups" in result:
        print(f"Compared {result['compared_groups']} rewrite groups; zero API calls")
    else:
        print(f"Compared {result['compared_pairs']} pairs; factual disagreements: {result['factual_disagreements']}")


if __name__ == "__main__":
    main()
