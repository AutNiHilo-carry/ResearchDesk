"""Three-arm regression comparison; do not rank coverage across granularities."""
import json
import time
from .claims import POLICY_VERSION, split_clauses, validate_anchors
from .evaluation import benchmark_inputs, score_result
from .verification import CITATION, VerificationError, assess, digest, prepare, failure_diagnostics


def reference_inputs(dataset, granularity):
    parents, evidence, sources, gold = benchmark_inputs(dataset)
    if granularity == "line":
        return parents, evidence, sources, gold
    if granularity != "clause":
        raise VerificationError("Unsupported unit granularity.")
    units = split_clauses(parents)
    validate_anchors(units)
    by_parent = {case["id"]: case for case in dataset["cases"]}
    clause_gold = {}
    grouped = {}
    for unit in units:
        grouped.setdefault(unit["parent_id"], []).append(unit)
    for parent_id, children in grouped.items():
        case = by_parent[parent_id]
        expected = case.get("clauses")
        if not isinstance(expected, list) or len(children) != len(expected):
            raise VerificationError("Clause reference count differs from the partition policy.")
        parent = next(row for row in parents if row["unit_id"] == parent_id)
        for unit, reference in zip(children, expected):
            if CITATION.sub("", unit["text"]).strip() != reference["text"]:
                raise VerificationError("Clause reference text differs from the exact source span.")
            if len(reference["verdicts"]) != len(parent["citations"]):
                raise VerificationError("Clause reference label count is invalid.")
            parent_labels = dict(zip(parent["citations"], reference["verdicts"]))
            clause_gold[unit["unit_id"]] = dict(
                factual=reference["factual"],
                verdicts={eid: parent_labels[eid] for eid in unit["citations"]})
    return units, evidence, sources, clause_gold


def compare(dataset, judge_factory, *, max_units=60, max_chars=60000, on_arm=None):
    arms = []
    plan = []
    for name, granularity, prompt in [
        ("baseline_line_v1", "line", "v1"),
        ("prompt_only_line_v2", "line", "v2"),
        ("candidate_clause_v2", "clause", "v2"),
    ]:
        units, evidence, sources, gold = reference_inputs(dataset, granularity)
        payload, fingerprint = prepare(units, evidence, sources, max_units=max_units, max_chars=max_chars)
        plan.append((name, granularity, prompt, payload, fingerprint, gold))
    # Validate every arm's source anchors and budgets before spending the first model call.
    for name, granularity, prompt, payload, fingerprint, gold in plan:
        start = time.monotonic()
        judge = None
        try:
            judge = judge_factory(prompt)
            result = assess(payload, judge)
            score_result(result, payload, gold, dataset, fingerprint=fingerprint)
        except Exception as error:
            # An invalid arm has no accepted scores. Other fully valid arms are independent.
            result = dict(status="failed", metrics=None, benchmark=None, input_sha256=fingerprint,
                          **failure_diagnostics(error, judge))
        result.update(arm=name, granularity=granularity, requested_prompt=prompt,
                      judge_prompt_sha256=getattr(judge, "prompt_sha256", None),
                      unitization_version=POLICY_VERSION if granularity == "clause" else "line-v1",
                      expected_units=len(payload["units"]),
                      elapsed_seconds=round(time.monotonic()-start, 3), usage=getattr(judge, "usage", None))
        arms.append(result)
        if on_arm is not None:
            on_arm(result)
    complete = all(arm["status"] == "complete" for arm in arms)
    return dict(status="complete" if complete else "partial", action="compare", prompt_version="mixed_explicit_in_arms", dataset_sha256=digest(
                    json.dumps(dataset, sort_keys=True, ensure_ascii=False)),
                dataset_version=dataset["version"], label_status=dataset.get("label_status"),
                arms=arms, promotion="manual_review_required",
                experiment_note=("baseline vs prompt_only changes prompt only; prompt_only vs candidate changes "
                                 "unitization/citation-binding policy. Coverage denominators differ; "
                                 "do not interpret higher coverage as better report quality."))


def comparison_markdown(result):
    lines = ["# Claim Granularity Regression Comparison", "",
             "固定参考回归集，尚未独立人工复核；不自动切换默认策略。", "",
             "| Arm | Status | Units | Label accuracy | Factual accuracy | False support | Gate |",
             "|---|---|---:|---:|---:|---:|---|"]
    for arm in result["arms"]:
        score = arm["benchmark"]
        if score is None:
            lines.append(f"| {arm['arm']} | failed | {arm['expected_units']} | N/A | N/A | N/A | false |")
            continue
        lines.append(f"| {arm['arm']} | complete | {score['case_count']} | {score['semantic_link_accuracy']} "
                     f"| {score['factual_classification_accuracy']} | {score['false_support_count']} "
                     f"| {score['gate_passed']} |")
    lines += ["", result["experiment_note"], "", "## Disagreements", ""]
    for arm in result["arms"]:
        lines += [f"### {arm['arm']}", ""]
        if arm["status"] != "complete":
            lines += [f"Failed: {arm['error_type']} · scores not accepted",
                      json.dumps(arm.get("schema_errors", arm.get("validation_error")), ensure_ascii=False)]
            continue
        lines += [json.dumps(row, ensure_ascii=False) for row in arm["benchmark"]["disagreements"]]
        for row in arm["units"]:
            lines.append(f"- {row['unit_id']}: factual={row['factual']} · {row['text']}")
    return "\n".join(lines) + "\n"

