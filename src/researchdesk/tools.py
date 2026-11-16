"""Research tools behind the application-side Tool Policy layer."""
from urllib.parse import urlsplit
import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from .policy import ToolExecutor, ToolFailure, ToolSpec
from .search_context import bind_policy,render_search_sources


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def nonblank(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("Blank argument")
        return value


class SearchArgs(Arguments):
    query: str = Field(min_length=1, max_length=1000)


class ReadArgs(Arguments):
    source_id: str = Field(pattern=r"^S[0-9a-f]{16}$")


class EvidenceArgs(ReadArgs):
    claim: str = Field(min_length=1, max_length=4000)
    snippet: str = Field(min_length=1, max_length=24000)


class SearchHit(BaseModel):
    model_config = ConfigDict(strict=True)
    title: str = ""
    url: str
    content: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def web_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Not an HTTP URL")
        return value

    @field_validator("content")
    @classmethod
    def text_content(cls, value):
        if not value.strip():
            raise ValueError("Empty content")
        return value


class SearchResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    results: list[SearchHit] = Field(min_length=1, max_length=5)


class ExtractHit(BaseModel):
    model_config = ConfigDict(strict=True)
    url: str
    raw_content: str = Field(min_length=1)


class ExtractResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    results: list[ExtractHit] = Field(min_length=1, max_length=1)


class ResearchTools:
    def __init__(self, ledger, api_key, limits=None, allowed_tools=None, **executor_options):
        self.ledger, self.api_key = ledger, api_key
        self.search_context_policy = bind_policy(ledger)
        self.executor = ToolExecutor(ledger.directory, {
            "search_sources": ToolSpec(SearchArgs, self.search, read_only=True, cacheable=True),
            "read_source": ToolSpec(ReadArgs, self.read, read_only=True, cacheable=True),
            "record_evidence": ToolSpec(EvidenceArgs, self.record, read_only=False),
        }, limits, allowed_tools, store=ledger.store, **executor_options)

    def request(self, endpoint, payload):
        if not self.api_key:
            raise ToolFailure("missing_credentials", "Tavily credentials are not configured.")
        timeout = self.executor.provider_timeout()
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(5, timeout))) as client:
                response = client.post("https://api.tavily.com/" + endpoint,
                    headers={"Authorization": "Bearer " + self.api_key}, json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException:
            raise ToolFailure("timeout", "Provider request timed out.", retryable=True) from None
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            raise ToolFailure("http_" + str(status), "Provider returned HTTP " + str(status) + ".",
                              retryable=status in {408, 429, 500, 502, 503, 504}) from None
        except httpx.TransportError:
            raise ToolFailure("network_error", "Provider connection failed.", retryable=True) from None
        except ValueError:
            raise ToolFailure("invalid_result", "Provider did not return valid JSON.") from None

    def search(self, query):
        data = self.request("search", {"query": query, "max_results": 5,
                                      "search_depth": "basic", "include_answer": False})
        try:
            rows = SearchResponse.model_validate(data).results
        except ValidationError:
            raise ToolFailure("invalid_result", "Search returned no usable or valid results.") from None
        # Validation of ALL rows precedes ledger mutation.
        sources = [self.ledger.add_source(row.title, row.url, row.content[:12000]) for row in rows]
        return render_search_sources(sources,self.search_context_policy)

    def read(self, source_id):
        source = self.ledger.sources.get(source_id)
        if source is None:
            raise ToolFailure("unknown_source", "Unknown source ID; search first.")
        if source["kind"] == "local":
            return source
        data = self.request("extract", {"urls": [source["location"]]})
        try:
            row = ExtractResponse.model_validate(data).results[0]
        except ValidationError:
            raise ToolFailure("invalid_result", "No valid extract; saved search snippet remains usable.") from None
        if row.url != source["location"] or not row.raw_content.strip():
            raise ToolFailure("invalid_result", "Extract URL or content does not match the requested source.")
        return self.ledger.add_source(source["title"], source["location"], row.raw_content[:24000])

    def record(self, claim, source_id, snippet):
        try:
            return self.ledger.add_evidence(claim, source_id, snippet)
        except ValueError:
            raise ToolFailure("invalid_evidence", "Use a known source and a nonempty exact quote.") from None


def build_tools(ledger, api_key, *, runtime=None):
    runtime = runtime or ResearchTools(ledger, api_key)

    @tool
    def search_sources(query: str) -> dict:
        """Search web sources. Identical successful requests reuse this run's saved snapshots."""
        return runtime.executor.execute("search_sources", {"query": query})

    @tool
    def read_source(source_id: str) -> dict:
        """Read a known source. Return a fuller web snapshot or an explicitly imported document."""
        return runtime.executor.execute("read_source", {"source_id": source_id})

    @tool
    def record_evidence(claim: str, source_id: str, snippet: str) -> dict:
        """Save a claim with an exact quote. Copy the returned full evidence id for report citations."""
        return runtime.executor.execute("record_evidence", {
            "claim": claim, "source_id": source_id, "snippet": snippet})

    return [search_sources, read_source, record_evidence]

