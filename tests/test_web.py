from io import BytesIO
import json
import time
from pathlib import Path
import shutil
import subprocess
from threading import Event
from zipfile import ZipFile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from researchdesk import recovery_bundle, storage, web


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


def comparison_result(client, boundary, budget):
    response = client.get("/api/recovery-comparison")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    cases = response.json()["cases"]
    assert len(cases) == 5
    return next(case["run"] for case in cases if (case["boundary"], case["model_limit"]) == (boundary, budget))


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
        assert comparison_result(client, "report", 3) is None

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
    ("payload", "boundary", "model_calls_before", "replays_after", "retries_after"),
    [({}, "report", 2, 0, 0), ({"recovery_point": "receipt"}, "receipt", 1, 1, 0),
     ({"recovery_point": "evidence"}, "evidence", 1, 0, 1)],
)
def test_offline_recovery_demo_resumes_after_web_server_restart(tmp_path, payload, boundary,
                                                               model_calls_before, replays_after, retries_after):
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
        before = dict(model_calls=model_calls_before, tool_calls=1, recovery_replays=0,
                      retry_count=0, evidence_count=1)
        assert stopped["recovery_demo_before"] == before
        assert stopped["metrics"]["tool_calls"] == 1
        assert stopped["metrics"]["provider_requests"] == 0
        assert client.get(f"/api/runs/{run_id}/report.zip").status_code == 409
        marker = json.loads((tmp_path / "jobs" / run_id / "recovery-demo-fault.json").read_text(encoding="utf-8"))
        assert marker["run_id"] == run_id and marker["point"] == boundary
        assert marker["before"] == before
        assert comparison_result(client, boundary, 3) is None
        evidence_path = tmp_path / "research" / run_id / "evidence.jsonl"
        original_evidence = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
        assert len(original_evidence) == 1

    with TestClient(web.create_app(tmp_path), base_url=base_url) as restarted:
        assert restarted.get("/api/runs").json()["allow_live"] is False
        assert restarted.post(f"/api/runs/{run_id}/resume").status_code == 202
        done = wait_for(restarted, run_id, lambda run: run["status"] == "completed")
        assert done["status"] == "completed" and done["id"] == run_id
        assert done["resume_count"] == 1
        assert done["metrics"] == {
            **stopped["metrics"], "model_calls": 2, "recovery_replays": replays_after,
            "retry_count": retries_after, "tool_success_rate": 1, "tool_successes": 1,
            "evidence_count": 1,
        }
        summary = restarted.get(f"/api/runs/{run_id}/run-summary.json").json()
        assert summary["recovery_demo_before"] == before
        assert summary["recovery_demo_boundary"] == boundary
        assert comparison_result(restarted, boundary, 3) == {
            "id": run_id, "status": "completed", "before": before,
            "after": {key: done["metrics"][key] for key in before},
        }
        assert done["recovery_demo_injected"]
        restored_evidence = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
        assert restored_evidence == original_evidence
        assert list(done["evidence"]) == [original_evidence[0]["id"]]
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
        before = dict(model_calls=1, tool_calls=0, recovery_replays=0, retry_count=0, evidence_count=0)
        assert stopped["recovery_demo_before"] == before

        assert client.post(f"/api/runs/{run_id}/resume").status_code == 202
        done = wait_for(client, run_id, lambda row: row["status"] in {"completed", "budget_exhausted"})
        assert done["status"] == outcome and done["id"] == run_id
        assert done["model_limit"] == budget and done["resume_count"] == 1
        assert done["metrics"]["model_calls"] == model_calls
        assert done["metrics"]["tool_calls"] == done["metrics"]["evidence_count"] == tool_calls
        assert done["metrics"]["provider_requests"] == 0
        assert done["recovery_demo_before"] == before
        compared = comparison_result(client, "model_response", budget)
        assert compared["id"] == run_id and compared["status"] == outcome
        assert compared["before"] == before
        assert compared["after"] == {key: done["metrics"][key] for key in before}
        report = client.get(f"/api/runs/{run_id}/report.zip")
        assert report.status_code == (200 if outcome == "completed" else 409)
        if outcome == "budget_exhausted":
            assert done["report"] is None and not done["can_resume"]
            assert any(event["event"] == "model_budget_denied" for event in done["activity"])


def test_offline_recovery_suite_continues_after_server_restart(tmp_path, monkeypatch):
    base_url = "http://127.0.0.1"
    reached_receipt = Event()
    wait = web.Runs.wait_for_suite_worker

    def pause_after_receipt(runs, job, process):
        finished = wait(runs, job, process)
        marker = job / "recovery-demo-fault.json"
        if finished and marker.is_file() and web.read_json(marker)["point"] == "receipt":
            reached_receipt.set()
            return not runs.shutting_down.wait(50)
        return finished

    with monkeypatch.context() as patch:
        patch.setattr(web.Runs, "wait_for_suite_worker", pause_after_receipt)
        with TestClient(web.create_app(tmp_path), base_url=base_url) as client:
            assert client.post("/api/recovery-suite/resume").status_code == 409
            response = client.post("/api/recovery-suite")
            assert response.status_code == 202
            started = response.json()
            assert started["status"] == "running"
            review_path = f"/api/recovery-suite/{started['id']}/review.zip"
            assert client.get(review_path).status_code == 409
            archived_comparison = f"/api/recovery-suite/{started['id']}/comparison"
            assert client.get(archived_comparison).status_code == 409
            assert client.get("/api/recovery-suites").json() == {"suites": []}
            assert client.post("/api/recovery-suite").status_code == 409
            assert client.post("/api/runs", json={"mode": "demo"}).status_code == 409
            assert reached_receipt.wait(90)
            before = client.get("/api/runs").json()["recovery_suite"]
            finished, interrupted, *_ = before["cases"]
            assert finished["phase"] == "finished" and interrupted["phase"] == "injecting"
            assert client.get(f"/api/runs/{interrupted['run_id']}").json()["recovery_demo_injected"]

    with TestClient(web.create_app(tmp_path), base_url=base_url) as restarted:
        assert restarted.get("/api/runs").json()["recovery_suite"]["status"] == "interrupted"
        resumed = restarted.post("/api/recovery-suite/resume")
        assert resumed.status_code == 202 and resumed.json()["id"] == started["id"]
        deadline = time.monotonic() + 140
        while time.monotonic() < deadline:
            overview = restarted.get("/api/runs").json()
            suite = overview["recovery_suite"]
            if suite["status"] != "running":
                break
            time.sleep(0.1)
        assert suite["id"] == started["id"] and suite["status"] == "completed", suite
        assert suite["cases"][0] == finished
        assert suite["cases"][1]["run_id"] == interrupted["run_id"]
        assert len(overview["runs"]) == 5
        assert [item["phase"] for item in suite["cases"]] == ["finished"] * 5
        assert [item["outcome"] for item in suite["cases"]] == ["completed"] * 4 + ["budget_exhausted"]
        for item in suite["cases"]:
            row = comparison_result(restarted, item["boundary"], item["model_limit"])
            assert row["id"] == item["run_id"] and row["status"] == item["outcome"]
            record = restarted.get(f"/api/runs/{row['id']}").json()
            assert record["metrics"]["provider_requests"] == 0 and record["resume_count"] == 1
        assert restarted.post("/api/recovery-suite/resume").status_code == 409
        archived = restarted.get(archived_comparison)
        assert archived.status_code == 200
        assert [row["run"]["id"] for row in archived.json()["cases"]] == [item["run_id"] for item in suite["cases"]]
        list_response = restarted.get("/api/recovery-suites")
        assert list_response.headers["cache-control"] == "no-store"
        assert list_response.json() == {"suites": [{key: suite[key] for key in ("id", "created_at", "finished_at")}]}
        response = restarted.get(review_path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"] == "application/zip"
        verified = recovery_bundle.verify_batch(BytesIO(response.content))
        assert verified == dict(batch_id=suite["id"], cases=5, reports=4, budget_stops=1, missing_before_snapshots=0)
        with ZipFile(BytesIO(response.content)) as review:
            assert len(review.namelist()) == 16
            assert review.testzip() is None
            assert json.loads(review.read("batch.json")) == {"schema_version": 1, **suite}
            comparison = review.read("comparison.md").decode()
            for item in suite["cases"]:
                prefix = f"runs/{item['run_id']}/"
                assert prefix + "run-summary.json" in comparison
                summary = json.loads(review.read(prefix + "run-summary.json"))
                assert summary.pop("recovery_suite_id") == started["id"]
                assert summary == restarted.get(f"/api/runs/{item['run_id']}/run-summary.json").json()
                if item["outcome"] == "completed":
                    assert prefix + "report.md" in comparison
                    assert len(json.loads(review.read(prefix + "evidence.json"))) == 1
                else:
                    assert prefix + "report.md" not in review.namelist()
                    assert prefix + "evidence.json" not in review.namelist()

        with ZipFile(BytesIO(response.content)) as review:
            files = {name: review.read(name) for name in review.namelist()}
        changed = BytesIO()
        with ZipFile(changed, "w") as tampered:
            for name, content in files.items():
                tampered.writestr(name, content + b"changed" if name == "comparison.md" else content)
        with pytest.raises(recovery_bundle.BundleError, match="Checksum mismatch"):
            recovery_bundle.verify_batch(changed)

        baseline = tmp_path / "baseline.zip"
        baseline.write_bytes(response.content)
        assert recovery_bundle.compare_batches(baseline, baseline)["status"] == "matching"
        first_summary = f"runs/{suite['cases'][0]['run_id']}/run-summary.json"

        def repack_summary(name, summary):
            path = tmp_path / name
            with ZipFile(path, "w") as candidate:
                for member, content in files.items():
                    if member != recovery_bundle.MANIFEST:
                        candidate.writestr(member, json.dumps(summary).encode() if member == first_summary else content)
                recovery_bundle.write_checksums(candidate)
            return path

        summary = json.loads(files[first_summary])
        summary["metrics"]["retry_count"] += 1
        changed_bundle = repack_summary("changed.zip", summary)
        changes = recovery_bundle.compare_batches(baseline, changed_bundle)
        assert changes["status"] == "different" and changes["changed_cases"] == 1
        assert changes["cases"][0]["changes"] == [
            {"phase": "after", "metric": "retry_count", "baseline": 0, "candidate": 1},
        ]
        assert recovery_bundle.main([str(changed_bundle), "--baseline", str(baseline)]) == 1

        summary = json.loads(files[first_summary])
        del summary["recovery_demo_before"]["evidence_count"]
        incomplete = repack_summary("older.zip", summary)
        missing = recovery_bundle.compare_batches(baseline, incomplete)
        assert missing["status"] == "incomplete" and missing["incomplete_cases"] == 1
        assert missing["cases"][0]["unavailable"] == [
            {"phase": "before", "metric": "evidence_count", "baseline": 1, "candidate": None},
        ]
        assert recovery_bundle.main([str(incomplete), "--baseline", str(baseline)]) == 2

        wrong_link = BytesIO()
        summary = json.loads(files[first_summary])
        summary["recovery_suite_id"] = "0" * 32
        files[first_summary] = json.dumps(summary).encode()
        with ZipFile(wrong_link, "w") as tampered:
            for name, content in files.items():
                if name != recovery_bundle.MANIFEST:
                    tampered.writestr(name, content)
            recovery_bundle.write_checksums(tampered)
        with pytest.raises(recovery_bundle.BundleError, match="does not match the batch"):
            recovery_bundle.verify_batch(wrong_link)

    with TestClient(web.create_app(tmp_path), base_url=base_url) as restarted:
        assert restarted.get("/api/runs").json()["recovery_suite"] == suite
        replacement = restarted.post("/api/recovery-suite")
        assert replacement.status_code == 202 and replacement.json()["id"] != started["id"]
        assert restarted.get(archived_comparison).json() == archived.json()
        assert restarted.get("/api/recovery-suites").json() == list_response.json()
        old_review = restarted.get(review_path)
        assert old_review.status_code == 200
        with ZipFile(BytesIO(old_review.content)) as review:
            assert json.loads(review.read("batch.json")) == {"schema_version": 1, **suite}


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
