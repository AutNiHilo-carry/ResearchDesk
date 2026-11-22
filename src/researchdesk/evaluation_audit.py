"""Read-only comparison diagnostics; never rerun graders or overwrite existing scores."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from .review import fingerprint
from .storage import atomic_text
from .verification import VerificationError, digest, load_run, prepare, split_units, covered_by_verdicts


CONTROLS_VERSION = "evaluation-controls-v2"


def _factuality_audit(task_rows, old_labels, new_labels, *, available, input_identical):
    """Mechanical counts on already bound labels; never a semantic judgment."""
    view = dict(status='unavailable', tasks=[], coverage_increase_task_ids=[],
        not_compared_task_ids=[r['task_id'] for r in task_rows if r['report_changed'] is not False],
        citation_input_identical=input_identical, original_scores_modified=False)
    if not available:
        return view
    def counts(rows):
        factual = [r for r in rows if r['factual']]
        covered = sum(covered_by_verdicts(True, {link['verdict'] for link in r['links']}) for r in factual)
        return dict(factual_units=len(factual), covered_units=covered,
                    evidence_coverage=covered / len(factual) if factual else None)
    changed = False
    for task in task_rows:
        if task['report_changed'] is not False:
            continue
        task_id = task['task_id']
        before = {uid: r for uid, r in old_labels.items() if r['task_id'] == task_id}
        after = {uid: r for uid, r in new_labels.items() if r['task_id'] == task_id}
        excluded = [dict(unit_id=uid, text=r['text'], citations=r['citations'])
                    for uid, r in before.items() if r['factual'] and not after[uid]['factual']]
        included = [dict(unit_id=uid, text=r['text'], citations=r['citations'])
                    for uid, r in after.items() if r['factual'] and not before[uid]['factual']]
        old_counts, new_counts = counts(before.values()), counts(after.values())
        old_ratio, new_ratio = old_counts['evidence_coverage'], new_counts['evidence_coverage']
        increased = bool(excluded and old_ratio is not None and new_ratio is not None
            and new_ratio > old_ratio and new_counts['factual_units'] < old_counts['factual_units']
            and new_counts['covered_units'] <= old_counts['covered_units'])
        if increased:
            view['coverage_increase_task_ids'].append(task_id)
        changed = changed or bool(excluded or included)
        view['tasks'].append(dict(task_id=task_id, before=old_counts, after=new_counts,
            excluded_units=excluded, included_units=included,
            coverage_increased_with_fewer_factual_units=increased))
    view['status'] = 'review_required' if changed else 'no_factuality_change' if view['tasks'] else 'no_unchanged_reports'
    return view


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
    if baseline.get('generation_contracts') != candidate.get('generation_contracts'):
        raise VerificationError('Generation contracts differ; refusing a controlled comparison label.')
    def records(result):
        found = {row["task_id"]: row for row in result["tasks"]}
        if len(found) != len(result["tasks"]) or set(found) != set(result["selected_tasks"]):
            raise VerificationError("Comparison task identities are inconsistent.")
        return found
    before, after = records(baseline), records(candidate)
    materials = [dict(units=[], evidence={}, sources={}) for _ in range(2)]
    def report_hash(directory, record, material):
        if record["generation_status"] != "completed":
            return None
        run_name = Path(record["run_directory"]).name
        if not re.fullmatch("[0-9a-f]{32}", run_name):
            raise VerificationError("Invalid comparison run identity.")
        run = (directory / "tasks" / record["task_id"] / "research" / run_name).resolve()
        if not run.is_relative_to(directory.resolve()):
            raise VerificationError("Comparison run escapes evaluation directory.")
        report, evidence, sources, _ = load_run(run)
        if "```" in report or "~~~" in report:
            raise VerificationError("Fenced reports cannot be audited with line-only scoring inputs.")
        units = split_units(report)
        for row in units:
            row.update(unit_id=record['task_id'] + ':' + row['unit_id'], task_id=record['task_id'])
        material['units'].extend(units)
        material['evidence'].update(evidence)
        material['sources'].update(sources)
        return digest(report)
    rows = []
    for task_id in baseline["selected_tasks"]:
        old, new = before[task_id], after[task_id]
        original_hash = report_hash(baseline_directory, old, materials[0])
        final_hash = report_hash(candidate_directory, new, materials[1])
        changed = original_hash != final_hash if original_hash and final_hash else None
        rows.append(dict(task_id=task_id, original_report_sha256=original_hash,
            final_report_sha256=final_hash, report_changed=changed, baseline_success=old.get("success"),
            candidate_success=new.get("success"), baseline_quality=old.get("quality"), candidate_quality=new.get("quality"),
            repair_selection=new.get("repair_selection")))
    def bound_input(result, material):
        declared = result.get('quality_input_sha256')
        if material['units']:
            _, actual = prepare(material['units'], material['evidence'], material['sources'])
            if declared is not None and declared != actual:
                raise VerificationError('Saved citation input differs from current reports and evidence.')
            return actual if declared is not None else None
        if declared is not None:
            raise VerificationError('Saved citation input has no completed report material.')
        return None
    old_input, new_input = [bound_input(result, material) for result, material in
                            zip((baseline, candidate), materials)]
    def labels(directory, material, input_hash):
        path = directory / "citation-quality.json"
        if not path.is_file():
            return {}, None, 'missing'
        raw = path.read_bytes()
        quality_hash = hashlib.sha256(raw).hexdigest()
        quality = json.loads(raw.decode('utf-8'))
        if quality.get('status') != 'complete':
            return {}, quality_hash, 'incomplete'
        if input_hash is None:
            raise VerificationError('Complete citation labels require a bound saved input.')
        units = quality['units']
        expected = {row['unit_id']: row for row in material['units']}
        rows = {row['unit_id']: row for row in units}
        if len(rows) != len(units) or set(rows) != set(expected):
            raise VerificationError('Citation label identities do not match current report units.')
        for unit_id, row in rows.items():
            if type(row.get('factual')) is not bool or any(row.get(key) != value for key, value in expected[unit_id].items()):
                raise VerificationError('Citation label anchors do not match current report units.')
            links = row['links']
            ids = [link['evidence_id'] for link in links]
            if len(set(ids)) != len(ids) or set(ids) != set(expected[unit_id]['citations']):
                raise VerificationError('Citation label references do not match current report units.')
            for link in links:
                verdict = link.get('verdict')
                if verdict not in {'supported', 'partial', 'unsupported', 'contradicted', 'insufficient', 'not_applicable', 'invalid_evidence'}:
                    raise VerificationError('Unknown citation verdict in saved labels.')
                if ((row['factual'] and verdict == 'not_applicable')
                        or (not row['factual'] and verdict not in {'not_applicable', 'invalid_evidence'})):
                    raise VerificationError('Saved factual classification and citation verdict disagree.')
        return rows, quality_hash, 'bound_anchors'
    old_labels, old_quality_hash, old_status = labels(baseline_directory, materials[0], old_input)
    new_labels, new_quality_hash, new_status = labels(candidate_directory, materials[1], new_input)
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
    input_identical = old_input == new_input if old_input and new_input else None
    factuality = _factuality_audit(rows, old_labels, new_labels,
        available=old_status == new_status == 'bound_anchors', input_identical=input_identical)
    control_values = {key: baseline[key] for key in controls}
    control_values['generation_contracts'] = baseline.get('generation_contracts')
    control_hash = fingerprint(dict(version=CONTROLS_VERSION, controls=control_values))
    return dict(status="comparison_audited", baseline_status=baseline.get("status"),
        candidate_status=candidate.get("status"), baseline_result_sha256=baseline_hash,
        candidate_result_sha256=candidate_hash, controls_version=CONTROLS_VERSION,
        controls=control_values, controls_sha256=control_hash,
        baseline_citation_quality_sha256=old_quality_hash, candidate_citation_quality_sha256=new_quality_hash,
        citation_label_status=dict(baseline=old_status, candidate=new_status),
        citation_label_comparison_status='compared' if old_status == new_status == 'bound_anchors' else 'unavailable',
        citation_input_binding=dict(baseline='verified' if old_input else 'not_recorded',
                                    candidate='verified' if new_input else 'not_recorded'),
        citation_label_validation='unit_anchors_and_reference_ids_not_semantic_regrading_or_metric_replay',
        citation_input_identical=input_identical, factuality_audit=factuality,
        unchanged_reports=sum(row["report_changed"] is False for row in rows),
        changed_reports=sum(row["report_changed"] is True for row in rows),
        unchanged_success_disagreements=[row["task_id"] for row in rows if row["report_changed"] is False
            and isinstance(row["baseline_success"], bool) and isinstance(row["candidate_success"], bool)
            and row["baseline_success"] != row["candidate_success"]],
        unchanged_report_citation_disagreements=sorted(differences, key=lambda row: (row["unit_id"], row.get("evidence_id", ""))),
        tasks=rows, interpretation="Unchanged-report score differences are grader disagreement, not evidence of repair effect; no automatic promotion.")


def factuality_gate(audit):
    """An opt-in consistency gate, never task acceptance or semantic correctness."""
    unknown, risks = [], []
    view = audit.get('factuality_audit')
    if not isinstance(view, dict):
        view = {}
    if audit.get('status') != 'comparison_audited':
        unknown.append('audit_incomplete')
    if any(audit.get(key) != 'complete' for key in ('baseline_status', 'candidate_status')):
        unknown.append('evaluation_incomplete')
    if audit.get('citation_label_comparison_status') != 'compared':
        unknown.append('labels_unavailable')
    if audit.get('citation_input_identical') is not True:
        unknown.append('full_citation_input_not_identical')
    rows = audit.get('tasks', [])
    if not rows or any(row.get('report_changed') is not False for row in rows):
        unknown.append('requires_complete_unchanged_reports')
    checked_rows = view.get('tasks', [])
    if (not checked_rows or len(checked_rows) != len(rows)
            or {r['task_id'] for r in checked_rows} != {r['task_id'] for r in rows}):
        unknown.append('factuality_coverage_incomplete')
    if view.get('status') not in {'no_factuality_change', 'review_required'}:
        unknown.append('factuality_check_unavailable')
    if view.get('status') == 'review_required' or any(r.get('excluded_units') or r.get('included_units') for r in checked_rows):
        risks.append('same_report_factuality_changed')
    status = 'review_required' if risks else 'unknown' if unknown else 'passed'
    return dict(version='factuality-comparison-gate-v1', gate='factuality', status=status,
        exit_code=0 if status == 'passed' else 3, reasons=risks + unknown,
        audit_content_sha256=fingerprint(audit),
        scope='same_report_factuality_consistency_not_citation_correctness_or_task_acceptance',
        human_review='pending', final_acceptance='pending', promotion='none')


def audit_summary(audit):
    if audit.get("status") != "comparison_audited":
        return "# 配对实验审计\n\n审计未完成，不能据此判断修订效果。\n"
    factuality = audit.get('factuality_audit', dict(status='unknown_legacy', tasks=[], coverage_increase_task_ids=[]))
    details = []
    for row in factuality['tasks']:
        before, after = row['before'], row['after']
        details.append(f"- {row['task_id']}：事实行 {before['factual_units']} → {after['factual_units']}；"
            f"覆盖行 {before['covered_units']} → {after['covered_units']}；"
            f"覆盖率 {before['evidence_coverage']} → {after['evidence_coverage']}；"
            f"排除行 {[u['unit_id'] for u in row['excluded_units']]}。")
    detail_text = '\n'.join(details)
    return ("# 配对实验审计\n\n"
        f"控制条件指纹版本：{audit.get('controls_version', 'legacy_unversioned')}。\n\n"
        f"正文未变化：{audit['unchanged_reports']}；正文变化：{audit['changed_reports']}。\n\n"
        f"引用评分完整输入相同：{audit['citation_input_identical']}。\n\n"
        f"引用标签比较状态：{audit.get('citation_label_comparison_status', 'unknown_legacy')}；不可比较时空分歧列表不代表一致。\n\n"
        f"未变正文的通过判定分歧：{audit['unchanged_success_disagreements']}。\n\n"
        f"未变正文的事实/引用标签分歧：{len(audit['unchanged_report_citation_disagreements'])} 项。\n\n"
        f"事实分母检查：{factuality['status']}；覆盖率升高伴随事实排除的任务：{factuality['coverage_increase_task_ids']}。\n\n"
        f"{detail_text}\n\n"
        "事实行被排除导致的更高覆盖率需要复核，不能据此认定正文改进；零事实行的覆盖率为未知。\n\n"
        "相同正文的评分差异不能归因于报告修订；需人工复核及重复实验。单次运行不能量化评分方差。\n")


def main():
    parser = argparse.ArgumentParser(description="Audit unchanged-report grading disagreement without model calls")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate", choices=["factuality"], help="Require identical-input factuality consistency; exit 3 for review or unknown")
    args = parser.parse_args()
    try:
        audit = compare_evaluations(args.baseline.resolve(), args.candidate.resolve())
        verdict = factuality_gate(audit) if args.gate == "factuality" else None
        args.output.mkdir(parents=True, exist_ok=False)
        atomic_text(args.output / "result.json", json.dumps(audit, ensure_ascii=False, indent=2))
        rendered = audit_summary(audit)
        if verdict is not None:
            atomic_text(args.output / "gate.json", json.dumps(verdict, ensure_ascii=False, indent=2))
            rendered += f"\nFactuality gate: {verdict['status']}; exit code: {verdict['exit_code']}. Not task acceptance.\n"
        atomic_text(args.output / "summary.md", rendered)
        print(f"Comparison audit: {args.output}; unchanged reports: {audit['unchanged_reports']}")
        return verdict["exit_code"] if verdict is not None else 0
    except Exception as error:
        print(f"Comparison audit failed ({type(error).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
