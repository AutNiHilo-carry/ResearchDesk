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

from researchdesk import web


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
            assert bundle.namelist() == ["report.md", "evidence.json"]
            exported = bundle.read("report.md").decode()
            assert exported.startswith(done["report"] + "\n\n")
            assert exported.count(f"### {eid}\n") == 1
            assert any(
                line.startswith("    ") and "exact evidence quotes" in line
                for line in exported.splitlines()
            )
            assert json.loads(bundle.read("evidence.json")) == done["evidence"]
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
        assert client.get(f"/api/runs/{run_id}/report.zip").status_code == 409
        assert client.post("/api/runs", json={"mode": "demo"}).status_code == 409
        assert len(client.get("/api/runs").json()["runs"]) == 1
        stopped = client.post(f"/api/runs/{run_id}/stop").json()
        assert stopped["status"] == "interrupted" and stopped["can_resume"]
        assert client.post(f"/api/runs/{run_id}/resume").status_code == 202
        done = wait_for(client, run_id, lambda item: item["status"] == "completed")
        assert done["id"] == run_id and done["resume_count"] == 1 and done["status"] == "completed"
        assert done["metrics"] == parked["metrics"] == dict(model_calls=2, tool_calls=1, provider_requests=0)
        assert done["report"] and done["evidence"]
        activity = done["activity"]
        assert 1 <= len(activity) <= 8
        assert [event["event_id"] for event in activity] == sorted(
            (event["event_id"] for event in activity), reverse=True,
        )
        assert any(event["event"] == "model_completed" for event in activity)
        assert all(set(event) <= {"event_id", "event", "tool", "timestamp", "error_code", "will_retry"}
                   for event in activity)


def test_report_renderer():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the JavaScript renderer checks")
    script = Path(__file__).with_name("test_web_report.mjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
