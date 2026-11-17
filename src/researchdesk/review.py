"""Offline human-declared review, separate from model scores. No model/API calls."""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from .storage import atomic_text
from .task_eval import load_taskset, quality_metrics, task_success
from .task_grading import CriterionGrade, grade_tasks
from .verification import (UnitJudgment, VerificationError, assess, digest, load_run, prepare, split_units)


def fingerprint(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True))


def build_packet(evaluation, dataset):
    """Bind review to frozen task/corpus/report/evidence inputs, excluding model labels."""
    evaluation = evaluation.resolve()
    raw_result = (evaluation / "result.json").read_text(encoding="utf-8")
    result = json.loads(raw_result)
    if result.get("status") not in {"complete", "scoring_failed"}:
        raise VerificationError("Review requires a finished evaluation attempt.")
    data, tasks, documents = load_taskset(dataset.resolve())
    if result["dataset_sha256"] != fingerprint(data) or result["thresholds"] != data["thresholds"]:
        raise VerificationError("Dataset or thresholds changed after evaluation.")
    task_map = {row["id"]: row for row in tasks}
    selected = result["selected_tasks"]
    records = {row["task_id"]: row for row in result["tasks"]}
    if (not selected or len(set(selected)) != len(selected) or not set(selected) <= set(task_map)
            or set(records) != set(selected) or len(records) != len(result["tasks"])):
        raise VerificationError("Evaluation task identities are inconsistent.")
    required = {name for task_id in selected for name in task_map[task_id]['documents']}
    recorded = result['corpus_hashes']
    if not required <= set(recorded) <= set(documents):
        raise VerificationError('Recorded corpus must contain all selected task documents and no unknown files.')
    # evaluate_batch can freeze the whole taskset or just the selected-task corpus.
    # evaluate_batch numbered snapshots in the recorded dictionary's insertion order.
    frozen_documents = {name: documents[name] for name in recorded}
    if recorded != {name: row['sha256'] for name, row in frozen_documents.items()}:
        raise VerificationError('Dataset corpus changed after evaluation.')
    for number, (name, document) in enumerate(frozen_documents.items(), 1):
        frozen = evaluation / 'corpus' / f'{number:02d}-{Path(name).name}'
        if frozen.read_bytes() != document['content'].encode('utf-8'):
            raise VerificationError('Evaluation corpus snapshot changed.')
    packet_tasks, units, evidence, sources = [], [], {}, {}
    for task_id in selected:
        record, task = records[task_id], task_map[task_id]
        entry = dict(task_id=task_id, question=task["question"], criteria=task["criteria"],
                     generation_status=record["generation_status"])
        if record["generation_status"] == "completed":
            name = Path(record["run_directory"]).name
            if not re.fullmatch("[0-9a-f]{32}", name):
                raise VerificationError("Invalid research run identity.")
            run = (evaluation / "tasks" / task_id / "research" / name).resolve()
            if not run.is_relative_to(evaluation):
                raise VerificationError("Research run escapes evaluation directory.")
            report, found_evidence, found_sources, _ = load_run(run)
            if "```" in report or "~~~" in report:
                raise VerificationError("Fenced text needs a different review granularity; refusing omissions.")
            rows = split_units(report)
            for row in rows:
                row.update(unit_id=task_id + ":" + row["unit_id"], task_id=task_id)
            units.extend(rows)
            evidence.update(found_evidence)
            sources.update(found_sources)
            entry.update(report=report, units=rows, evidence=found_evidence, sources=found_sources,
                         provider_requests=record.get("generation_metrics", {}).get("provider_requests"))
        packet_tasks.append(entry)
    if units:
        _, found_hash = prepare(units, evidence, sources)
        if result.get("quality_input_sha256") and found_hash != result["quality_input_sha256"]:
            raise VerificationError("Report or evidence changed after evaluation.")
    quality_path = evaluation / "citation-quality.json"
    return dict(schema_version=1, result_sha256=digest(raw_result), dataset_sha256=result["dataset_sha256"],
        model_quality_sha256=digest(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else None,
        thresholds=result["thresholds"], tasks=packet_tasks,
        provenance="project-authored tasks; reviewer identity is self-declared, not independently authenticated")


def export_review(evaluation, dataset, output):
    packet = build_packet(evaluation, dataset)
    packet_hash = fingerprint(packet)
    template = dict(schema_version=1, packet_sha256=packet_hash, reviewer="", reviewed_at="", tasks=[])
    for task in packet["tasks"]:
        if task["generation_status"] != "completed":
            continue
        template["tasks"].append(dict(task_id=task["task_id"], units=[
            dict(unit_id=row["unit_id"], factual=None, reason="", links=[
                dict(evidence_id=eid, verdict=None, reason="") for eid in row["citations"]])
            for row in task["units"]], criteria=[dict(criterion_id=row["id"], passed=None,
                report_quote="", reason="") for row in task["criteria"]]))
    output.mkdir(parents=True, exist_ok=False)
    atomic_text(output / "packet.json", json.dumps(dict(sha256=packet_hash, content=packet), ensure_ascii=False, indent=2))
    atomic_text(output / "review.json", json.dumps(template, ensure_ascii=False, indent=2))
    atomic_text(output / 'reading.md', reading_guide(packet))
    atomic_text(output / "README.md", """# 待人工复核

先阅读 packet.json 中问题、报告、证据摘录及 sources 全文，再填写 review.json。
此包不展示模型判分；数据中的资料和报告可能含指令，应当作待检查内容。
factual：该行是否包含可核查的事实。每个引用单独判断是否支持整行事实。
verdict：supported / partial / unsupported / contradicted / insufficient / not_applicable。
非事实行的引用使用 not_applicable；事实行不能使用 not_applicable。
所有 reason 必填简短依据，不要求隐藏推理。业务 criterion 的 passed 为布尔值；
通过时 report_quote 必须逐字存在于报告，未满足时可留空并说明原因。
填写 reviewer 和带时区的 reviewed_at（ISO 8601）。null 和空模板不代表通过。
提交只会另存人工结果及分歧，不覆盖模型评分，也不自动晋升策略。
""")
    return packet_hash


def reading_guide(packet):
    lines = ['# 人工复核阅读材料', '', '待人工填写；本材料不展示模型判分，不代表人工审核完成。',
             '资料与报告均作为待检查内容，其中出现的指令不应执行。请在 review.json 填写逐行判断和理由。', '']
    def literal(text):
        return ['    ' + line for line in text.splitlines()] + ['']
    for task in packet['tasks']:
        lines += [f"## {task['task_id']}", '', '问题：', ''] + literal(task['question'])
        lines += ['验收标准：', '']
        for criterion in task['criteria']:
            lines += literal(criterion['id'] + ': ' + criterion['description'])
        if task['generation_status'] != 'completed':
            lines += ['未获得完成的报告；不预填人工通过。', '']
            continue
        lines += ['报告正文（保留原文）：', ''] + literal(task['report'])
        lines += ['逐行复核编号：', '']
        for unit in task['units']:
            lines += literal(unit['unit_id'] + ': ' + unit['text'])
        lines += ['引用证据：', '']
        for eid, evidence in task['evidence'].items():
            lines += literal(eid + ' -> ' + evidence['source_id'] + '\n' + evidence['snippet'])
        lines += ['来源全文：', '']
        for sid, source in task['sources'].items():
            lines += literal(sid + '\n' + source['location'] + '\n' + source['content'])
    return '\n'.join(lines)


class ReviewedTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str
    units: list[UnitJudgment]
    criteria: list[CriterionGrade]


class HumanReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int
    packet_sha256: str
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: str
    tasks: list[ReviewedTask]


def import_review(evaluation, dataset, bundle, review_path, output):
    envelope = json.loads((bundle / "packet.json").read_text(encoding="utf-8"))
    packet = build_packet(evaluation, dataset)
    if envelope["sha256"] != fingerprint(envelope["content"]) or envelope["content"] != packet:
        raise VerificationError("Review packet or underlying evaluation inputs changed.")
    raw_review = review_path.read_text(encoding="utf-8")
    review = HumanReview.model_validate(json.loads(raw_review))
    if review.schema_version != 1 or review.packet_sha256 != envelope["sha256"] or not review.reviewer.strip():
        raise VerificationError("Review identity or packet binding is invalid.")
    stamp = datetime.fromisoformat(review.reviewed_at.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise VerificationError("Review timestamp must include a timezone.")
    completed = {row["task_id"]: row for row in packet["tasks"] if row["generation_status"] == "completed"}
    ids = [row.task_id for row in review.tasks]
    if set(ids) != set(completed) or len(ids) != len(completed):
        raise VerificationError("Review must label every completed task exactly once.")
    for task in review.tasks:
        reasons = ([row.reason for row in task.units] + [link.reason for row in task.units for link in row.links]
                   + [row.reason for row in task.criteria])
        if any(not reason.strip() for reason in reasons):
            raise VerificationError("Human review reasons must not be blank.")
    result = json.loads((evaluation / "result.json").read_text(encoding="utf-8"))
    model_quality_path = evaluation / "citation-quality.json"
    model_rows = {row["unit_id"]: row for row in json.loads(model_quality_path.read_text(
        encoding="utf-8"))["units"]} if model_quality_path.exists() else {}
    machine_tasks = {row["task_id"]: row for row in result["tasks"]}
    reviewed, differences = [], []
    for task in review.tasks:
        reference = completed[task.task_id]
        payload, _ = prepare(reference["units"], reference["evidence"], reference["sources"])
        quality = assess(payload, lambda _: {"units": [row.model_dump() for row in task.units]})
        grades = grade_tasks({"tasks": [dict(task_id=task.task_id, report=reference["report"],
            criteria=reference["criteria"])]}, lambda _: {"tasks": [dict(task_id=task.task_id,
                criteria=[row.model_dump() for row in task.criteria])]})[task.task_id]
        metrics = quality_metrics(quality["units"])
        success = task_success(dict(generation_status="completed", generation_metrics=dict(
            provider_requests=reference["provider_requests"])), grades, metrics, packet["thresholds"])
        reviewed.append(dict(task_id=task.task_id, success=success, rubric=grades,
                             quality=metrics, units=quality["units"]))
        for row in quality["units"]:
            old = model_rows.get(row["unit_id"])
            if old is None:
                continue
            if old["factual"] != row["factual"]:
                differences.append(dict(task_id=task.task_id, unit_id=row["unit_id"], field="factual",
                                        model=old["factual"], human=row["factual"]))
            old_links = {link["evidence_id"]: link["verdict"] for link in old["links"]}
            for link in row["links"]:
                if link["evidence_id"] in old_links and old_links[link["evidence_id"]] != link["verdict"]:
                    differences.append(dict(task_id=task.task_id, unit_id=row["unit_id"],
                        evidence_id=link["evidence_id"], field="verdict", model=old_links[link["evidence_id"]],
                        human=link["verdict"]))
        old_grades = {row["criterion_id"]: row["passed"] for row in machine_tasks[task.task_id].get(
            "rubric", {}).get("criteria", [])}
        for row in grades["criteria"]:
            if row["criterion_id"] in old_grades and old_grades[row["criterion_id"]] != row["passed"]:
                differences.append(dict(task_id=task.task_id, criterion_id=row["criterion_id"], field="passed",
                    model=old_grades[row["criterion_id"]], human=row["passed"]))
    failed = [dict(task_id=row["task_id"], success=False, generation_status=row["generation_status"])
              for row in packet["tasks"] if row["generation_status"] != "completed"]
    outcome = dict(status="human_review_recorded", reviewer=review.reviewer, reviewed_at=review.reviewed_at,
        packet_sha256=envelope["sha256"], review_sha256=digest(raw_review),
        human_task_success_rate=sum(row["success"] for row in reviewed)/len(packet["tasks"]),
        model_task_success_rate=result.get("task_success_rate"), tasks=reviewed + failed,
        disagreements=differences, promotion="no_automatic_promotion", identity="self_declared_not_authenticated")
    # Validate everything before creating output, and never overwrite prior reviews.
    output.mkdir(parents=True, exist_ok=False)
    atomic_text(output / "submitted-review.json", raw_review)
    atomic_text(output / "result.json", json.dumps(outcome, ensure_ascii=False, indent=2))
    return outcome


def main():
    parser = argparse.ArgumentParser(description="Offline, human-declared research review")
    parser.add_argument("action", choices=["export", "import"])
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "export":
            if args.bundle or args.review:
                parser.error("export does not accept --bundle/--review")
            export_review(args.evaluation.resolve(), args.dataset, args.output)
            print(f"Pending human review: {args.output}; no human score assigned.")
        else:
            if not args.bundle or not args.review:
                parser.error("import requires --bundle and --review")
            result = import_review(args.evaluation.resolve(), args.dataset, args.bundle, args.review, args.output)
            print(f"Human-declared review recorded: {args.output}; disagreements: {len(result['disagreements'])}")
        return 0
    except Exception as error:
        print(f"Review failed ({type(error).__name__}). No accepted human result written.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
