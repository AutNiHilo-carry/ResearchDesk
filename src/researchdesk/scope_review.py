"""Blind scope-reference form and human-declared import; no automatic gold labels."""
import argparse
import json
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from .scope_check import Review, assess, fingerprint, prompt_text
from .storage import atomic_text
from .verification import VerificationError, digest


class Form(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    packet_sha256: str
    reviewer: str = Field(min_length=1, max_length=100)
    reviewed_at: str
    confirmed: bool
    judgment: Review


def packet(plan_path):
    raw = Path(plan_path).read_text(encoding="utf-8")
    plan = json.loads(raw)
    payload = plan["payload"]
    version = plan["judge_prompt_version"]
    if plan["payload_sha256"] != fingerprint(payload) or plan["prompt_sha256"] != digest(prompt_text(version)):
        raise VerificationError("Reference source plan binding changed")
    # Deliberately exclude model outputs AND the author's expected labels in plan.cases.
    return dict(version="scope-reference-packet-v1", payload=payload,
        payload_sha256=fingerprint(payload), source_plan_sha256=digest(raw),
        judge_prompt_version=version, prompt_sha256=plan["prompt_sha256"],
        rubric_version="scope-reference-rubric-v1", human_review="pending")


def template(data):
    payload = data["payload"]
    return dict(packet_sha256=fingerprint(data), reviewer=None, reviewed_at=None, confirmed=False,
        judgment=dict(parents=[dict(parent_id=p["unit_id"], declarations=None, reason="") for p in payload["parents"]],
        assertions=[dict(segment_id=s["id"], conditions=None, support=None,
            anchors=[dict(evidence_id=eid, quote="") for eid in sorted({b["evidence_id"] for b in s["bindings"]})],
            reason="") for s in payload["segments"] if s["role"] == "assertion"]))


def import_review(data, raw):
    form = Form.model_validate(raw)
    if form.packet_sha256 != fingerprint(data):
        raise VerificationError("Review packet changed")
    if not form.confirmed or not form.reviewer.strip():
        raise VerificationError("Reviewer identity and explicit confirmation are required")
    try:
        timestamp = datetime.fromisoformat(form.reviewed_at)
    except ValueError as error:
        raise VerificationError("Review requires an ISO timestamp") from error
    if timestamp.tzinfo is None:
        raise VerificationError("Review timestamp requires a timezone")
    # Reuse strict IDs, exact anchors and prompt/input binding validation, discard model assessments.
    assess(data["payload"], dict(payload_sha256=data["payload_sha256"],
        prompt_sha256=data["prompt_sha256"], judgment=form.judgment.model_dump()),
        prompt_version=data["judge_prompt_version"])
    return dict(version="scope-reference-import-v1", packet_sha256=fingerprint(data),
        reviewer=form.reviewer, reviewed_at=form.reviewed_at, references=form.judgment.model_dump(),
        label_status="human_declared_not_independently_verified", identity_verified=False,
        affects_original_scores=False, promotion="not_performed", api_calls=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "import"])
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if (args.action == "import") != (args.review is not None):
        parser.error("import requires --review; export does not accept it")
    data = packet(args.plan)
    artifacts = {"packet.json": data, "review-template.json": template(data)}
    if args.action == "import":
        artifacts = {"review-result.json": import_review(data, json.loads(args.review.read_text(encoding="utf-8")))}
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in artifacts.items():
        atomic_text(args.output / name, json.dumps(value, ensure_ascii=False, indent=2))
    print(f"Scope reference {args.action}: {args.output}; no API call or score promotion")


if __name__ == "__main__":
    main()
