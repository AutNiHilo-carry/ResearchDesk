import json
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from researchdesk.ledger import Ledger
from researchdesk.policy import ToolFailure, ToolLimits, ToolSpec, ToolExecutor
from researchdesk.tools import ResearchTools, SearchArgs

CLIENT = httpx.Client
GOOD = {"results": [{"title": "Doc", "url": "https://example.org/doc", "content": "Saved exact text."}]}


@pytest.fixture
def make_runtime(tmp_path, monkeypatch):
    def build(responder=None, limits=None, allowed_tools=None):
        seen = []
        tick = [0.0]
        def handle(request):
            seen.append(request)
            return responder(request, len(seen)) if responder else httpx.Response(200, json=GOOD)
        monkeypatch.setattr("researchdesk.tools.httpx.Client",
                            lambda **kw: CLIENT(transport=httpx.MockTransport(handle), **kw))
        ledger = Ledger(tmp_path / str(len(list(tmp_path.iterdir()))))
        runtime = ResearchTools(ledger, "secret-fixture", limits=limits, allowed_tools=allowed_tools,
                                clock=lambda: tick[0], sleeper=lambda n: tick.__setitem__(0, tick[0]+n))
        return runtime, seen, tick
    return build


def events(runtime):
    return [json.loads(line) for line in
            (runtime.ledger.directory / "tool_events.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("args", [{}, {"query": 123}, {"query": " "},
                                 {"query": "x", "extra": "forbidden"}, {"query": "x"*1001}])
def test_schema_errors_do_not_call_provider(make_runtime, args):
    runtime, seen, _ = make_runtime()
    result = runtime.executor.execute("search_sources", args)
    assert result["error_code"] == "invalid_arguments"
    assert not seen
    assert events(runtime)[-1]["event"] == "rejected"


def test_permission_denial_before_execution(make_runtime):
    runtime, seen, _ = make_runtime(allowed_tools={"record_evidence"})
    assert runtime.executor.execute("search_sources", {"query": "x"})["error_code"] == "permission_denied"
    assert runtime.executor.execute("execute", {})["error_code"] == "permission_denied"
    assert not seen


def test_transient_error_retries_with_budget_and_logs(make_runtime):
    runtime, seen, _ = make_runtime(
        lambda r, n: httpx.Response(503) if n == 1 else httpx.Response(200, json=GOOD),
        ToolLimits(timeout_seconds=2))
    assert "sources" in runtime.executor.execute("search_sources", {"query": "x"})
    assert len(seen) == runtime.executor.provider_requests == 2
    assert seen[0].extensions["timeout"]["read"] == 2
    assert events(runtime)[-1]["retry_count"] == 1
    log = (runtime.ledger.directory / "tool_events.jsonl").read_text()
    assert "secret-fixture" not in log
    assert "Authorization" not in log


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_http_error_is_not_retried(make_runtime, status):
    runtime, seen, _ = make_runtime(lambda r, n: httpx.Response(status, text="provider-secret"))
    result = runtime.executor.execute("search_sources", {"query": "x"})
    assert result["error_code"] == f"http_{status}"
    assert len(seen) == 1
    assert "provider-secret" not in str(result)


def test_timeout_retry_exhaustion(make_runtime):
    def timeout(request, number):
        raise httpx.ReadTimeout("secret-error", request=request)
    runtime, seen, _ = make_runtime(timeout)
    assert runtime.executor.execute("search_sources", {"query": "x"})["error_code"] == "timeout"
    assert len(seen) == 2
    assert events(runtime)[-1]["retry_count"] == 1


def test_request_budget_also_bounds_retries(make_runtime):
    runtime, seen, _ = make_runtime(lambda r, n: httpx.Response(503),
                                    ToolLimits(max_provider_requests=1, max_retries=3))
    runtime.executor.execute("search_sources", {"query": "x"})
    result = runtime.executor.execute("search_sources", {"query": "y"})
    assert result["error_code"] == "request_budget_exceeded"
    assert len(seen) == 1


def test_cache_reuse_after_provider_budget_exhausted(make_runtime):
    runtime, seen, _ = make_runtime(limits=ToolLimits(max_provider_requests=1))
    first = runtime.executor.execute("search_sources", {"query": "x"})
    first["sources"].clear()  # Returned values cannot corrupt cached records.
    second = runtime.executor.execute("search_sources", {"query": "x"})
    assert len(second["sources"]) == 1
    assert len(seen) == 1
    assert events(runtime)[-1]["event"] == "cache_hit"


def test_call_budget_includes_cache_hits_and_invalid_calls(make_runtime):
    runtime, seen, _ = make_runtime(limits=ToolLimits(max_calls=2))
    runtime.executor.execute("search_sources", {"query": ""})
    runtime.executor.execute("search_sources", {"query": "x"})
    assert runtime.executor.execute("search_sources", {"query": "x"})["error_code"] == "call_budget_exceeded"
    assert len(seen) == 1


@pytest.mark.parametrize("body", [
    {"results": [{"url": "https://example.org", "content": "ok"}, {"url": "file:///bad", "content": "bad"}]},
    {"results": []}, {"results": "wrong type"}, None,
])
def test_invalid_results_do_not_mutate_or_retry(make_runtime, body):
    runtime, seen, _ = make_runtime(lambda r, n: httpx.Response(200, json=body))
    assert runtime.executor.execute("search_sources", {"query": "x"})["error_code"] == "invalid_result"
    assert not runtime.ledger.sources
    assert len(seen) == 1


def test_extract_url_must_match_requested_snapshot(make_runtime):
    runtime, seen, _ = make_runtime(lambda r, n: httpx.Response(200, json={
        "results": [{"url": "https://other.org", "raw_content": "Wrong document"}]}))
    source = runtime.ledger.add_source("Doc", "https://example.org/doc", "Original text")
    result = runtime.executor.execute("read_source", {"source_id": source["id"]})
    assert result["error_code"] == "invalid_result"
    assert len(runtime.ledger.sources) == 1


def test_repeated_evidence_write_is_deduplicated(make_runtime):
    runtime, seen, _ = make_runtime()
    source = runtime.ledger.add_source("Doc", "file://doc", "Saved exact text.", "local")
    args = {"claim": "A fact", "source_id": source["id"], "snippet": "exact text"}
    assert runtime.executor.execute("record_evidence", args) == runtime.executor.execute("record_evidence", args)
    assert len((runtime.ledger.directory / "evidence.jsonl").read_text().splitlines()) == 1
    assert not seen


def test_side_effect_operation_never_retries(tmp_path):
    calls = []
    def side_effect(query):
        calls.append(query)
        raise ToolFailure("transient_write", "Write uncertain", retryable=True)
    executor = ToolExecutor(tmp_path, {"write": ToolSpec(SearchArgs, side_effect, read_only=False)})
    assert executor.execute("write", {"query": "x"})["error_code"] == "transient_write"
    assert calls == ["x"]


def test_deadline_prevents_new_request(make_runtime):
    runtime, seen, tick = make_runtime(limits=ToolLimits(max_duration_seconds=1))
    tick[0] = 2
    assert runtime.executor.execute("search_sources", {"query": "x"})["error_code"] == "deadline_exceeded"
    assert not seen


def test_concurrent_identical_requests_use_one_provider_call(make_runtime):
    runtime, seen, _ = make_runtime()
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: runtime.executor.execute("search_sources", {"query": "x"}), range(2)))
    assert rows[0] == rows[1]
    assert len(seen) == 1

