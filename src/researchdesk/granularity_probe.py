"""Freeze unscored line/clause inputs from one report and evidence snapshot."""
import argparse
import json
from pathlib import Path
from .claims import POLICY_VERSION, V2, V3, split_clauses, validate_anchors
from .verification import SYSTEM_PROMPT_V2, VerificationError, digest, load_run, prepare, split_units


def build(body, evidence, sources, *, clause_policy=POLICY_VERSION):
    if "```" in body or "~~~" in body:
        raise VerificationError("Fenced content cannot be omitted from the comparison")
    parents = split_units(body)
    clauses = split_clauses(parents, policy_version=clause_policy)
    validate_anchors(clauses, parents)
    arms = []
    for name, units in [("line", parents), ("clause", clauses)]:
        payload, fingerprint = prepare(units, evidence, sources)
        if any(row["integrity"] != "valid" for row in payload["evidence"]):
            raise VerificationError("Comparison requires valid evidence")
        arms.append(dict(granularity=name, input_sha256=fingerprint, payload=payload,
            units=len(units), citation_links=sum(len(u["citations"]) for u in units),
            uncited_units=sum(not u["citations"] for u in units)))
    if arms[0]["payload"]["evidence"] != arms[1]["payload"]["evidence"]:
        raise VerificationError("Unitization changed the citation evidence inventory")
    frozen = dict(body=body, evidence=evidence, sources=sources)
    return dict(version="granularity-probe-v1", frozen=frozen,
        frozen_sha256=digest(json.dumps(frozen, ensure_ascii=False, sort_keys=True)),
        prompt_sha256=digest(SYSTEM_PROMPT_V2), prompt=SYSTEM_PROMPT_V2,
        clause_policy=clause_policy, arms=arms,
        binding_counts={kind: sum(u["citation_binding"] == kind for u in clauses)
            for kind in ["original_line", "explicit_span_only", "parent_final_candidate_pool"]},
        status="prepared_unscored", api_calls=0, metrics=None, human_review="pending",
        note="Same report, corpus and v2 prompt; unitization AND citation binding change. Counts are not quality scores.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--body", required=True, type=Path, help="Report file within completed run directory")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--clause-policy", choices=[POLICY_VERSION, V2, V3], default=POLICY_VERSION)
    args = parser.parse_args()
    if args.output.exists() or args.output.suffix != ".json":
        parser.error("Output must be a new .json file")
    if not args.body.resolve().is_relative_to(args.directory.resolve()):
        parser.error("Body must belong to the supplied run directory")
    _, evidence, sources, _ = load_run(args.directory)
    body = args.body.read_text(encoding="utf-8")
    packet = build(body, evidence, sources, clause_policy=args.clause_policy)
    packet["body_file_sha256"] = digest(body)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(packet, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"status": packet["status"], "arms": [
        {k: arm[k] for k in ["granularity", "units", "citation_links", "uncited_units"]}
        for arm in packet["arms"]], "binding_counts": packet["binding_counts"]}))


if __name__ == "__main__":
    main()
