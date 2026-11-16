"""Run-level metrics from durable events. Unknown token usage stays unknown."""
import json
import sqlite3
from collections import Counter


def run_metrics(directory):
    path = directory / "application.sqlite3"
    if not path.exists():
        return {"model_calls": None, "total_tokens": None, "tool_success_rate": None, "retry_count": None}
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        events = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM events ORDER BY id")]
        operations = list(connection.execute("SELECT id,attempts,result FROM dispatches"))
        counters = dict(connection.execute("SELECT name,value FROM counters"))
    finally:
        connection.close()
    return metrics_from_rows(events, operations, counters)


def metrics_from_rows(events, operations, counters):
    """Compute from one SQLite snapshot; missing/ambiguous request identity stays unknown."""
    started = [row for row in events if row["event"] == "model_started"]
    completed = [row for row in events if row["event"] == "model_completed"]
    starts = Counter(row.get("model_call_id") for row in started)
    ends = Counter(row.get("model_call_id") for row in completed)
    failed = {row.get("model_call_id") for row in events if row["event"] == "model_failed"}
    identities_valid = (bool(starts) and all(isinstance(key, str) and bool(key.strip()) for key in starts)
                        and starts == ends
                        and all(count == 1 for count in starts.values()) and not failed)
    positions = {row.get("model_call_id"): index for index, row in enumerate(events) if row["event"] == "model_started"}
    ordered = all(positions.get(row.get("model_call_id"), len(events)) < index
                  for index, row in enumerate(events) if row["event"] == "model_completed")
    known = identities_valid and ordered and all(
        isinstance(row.get("usage"), dict) and all(type(row["usage"].get(key)) is int and row["usage"][key] >= 0
        for key in ("input_tokens", "output_tokens", "total_tokens")) for row in completed)
    tokens = {key: sum(row["usage"][key] for row in completed) if known else None
              for key in ("input_tokens", "output_tokens", "total_tokens")}
    op_ids = {row[0] for row in operations}
    extra_rejections = set()
    for index, row in enumerate(events):
        if row["event"] == "rejected" and "tool" in row and row.get("call_id") not in op_ids:
            extra_rejections.add(row.get("call_id", f"unreserved-{index}"))
    successes = sum(result is not None and "error" not in json.loads(result) for _, _, result in operations)
    denominator = len(operations) + len(extra_rejections)
    return dict(**tokens, model_calls=len(started), model_completed=len(completed),
                token_usage_complete=known, tool_calls=counters["calls"], provider_requests=counters["requests"],
                tool_successes=successes, tool_outcomes=denominator,
                tool_success_rate=successes/denominator if denominator else None,
                retry_count=sum(max(0, attempts-1) for _, attempts, _ in operations),
                recovery_replays=sum(row["event"] == "replayed" for row in events),
                retry_metric_scope="tool executions beyond first attempt; includes recovery reexecution",
                estimated_fee=None)

