"""Reference-label benchmark for the citation verifier, not end-to-end task success."""
import json
from .verification import digest, prepare, assess, VerificationError


def benchmark_inputs(dataset):
    cases = dataset.get("cases")
    if dataset.get("version") not in {"evidence-v1", "evidence-v2"} or not isinstance(cases, list) or not cases:
        raise VerificationError("Unsupported or empty benchmark dataset.")
    if len({case["id"] for case in cases}) != len(cases):
        raise VerificationError("Duplicate benchmark case IDs.")
    sources, evidence, units, gold = {}, {}, [], {}
    for number, case in enumerate(cases, 1):
        quotes, ids = case["quotes"], []
        if len(quotes) != len(case["expected_verdicts"]):
            raise VerificationError("Reference label count differs from quote count.")
        for index, quote in enumerate(quotes):
            location = f"fixture://{case['id']}/{index}"
            content = case.get("source_texts", quotes)[index]
            source_hash = digest(location + "\0" + content)
            sid = "S" + source_hash[:16]
            sources[sid] = dict(id=sid, location=location, content=content, sha256=source_hash)
            original_claim = "Fixture " + case["id"] + str(index)
            eid = "E" + digest(original_claim + "\0" + sid + "\0" + quote)[:16]
            ids.append(eid)
            if not case.get("omit_evidence"):
                evidence[eid] = dict(id=eid, source_id=sid, claim=original_claim,
                                     snippet=quote, location=location)
        units.append(dict(unit_id=case["id"], line=number, text=case["claim"] +
                          "".join(f" [{eid}]" for eid in ids), citations=ids))
        gold[case["id"]] = dict(factual=case["expected_factual"],
                                verdicts=dict(zip(ids, case["expected_verdicts"])))
    return units, evidence, sources, gold


def evaluate(dataset, judge, minimum_accuracy=0.85):
    units, evidence, sources, gold = benchmark_inputs(dataset)
    payload, fingerprint = prepare(units, evidence, sources)
    result = assess(payload, judge)
    return score_result(result, payload, gold, dataset, minimum_accuracy, fingerprint)


def score_result(result, payload, gold, dataset, minimum_accuracy=0.85, fingerprint=None):
    integrity = {row["evidence_id"]: row["integrity"] for row in payload["evidence"]}
    correct, baseline_correct, total, false_support, factual_correct = 0, 0, 0, 0, 0
    disagreements = []
    factual_disagreements = []
    for row in result["units"]:
        expected = gold[row["unit_id"]]
        factual_correct += row["factual"] == expected["factual"]
        if row["factual"] != expected["factual"]:
            factual_disagreements.append(dict(case_id=row["unit_id"], expected=expected["factual"],
                                              actual=row["factual"]))
        for link in row["links"]:
            target = expected["verdicts"][link["evidence_id"]]
            baseline = "supported" if integrity[link["evidence_id"]] == "valid" else "invalid_evidence"
            total += 1
            correct += link["verdict"] == target
            baseline_correct += baseline == target
            false_support += link["verdict"] == "supported" and target != "supported"
            if link["verdict"] != target:
                disagreements.append(dict(case_id=row["unit_id"], evidence_id=link["evidence_id"],
                                          expected=target, actual=link["verdict"]))
    accuracy = correct/total if total else None
    result["benchmark"] = dict(
        dataset_version=dataset["version"], dataset_sha256=digest(json.dumps(dataset, sort_keys=True, ensure_ascii=False)),
        label_status=dataset.get("label_status", "unspecified"),
        case_count=len(gold), labeled_links=total, correct_links=correct,
        semantic_link_accuracy=accuracy,
        exact_match_baseline_link_accuracy=baseline_correct/total if total else None,
        factual_classification_accuracy=factual_correct/len(gold),
        false_support_count=false_support, minimum_accuracy=minimum_accuracy,
        gate_passed=(accuracy is not None and accuracy >= minimum_accuracy and false_support == 0
                     and factual_correct == len(gold)),
        disagreements=disagreements, factual_disagreements=factual_disagreements)
    result["input_sha256"] = fingerprint
    return result

