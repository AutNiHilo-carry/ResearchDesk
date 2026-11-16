"""Format-only citation handling; never invent IDs or establish semantic support."""
import hashlib
import json
import re
from .storage import RecoveryError, atomic_text

VERSION = "citation-brackets-v1"
# One capture group and original offsets are part of the consumers' contract.
CITATION = re.compile(r"[\[［](E[^\]］\[［\s]*)[\]］]")
START = re.compile(r"[\[［][EＥ]")


class CitationSyntaxError(ValueError):
    pass


def validate_citation_syntax(text):
    matches = {match.start(): match for match in CITATION.finditer(text)}
    for start in START.finditer(text):
        match = matches.get(start.start())
        if match is None or (match[0][0], match[0][-1]) not in {("[", "]"), ("［", "］")}:
            raise CitationSyntaxError("Malformed evidence citation delimiters.")


def normalize_citation_delimiters(text):
    validate_citation_syntax(text)
    return CITATION.sub(lambda match: "[" + match[1] + "]", text)


def archive_and_normalize(directory, text):
    """Archive the exact save/repair input before validation, including failures."""
    raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    archive = directory / "report-inputs"
    archive.mkdir(exist_ok=True)
    raw_path = archive / (raw_hash + ".md")
    if raw_path.exists():
        if raw_path.read_bytes() != text.encode("utf-8"):
            raise RecoveryError("Archived report input changed.")
    else:
        atomic_text(raw_path, text)
    normalized = normalize_citation_delimiters(text)
    receipt = dict(version=VERSION, raw_sha256=raw_hash,
        normalized_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        changed_references=sum(match[0].startswith("［") for match in CITATION.finditer(text)),
        scope="format_only_not_id_or_semantic_validation")
    atomic_text(archive / (raw_hash + ".json"), json.dumps(receipt, ensure_ascii=False, indent=2))
    return normalized
