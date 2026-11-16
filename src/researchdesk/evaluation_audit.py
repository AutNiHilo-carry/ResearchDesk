"""Read-only comparison audit; never rerun graders or reinterpret existing scores."""
import argparse
import json
import re
from pathlib import Path
from .review import fingerprint
from .storage import atomic_text
from .verification import VerificationError, digest, load_run


def compare_evaluations(baseline_directory, candidate_directory):
    def read(directory):
        raw = (directory / "result.json").read_text(encoding="utf-8")
        return json.loads(raw), digest(raw)
    baseline, baseline_hash = read(baseline_directory)
    candidate, candidate_hash = read(candidate_directory)
    controls = ("dataset_sha256", "selected_tasks", "thresholds", "corpus_hashes", "generator_model",
                "judge_model", "model_settings", "task_prompt_suffix_sha256",
                "task_grader_prompt_sha256", "citation_judge_prompt_sha256")
    if any(baseline.get(key) is None or baseline[key] != candidate.get(key) for key in controls):
        raise VerificationError("Evaluation controls differ; refusing a controlled comparison label.")
    def records(result):
        found = {row["task_id"]: row for row in result["tasks"]}
        if len(found) != len(result["tasks"]) or set(found) != set(result["selected_tasks"]):
            raise VerificationError("Comparison task identities are inconsistent.")
        return found
    before, after = records(baseline), records(candidate)
    def report_hash(directory, record):
        if record["generation_status"] != "completed":
            return None
        run_name = Path(record["run_directory"]).name
        if not re.fullmatch("[0-9a-f]{32}", run_name):
            raise VerificationError("Invalid comparison run identity.")
        run = (directory / "tasks" / record["task_id"] / "research" / run_name).resolve()
        if not run.is_relative_to(directory.resolve()):
            raise VerificationError("Comparison run escapes evaluation directory.")
        return digest(load_run(run)[0])
    rows = []
    for task_id in baseline["selected_tasks"]:
        old, new = before[task_id], after[task_id]
        original_hash = report_hash(baseline_directory, old)
        final_hash = report_hash(candidate_directory, new)
        changed = original_hash != final_hash if original_hash and final_hash else None
        rows.append(dict(task_id=task_id, original_report_sha256=original_hash,
            final_report_sha256=final_hash, report_changed=changed, baseline_success=old.get("success"),
            candidate_success=new.get("success"), baseline_quality=old.get("quality"), candidate_quality=new.get("quality"),
            repair_selection=new.get("repair_selection")))
    def labels(directory):
        path = directory / "citation-quality.json"
        if not path.is_file():
            return {}
        quality = json.loads(path.read_text(encoding="utf-8"))
        return {row["unit_id"]: row for row in quality["units"]} if quality.get("status") == "complete" else {}
    old_labels, new_labels = labels(baseline_directory), labels(candidate_directory)
    differences = []
    unchanged_ids = {row["task_id"] for row in rows if row["report_changed"] is False}
    for unit_id in old_labels.keys() & new_labels.keys():
        old, new = old_labels[unit_id], new_labels[unit_id]
        if old["task_id"] not in unchanged_ids or old["text"] != new["text"]:
            continue
        if old["factual"] != new["factual"]:
            differences.append(dict(unit_id=unit_id, field="factual", before=old["factual"], after=new["factual"]))
        old_links = {link["evidence_id"]: link["verdict"] for link in old["links"]}
        for link in new["links"]:
            eid = link["evidence_id"]
            if eid in old_links and old_links[eid] != link["verdict"]:
                differences.append(dict(unit_id=unit_id, evidence_id=eid, field="verdict",
                                        before=old_links[eid], after=link["verdict"]))
    old_input, new_input = baseline.get("quality_input_sha256"), candidate.get("quality_input_sha256")
    return dict(status="comparison_audited", baseline_result_sha256=baseline_hash,
        candidate_result_sha256=candidate_hash, controls_sha256=fingerprint({key: baseline[key] for key in controls}),
        citation_input_identical=old_input == new_input if old_input and new_input else None,
        unchanged_reports=sum(row["report_changed"] is False for row in rows),
        changed_reports=sum(row["report_changed"] is True for row in rows),
        unchanged_success_disagreements=[row["task_id"] for row in rows if row["report_changed"] is False
            and isinstance(row["baseline_success"], bool) and isinstance(row["candidate_success"], bool)
            and row["baseline_success"] != row["candidate_success"]],
        unchanged_report_citation_disagreements=sorted(differences, key=lambda row: (row["unit_id"], row.get("evidence_id", ""))),
        tasks=rows, interpretation="Unchanged-report score differences are grader disagreement, not evidence of repair effect; no automatic promotion.")


def audit_summary(audit):
    if audit.get("status") != "comparison_audited":
        return "# 配对实验审计\n\n审计未完成，不能据此判断修订效果。\n"
    return ("# 配对实验审计\n\n"
        f"正文未变化：{audit['unchanged_reports']}；正文变化：{audit['changed_reports']}。\n\n"
        f"引用评分完整输入相同：{audit['citation_input_identical']}。\n\n"
        f"未变正文的通过判定分歧：{audit['unchanged_success_disagreements']}。\n\n"
        f"未变正文的事实/引用标签分歧：{len(audit['unchanged_report_citation_disagreements'])} 项。\n\n"
        "相同正文的评分差异不能归因于报告修订；需人工复核及重复实验。单次运行不能量化评分方差。\n")


def main():
    parser = argparse.ArgumentParser(description="Audit unchanged-report grading disagreement without model calls")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        audit = compare_evaluations(args.baseline.resolve(), args.candidate.resolve())
        args.output.mkdir(parents=True, exist_ok=False)
        atomic_text(args.output / "result.json", json.dumps(audit, ensure_ascii=False, indent=2))
        atomic_text(args.output / "summary.md", audit_summary(audit))
        print(f"Comparison audit: {args.output}; unchanged reports: {audit['unchanged_reports']}")
        return 0
    except Exception as error:
        print(f"Comparison audit failed ({type(error).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
