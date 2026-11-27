"""Loopback workbench over the same CLI, ledger, and recovery policy."""
import argparse
from contextlib import asynccontextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from textwrap import indent
from threading import Event, RLock, Thread
from time import monotonic
from typing import Literal
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .citations import CITATION
from .ledger import now
from .inspect_run import safe_event
from .recovery_bundle import COMPARISON_METRICS, EVIDENCE_APPENDIX, RECOVERY_CASES, write_checksums
from .run_metrics import run_metrics_snapshot
from .storage import RecoveryError, RunLock, atomic_text
from .verification import evidence_context, load_run

ID = re.compile(r"[0-9a-f]{32}")
STATIC = Path(__file__).with_name("static")
LIVE_MODES = {"local", "web"}
RECOVERY_DEMO_NOTES = "ResearchDesk saves source evidence before publishing the report."
WORKER = "from researchdesk.web import worker; import sys; raise SystemExit(worker(*sys.argv[1:]))"


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    mode: Literal["demo", "recovery_demo", "local", "web"] = "demo"
    question: str = Field(default="", max_length=4000)
    notes: str = Field(default="", max_length=50000)
    recovery_point: Literal["report", "receipt", "model_response", "evidence"] | None = None
    recovery_budget: Literal[1, 3] | None = None
    max_model_calls: int = Field(default=6, ge=1, le=12)

    @model_validator(mode="after")
    def require_inputs(self):
        if self.mode in LIVE_MODES and not self.question:
            raise ValueError("A research question is required.")
        if self.mode == "recovery_demo" and self.question:
            raise ValueError("The offline recovery demo uses a fixed question.")
        if self.recovery_point is not None and self.mode != "recovery_demo":
            raise ValueError("Recovery points are only available for the offline recovery demo.")
        if self.recovery_budget is not None and (self.mode != "recovery_demo" or self.recovery_point != "model_response"):
            raise ValueError("A demo budget can only be selected for the model response boundary.")
        if self.mode == "local" and not self.notes:
            raise ValueError("Local research requires source notes.")
        if self.mode != "local" and self.notes:
            raise ValueError("Source notes are only accepted in local mode.")
        return self


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def child_run_path(parent, run_id):
    target = (parent / run_id).resolve()
    if not ID.fullmatch(run_id) or target.parent != parent or target.name != run_id:
        raise HTTPException(404, "Run not found.")
    return target


def worker(root, run_id, action):
    from .cli import main as research

    root = Path(root).resolve()
    job = child_run_path(root / "jobs", run_id)
    with RunLock(job):
        request = read_json(job / "job.json")
        research_root = root / "research"
        if request["mode"] == "recovery_demo":
            from .fault_worker import configure_workbench_demo

            configure_workbench_demo(job, research_root, action, request.get("recovery_point") or "report")
        args = ["--output", str(research_root)]
        if action == "resume":
            args.extend(["--resume", run_id])
        elif request["mode"] == "demo":
            args.extend(["--demo", "--", request["question"]])
        else:
            args.extend(["--max-model-calls", str(request["max_model_calls"]), "--max-tool-calls", "10",
                         "--max-provider-requests", "4" if request["mode"] == "web" else "0",
                         "--tool-retries", "0"])
            if request["mode"] in {"local", "recovery_demo"}:
                args.extend(["--local-only", "--document", str(job / "notes.md")])
            args.extend(["--", request["question"]])
        save_json(job / "attempt.json", {"status": "running"})
        code = 1
        try:
            code = research(args, run_id=None if action == "resume" else run_id)
        finally:
            save_json(job / "attempt.json", {"status": "exited", "exit_code": code})
        return code


class Runs:
    """Own local subprocess handles. SQLite and the CLI own research state."""

    def __init__(self, root, allow_live):
        self.root = root.resolve()
        self.jobs = self.root / "jobs"
        self.research = self.root / "research"
        self.allow_live = allow_live
        self.processes = {}
        self.lock = RLock()
        self.suite_file = self.root / "recovery-suite.json"
        self.suite_archive = self.root / "recovery-suites"
        self.suite_thread = None
        self.shutting_down = Event()

    def job(self, run_id):
        job = child_run_path(self.jobs, run_id)
        if not (job / "job.json").is_file():
            raise HTTPException(404, "Run not found.")
        return job

    def running(self, job):
        with self.lock:
            process = self.processes.get(job.name)
            if process is not None:
                if process.poll() is None:
                    return True
                del self.processes[job.name]
            attempt = job / "attempt.json"
            if not attempt.is_file() or read_json(attempt)["status"] not in {"starting", "running"}:
                return False
            # An orphaned worker can still own the attempt after the web server restarts.
            try:
                with RunLock(job):
                    return False
            except RecoveryError:
                return True

    def overview(self, job):
        details = read_json(job / "job.json")
        active = self.running(job)
        directory = child_run_path(self.research, job.name)
        manifest = directory / "run.json"
        metadata = read_json(manifest) if manifest.is_file() else {}
        attempt_file = job / "attempt.json"
        attempt = read_json(attempt_file) if attempt_file.is_file() else {}
        if active:
            status = "running"
        elif metadata.get("status") in {"completed", "budget_exhausted", "failed", "interrupted"}:
            status = metadata["status"]
        elif attempt.get("status") == "exited":
            status = "failed"
        else:
            status = "interrupted"
        recovery_demo = details["mode"] == "recovery_demo"
        return dict(id=job.name, mode=details["mode"], question=details["question"],
                    created_at=details["created_at"], status=status,
                    can_stop=active and job.name in self.processes,
                    can_resume=(self.allow_live or recovery_demo) and status == "interrupted"
                               and metadata.get("mode") == "live"
                               and metadata.get("prepared") is True,
                    stopped_by_workbench=attempt.get("status") == "stopped",
                    recovery_demo_boundary=(details.get("recovery_point") or "report") if recovery_demo else None,
                    recovery_demo_injected=recovery_demo and (job / "recovery-demo-fault.json").is_file(),
                    error_type=metadata.get("error_type"),
                    exit_code=attempt.get("exit_code"), resume_count=metadata.get("resume_count", 0),
                    model_limit=(metadata.get("model_policy") or {}).get("max_calls"))

    def history(self):
        with self.lock:
            items = [self.overview(self.job(job.name)) for job in self.jobs.iterdir()
                     if ID.fullmatch(job.name) and (job / "job.json").is_file()]
        return sorted(items, key=lambda row: (row["created_at"], row["id"]), reverse=True)

    def suite_active(self):
        return self.suite_thread is not None and self.suite_thread.is_alive()

    def ensure_manual_run(self):
        if self.suite_active():
            raise HTTPException(409, "The offline recovery suite is using the workbench.")

    def ensure_idle(self):
        if any(self.running(other) for other in self.jobs.iterdir()
               if ID.fullmatch(other.name) and (other / "job.json").is_file()):
            raise HTTPException(409, "Wait for the current run or stop it first.")

    def start_job(self, request, *, run_id=None):
        job = child_run_path(self.jobs, run_id or uuid4().hex)
        job.mkdir(exist_ok=run_id is not None)
        recovery_demo = request.mode == "recovery_demo"
        notes = RECOVERY_DEMO_NOTES if recovery_demo else request.notes
        if notes:
            atomic_text(job / "notes.md", notes)
        question = "离线恢复演示" if recovery_demo else request.question or "证据台账示例"
        save_json(job / "job.json", dict(mode=request.mode, question=question,
                                        recovery_point=(request.recovery_point or "report") if recovery_demo else None,
                                        max_model_calls=(request.recovery_budget or 3) if recovery_demo else request.max_model_calls,
                                        created_at=now()))
        self.launch(job, "start")
        return job

    def suite_state(self):
        with self.lock:
            if not self.suite_file.is_file():
                return None
            suite = read_json(self.suite_file)
            if suite["status"] == "running" and not self.suite_active():
                archived = self.suite_archive / (suite["id"] + ".json")
                if archived.is_file():
                    return read_json(archived)
                suite["status"] = "interrupted"
            return suite

    def archive_suite(self, suite):
        if suite and suite["status"] == "completed":
            self.suite_archive.mkdir(exist_ok=True)
            path = self.suite_archive / (suite["id"] + ".json")
            if not path.is_file():
                save_json(path, suite)

    def save_suite(self, suite):
        with self.lock:
            self.archive_suite(suite)
            save_json(self.suite_file, suite)

    def completed_suites(self):
        with self.lock:
            summaries = {}
            for path in self.suite_archive.glob("*.json"):
                if not ID.fullmatch(path.stem):
                    continue
                record = read_json(path)
                if record["status"] == "completed" and record["id"] == path.stem:
                    summaries[path.stem] = record
            current = self.suite_state()
            if current and current["status"] == "completed":
                summaries.setdefault(current["id"], current)
        ordered = sorted(summaries.values(), key=lambda suite: (suite["finished_at"], suite["id"]), reverse=True)
        return [{key: suite[key] for key in ("id", "created_at", "finished_at")} for suite in ordered]

    def completed_suite(self, suite_id):
        if not ID.fullmatch(suite_id):
            raise HTTPException(404, "Recovery suite not found.")
        with self.lock:
            archived = self.suite_archive / (suite_id + ".json")
            if archived.is_file():
                return read_json(archived)
            current = self.suite_state()
            if not current or current["id"] != suite_id:
                raise HTTPException(404, "Recovery suite not found.")
            if current["status"] != "completed":
                raise HTTPException(409, "The recovery suite must complete before export.")
            return current

    def archived_run(self, case):
        run_id = case["run_id"]
        try:
            job = self.job(run_id)
        except HTTPException as error:
            raise HTTPException(409, "An archived run is missing.") from error
        record = self.overview(job)
        if (record["mode"] != "recovery_demo" or record["status"] != case["outcome"]
                or record["model_limit"] != case["model_limit"]
                or record["recovery_demo_boundary"] != case["boundary"]
                or not record["recovery_demo_injected"] or not record["resume_count"]):
            raise HTTPException(409, "The saved run does not match its recovery suite.")
        return job, record

    def start_suite(self, *, resume=False):
        with self.lock:
            self.ensure_manual_run()
            self.ensure_idle()
            if resume:
                suite = self.suite_state()
                if not suite or suite["status"] != "interrupted":
                    raise HTTPException(409, "There is no interrupted recovery suite.")
                suite.pop("finished_at", None)
            else:
                self.archive_suite(self.suite_state())
                suite = dict(id=uuid4().hex, created_at=now(), cases=[
                    dict(boundary=boundary, model_limit=budget, phase="pending") for boundary, budget in RECOVERY_CASES
                ])
            suite["status"] = "running"
            self.save_suite(suite)
            self.suite_thread = Thread(target=self.run_suite, args=(suite,), name="offline-recovery-suite", daemon=True)
            self.suite_thread.start()
            return self.suite_state()

    def wait_for_suite_worker(self, job, process):
        deadline = monotonic() + 60
        while process.poll() is None:
            interrupted = self.shutting_down.wait(0.1)
            if interrupted or monotonic() >= deadline:
                with self.lock:
                    if process.poll() is None:
                        self.stop(job)
                        return False
                return not interrupted
        return not self.shutting_down.is_set()

    def run_suite_case(self, suite, case):
        boundary, budget = case["boundary"], case["model_limit"]
        request = ResearchRequest(mode="recovery_demo", recovery_point=boundary,
                                  recovery_budget=budget if boundary == "model_response" else None)
        with self.lock:
            if self.shutting_down.is_set():
                return False
            run_id = case.get("run_id")
            job = child_run_path(self.jobs, run_id) if run_id else None
            record = self.overview(job) if job and (job / "job.json").is_file() else None
            process = None
            case.pop("error", None)
            if not record or (record["status"] == "interrupted" and not record["recovery_demo_injected"]):
                # Save the assignment before launching so a restart can find this attempt.
                run_id = (None if record else run_id) or uuid4().hex
                case.update(run_id=run_id, phase="injecting")
                self.save_suite(suite)
                job = self.start_job(request, run_id=run_id)
                process = self.processes[job.name]
        if process is not None and not self.wait_for_suite_worker(job, process):
            return False

        with self.lock:
            if self.shutting_down.is_set():
                return False
            record = self.overview(job)
            if not record["recovery_demo_injected"]:
                case.update(phase="failed", outcome=record["status"], error="fault_not_confirmed")
                return True
            process = None
            if record["can_resume"]:
                case["phase"] = "resuming"
                self.save_suite(suite)
                self.launch(job, "resume")
                process = self.processes[job.name]
        if process is not None and not self.wait_for_suite_worker(job, process):
            return False

        record = self.overview(job)
        expected = "budget_exhausted" if budget == 1 else "completed"
        succeeded = record["status"] == expected and record["resume_count"] >= 1
        case.update(phase="finished" if succeeded else "failed", outcome=record["status"])
        return True

    def run_suite(self, suite):
        try:
            for case in suite["cases"]:
                if case["phase"] == "finished" or (case["phase"] == "failed" and case.get("error") != "worker_stopped"):
                    continue
                if not self.run_suite_case(suite, case):
                    if not self.shutting_down.is_set():
                        case.update(phase="failed", error="worker_stopped")
                    break
                self.save_suite(suite)
            if self.shutting_down.is_set():
                suite["status"] = "interrupted"
            else:
                suite["status"] = "completed" if all(case["phase"] == "finished" for case in suite["cases"]) else "failed"
        except Exception as error:
            suite.update(status="failed", error_type=type(error).__name__)
        finally:
            suite["finished_at"] = now()
            self.save_suite(suite)

    def launch(self, job, action):
        save_json(job / "attempt.json", {"status": "starting"})
        try:
            with (job / "worker.log").open("ab") as log:
                self.processes[job.name] = subprocess.Popen(
                    [sys.executable, "-X", "utf8", "-c", WORKER, str(self.root), job.name, action],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
        except OSError as error:
            save_json(job / "attempt.json", {"status": "exited", "exit_code": 1})
            raise HTTPException(503, "Could not start the research process.") from error

    def stop(self, job):
        with self.lock:
            process = self.processes.get(job.name)
            if process is None or process.poll() is not None:
                raise HTTPException(409, "This server does not own an active process for this run.")
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            save_json(job / "attempt.json", {"status": "stopped", "exit_code": process.returncode})
            del self.processes[job.name]


def read_completed_report(directory):
    citations = {}
    try:
        report, evidence, sources, _ = load_run(directory)
    except (ValueError, OSError, sqlite3.Error) as error:
        raise HTTPException(409, "The completed report could not be verified.") from error
    for eid, item in evidence.items():
        source = sources.get(item["source_id"], {})
        location = source.get("location") or ""
        citations[eid] = dict(claim=item["claim"], snippet=item["snippet"],
            title=source.get("title"), location=Path(location).name if source.get("kind") == "local" else location)
    for eid in dict.fromkeys(CITATION.findall(report)):
        if eid in citations:
            citations[eid]["integrity"] = evidence_context(eid, evidence, sources)["integrity"]
    return report, citations


def report_with_evidence(report, citations):
    blocks = []
    for evidence_id in dict.fromkeys(CITATION.findall(report)):
        evidence = citations.get(evidence_id)
        if evidence is None:
            continue
        source = "\n".join(filter(None, (evidence["title"], evidence["location"])))
        excerpt = "\n\n".join(filter(None, (source, evidence["snippet"])))
        # Preserve source text literally even if it contains Markdown or HTML.
        blocks.append(f"### {evidence_id}\n\n{indent(excerpt, '    ')}")
    if not blocks:
        return report
    return report + EVIDENCE_APPENDIX + "\n\n".join(blocks) + "\n"


def read_run_observability(directory):
    try:
        metrics, events = run_metrics_snapshot(directory, count_evidence=True)
    except sqlite3.Error:
        metrics, events = {}, []
    activity = []
    for event_id, event in reversed(events[-8:]):
        safe = safe_event(event, event_id)
        activity.append({key: safe[key] for key in
                         ("event_id", "event", "tool", "timestamp", "error_code", "will_retry") if key in safe})
    public_metrics = {key: metrics.get(key) for key in
                      ("model_calls", "tool_calls", "provider_requests", "tool_successes",
                       "tool_outcomes", "tool_success_rate", "retry_count", "recovery_replays", "evidence_count")}
    return public_metrics, activity


def summarize_citations(report, citations):
    cited_ids = set(CITATION.findall(report or ""))
    citation_check = dict(total=len(cited_ids), matched=0, mismatched=0, missing=0)
    for eid in cited_ids:
        item = citations.get(eid)
        if item is None:
            citation_check["missing"] += 1
        elif item["integrity"] == "valid":
            citation_check["matched"] += 1
        else:
            citation_check["mismatched"] += 1
    return citation_check


def recovery_demo_before(record, job):
    if not record["recovery_demo_injected"]:
        return None
    try:
        marker = read_json(job / "recovery-demo-fault.json")
    except (OSError, ValueError):
        return None
    if (not isinstance(marker, dict) or marker.get("run_id") != record["id"]
            or marker.get("point") != record["recovery_demo_boundary"]):
        return None
    before = marker.get("before")
    keys = ("model_calls", "tool_calls", "recovery_replays")
    if not isinstance(before, dict) or any(type(before.get(key)) is not int or before[key] < 0 for key in keys):
        return None
    optional = ("retry_count", "evidence_count")
    return {key: before[key] for key in (*keys, *optional)
            if type(before.get(key)) is int and before[key] >= 0}


def build_run_summary(record, directory, citation_check, job):
    metrics, activity = read_run_observability(directory)
    return {
        "schema_version": 1,
        **{key: record[key] for key in ("id", "mode", "created_at", "status", "resume_count",
                                       "model_limit", "exit_code", "error_type", "stopped_by_workbench", "can_resume",
                                       "recovery_demo_injected", "recovery_demo_boundary")},
        "recovery_demo_before": recovery_demo_before(record, job),
        "metrics": metrics,
        "citation_check": citation_check,
        "recent_activity": activity,
    }


def write_json_member(archive, name, value):
    archive.writestr(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_report_members(archive, prefix, report, evidence):
    archive.writestr(prefix + "report.md", report_with_evidence(report, evidence))
    write_json_member(archive, prefix + "evidence.json", evidence)


def zip_download(archive, filename):
    return Response(archive.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    })


def suite_comparison_markdown(suite, results):
    labels = {
        "report": "报告写入后", "receipt": "工具结果写入后",
        "evidence": "证据写入后，工具结果提交前",
        "model_response": "模型响应返回后，写入检查点前",
    }
    lines = [
        "# ResearchDesk 离线恢复对照", "",
        f"批次 ID `{suite['id']}`", "",
        "演示使用固定资料和脚本模型。计数按中断前 → 恢复后显示。", "",
        "| 中断位置 | 模型预算 | 结果 | 模型调用 | 工具调用 | 工具再次执行 | 结果复用 | 台账证据 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case, summary in results:
        before = summary["recovery_demo_before"] or {}
        metrics = summary["metrics"]
        prefix = f"runs/{summary['id']}/"
        outcome = "已完成" if summary["status"] == "completed" else "额度用尽"
        detail = f"[{outcome}]({prefix}run-summary.json)"
        if summary["status"] == "completed":
            detail += f" · [报告]({prefix}report.md)"
        values = [f"{before.get(key, '—')} → {metrics[key] if metrics[key] is not None else '—'}"
                  for key in COMPARISON_METRICS]
        row = [labels[case["boundary"]], str(case["model_limit"]), detail, *values]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def create_app(output=Path("runs/web"), *, allow_live=False):
    runs = Runs(Path(output), allow_live)

    @asynccontextmanager
    async def lifespan(_app):
        runs.root.mkdir(parents=True, exist_ok=True)
        with RunLock(runs.root):
            runs.jobs.mkdir(exist_ok=True)
            runs.research.mkdir(exist_ok=True)
            try:
                yield
            finally:
                runs.shutting_down.set()
                if runs.suite_thread is not None:
                    runs.suite_thread.join()
                for run_id, process in list(runs.processes.items()):
                    if process.poll() is None:
                        runs.stop(runs.job(run_id))

    app = FastAPI(title="ResearchDesk", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.get("/static/report.mjs", include_in_schema=False)
    def report_renderer():
        return FileResponse(STATIC / "report.mjs", media_type="text/javascript")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin != f"http://{request.headers.get('host')}":
            return JSONResponse({"detail": "Cross-origin requests are disabled."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Content-Security-Policy":
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"})

    @app.get("/api/runs")
    def history():
        suite = runs.suite_state()
        history = dict(allow_live=runs.allow_live, runs=runs.history())
        if suite is not None:
            history["recovery_suite"] = suite
        return history

    @app.post("/api/recovery-suite", status_code=202)
    def start_recovery_suite():
        return runs.start_suite()

    @app.post("/api/recovery-suite/resume", status_code=202)
    def resume_recovery_suite():
        return runs.start_suite(resume=True)

    @app.get("/api/recovery-suites")
    def list_recovery_suites():
        return JSONResponse({"suites": runs.completed_suites()}, headers={"Cache-Control": "no-store"})

    def comparison_run(job, record):
        directory = child_run_path(runs.research, record["id"])
        metrics, _ = read_run_observability(directory)
        return dict(id=record["id"], status=record["status"], before=recovery_demo_before(record, job),
                    after={key: metrics[key] for key in COMPARISON_METRICS})

    @app.get("/api/recovery-suite/{suite_id}/comparison")
    def archived_recovery_comparison(suite_id: str):
        suite = runs.completed_suite(suite_id)
        cases = [dict(boundary=case["boundary"], model_limit=case["model_limit"],
                      run=comparison_run(*runs.archived_run(case))) for case in suite["cases"]]
        return JSONResponse({"cases": cases}, headers={"Cache-Control": "no-store"})

    @app.get("/api/recovery-suite/{suite_id}/review.zip", response_class=Response)
    def export_recovery_suite(suite_id: str):
        suite = runs.completed_suite(suite_id)
        archive = BytesIO()
        results = []
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
            for case in suite["cases"]:
                run_id = case["run_id"]
                job, record = runs.archived_run(case)
                directory = child_run_path(runs.research, run_id)
                prefix = f"runs/{run_id}/"
                citation_check = None
                if record["status"] == "completed":
                    report, evidence = read_completed_report(directory)
                    write_report_members(bundle, prefix, report, evidence)
                    citation_check = summarize_citations(report, evidence)
                summary = build_run_summary(record, directory, citation_check, job)
                summary["recovery_suite_id"] = suite_id
                write_json_member(bundle, prefix + "run-summary.json", summary)
                results.append((case, summary))
            write_json_member(bundle, "batch.json", {"schema_version": 1, **suite})
            bundle.writestr("comparison.md", suite_comparison_markdown(suite, results))
            write_checksums(bundle)
        return zip_download(archive, f"researchdesk-recovery-{suite_id}.zip")

    @app.get("/api/recovery-comparison")
    def recovery_comparison():
        latest = {}
        for record in runs.history():
            if (record["mode"] != "recovery_demo" or not record["recovery_demo_injected"]
                    or not record["resume_count"] or record["status"] not in {"completed", "budget_exhausted", "failed"}):
                continue
            key = (record["recovery_demo_boundary"], record["model_limit"])
            latest.setdefault(key, record)

        cases = []
        for boundary, budget in RECOVERY_CASES:
            record = latest.get((boundary, budget))
            result = None
            if record:
                result = comparison_run(runs.job(record["id"]), record)
            cases.append(dict(boundary=boundary, model_limit=budget, run=result))
        return JSONResponse({"cases": cases}, headers={"Cache-Control": "no-store"})

    @app.post("/api/runs", status_code=202)
    def start(request: ResearchRequest):
        if request.mode in LIVE_MODES and not runs.allow_live:
            raise HTTPException(403, "Real research requires starting the server with --allow-live.")
        with runs.lock:
            runs.ensure_manual_run()
            runs.ensure_idle()
            job = runs.start_job(request)
            return runs.overview(job)

    @app.get("/api/runs/{run_id}")
    def detail(run_id: str):
        job = runs.job(run_id)
        record = runs.overview(job)
        directory = child_run_path(runs.research, run_id)
        metrics, activity = read_run_observability(directory)
        report, citations = None, {}
        if record["status"] == "completed":
            report, citations = read_completed_report(directory)
        return dict(**record, report=report, evidence=citations, citation_check=summarize_citations(report, citations),
                    activity=activity, metrics=metrics, recovery_demo_before=recovery_demo_before(record, job))

    @app.get("/api/runs/{run_id}/run-summary.json")
    def export_run_summary(run_id: str):
        job = runs.job(run_id)
        record = runs.overview(job)
        directory = child_run_path(runs.research, run_id)
        citation_check = None
        if record["status"] == "completed":
            try:
                report, citations = read_completed_report(directory)
            except HTTPException as error:
                if error.status_code != 409:
                    raise
            else:
                citation_check = summarize_citations(report, citations)
        return JSONResponse(build_run_summary(record, directory, citation_check, job), headers={
            "Content-Disposition": f'attachment; filename="researchdesk-{run_id}-summary.json"',
            "Cache-Control": "no-store",
        })

    @app.get("/api/runs/{run_id}/report.zip", response_class=Response)
    def export_report(run_id: str):
        job = runs.job(run_id)
        record = runs.overview(job)
        if record["status"] != "completed":
            raise HTTPException(409, "The report is available after the run completes.")
        directory = child_run_path(runs.research, run_id)
        report, citations = read_completed_report(directory)
        summary = build_run_summary(record, directory, summarize_citations(report, citations), job)
        archive = BytesIO()
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
            write_report_members(bundle, "", report, citations)
            write_json_member(bundle, "run-summary.json", summary)
        return zip_download(archive, f"researchdesk-{run_id}.zip")

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str):
        with runs.lock:
            runs.ensure_manual_run()
            job = runs.job(run_id)
            if not runs.overview(job)["can_resume"]:
                raise HTTPException(409, "This run is not ready to resume.")
            runs.ensure_idle()
            runs.launch(job, "resume")
            return runs.overview(job)

    @app.post("/api/runs/{run_id}/stop")
    def stop(run_id: str):
        with runs.lock:
            runs.ensure_manual_run()
            job = runs.job(run_id)
            runs.stop(job)
            return runs.overview(job)

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/web"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-live", action="store_true", help="Enable explicitly submitted model/search runs")
    options = parser.parse_args()
    uvicorn.run(create_app(options.output, allow_live=options.allow_live), host="127.0.0.1", port=options.port)


if __name__ == "__main__":
    main()
