"""Exact source spans and declared condition/citation scope; integrity only."""
import argparse
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .inference import inputs
from .storage import atomic_text
from .verification import CITATION, VerificationError, digest, evidence_context


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    scope: Literal["declared_direct", "candidate_pool", "unresolved"]


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    parent_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)
    role: Literal["unclassified", "assertion", "condition", "context", "nonfactual"]
    condition_review: Literal["pending", "none_declared", "linked"]
    conditions: list[str] = Field(max_length=12)
    bindings: list[Binding] = Field(max_length=12)


class Trace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["scope-trace-v1"]
    input_sha256: str
    segments: list[Segment] = Field(min_length=1, max_length=240)


def create(directory):
    units, _, _, fingerprint = inputs(directory)
    return dict(version="scope-trace-v1", input_sha256=fingerprint, segments=[dict(
        id=f"S{i}", parent_id=u["unit_id"], start=0, end=len(u["text"]), text=u["text"],
        role="unclassified", condition_review="pending", conditions=[], bindings=[])
        for i, u in enumerate(units, 1)])


def check(directory, raw):
    trace = Trace.model_validate(raw)
    units, evidence, sources, fingerprint = inputs(directory)
    if trace.input_sha256 != fingerprint:
        raise VerificationError("Report or corpus changed after scope export")
    parents = {u["unit_id"]: u for u in units}
    segments = {s.id: s for s in trace.segments}
    if len(segments) != len(trace.segments) or any(not s.id.strip() for s in trace.segments):
        raise VerificationError("Scope segment IDs must be unique and nonblank")
    groups = {}
    for s in trace.segments:
        parent = parents.get(s.parent_id)
        if parent is None or not 0 <= s.start < s.end <= len(parent["text"]) or parent["text"][s.start:s.end] != s.text:
            raise VerificationError("Scope segment anchor differs from original text")
        groups.setdefault(s.parent_id, []).append(s)
    if set(groups) != set(parents):
        raise VerificationError("Scope trace omitted a report unit")
    for pid, rows in groups.items():
        cursor = 0
        for s in sorted(rows, key=lambda s: s.start):
            if s.start != cursor:
                raise VerificationError("Scope segments overlap or omit source characters")
            cursor = s.end
        if cursor != len(parents[pid]["text"]):
            raise VerificationError("Scope segments omit source tail")
    pending, used_conditions, assigned = [], set(), set()
    for s in trace.segments:
        if s.role == "unclassified":
            pending.append(dict(segment_id=s.id, reason="unclassified"))
        if s.role != "assertion" and (s.conditions or s.bindings):
            raise VerificationError("Only assertions declare condition and citation bindings")
        if s.role == "assertion":
            if s.condition_review == "pending":
                pending.append(dict(segment_id=s.id, reason="condition_scope_unreviewed"))
            if ((s.condition_review == "linked" and not s.conditions)
                    or (s.condition_review == "none_declared" and s.conditions)):
                raise VerificationError("Condition review contradicts declared links")
            if not s.bindings:
                pending.append(dict(segment_id=s.id, reason="no_citation_binding"))
        if len(set(s.conditions)) != len(s.conditions):
            raise VerificationError("Duplicate condition links")
        for cid in s.conditions:
            condition = segments.get(cid)
            if condition is None or condition.role != "condition" or condition.parent_id != s.parent_id:
                raise VerificationError("Condition must be an anchored condition in the same parent unit")
            used_conditions.add(cid)
        seen = set()
        for b in s.bindings:
            text = parents[s.parent_id]["text"]
            match = CITATION.fullmatch(text[b.start:b.end]) if 0 <= b.start < b.end <= len(text) else None
            if match is None or match[1] != b.evidence_id:
                raise VerificationError("Citation anchor differs from the original reference")
            if evidence_context(b.evidence_id, evidence, sources)["integrity"] != "valid":
                raise VerificationError("Citation evidence is invalid")
            key = (s.parent_id, b.start, b.end)
            if key in seen:
                raise VerificationError("Duplicate binding within an assertion")
            seen.add(key)
            assigned.add(key)
            if b.scope != "declared_direct":
                pending.append(dict(segment_id=s.id, reason="citation_scope_unresolved"))
    for s in trace.segments:
        if s.role == "condition" and s.id not in used_conditions:
            pending.append(dict(segment_id=s.id, reason="orphan_condition"))
    for parent in units:
        for m in CITATION.finditer(parent["text"]):
            if (parent["unit_id"], m.start(), m.end()) not in assigned:
                pending.append(dict(parent_id=parent["unit_id"], reason="unassigned_citation", start=m.start()))
    return dict(version="scope-trace-v1", status="draft" if pending else "structure_checked",
        input_sha256=fingerprint, trace_sha256=digest(json.dumps(trace.model_dump(), ensure_ascii=False, sort_keys=True)),
        pending=pending, semantic_verification="not_performed", human_review="pending", api_calls=0,
        scope="declared source anchors and links only; missing undeclared conditions cannot be detected")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "check"])
    parser.add_argument("run", type=Path)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.action == "check") != (args.trace is not None):
        parser.error("check requires --trace; export does not accept it")
    raw = create(args.run) if args.action == "export" else json.loads(args.trace.read_text(encoding="utf-8"))
    result = check(args.run, raw)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in [("trace", raw), ("integrity", result)]:
        atomic_text(args.output / (name + ".json"), json.dumps(value, ensure_ascii=False, indent=2))
    print(f"Scope trace: {result['status']}; semantic verification not performed")


if __name__ == "__main__":
    main()
