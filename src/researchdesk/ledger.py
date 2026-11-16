"""Append-only evidence records. These records are NOT runtime checkpoints."""
from .citations import CITATION, CitationSyntaxError, archive_and_normalize, normalize_citation_delimiters
import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from .storage import RecoveryStore, atomic_text


def now():
    return datetime.now(timezone.utc).isoformat()


class CitationValidationError(ValueError):
    """A local report validation failure, safe to show without provider payloads."""


class Ledger:
    def __init__(self, directory: Path, *, resume=False):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=resume)
        self.store = RecoveryStore(directory, resume=resume)
        self.sources = {row["id"]: row for row in self.store.records("sources")}
        self.evidence = {row["id"]: row for row in self.store.records("evidence")}
        self._lock = threading.RLock()
        if resume:
            self.store.export()  # Repair missing/torn JSONL exports from authoritative SQLite.

    def _append(self, filename, record):
        self.store.save_record(filename.removesuffix(".jsonl"), record)
        with (self.directory / filename).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def add_source(self, title, location, content, kind="web"):
        if not content.strip():
            raise ValueError("Source has no usable text")
        # Include content: refreshed versions of a URL are distinct immutable snapshots.
        digest = hashlib.sha256((location + "\0" + content).encode()).hexdigest()
        sid = "S" + digest[:16]
        with self._lock:
            if sid not in self.sources:
                record = dict(id=sid, title=title, location=location, content=content,
                              kind=kind, retrieved_at=now(), sha256=digest)
                self._append("sources.jsonl", record)
                self.sources[sid] = record
        return self.sources[sid]

    def add_evidence(self, claim, source_id, snippet):
        if not claim.strip() or not snippet.strip():
            raise ValueError("Claim and snippet must not be empty")
        with self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise ValueError("Unknown source ID")
            if snippet not in source["content"]:
                raise ValueError("Snippet must exactly match the saved source text")
            digest = hashlib.sha256((claim + "\0" + source_id + "\0" + snippet).encode()).hexdigest()
            eid = "E" + digest[:16]
            if eid not in self.evidence:
                record = dict(id=eid, claim=claim, source_id=source_id, snippet=snippet,
                              location=source["location"], retrieved_at=source["retrieved_at"],
                              recorded_at=now(), verification="exact_quote_only")
                self._append("evidence.jsonl", record)
                self.evidence[eid] = record
        return self.evidence[eid]

    def validate_report(self, report):
        # Check every bracketed E reference, including malformed / invented IDs.
        try:
            report = normalize_citation_delimiters(report)
        except CitationSyntaxError as error:
            raise CitationValidationError(str(error)) from error
        cited = set(CITATION.findall(report))
        if not self.evidence or not cited:
            raise CitationValidationError("Report needs at least one recorded evidence citation")
        unknown = cited - self.evidence.keys()
        if unknown or any(not re.fullmatch(r"E[0-9a-f]{16}", eid) for eid in cited):
            raise CitationValidationError("Unknown citations: " + ", ".join(sorted(unknown)))
        return sorted(cited)

    def save_report(self, report):
        atomic_text(self.directory / "draft.md", report)
        try:
            report = archive_and_normalize(self.directory, report)
        except CitationSyntaxError as error:
            raise CitationValidationError(str(error)) from error
        cited = self.validate_report(report)
        atomic_text(self.directory / "draft.md", report)
        appendix = ["", "## Evidence Ledger", "",
                    "引用检查仅验证证据编号及原文匹配，不代表结论获得语义验证。", ""]
        for eid in cited:
            item = self.evidence[eid]
            appendix += [f"- [{eid}] {item['claim']}",
                         f"  - Source: {item['location']}",
                         f"  - Retrieved: {item['retrieved_at']}",
                         f"  - Quote: {item['snippet']}"]
        path = self.directory / "report.md"
        atomic_text(path, report + "\n" + "\n".join(appendix) + "\n")
        return path

