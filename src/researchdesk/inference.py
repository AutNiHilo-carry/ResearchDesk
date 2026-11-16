"""Offline claim -> declared premises -> evidence integrity; never semantic approval."""
import argparse
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .storage import atomic_text
from .verification import VerificationError, digest, evidence_context, load_run, split_units

VERSION = "inference-trace-v1"
Kind = Literal["unclassified", "direct_fact", "source_attribution", "synthesis",
               "recommendation", "abstention", "nonfactual"]


class Premise(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^P[1-9][0-9]{0,2}$")
    statement: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(max_length=12)


class ClaimTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    unit_id: str
    text: str
    kind: Kind
    premises: list[Premise] = Field(max_length=8)
    rationale: str = Field(max_length=2000)


class Trace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["inference-trace-v1"]
    input_sha256: str
    claims: list[ClaimTrace] = Field(min_length=1, max_length=60)


def inputs(directory):
    report, evidence, sources, _ = load_run(directory)
    draft = directory / "draft.md"
    if "```" in report or "~~~" in report or (draft.is_file() and draft.read_text(encoding="utf-8").strip() != report.strip()):
        raise VerificationError("Inference tracing refuses fenced or omitted report content.")
    units = split_units(report)
    if len(units) > 60:
        raise VerificationError("Inference trace supports at most 60 complete report units.")
    for unit in units:
        for eid in unit["citations"]:
            if evidence_context(eid, evidence, sources)["integrity"] != "valid":
                raise VerificationError("Original report has invalid evidence.")
    frozen = dict(report=report, evidence=evidence, sources=sources)
    fingerprint = digest(json.dumps(frozen, ensure_ascii=False, sort_keys=True))
    return units, evidence, sources, fingerprint


def create_trace(directory):
    units, _, _, fingerprint = inputs(directory)
    return dict(version=VERSION, input_sha256=fingerprint, claims=[dict(unit_id=u["unit_id"], text=u["text"],
        kind="unclassified", premises=[], rationale="") for u in units])


def check_trace(directory, raw):
    trace = Trace.model_validate(raw)
    units, evidence, sources, fingerprint = inputs(directory)
    if trace.input_sha256 != fingerprint:
        raise VerificationError("Report, corpus or evidence changed after trace export.")
    originals = {u["unit_id"]: u for u in units}
    ids = [c.unit_id for c in trace.claims]
    if len(set(ids)) != len(ids) or set(ids) != set(originals):
        raise VerificationError("Trace must retain every report unit exactly once.")
    pending, bindings = [], []
    for claim in trace.claims:
        original = originals[claim.unit_id]
        if claim.text != original["text"]:
            raise VerificationError("Trace conclusion no longer matches its report unit.")
        premise_ids = [p.id for p in claim.premises]
        if len(set(premise_ids)) != len(premise_ids):
            raise VerificationError("Duplicate premise IDs within a conclusion.")
        if claim.kind == "unclassified":
            pending.append(dict(unit_id=claim.unit_id, reason="unclassified"))
        if claim.kind != "unclassified" and not claim.rationale.strip():
            pending.append(dict(unit_id=claim.unit_id, reason="missing_rationale"))
        if claim.kind in {"direct_fact", "source_attribution", "synthesis", "abstention"} and not claim.premises:
            pending.append(dict(unit_id=claim.unit_id, reason="missing_premises"))
        for premise in claim.premises:
            if not premise.statement.strip():
                raise VerificationError("Premise statement must not be blank.")
            if len(set(premise.evidence_ids)) != len(premise.evidence_ids):
                raise VerificationError("Duplicate evidence within a premise.")
            if not premise.evidence_ids:
                pending.append(dict(unit_id=claim.unit_id, premise_id=premise.id, reason="missing_evidence"))
            for eid in premise.evidence_ids:
                if evidence_context(eid, evidence, sources)["integrity"] != "valid":
                    raise VerificationError("Premise references missing or invalid evidence.")
                bindings.append(dict(unit_id=claim.unit_id, premise_id=premise.id, evidence_id=eid,
                    source_id=evidence[eid]["source_id"], cited_on_original_line=eid in original["citations"]))
    return dict(version=VERSION, status="draft" if pending else "structure_checked", input_sha256=fingerprint,
        trace_sha256=digest(json.dumps(trace.model_dump(), ensure_ascii=False, sort_keys=True)),
        units=len(units), premise_count=sum(len(c.premises) for c in trace.claims),
        bindings=bindings, pending=pending, semantic_verification="not_performed", human_review="pending",
        assessment="integrity_only_not_evidence_support_or_inference_validity", promotion="no_automatic_promotion")


def main():
    parser = argparse.ArgumentParser(description="Offline inference trace export and integrity check; no semantic scores")
    parser.add_argument("action", choices=["export", "check"])
    parser.add_argument("run", type=Path)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.action == "check") != (args.trace is not None):
        parser.error("check requires --trace; export does not accept it")
    try:
        trace = create_trace(args.run) if args.action == "export" else json.loads(args.trace.read_text(encoding="utf-8"))
        result = check_trace(args.run, trace)
        args.output.mkdir(parents=True, exist_ok=False)
        atomic_text(args.output / "trace.json", json.dumps(trace, ensure_ascii=False, indent=2))
        atomic_text(args.output / "integrity.json", json.dumps(result, ensure_ascii=False, indent=2))
        print(f"Inference trace {result['status']}: {args.output}; semantic verification not performed; human review pending.")
        return 0
    except Exception as error:
        print(f"Inference trace rejected ({type(error).__name__}); no semantic score assigned.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
