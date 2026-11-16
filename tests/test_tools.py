import httpx
from researchdesk.ledger import Ledger
from researchdesk.tools import build_tools


def test_search_extract_and_record_without_network(tmp_path, monkeypatch):
    def handle(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [
                {"title": "Official doc", "url": "https://example.org/doc", "content": "Short text."}
            ]})
        return httpx.Response(200, json={"results": [
            {"url": "https://example.org/doc", "raw_content": "Full source text with a quote."}
        ]})

    original = httpx.Client
    monkeypatch.setattr("researchdesk.tools.httpx.Client",
                        lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    ledger = Ledger(tmp_path / "run")
    search, read, record = build_tools(ledger, "test-not-a-real-key")
    result = search.invoke({"query": "a technical question"})
    sid = result["sources"][0]["id"]
    full = read.invoke({"source_id": sid})
    assert full["id"] != sid
    assert ledger.sources[sid]["content"] == "Short text."
    evidence = record.invoke({"claim": "A claim", "source_id": full["id"], "snippet": "a quote"})
    assert evidence["id"] in ledger.evidence


def test_provider_error_does_not_leak_secret(tmp_path, monkeypatch):
    def handle(request):
        return httpx.Response(401, json={"error": "sensitive-provider-payload"})

    original = httpx.Client
    monkeypatch.setattr("researchdesk.tools.httpx.Client",
                        lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    tools = build_tools(Ledger(tmp_path / "run"), "test-secret")
    result = tools[0].invoke({"query": "test"})
    assert "error" in result
    assert "test-secret" not in str(result)
    assert "sensitive-provider-payload" not in str(result)
