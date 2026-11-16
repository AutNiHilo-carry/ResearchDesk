"""Opt-in offline composition of cached semantic checks and receipt provenance."""
import argparse
import json
from pathlib import Path
from .source_audit import audit
from .workflow import load_quality
from .repair import selection
from .verification import VerificationError, digest, load_run, prepare, split_units

VERSION = "verification-pipeline-v1"
ORIGINS = ("extracted_body", "search_snippet")


def verify(directory, quality_path, *, require_origin):
    if require_origin not in ORIGINS:
        raise VerificationError("Choose an explicit supported origin requirement")
    directory, quality_path = Path(directory), Path(quality_path)
    body, evidence, sources, _ = load_run(directory)
    _, fingerprint = prepare(split_units(body), evidence, sources)
    cached_hash = digest(quality_path.read_text(encoding="utf-8"))
    # Validates input fingerprint, v2 prompt hash and labels; recomputes original metrics.
    # No model is constructed or invoked, and source metadata never changes these labels.
    semantic = load_quality(quality_path, body, evidence, sources)
    provenance = audit(directory)
    if provenance["body_sha256"] != digest(body):
        raise VerificationError("Source audit and semantic check refer to different report bodies")
    checks = []
    for row in provenance["citations"]:
        observed = row["observed_origin"]
        status = ("invalid" if row["integrity"] != "valid" else
                  "inconclusive" if observed in {"unknown", "both"} else
                  "matched" if observed == require_origin else "mismatch")
        checks.append(dict(**row, required_origin=require_origin, status=status))
    provenance_passed = bool(checks) and all(row["status"] == "matched" for row in checks)
    semantic_gate = selection(semantic, semantic)
    # Refuse concurrent edits to the body/evidence/cache; stopped runs remain required.
    again, ev, src, _ = load_run(directory)
    if (prepare(split_units(again), ev, src)[1] != fingerprint
            or digest(quality_path.read_text(encoding="utf-8")) != cached_hash):
        raise VerificationError("Verification inputs changed during the offline check")
    return dict(version=VERSION, input_sha256=fingerprint, body_sha256=digest(body),
        quality_file_sha256=cached_hash, required_origin=require_origin,
        semantic=semantic, semantic_gate=semantic_gate,
        provenance=dict(version=provenance["version"], passed=provenance_passed, citations=checks),
        status="checks_passed_pending_human" if semantic_gate["accepted"] and provenance_passed else "needs_review",
        human_review="pending", api_calls=0,
        scope="cached v2 quote assessment plus snapshot origin requirement; not chapter verification or task success")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--quality", required=True, type=Path)
    parser.add_argument("--require-origin", required=True, choices=ORIGINS)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.suffix != ".json" or args.output.exists():
        parser.error("Output must be a new .json file; existing artifacts cannot be overwritten")
    result = verify(args.directory, args.quality, require_origin=args.require_origin)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(f"Offline verification: {result['status']}; {args.output}")
    return 0 if result["status"] == "checks_passed_pending_human" else 3


if __name__ == "__main__":
    raise SystemExit(main())
