"""Offline factual-label comparison for explicitly declared candidate paraphrases."""
import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Anchor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    unit_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    left: Anchor
    right: Anchor


class Cases(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["judge-consistency-cases-v1"]
    relation: Literal["candidate_paraphrase_not_verified_equivalence"]
    left_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    right_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pairs: list[Pair] = Field(min_length=1, max_length=100)


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compare(left_path, right_path, case_path):
    # Parse and bind the same bytes even if the path is replaced during comparison.
    raw_cases = Path(case_path).read_bytes()
    cases = Cases.model_validate_json(raw_cases.decode("utf-8"))
    if len({pair.id for pair in cases.pairs}) != len(cases.pairs):
        raise ValueError("Duplicate pair IDs")
    anchors = [(p.left.unit_id, p.right.unit_id) for p in cases.pairs]
    if len(set(anchors)) != len(anchors):
        raise ValueError("Duplicate unit pairs")
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
    rows = []
    for pair in cases.pairs:
        labels = []
        for anchor, units in zip([pair.left, pair.right], indexed):
            row = units.get(anchor.unit_id)
            if row is None or row["text"] != anchor.text:
                raise ValueError("Candidate pair anchor does not match saved assessment text")
            labels.append(row["factual"])
        rows.append(dict(id=pair.id, left=pair.left.model_dump(), right=pair.right.model_dump(),
                         left_factual=labels[0], right_factual=labels[1], disagreement=labels[0] != labels[1]))
    return dict(version="judge-consistency-v1", left_sha256=hashes[0], right_sha256=hashes[1],
        cases_sha256=hashlib.sha256(raw_cases).hexdigest(), prompt_sha256=caches[0]["prompt_sha256"],
        pairs=rows, compared_pairs=len(rows), factual_disagreements=sum(r["disagreement"] for r in rows),
        scope="declared candidate paraphrases; equivalence and correctness not verified; contexts may differ",
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
    print(f"Compared {result['compared_pairs']} pairs; factual disagreements: {result['factual_disagreements']}")


if __name__ == "__main__":
    main()
