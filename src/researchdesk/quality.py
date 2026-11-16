"""Explicit quality checks. Never rewrites a generated research report."""
import argparse
import json
import os
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from .ledger import now
from .storage import RunLock, atomic_text
from .verification import (PROMPT_VERSION, ModelJudge, VerificationError, assess,
                           digest, load_run, markdown, prepare, split_units)
from .verification import failure_diagnostics
from .evaluation import evaluate
from .claims import POLICY_VERSION, V2, V3


def main():
    parser = argparse.ArgumentParser(description="ResearchDesk model-assisted evidence quality")
    actions = parser.add_subparsers(dest="action", required=True)
    verify = actions.add_parser("verify")
    verify.add_argument("run_id")
    benchmark = actions.add_parser("benchmark")
    benchmark.add_argument("--dataset", type=Path, default=Path("evals/evidence-v1.json"))
    benchmark.add_argument("--minimum-accuracy", type=float, default=0.85)
    comparison = actions.add_parser("compare")
    comparison.add_argument("--dataset", type=Path, default=Path("evals/claims-v2.json"))
    verify.add_argument("--granularity", choices=["line", "clause"], default="line")
    verify.add_argument("--clause-policy", choices=[POLICY_VERSION, V2, V3])
    for command in (verify, benchmark):
        command.add_argument("--judge-prompt", choices=["v1", "v2"], default="v1")
    for command in (verify, benchmark, comparison):
        command.add_argument("--output", type=Path, default=Path("runs"))
        command.add_argument("--judge-model")
        command.add_argument("--max-units", type=int, default=60)
        command.add_argument("--max-input-chars", type=int, default=60000)
    args = parser.parse_args()
    if not 1 <= args.max_units <= 60 or not 1 <= args.max_input_chars <= 120000:
        parser.error("max-units must be 1..60 and max-input-chars 1..120000")
    if args.action == "verify" and not re.fullmatch("[0-9a-f]{32}", args.run_id):
        parser.error("run_id must be 32 hex characters, not a path")
    if args.action == "benchmark" and not 0 <= args.minimum_accuracy <= 1:
        parser.error("minimum-accuracy must be in [0,1]")
    if args.action == "verify" and args.clause_policy is not None:
        if args.granularity != "clause":
            parser.error("--clause-policy requires --granularity clause")
        if args.clause_policy != POLICY_VERSION and args.judge_prompt != "v2":
            parser.error("New clause policies require --judge-prompt v2")
    load_dotenv()
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        print("Missing DEEPSEEK_API_KEY.", file=sys.stderr)
        return 2
    model_name = args.judge_model or os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    check_id = uuid4().hex
    result = dict(status="failed", action=args.action, prompt_version=PROMPT_VERSION,
                  judge_model=model_name, started_at=now(), assessment="human_review_required",
                  judge_settings=dict(temperature=0, max_tokens=8192, max_retries=0,
                                      timeout=90, response_format="json_object", thinking="disabled"),
                  limits=dict(max_units=args.max_units, max_input_chars=args.max_input_chars))
    artifact = None
    judge = None
    created_judges = []
    requested_prompt = getattr(args, "judge_prompt", "v1")
    result["requested_prompt"] = requested_prompt if args.action != "compare" else "three_arm_comparison"
    result["limits"]["max_model_calls"] = 3 if args.action == "compare" else 1
    def make_judge(prompt):
        candidate = ModelJudge(model_name) if prompt == "v1" else ModelJudge(model_name, prompt_version=prompt)
        created_judges.append(candidate)
        return candidate
    try:
        with ExitStack() as stack:
            if args.action == "verify":
                directory = args.output / args.run_id
                if not (directory / "run.json").is_file():
                    raise VerificationError("Run does not exist.")
                stack.enter_context(RunLock(directory))
                report, evidence, sources, original = load_run(directory)
                units = split_units(report)
                if args.granularity == "clause":
                    from .claims import split_clauses, validate_anchors
                    effective_policy = args.clause_policy or POLICY_VERSION
                    units = split_clauses(units, policy_version=effective_policy)
                    validate_anchors(units)
                    result["unitization_version"] = effective_policy
                result["granularity"] = args.granularity
                payload, input_hash = prepare(units, evidence, sources,
                    max_units=args.max_units, max_chars=args.max_input_chars)
                artifact = directory / "verification" / check_id
                artifact.mkdir(parents=True, exist_ok=False)
                result.update(original_run_id=args.run_id, original_mode=original.get("mode"),
                              report_sha256=digest(report), input_sha256=input_hash)
                # Save exact versioned input before the single model request.
                atomic_text(artifact / "input.json", json.dumps(payload, ensure_ascii=False, indent=2))
                judge = make_judge(requested_prompt)
                result.update(assess(payload, judge))
                metrics = result["metrics"]
                gaps = (not metrics["factual_units"] or metrics["invalid_links"] or metrics["conflicted_units"]
                        or metrics["covered_units"] < metrics["factual_units"])
                result["assessment"] = "support_gaps_detected" if gaps else "no_support_gap_detected"
            elif args.action == "benchmark":
                artifact = args.output / "evaluations" / check_id
                artifact.mkdir(parents=True, exist_ok=False)
                dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
                # Enforce the same limits before the single judge call.
                from .evaluation import benchmark_inputs
                units, evidence, sources, _ = benchmark_inputs(dataset)
                prepare(units, evidence, sources, max_units=args.max_units, max_chars=args.max_input_chars)
                judge = make_judge(requested_prompt)
                result.update(evaluate(dataset, judge, args.minimum_accuracy))
                result["assessment"] = "benchmark_gate_passed" if result["benchmark"]["gate_passed"] else "benchmark_gate_failed"
            else:
                from .comparison import compare
                artifact = args.output / "comparisons" / check_id
                artifact.mkdir(parents=True, exist_ok=False)
                dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
                result.update(compare(dataset, make_judge, max_units=args.max_units,
                                      max_chars=args.max_input_chars,
                                      on_arm=lambda arm: atomic_text(artifact / (arm["arm"] + ".json"),
                                          json.dumps(arm, ensure_ascii=False, indent=2))))
                result["assessment"] = ("comparison_completed_manual_review_required" if result["status"] == "complete"
                                        else "comparison_incomplete_no_promotion")
            result["usage"] = [candidate.usage for candidate in created_judges] if args.action == "compare" else judge.usage
            result["finished_at"] = now()
            atomic_text(artifact / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
            summary = markdown(result)
            if args.action == "verify" and args.granularity == "clause":
                summary += ("\n## Evaluation policy\n\n"
                            f"Unitization: {result['unitization_version']}\n\n"
                            f"Judge prompt: {requested_prompt}\n\n"
                            "Counts and coverage are not directly comparable across unitization policies.\n")
            if args.action == "compare":
                from .comparison import comparison_markdown
                summary = comparison_markdown(result)
            if "benchmark" in result:
                summary += "\n## Benchmark\n\n" + json.dumps(result["benchmark"], ensure_ascii=False, indent=2) + "\n"
            atomic_text(artifact / "summary.md", summary)
            print(f"Quality result: {artifact / 'summary.md'}")
            print("Metrics are model estimates; human review is required.")
            if args.action == "compare":
                print(json.dumps([{key: arm[key] for key in ("arm", "status", "granularity", "benchmark")}
                                  for arm in result["arms"]], ensure_ascii=False))
                return 0 if (result["status"] == "complete" and result["arms"][-1]["benchmark"]["gate_passed"]) else 3
            if "benchmark" in result:
                print(json.dumps(result["benchmark"], ensure_ascii=False))
                return 0 if result["benchmark"]["gate_passed"] else 3
            print(json.dumps(result["metrics"], ensure_ascii=False))
            return 0
    except Exception as error:
        result.update(status="failed", error_type=type(error).__name__, finished_at=now(), metrics=None)
        result.update(failure_diagnostics(error, judge))
        if args.action == "compare":
            result["usage"] = [candidate.usage for candidate in created_judges]
        elif judge is not None:
            result["usage"] = judge.usage
        if artifact is not None:
            atomic_text(artifact / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
            atomic_text(artifact / "summary.md", markdown(result))
        # Never print provider bodies or arbitrary exception text.
        print(f"Quality check failed ({type(error).__name__}); no scores accepted.", file=sys.stderr)
        if isinstance(error, VerificationError):
            print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

