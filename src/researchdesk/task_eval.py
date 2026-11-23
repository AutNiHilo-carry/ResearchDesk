"""Controlled-corpus, end-to-end task evaluation. No automatic promotion."""
import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field
from .ledger import now
from .storage import atomic_text
from .verification import (ModelJudge, VerificationError, assess, replay_assessment, digest, failure_diagnostics,
                           load_run, prepare, split_units, markdown as quality_markdown)
from .task_grading import TaskJudge, grade_tasks
from .run_metrics import run_metrics
from .report_constraints import check_body, policy as constraint_policy, counting_instruction

TASK_SUFFIX = ("\n仅使用附带的本地资料，不进行外部检索。正文控制在200字以内。"
               "每个事实单独一行并紧跟完整证据引用，纯建议和证据不足声明单独成行。"
               "至少记录并引用一条资料中可确认的事实。")


def task_suffix(task):
    version = task.get('generation_contract', 'legacy')
    if not isinstance(version, str) or version not in {'legacy', 'body-length-v1'}:
        raise VerificationError('Unknown task generation contract.')
    return TASK_SUFFIX if version == 'legacy' else TASK_SUFFIX + '\n' + counting_instruction()


def generation_contract_spec(task):
    suffix = task_suffix(task)
    spec = dict(version=task.get('generation_contract', 'legacy'), suffix_sha256=digest(suffix))
    if spec['version'] != 'legacy':
        spec.update(constraint_policy=constraint_policy(), instruction=counting_instruction())
    return spec


def common_suffix_hash(tasks):
    hashes = {digest(task_suffix(task)) for task in tasks}
    return next(iter(hashes)) if len(hashes) == 1 else None


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    description: str = Field(min_length=1, max_length=1000)


class ResearchTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    question: str = Field(min_length=1, max_length=4000)
    documents: list[str] = Field(min_length=1, max_length=4)
    criteria: list[Criterion] = Field(min_length=1, max_length=8)


def load_taskset(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != "research-tasks-v1":
        raise VerificationError("Unsupported research taskset.")
    tasks = [ResearchTask.model_validate(row).model_dump() for row in data["tasks"]]
    if not 1 <= len(tasks) <= 10 or len({row["id"] for row in tasks}) != len(tasks):
        raise VerificationError("Taskset has invalid task count or duplicate IDs.")
    documents = {}
    for task in tasks:
        if len({row["id"] for row in task["criteria"]}) != len(task["criteria"]):
            raise VerificationError("Duplicate criterion IDs.")
        for name in task["documents"]:
            location = (path.parent / name).resolve()
            if not location.is_relative_to(path.parent.resolve()) or location.suffix.lower() not in {".txt", ".md"}:
                raise VerificationError("Task document must stay inside the dataset directory.")
            raw = location.read_bytes()
            if len(raw) > 200000:
                raise VerificationError("Task document exceeds 200 KB.")
            documents[name] = dict(content=raw.decode("utf-8"), sha256=hashlib.sha256(raw).hexdigest())
    thresholds = data.get("thresholds", {})
    if set(thresholds) != {"citation_precision", "evidence_coverage"} or not all(
            isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
            for value in thresholds.values()):
        raise VerificationError("Invalid task quality thresholds.")
    return data, tasks, documents


def stop_owned(process):
    """Stop only a subprocess/tree created by this evaluator."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                 capture_output=True, timeout=10)
        if stopped.returncode != 0 and process.poll() is None:
            process.kill()
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def run_command(command, directory, log_path, timeout, environment=None):
    started = time.monotonic()
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                   env=environment, **options)
        timed_out = False
        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_owned(process)
        finally:
            if process.poll() is None:
                stop_owned(process)
    return dict(returncode=process.returncode, timed_out=timed_out,
                elapsed_seconds=round(time.monotonic()-started, 3))


def run_task(task, paths, directory, timeout):
    question = task["question"] + task_suffix(task)  # Rubric/expected criteria never go to generator.
    directory.mkdir(parents=True, exist_ok=False)
    research = directory / "research"
    command = [sys.executable, "-m", "researchdesk.cli", question, "--local-only",
               "--output", str(research), "--max-tool-calls", "10", "--max-provider-requests", "0",
               "--tool-retries", "0"]
    if task.get("context_strategy") is not None:
        command.extend(["--context-mode", task["context_strategy"]])
    if task.get("report_policy") is not None:
        command.extend(["--report-policy", task["report_policy"]])
    if task.get("max_model_calls") is not None:
        command.extend(["--max-model-calls", str(task["max_model_calls"])])
    for path in paths:
        command.extend(["--document", str(path)])
    environment = os.environ.copy()
    environment["DEEPSEEK_MODEL"] = task.get("runtime_model", os.getenv("DEEPSEEK_MODEL", "deepseek-flash"))
    outcome = run_command(command, Path.cwd(), directory / "generation.log", timeout, environment)
    runs = list(research.glob("[0-9a-f]" * 32))
    run = runs[0] if len(runs) == 1 else None
    status = "timed_out" if outcome["timed_out"] else "generation_failed"
    record = dict(task_id=task["id"], question_sha256=digest(question), **outcome)
    if task.get('generation_contract', 'legacy') != 'legacy':
        record['generation_contract'] = generation_contract_spec(task)
    if run is not None:
        record["run_directory"] = str(run)
        record["generation_metrics"] = run_metrics(run)
        if (run / "run.json").is_file():
            manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
            if outcome["returncode"] == 0 and manifest.get("status") == "completed":
                status = "completed"
            record["model"] = manifest.get("model")
    record["generation_status"] = status
    return record


def quality_metrics(rows):
    factual = [row for row in rows if row["factual"]]
    links = [link for row in factual for link in row["links"]]
    return dict(factual_units=len(factual), citation_links=len(links),
                invalid_links=sum(link["verdict"] == "invalid_evidence" for row in rows for link in row["links"]),
                citation_precision=sum(link["verdict"] == "supported" for link in links)/len(links) if links else None,
                evidence_coverage=sum(row["covered"] for row in factual)/len(factual) if factual else None)


def task_success(record, rubric, metrics, thresholds):
    return (record["generation_status"] == "completed" and rubric["rubric_passed"]
            and metrics["invalid_links"] == 0
            and metrics["citation_precision"] is not None and metrics["evidence_coverage"] is not None
            and metrics["citation_precision"] >= thresholds["citation_precision"]
            and metrics["evidence_coverage"] >= thresholds["evidence_coverage"]
            and record.get("generation_metrics", {}).get("provider_requests") == 0)


def assessment(record, batch_status):
    """Describe model scoring only; this evaluator cannot perform human acceptance."""
    scoring, gate = "pending", "unknown"
    if record.get("generation_status") != "completed":
        scoring = "not_scored"
    elif batch_status == "scoring_failed":
        scoring = "invalid"
    elif batch_status == "complete":
        if (type(record.get("success")) is bool and isinstance(record.get("rubric"), dict)
                and isinstance(record.get("quality"), dict)):
            scoring, gate = "valid", "passed" if record["success"] else "failed"
        else:
            scoring = "invalid"
    elif batch_status != "running":
        scoring = "invalid"
    return dict(scoring_status=scoring, model_gate=gate, human_review="pending", final_acceptance="pending")


def application_assessment(record, batch_status):
    """A separate application check; never relabel historical model scores."""
    model = assessment(record, batch_status)['model_gate']
    check = record.get('constraints')
    if check is None:
        constraint = 'not_checked'
    elif (not isinstance(check, dict) or check.get('policy') != constraint_policy()
          or type(check.get('counted_chars')) is not int or check['counted_chars'] < 0
          or not isinstance(check.get('body_sha256'), str)
          or not re.fullmatch('[0-9a-f]{64}', check['body_sha256'])
          or check.get('status') not in {'passed', 'failed', 'invalid'}
          or check.get('passed') is not (check.get('status') == 'passed')
          or (check['status'] == 'passed' and not 0 < check['counted_chars'] <= 200)
          or (check['status'] == 'failed' and check['counted_chars'] <= 200)):
        constraint = 'invalid'
    else:
        constraint = check['status']
    if record.get('generation_status') != 'completed' or model == 'failed' or constraint in {'failed', 'invalid'}:
        gate = 'failed'
    else:
        gate = 'passed' if model == constraint == 'passed' else 'unknown'
    return dict(constraint_gate=constraint, application_gate=gate,
                human_review='pending', final_acceptance='pending')


def evaluate_batch(data, tasks, documents, artifact, *, runner=run_task, model_name="deepseek-flash",
                   task_judge=None, evidence_judge=None, timeout=180):
    evaluation_started = time.monotonic()
    contracts = {task['id']: generation_contract_spec(task) for task in tasks}
    estimated_rubric = []
    for task in tasks:
        texts = [documents[name]["content"] for name in task["documents"]]
        if len(task["question"] + task_suffix(task) + "".join(texts)) > 20000:
            raise VerificationError("Controlled task input exceeds generation character budget.")
        estimated_rubric.append(dict(question=task["question"], criteria=task["criteria"],
                                      documents=texts, report="x" * 4000))
    if len(json.dumps(estimated_rubric, ensure_ascii=False)) > 60000:
        raise VerificationError("Task batch exceeds preflight grader character budget.")
    artifact.mkdir(parents=True, exist_ok=False)
    corpus = artifact / "corpus"
    corpus.mkdir()
    paths = {}
    for number, (name, document) in enumerate(documents.items(), 1):
        path = corpus / f"{number:02d}-{Path(name).name}"
        atomic_text(path, document["content"])
        paths[name] = path.resolve()
    result = dict(status="running", dataset_version=data["version"], label_status=data.get("label_status"),
                  dataset_sha256=digest(json.dumps(data, sort_keys=True, ensure_ascii=False)),
                  selected_tasks=[task["id"] for task in tasks], scope=data.get("scope"),
                  started_at=now(), generator_model=model_name, judge_model=model_name,
                  model_settings=dict(temperature=0, generator_max_tokens=4096, evaluator_max_tokens=8192),
                  thresholds=data["thresholds"], corpus_hashes={name: row["sha256"] for name, row in documents.items()},
                  task_prompt_suffix_sha256=common_suffix_hash(tasks), task_timeout_seconds=timeout,
                  generation_contracts=contracts,
                  constraint_policy=constraint_policy(),
                  tasks=[], promotion="human_review_required", human_review="pending", final_acceptance="pending")
    def save():
        for row in result["tasks"]:
            row["assessment"] = assessment(row, result["status"])
            row['application_assessment'] = application_assessment(row, result['status'])
        atomic_text(artifact / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
    save()
    for index, task in enumerate(tasks, 1):
        print(f"Research task {index}/{len(tasks)}: {task['id']}", flush=True)
        try:
            record = runner(dict(task, runtime_model=model_name), [paths[name] for name in task["documents"]],
                            artifact / "tasks" / task["id"], timeout)
        except Exception as error:
            record = dict(task_id=task["id"], generation_status="execution_error", error_type=type(error).__name__)
        result["tasks"].append(record)
        save()
        print(f"Generation: {record['generation_status']}", flush=True)
    completed = [row for row in result["tasks"] if row["generation_status"] == "completed"]
    task_map = {task["id"]: task for task in tasks}
    all_units, all_evidence, all_sources, grade_input = [], {}, {}, []
    for record in completed:
        try:
            if record.get("model") != model_name:
                raise VerificationError("Generator model differs from the frozen evaluation configuration.")
            report, evidence, sources, manifest = load_run(Path(record["run_directory"]))
            task = task_map[record['task_id']]
            if task.get('generation_contract', 'legacy') != 'legacy':
                expected_question = task['question'] + task_suffix(task)
                if (record.get('generation_contract') != contracts[task['id']]
                        or record.get('question_sha256') != digest(expected_question)
                        or manifest.get('question') != expected_question):
                    raise VerificationError('Generation contract did not reach the recorded research question.')
            record['constraints'] = check_body(report)
            units = split_units(report)
        except Exception as error:
            record.update(generation_status="invalid_artifacts", error_type=type(error).__name__)
            continue
        for unit in units:
            unit["unit_id"] = record["task_id"] + ":" + unit["unit_id"]
            unit["task_id"] = record["task_id"]
        all_units.extend(units)
        all_evidence.update(evidence)
        all_sources.update(sources)
        task = task_map[record["task_id"]]
        grade_input.append(dict(task_id=task["id"], question=task["question"], criteria=task["criteria"],
            report=report, documents=[documents[name]["content"] for name in task["documents"]]))
    completed = [row for row in result["tasks"] if row["generation_status"] == "completed"]
    judges = []
    grading_started = time.monotonic()
    try:
        if completed:
            payload, fingerprint = prepare(all_units, all_evidence, all_sources)
            rubric_payload = {"tasks": grade_input}
            if len(json.dumps(rubric_payload, ensure_ascii=False)) > 60000:
                raise VerificationError("Rubric input budget exceeded.")
            # Both payloads validated before the first grader request.
            task_judge = task_judge or TaskJudge(model_name)
            evidence_judge = evidence_judge or ModelJudge(model_name, prompt_version="v2")
            judges = [task_judge, evidence_judge]
            grades = grade_tasks(rubric_payload, task_judge)
            result["task_grades"] = grades
            save()
            quality = assess(payload, evidence_judge)
            atomic_text(artifact / "citation-quality.json", json.dumps(quality, ensure_ascii=False, indent=2))
            atomic_text(artifact / "citation-quality.md", quality_markdown(quality))
            result["quality_input_sha256"] = fingerprint
            for record in completed:
                rows = [row for row in quality["units"] if row["task_id"] == record["task_id"]]
                metrics = quality_metrics(rows)
                record.update(quality=metrics, rubric=grades[record["task_id"]],
                              success=task_success(record, grades[record["task_id"]], metrics, data["thresholds"]))
        for record in result["tasks"]:
            if record["generation_status"] != "completed":
                record["success"] = False
        result["status"] = "complete"
        result["task_success_rate"] = sum(row["success"] for row in result["tasks"])/len(tasks)
    except Exception as error:
        result.update(status="scoring_failed", task_success_rate=None, **failure_diagnostics(error))
        result["unaccepted_evaluator_outputs"] = [getattr(judge, "last_output", None) for judge in judges]
        for record in completed:
            record["success"] = None
    result["generation_completion_rate"] = len(completed)/len(tasks)
    result["grading_elapsed_seconds"] = round(time.monotonic()-grading_started, 3)
    result["task_grader_prompt_version"] = getattr(task_judge, "prompt_version", "research-task-rubric-v1")
    result["task_grader_prompt_sha256"] = getattr(task_judge, "prompt_sha256", None)
    result["citation_judge_prompt_version"] = getattr(evidence_judge, "prompt_version", "report-quote-v2")
    result["citation_judge_prompt_sha256"] = getattr(evidence_judge, "prompt_sha256", None)
    generation = [row.get("generation_metrics", {}) for row in result["tasks"]]
    outcomes_known = all(isinstance(row.get("tool_outcomes"), int) and
                         isinstance(row.get("tool_successes"), int) for row in generation)
    total_outcomes = sum(row["tool_outcomes"] for row in generation) if outcomes_known else 0
    result["tool_success_rate"] = (sum(row["tool_successes"] for row in generation)/total_outcomes
                                   if outcomes_known and total_outcomes else None)
    result["retry_count"] = (sum(row["retry_count"] for row in generation)
                             if all(isinstance(row.get("retry_count"), int) for row in generation) else None)
    known = all(row.get("token_usage_complete") for row in generation)
    result["generation_total_tokens"] = sum(row["total_tokens"] for row in generation) if known else None
    result["evaluator_usage"] = [getattr(judge, "usage", None) for judge in judges]
    evaluator_known = all(isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int)
                          for usage in result["evaluator_usage"])
    result["evaluator_total_tokens"] = sum(row["total_tokens"] for row in result["evaluator_usage"]) if evaluator_known else None
    result["total_tokens"] = (result["generation_total_tokens"] + result["evaluator_total_tokens"]
                              if known and evaluator_known else None)
    result.update(estimated_fee=None, recovery_rate=None, finished_at=now(),
                  evaluation_elapsed_seconds=round(time.monotonic()-evaluation_started, 3))
    save()
    return result


def summary(result, *, quality_details=False):
    lines = ["# End-to-End Research Task Evaluation", "", "受控资料，模型辅助评分；需人工复核。",
             f"Scope: {result.get('scope') or 'unspecified'}",
             f"Label status: {result.get('label_status') or 'unspecified'}", "",
             'Generation contracts: ' + (json.dumps({key: spec.get('version') for key, spec in result['generation_contracts'].items()},
                                                    ensure_ascii=False, sort_keys=True)
                                        if isinstance(result.get('generation_contracts'), dict) else 'not_recorded'), '',
             f"Status: {result['status']}", f"Task Success Rate: {result.get('task_success_rate')}",
             f"Generation Completion Rate: {result.get('generation_completion_rate')}", "",
             "Human review: pending; Final acceptance: pending（本入口不执行人工验收）。",
             "valid仅表示评分合同通过；model_gate为模型辅助判定，不代表人工验收。",
             "invalid表示评分无效；not_scored表示生成未完成；unknown不计为模型通过。", "",
             "Task Success Rate 保留原模型口径，不含长度约束；应用检查单独合并模型与确定性检查。",
             "旧结果 not_checked 不自动补判为通过；应用检查通过仍不是人工验收。", "",
             "| Task | Generation | Scoring | Model gate | Body limit | Application gate | Citation precision | Coverage | Tokens | Seconds | Retries |",
             "|---|---|---|---|---|---|---:|---:|---:|---:|---:|"]
    counts = []
    for row in result["tasks"]:
        metrics, quality = row.get("generation_metrics", {}), row.get("quality", {})
        state = assessment(row, result["status"])
        app = application_assessment(row, result['status'])
        lines.append(f"| {row['task_id']} | {row['generation_status']} | {state['scoring_status']} | {state['model_gate']} "
                     f"| {app['constraint_gate']} | {app['application_gate']} "
                     f"| {quality.get('citation_precision')} | {quality.get('evidence_coverage')} "
                     f"| {metrics.get('total_tokens')} | {row.get('elapsed_seconds')} | {metrics.get('retry_count')} |")
        if isinstance(row.get('constraints'), dict):
            check = row['constraints']
            counts.append(f"Body count {row['task_id']}: {check.get('counted_chars')}/200 Unicode codepoints; policy body-length-v1.")
    lines += [''] + counts
    lines += ["", f"Generation tokens: {result.get('generation_total_tokens')}",
              f"Evaluator tokens: {result.get('evaluator_total_tokens')}",
              "Monetary cost and Recovery Rate: N/A (not measured by this batch).", ""]
    if quality_details:
        lines += ['[查看逐行引用与排除项](citation-quality.md)', '']
    return "\n".join(lines)


def saved_quality(evaluation, dataset, result):
    """Validate saved labels against frozen inputs, without another semantic/model judgment."""
    from .review import build_packet
    if result.get('status') != 'complete' or not result.get('quality_input_sha256'):
        raise VerificationError('A completed batch with saved citation input is required.')
    packet = build_packet(evaluation, dataset)
    units, evidence, sources = [], {}, {}
    for task in packet['tasks']:
        if task.get('generation_status') == 'completed':
            recorded = next(row for row in result['tasks'] if row['task_id'] == task['task_id'])
            if ('constraint_policy' in result or 'constraints' in recorded):
                if result.get('constraint_policy') != constraint_policy() or recorded.get('constraints') != check_body(task['report']):
                    raise VerificationError('Saved body constraint differs from the frozen report or policy.')
        units.extend(task.get('units', []))
        evidence.update(task.get('evidence', {}))
        sources.update(task.get('sources', {}))
    payload, fingerprint = prepare(units, evidence, sources)
    if fingerprint != result['quality_input_sha256']:
        raise VerificationError('Saved quality input no longer matches reports and evidence.')
    raw = (evaluation / 'citation-quality.json').read_bytes()
    quality = json.loads(raw.decode('utf-8'))
    checked = replay_assessment(payload, quality)
    for task in result['tasks']:
        if task['generation_status'] == 'completed':
            rows = [row for row in checked['units'] if row['task_id'] == task['task_id']]
            if task.get('quality') != quality_metrics(rows):
                raise VerificationError('Task metrics differ from saved citation labels.')
    return quality, dict(quality_sha256=hashlib.sha256(raw).hexdigest(), input_sha256=fingerprint,
                         label_validation='deterministic_replay_of_saved_labels_not_new_semantic_scoring')


def render_summary(source, directory, *, with_quality=False, dataset=None):
    raw = source.read_bytes()
    result = json.loads(raw.decode("utf-8"))
    rendered = summary(result, quality_details=with_quality)
    quality, quality_binding = None, {}
    if with_quality:
        if dataset is None:
            raise VerificationError('Dataset is required for bound citation rendering.')
        quality, quality_binding = saved_quality(source.parent, dataset, result)
        if source.read_bytes() != raw:
            raise VerificationError('Result changed while preparing the quality view.')
    directory.mkdir(parents=True, exist_ok=False)
    atomic_text(directory / "summary.md", rendered)
    if quality is not None:
        atomic_text(directory / 'citation-quality.md', quality_markdown(quality))
    atomic_text(directory / "provenance.json", json.dumps(dict(mode="offline_summary", api_requests=0,
        source_sha256=hashlib.sha256(raw).hexdigest(), scoring_recomputed=False,
        human_review="pending", final_acceptance="pending", **quality_binding), indent=2))


def result_gate(result, *, gate='application'):
    """Derive an automation verdict without trusting cached gate badges or batch rates."""
    if gate not in {'model', 'application'}:
        raise VerificationError('Choose a supported evaluation gate.')
    tasks = result.get('tasks')
    if not isinstance(tasks, list) or not tasks:
        raise VerificationError('A nonempty task batch is required.')
    ids = [row.get('task_id') for row in tasks]
    if any(not isinstance(key, str) or not key for key in ids) or len(ids) != len(set(ids)):
        raise VerificationError('Gate tasks must have distinct IDs.')
    rows = []
    for record in tasks:
        model = assessment(record, result.get('status'))
        if model['scoring_status'] == 'valid':
            rubric = record['rubric']
            if (type(rubric.get('rubric_passed')) is not bool
                    or record['success'] != task_success(record, rubric, record['quality'], result['thresholds'])):
                raise VerificationError('Recorded model success differs from the saved component checks.')
        app = application_assessment(record, result.get('status'))
        if (app['application_gate'] == 'passed' and result.get('constraint_policy') != constraint_policy()):
            app['application_gate'] = 'unknown'
        selected = model['model_gate'] if gate == 'model' else app['application_gate']
        rows.append(dict(task_id=record['task_id'], model_gate=model['model_gate'],
                         constraint_gate=app['constraint_gate'], application_gate=app['application_gate'],
                         selected_gate=selected))
    states = [row['selected_gate'] for row in rows]
    status = ('failed' if 'failed' in states else 'passed'
              if result.get('status') == 'complete' and all(value == 'passed' for value in states) else 'unknown')
    return dict(version='evaluation-gate-v1', gate=gate, status=status, tasks=rows,
                exit_code=0 if status == 'passed' else 3,
                human_review='pending', final_acceptance='pending')


def check_saved_result(source, dataset, directory, *, gate='application'):
    """Validate frozen body/labels offline before issuing a new, separate gate verdict."""
    raw = source.read_bytes()
    result = json.loads(raw.decode('utf-8'))
    _, binding = saved_quality(source.parent, dataset, result)
    # Check business labels against their frozen report and criterion IDs as well.
    from .review import build_packet
    packet = build_packet(source.parent, dataset)
    for task in packet['tasks']:
        if task['generation_status'] != 'completed':
            continue
        record = next(row for row in result['tasks'] if row['task_id'] == task['task_id'])
        rubric = record.get('rubric')
        if not isinstance(rubric, dict) or not isinstance(rubric.get('criteria'), list):
            raise VerificationError('Missing saved business labels.')
        payload = dict(tasks=[dict(task_id=task['task_id'], report=task['report'], criteria=task['criteria'])])
        def labels(_):
            return dict(tasks=[dict(task_id=task['task_id'], criteria=rubric['criteria'])])
        checked = grade_tasks(payload, labels)[task['task_id']]
        if rubric != checked or result.get('task_grades', {}).get(task['task_id']) != checked:
            raise VerificationError('Saved business labels or derived gate changed.')
    verdict = result_gate(result, gate=gate)
    if source.read_bytes() != raw:
        raise VerificationError('Result changed during gate verification.')
    directory.mkdir(parents=True, exist_ok=False)
    atomic_text(directory/'gate.json', json.dumps(verdict, ensure_ascii=False, indent=2))
    atomic_text(directory/'summary.md', summary(result))
    atomic_text(directory/'provenance.json', json.dumps(dict(mode='offline_gate', api_requests=0,
        source_sha256=hashlib.sha256(raw).hexdigest(), scoring_recomputed=False,
        scope='revalidated_saved_labels_and_body_constraints_not_new_semantic_scoring', **binding), indent=2))
    return verdict


def main():
    parser = argparse.ArgumentParser(description="Controlled-corpus end-to-end research evaluation")
    parser.add_argument("--dataset", type=Path, default=Path("evals/research-tasks-v1.json"))
    parser.add_argument("--output", type=Path, default=Path("runs/task-evaluations"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float)
    offline = parser.add_mutually_exclusive_group()
    offline.add_argument("--summarize", type=Path, help="Render an existing result offline; no grading or credentials")
    offline.add_argument('--check-result', type=Path, help='Validate saved inputs and issue a separate offline gate verdict')
    parser.add_argument('--gate', choices=['model', 'application'], help='Automation exit gate; check-result defaults to application, new evaluation to model')
    parser.add_argument('--with-quality', action='store_true', help='Include bound saved citation details with --summarize')
    parser.add_argument('--generation-contract', choices=['legacy', 'body-length-v1'], help='Explicit generator counting instructions; default legacy')
    args = parser.parse_args()
    if args.with_quality and not args.summarize:
        parser.error('--with-quality requires --summarize.')
    if args.summarize and args.gate is not None:
        parser.error('--summarize is formatting only; use --check-result for gate exit codes.')
    if (args.summarize or args.check_result) and args.generation_contract is not None:
        parser.error('Offline views and gates cannot replace the recorded generation contract.')
    if args.check_result and (args.limit is not None or args.timeout is not None):
        parser.error('--check-result validates the whole saved batch; generation controls are not accepted.')
    args.limit = args.limit if args.limit is not None else 3
    args.timeout = args.timeout if args.timeout is not None else 180
    if not 1 <= args.limit <= 10 or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 600:
        parser.error("limit must be 1..10 and timeout 1..600 seconds")
    if args.summarize:
        artifact = args.output / uuid4().hex
        try:
            render_summary(args.summarize, artifact, with_quality=args.with_quality, dataset=args.dataset)
            print(f"Offline summary; zero API calls: {artifact / 'summary.md'}")
            return 0
        except Exception as error:
            print(f"Offline summary failed ({type(error).__name__}).", file=sys.stderr)
            return 1
    if args.check_result:
        artifact = args.output / uuid4().hex
        try:
            verdict = check_saved_result(args.check_result, args.dataset, artifact, gate=args.gate or 'application')
            print(f"Offline {verdict['gate']} gate: {verdict['status']}; zero API calls: {artifact / 'gate.json'}")
            return verdict['exit_code']
        except Exception as error:
            print(f'Offline gate rejected ({type(error).__name__}); no passing verdict.', file=sys.stderr)
            return 1
    load_dotenv()
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        print("Missing DEEPSEEK_API_KEY.", file=sys.stderr)
        return 2
    artifact = args.output / uuid4().hex
    try:
        data, tasks, documents = load_taskset(args.dataset.resolve())
        if args.generation_contract == 'body-length-v1':
            tasks = [dict(task, generation_contract=args.generation_contract) for task in tasks]
        result = evaluate_batch(data, tasks[:args.limit], documents, artifact,
                                model_name=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"), timeout=args.timeout)
        atomic_text(artifact / "summary.md", summary(result, quality_details=result['status'] == 'complete'
                                                   and bool(result.get('quality_input_sha256'))))
        print(f"Task evaluation: {artifact / 'summary.md'}")
        print(f"Status: {result['status']}; Task Success Rate: {result.get('task_success_rate')}")
        verdict = result_gate(result, gate=args.gate or 'model')
        atomic_text(artifact/'cli-gate.json', json.dumps(verdict, ensure_ascii=False, indent=2))
        print(f"CLI {verdict['gate']} gate: {verdict['status']}")
        return verdict['exit_code']
    except Exception as error:
        print(f"Task evaluation failed ({type(error).__name__}); artifacts: {artifact}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

