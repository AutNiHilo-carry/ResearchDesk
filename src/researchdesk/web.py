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
from threading import RLock
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
    recovery_point: Literal["report", "receipt", "model_response"] | None = None
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

    def ensure_idle(self):
        if any(self.running(other) for other in self.jobs.iterdir()
               if ID.fullmatch(other.name) and (other / "job.json").is_file()):
            raise HTTPException(409, "Wait for the current run or stop it first.")

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
    return report + "\n\n## \u5f15\u7528\u8bc1\u636e\n\n" + "\n\n".join(blocks) + "\n"


def read_run_observability(directory):
    try:
        metrics, events = run_metrics_snapshot(directory)
    except sqlite3.Error:
        metrics, events = {}, []
    activity = []
    for event_id, event in reversed(events[-8:]):
        safe = safe_event(event, event_id)
        activity.append({key: safe[key] for key in
                         ("event_id", "event", "tool", "timestamp", "error_code", "will_retry") if key in safe})
    public_metrics = {key: metrics.get(key) for key in
                      ("model_calls", "tool_calls", "provider_requests", "tool_successes",
                       "tool_outcomes", "tool_success_rate", "retry_count", "recovery_replays")}
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
    return {key: before[key] for key in keys}


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
        with runs.lock:
            items = [runs.overview(runs.job(job.name)) for job in runs.jobs.iterdir()
                     if ID.fullmatch(job.name) and (job / "job.json").is_file()]
        return dict(allow_live=runs.allow_live, runs=sorted(items, key=lambda row: row["created_at"], reverse=True))

    @app.post("/api/runs", status_code=202)
    def start(request: ResearchRequest):
        if request.mode in LIVE_MODES and not runs.allow_live:
            raise HTTPException(403, "Real research requires starting the server with --allow-live.")
        with runs.lock:
            runs.ensure_idle()
            job = runs.jobs / uuid4().hex
            job.mkdir()
            recovery_demo = request.mode == "recovery_demo"
            notes = RECOVERY_DEMO_NOTES if recovery_demo else request.notes
            if notes:
                atomic_text(job / "notes.md", notes)
            question = "离线恢复演示" if recovery_demo else request.question or "证据台账示例"
            save_json(job / "job.json", dict(mode=request.mode, question=question,
                                            recovery_point=(request.recovery_point or "report") if recovery_demo else None,
                                            max_model_calls=(request.recovery_budget or 3) if recovery_demo else request.max_model_calls,
                                            created_at=now()))
            runs.launch(job, "start")
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
            bundle.writestr("report.md", report_with_evidence(report, citations))
            bundle.writestr("evidence.json", json.dumps(citations, ensure_ascii=False, indent=2) + "\n")
            bundle.writestr("run-summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        return Response(archive.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="researchdesk-{run_id}.zip"',
            "Cache-Control": "no-store",
        })

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str):
        with runs.lock:
            job = runs.job(run_id)
            if not runs.overview(job)["can_resume"]:
                raise HTTPException(409, "This run is not ready to resume.")
            runs.ensure_idle()
            runs.launch(job, "resume")
            return runs.overview(job)

    @app.post("/api/runs/{run_id}/stop")
    def stop(run_id: str):
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
