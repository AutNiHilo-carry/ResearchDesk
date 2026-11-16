"""Application source presentation policy; framework context primitives stay unchanged."""
import json
from .verification import VerificationError, digest
from .storage import RecoveryError

POLICY_VERSION = "imported-sources-v1"
STRATEGIES = ("full", "catalog")


def prepare_input(question, documents, strategy="full"):
    if strategy not in STRATEGIES or (strategy == "catalog" and not documents):
        raise VerificationError("Catalog context requires imported sources; strategy must be full or catalog.")
    entries = documents
    if strategy == "catalog":
        entries = [dict(id=row["id"], title=row["title"], kind=row["kind"], sha256=row["sha256"],
                        content_chars=len(row["content"]), read_via="read_source") for row in documents]
    initial = {"messages": [{"role": "user", "content":
        (question or "") + "\nImported sources:\n" + json.dumps(entries, ensure_ascii=False)}]}
    encoded = json.dumps(initial, ensure_ascii=False)
    return initial, dict(strategy=strategy, policy_version=POLICY_VERSION, input_sha256=digest(encoded),
        initial_chars=len(encoded), source_count=len(documents), source_content_chars=sum(len(row["content"]) for row in documents))


def verify_saved_input(initial, policy):
    if policy is not None and (policy.get("policy_version") != POLICY_VERSION
            or policy.get("strategy") not in STRATEGIES
            or policy.get("input_sha256") != digest(json.dumps(initial, ensure_ascii=False))):
        raise RecoveryError("Saved context policy or prepared input changed.")
