"""Loopback workbench over the same CLI, ledger, and recovery policy."""
import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import RLock
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ledger import now
from .run_metrics import run_metrics
from .storage import RecoveryError, RunLock, atomic_text
from .verification import load_run

ID = re.compile(r"[0-9a-f]{32}")
STATIC = Path(__file__).with_name("static")
WORKER = "from researchdesk.web import worker; import sys; raise SystemExit(worker(*sys.argv[1:]))"


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    mode: Literal["demo", "local", "web"] = "demo"
    question: str = Field(default="", max_length=4000)
    notes: str = Field(default="", max_length=50000)
    max_model_calls: int = Field(default=6, ge=1, le=12)

    @model_validator(mode="after")
    def require_inputs(self):
        if self.mode != "demo" and not self.question:
            raise ValueError("A research question is required.")
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
        args = ["--output", str(root / "research")]
        if action == "resume":
            args.extend(["--resume", run_id])
        elif request["mode"] == "demo":
            args.extend(["--demo", "--", request["question"]])
        else:
            args.extend(["--max-model-calls", str(request["max_model_calls"]), "--max-tool-calls", "10",
                         "--max-provider-requests", "0" if request["mode"] == "local" else "4",
                         "--tool-retries", "0"])
            if request["mode"] == "local":
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
            if not attempt.is_file() or read_json(attempt)["status"] == "exited":
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
        return dict(id=job.name, mode=details["mode"], question=details["question"],
                    created_at=details["created_at"], status=status,
                    can_stop=active and job.name in self.processes,
                    can_resume=self.allow_live and status == "interrupted" and metadata.get("mode") == "live"
                               and metadata.get("prepared") is True,
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
            del self.processes[job.name]


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
        if request.mode != "demo" and not runs.allow_live:
            raise HTTPException(403, "Real research requires starting the server with --allow-live.")
        with runs.lock:
            runs.ensure_idle()
            job = runs.jobs / uuid4().hex
            job.mkdir()
            if request.notes:
                atomic_text(job / "notes.md", request.notes)
            save_json(job / "job.json", dict(mode=request.mode, question=request.question or "证据台账示例",
                                            max_model_calls=request.max_model_calls, created_at=now()))
            runs.launch(job, "start")
            return runs.overview(job)

    @app.get("/api/runs/{run_id}")
    def detail(run_id: str):
        job = runs.job(run_id)
        record = runs.overview(job)
        directory = child_run_path(runs.research, run_id)
        try:
            metrics = run_metrics(directory)
        except sqlite3.Error:
            metrics = {}
        report, citations = None, {}
        if record["status"] == "completed":
            try:
                report, evidence, sources, _ = load_run(directory)
            except (ValueError, OSError, sqlite3.Error) as error:
                raise HTTPException(409, "The completed report could not be verified.") from error
            for eid, item in evidence.items():
                source = sources.get(item["source_id"], {})
                location = source.get("location") or ""
                citations[eid] = dict(claim=item["claim"], snippet=item["snippet"],
                    title=source.get("title"), location=Path(location).name if source.get("kind") == "local" else location)
        return dict(**record, report=report, evidence=citations,
                    metrics={key: metrics.get(key) for key in ("model_calls", "tool_calls", "provider_requests")})

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
