import time

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


def test_offline_demo_serves_assets_and_resolves_report_citations(tmp_path):
    with TestClient(web.create_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert "ResearchDesk" in client.get("/").text
        assert client.get("/static/workbench.js").status_code == 200
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
        assert done["metrics"]["provider_requests"] == 0
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
        assert client.post("/api/runs", json={"mode": "demo"}).status_code == 409
        assert len(client.get("/api/runs").json()["runs"]) == 1
        stopped = client.post(f"/api/runs/{run_id}/stop").json()
        assert stopped["status"] == "interrupted" and stopped["can_resume"]
        assert client.post(f"/api/runs/{run_id}/resume").status_code == 202
        done = wait_for(client, run_id, lambda item: item["status"] == "completed")
        assert done["id"] == run_id and done["resume_count"] == 1 and done["status"] == "completed"
        assert done["metrics"] == parked["metrics"] == dict(model_calls=2, tool_calls=1, provider_requests=0)
        assert done["report"] and done["evidence"]
