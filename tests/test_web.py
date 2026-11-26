from io import BytesIO
import json
import time
from pathlib import Path
import shutil
import subprocess
from zipfile import ZipFile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from researchdesk import storage, web


def wait_for(client, run_id, ready, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        record = response.json()
        if ready(record) or record["status"] == "failed":
            return record
        time.sleep(0.05)
    raise AssertionError(f"Run did not reach expected state: {record}")


def test_offline_demo_serves_assets_and_resolves_report_citations(tmp_path, monkeypatch):
    with TestClient(web.create_app(tmp_path), base_url="http://127.0.0.1") as client:
        page = client.get("/").text
        assert "ResearchDesk" in page and 'id="download-report"' in page
        assert client.get("/api/runs/unknown/report.zip").status_code == 404
        assert client.get("/static/workbench.js").status_code == 200
        renderer = client.get("/static/report.mjs")
        assert renderer.status_code == 200 and "javascript" in renderer.headers["content-type"]
        assert client.get("/api/runs").json() == {"allow_live": False, "runs": []}
        assert client.post("/api/runs", json={"mode": "web", "question": "Question"}).status_code == 403
        assert client.post("/api/runs", json={}, headers={"Origin": "http://elsewhere.test"}).status_code == 403
        assert client.post("/api/runs", json={"mode": "demo", "max_model_calls": 13}).status_code == 422
        started = client.post("/api/runs", json={"mode": "demo"})
        assert started.status_code == 202
        run_id = started.json()["id"]
        done = wait_for(client, run_id, lambda item: item["status"] == "completed")
        assert done["status"] == "completed"
        eid, = done["evidence"]
        assert f"[{eid}]" in done["report"] and "exact evidence quotes" in done["evidence"][eid]["snippet"]
        download = client.get(f"/api/runs/{run_id}/report.zip")
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert download.headers["content-disposition"] == f'attachment; filename="researchdesk-{run_id}.zip"'
        assert download.headers["cache-control"] == "no-store"
        with ZipFile(BytesIO(download.content)) as bundle:
            assert bundle.namelist() == ["report.md", "evidence.json", "run-summary.json"]
            exported = bundle.read("report.md").decode()
            assert exported.startswith(done["report"] + "\n\n")
            assert exported.count(f"### {eid}\n") == 1
            assert any(
                line.startswith("    ") and "exact evidence quotes" in line
                for line in exported.splitlines()
            )
            assert json.loads(bundle.read("evidence.json")) == done["evidence"]
            summary = json.loads(bundle.read("run-summary.json"))
            assert summary == {
                "schema_version": 1,
                **{key: done[key] for key in ("id", "mode", "created_at", "status", "resume_count", "model_limit", "exit_code", "error_type", "stopped_by_workbench", "can_resume",
                                             "recovery_demo_injected", "recovery_demo_boundary")},
                "recovery_demo_before": None,
                "metrics": done["metrics"],
                "citation_check": done["citation_check"],
                "recent_activity": done["activity"],
            }
        standalone = client.get(f"/api/runs/{run_id}/run-summary.json")
        assert standalone.status_code == 200 and standalone.json() == summary
        assert standalone.headers["cache-control"] == "no-store"
        assert standalone.headers["content-disposition"] == f'attachment; filename="researchdesk-{run_id}-summary.json"'

        assert done["citation_check"] == dict(total=1, matched=1, mismatched=0, missing=0)
        assert done["evidence"][eid]["integrity"] == "valid"
        assert done["metrics"]["provider_requests"] == 0

        report, evidence, sources, metadata = web.load_run(tmp_path / "research" / run_id)
        evidence[eid]["snippet"] = "An excerpt absent from the source."
        monkeypatch.setattr(web, "load_run", lambda _: (
            report + f" [{eid}] [E0000000000000000]", evidence, sources, metadata,
        ))
        checked = client.get(f"/api/runs/{run_id}").json()
        assert checked["citation_check"] == dict(total=2, matched=0, mismatched=1, missing=1)
        assert checked["evidence"][eid]["integrity"] == "invalid"
        assert client.post(f"/api/runs/{run_id}/resume").status_code == 409


def test_web_stops_and_resumes_same_scripted_checkpoint_without_new_calls(tmp_path, monkeypatch):
    setup = """
import sys
from pathlib import Path
def offline(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('Offline worker test')
sys.addaudithook(offline)
from researchdesk.fault_worker import install_fault, scripted_model
from researchdesk import cli
cli.load_dotenv = lambda: None
scripted_model()
if sys.argv[3] == 'start':
    install_fault('report', Path(sys.argv[1]) / 'ready.json', Path(sys.argv[1]) / 'research')
"""
    monkeypatch.setattr(web, "WORKER", setup + web.WORKER)
    with TestClient(web.create_app(tmp_path, allow_live=True), base_url="http://127.0.0.1") as client:
        started = client.post("/api/runs", json=dict(mode="local", question="Answer from the attached document.",
                                                   notes="Fixed evidence.", max_model_calls=3))
        assert started.status_code == 202
        run_id = started.json()["id"]
        parked = wait_for(client, run_id, lambda _: (tmp_path / "ready.json").is_file())
        assert parked["status"] == "running" and parked["report"] is None
        running_summary = client.get(f"/api/runs/{run_id}/run-summary.json").json()
        assert running_summary["status"] == "running" and running_summary["citation_check"] is None
        assert running_summary["metrics"] == parked["metrics"]
        assert client.get(f"/api/runs/{run_id}/report.zip").status_code == 409
        assert client.post("/api/runs", json={"mode": "demo"}).status_code == 409
        assert len(client.get("/api/runs").json()["runs"]) == 1
        stopped = client.post(f"/api/runs/{run_id}/stop").json()
        assert stopped["status"] == "interrupted" and stopped["can_resume"]
        stopped_summary = client.get(f"/api/runs/{run_id}/run-summary.json").json()
        assert stopped_summary["status"] == "interrupted" and stopped_summary["citation_check"] is None
        assert stopped_summary["exit_code"] == stopped["exit_code"]
        assert stopped["stopped_by_workbench"] is stopped_summary["stopped_by_workbench"] is True
        assert stopped_summary["can_resume"] is True
        assert running_summary["stopped_by_workbench"] is False
        assert stopped_summary["metrics"] == running_summary["metrics"]
        assert stopped_summary["recent_activity"] == running_summary["recent_activity"]
        assert client.post(f"/api/runs/{run_id}/resume").status_code == 202
        done = wait_for(client, run_id, lambda item: item["status"] == "completed")
        assert done["id"] == run_id and done["resume_count"] == 1 and done["status"] == "completed"
        for snapshot in (parked, done):
            assert {key: snapshot["metrics"][key] for key in ("model_calls", "tool_calls", "provider_requests")} == {
                "model_calls": 2, "tool_calls": 1, "provider_requests": 0,
            }
        assert done["metrics"]["tool_successes"] == done["metrics"]["tool_outcomes"] == 1
        assert done["metrics"]["tool_success_rate"] == 1
        assert done["metrics"]["retry_count"] == 0
        assert done["metrics"]["recovery_replays"] == 0
        assert done["report"] and done["evidence"]
        activity = done["activity"]
        assert 1 <= len(activity) <= 8
        assert [event["event_id"] for event in activity] == sorted(
            (event["event_id"] for event in activity), reverse=True,
        )
        assert any(event["event"] == "model_completed" for event in activity)
        assert all(set(event) <= {"event_id", "event", "tool", "timestamp", "error_code", "will_retry"}
                   for event in activity)


@pytest.mark.parametrize(
    ("payload", "boundary", "model_calls_before", "replays_after"),
    [({}, "report", 2, 0), ({"recovery_point": "receipt"}, "receipt", 1, 1)],
)
def test_offline_recovery_demo_resumes_after_web_server_restart(tmp_path, payload, boundary,
                                                               model_calls_before, replays_after):
    base_url = "http://127.0.0.1"
    with TestClient(web.create_app(tmp_path), base_url=base_url) as client:
        assert client.post("/api/runs", json={"mode": "recovery_demo", "question": "Custom"}).status_code == 422
        assert client.post("/api/runs", json={"mode": "demo", "recovery_point": "receipt"}).status_code == 422
        assert client.post("/api/runs", json={"mode": "recovery_demo", "recovery_point": "invalid"}).status_code == 422
        started = client.post("/api/runs", json={"mode": "recovery_demo", **payload})
        assert started.status_code == 202
        run_id = started.json()["id"]
        stopped = wait_for(client, run_id, lambda run: run["status"] == "interrupted")
        assert stopped["status"] == "interrupted" and stopped["can_resume"]
        assert stopped["recovery_demo_injected"] and not stopped["stopped_by_workbench"]
        assert stopped["report"] is None
        assert stopped["metrics"]["model_calls"] == model_calls_before
        assert stopped["recovery_demo_boundary"] == boundary
        before = dict(model_calls=model_calls_before, tool_calls=1, recovery_replays=0)
        assert stopped["recovery_demo_before"] == before
        assert stopped["metrics"]["tool_calls"] == 1
        assert stopped["metrics"]["provider_requests"] == 0
        assert client.get(f"/api/runs/{run_id}/report.zip").status_code == 409
        marker = json.loads((tmp_path / "jobs" / run_id / "recovery-demo-fault.json").read_text(encoding="utf-8"))
        assert marker["run_id"] == run_id and marker["point"] == boundary
        assert marker["before"] == before

    with TestClient(web.create_app(tmp_path), base_url=base_url) as restarted:
        assert restarted.get("/api/runs").json()["allow_live"] is False
        assert restarted.post(f"/api/runs/{run_id}/resume").status_code == 202
        done = wait_for(restarted, run_id, lambda run: run["status"] == "completed")
        assert done["status"] == "completed" and done["id"] == run_id
        assert done["resume_count"] == 1
        assert done["metrics"] == {
            **stopped["metrics"], "model_calls": 2, "recovery_replays": replays_after,
        }
        summary = restarted.get(f"/api/runs/{run_id}/run-summary.json").json()
        assert summary["recovery_demo_before"] == before
        assert summary["recovery_demo_boundary"] == boundary
        assert done["recovery_demo_injected"]
        assert done["citation_check"] == dict(total=1, matched=1, mismatched=0, missing=0)
        assert restarted.get(f"/api/runs/{run_id}/report.zip").status_code == 200


@pytest.mark.parametrize(
    ("budget", "outcome", "model_calls", "tool_calls"),
    [(3, "completed", 3, 1), (1, "budget_exhausted", 1, 0)],
)
def test_model_response_demo_keeps_original_budget(tmp_path, budget, outcome, model_calls, tool_calls):
    with TestClient(web.create_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.post("/api/runs", json={
            "mode": "recovery_demo", "recovery_point": "report", "recovery_budget": 1,
        }).status_code == 422
        assert client.post("/api/runs", json={
            "mode": "recovery_demo", "recovery_point": "model_response", "recovery_budget": 2,
        }).status_code == 422
        created = client.post("/api/runs", json={
            "mode": "recovery_demo", "recovery_point": "model_response", "recovery_budget": budget,
        })
        assert created.status_code == 202
        run_id = created.json()["id"]
        stopped = wait_for(client, run_id, lambda row: row["status"] == "interrupted")
        assert stopped["status"] == "interrupted" and stopped["can_resume"]
        assert stopped["recovery_demo_injected"] and stopped["recovery_demo_boundary"] == "model_response"
        assert stopped["model_limit"] == budget
        before = dict(model_calls=1, tool_calls=0, recovery_replays=0)
        assert stopped["recovery_demo_before"] == before

        assert client.post(f"/api/runs/{run_id}/resume").status_code == 202
        done = wait_for(client, run_id, lambda row: row["status"] in {"completed", "budget_exhausted"})
        assert done["status"] == outcome and done["id"] == run_id
        assert done["model_limit"] == budget and done["resume_count"] == 1
        assert done["metrics"]["model_calls"] == model_calls
        assert done["metrics"]["tool_calls"] == tool_calls
        assert done["metrics"]["provider_requests"] == 0
        assert done["recovery_demo_before"] == before
        report = client.get(f"/api/runs/{run_id}/report.zip")
        assert report.status_code == (200 if outcome == "completed" else 409)
        if outcome == "budget_exhausted":
            assert done["report"] is None and not done["can_resume"]
            assert any(event["event"] == "model_budget_denied" for event in done["activity"])


def test_atomic_state_update_retries_windows_reader_conflict(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    path.write_text('{"status":"running"}', encoding="utf-8")
    replace = storage.os.replace
    calls = []

    def temporarily_locked(source, destination):
        calls.append(destination)
        if len(calls) == 1:
            assert destination.read_text(encoding="utf-8") == '{"status":"running"}'
            error = PermissionError("File held open by reader")
            error.winerror = 5
            raise error
        replace(source, destination)

    monkeypatch.setattr(storage.os, "replace", temporarily_locked)
    storage.atomic_text(path, '{"status":"completed"}')
    assert calls == [path, path]
    assert path.read_text(encoding="utf-8") == '{"status":"completed"}'
    assert list(tmp_path.iterdir()) == [path]


def test_report_renderer():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the JavaScript renderer checks")
    script = Path(__file__).with_name("test_web_report.mjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
