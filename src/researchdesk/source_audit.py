"""Read-only citation provenance from durable tool results, never semantic approval."""
import argparse
import json
import sqlite3
from pathlib import Path
from .verification import CITATION, digest, evidence_context


def audit(directory):
    directory = Path(directory).resolve()
    draft = directory / "draft.md"
    body = (draft if draft.is_file() else directory / "report.md").read_text(encoding="utf-8")
    body = body.split("\n## Evidence Ledger\n", 1)[0]
    # A single transaction binds the records and dispatch receipts to one snapshot.
    with sqlite3.connect((directory / "application.sqlite3").as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("BEGIN")
        if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Unsupported recovery database version")
        sources = {row["id"]: row for (raw,) in conn.execute("SELECT payload FROM records WHERE kind='sources'")
                   for row in [json.loads(raw)]}
        evidence = {row["id"]: row for (raw,) in conn.execute("SELECT payload FROM records WHERE kind='evidence'")
                    for row in [json.loads(raw)]}
        receipts = list(conn.execute("SELECT tool,result FROM dispatches WHERE result IS NOT NULL"))
    origins = {}
    for tool, raw in receipts:
        if tool not in {"search_sources", "read_source"}:
            continue
        result = json.loads(raw)
        if not isinstance(result, dict) or "error" in result:
            continue
        rows = result.get("sources", []) if tool == "search_sources" else [result]
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = row.get("id")
            source = sources.get(sid) if isinstance(sid, str) else None
            # Metadata-only / preview receipts cannot prove a complete body match.
            if source is None or not isinstance(row.get("content"), str):
                continue
            expected = digest(source["location"] + "\0" + source["content"])
            if (sid != "S" + expected[:16] or source.get("sha256") != expected
                    or row.get("location") != source["location"] or row["content"] != source["content"]):
                continue
            origins.setdefault(sid, set()).add(tool)
    rows = []
    for eid in sorted(set(CITATION.findall(body))):
        context = evidence_context(eid, evidence, sources)
        sid = evidence.get(eid, {}).get("source_id")
        tools = sorted(origins.get(sid, set())) if context["integrity"] == "valid" else []
        origin = ("both" if len(tools) == 2 else "extracted_body" if tools == ["read_source"]
                  else "search_snippet" if tools == ["search_sources"] else "unknown")
        rows.append(dict(evidence_id=eid, source_id=sid, integrity=context["integrity"],
                         location=context.get("location"), observed_origin=origin, matching_tools=tools))
    return dict(version="citation-provenance-v1", body_sha256=digest(body), citations=rows,
        scope="saved snapshot matched to durable tool result; not chapter, authority or semantic verification",
        preview_receipts="not classified without full snapshot content", semantic_assessment="not_performed",
        report_consistency="body is outside database transaction; audit only stopped runs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.suffix != ".json" or args.output.exists():
        parser.error("Output must be a new .json file; existing artifacts cannot be overwritten")
    result = audit(args.directory)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(f"Citation provenance: {args.output}")


if __name__ == "__main__":
    main()
