"""Durable admission quota for model requests. Uncertain requests are never refunded."""
import json
import threading
import time
from collections import Counter
from langchain_core.callbacks import BaseCallbackHandler
from .ledger import now
from .storage import RecoveryError

VERSION = "model-call-budget-v1"


class ModelBudgetExceeded(RecoveryError):
    pass


class VerificationTailProtected(ModelBudgetExceeded):
    """Generation admission stopped while the final verifier still has one slot."""
    pass


def validate_limit(maximum):
    if type(maximum) is not int or not 0 <= maximum <= 1000:
        raise RecoveryError("Model call limit must be an integer from 0 to 1000.")


def budget_snapshot(connection):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_call_policy'").fetchone() is None:
        return None
    policy = connection.execute("SELECT max_calls FROM model_call_policy WHERE singleton=1").fetchone()
    if policy is None:
        raise RecoveryError("Persisted model call policy is incomplete.")
    records = list(connection.execute("SELECT state,role FROM model_call_reservations"))
    result = dict(limit=policy[0], reserved=len(records), remaining=max(0, policy[0]-len(records)),
                states=dict(Counter(row[0] for row in records)), roles=dict(Counter(row[1] for row in records)))
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_verification_holds'").fetchone():
        holds = list(connection.execute("SELECT id,state,call_id FROM model_verification_holds"))
        if holds:
            active = sum(row[1] == 'active' for row in holds)
            result.update(verification_slots_held=active, generation_remaining=max(0,result['remaining']-active),
                          verification_holds=[dict(id=row[0],state=row[1],call_id=row[2]) for row in holds])
    return result


class ModelCallBudget:
    def __init__(self, store, maximum):
        validate_limit(maximum)
        self.store, self.maximum = store, maximum
        with store.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS model_call_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1), max_calls INTEGER NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS model_call_reservations (call_id TEXT PRIMARY KEY, role TEXT NOT NULL, state TEXT NOT NULL, usage TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS model_verification_holds (id TEXT PRIMARY KEY, state TEXT NOT NULL, call_id TEXT)")
            policy = connection.execute("SELECT max_calls FROM model_call_policy WHERE singleton=1").fetchone()
            if policy is None:
                if connection.execute("SELECT COUNT(*) FROM model_call_reservations").fetchone()[0] or any(
                        json.loads(row[0]).get("event") == "model_started" for row in connection.execute("SELECT payload FROM events")):
                    raise RecoveryError("Cannot enable a new model quota after unbudgeted model requests.")
                connection.execute("INSERT INTO model_call_policy VALUES (1,?)", (maximum,))
            elif policy[0] != maximum:
                raise RecoveryError("Model call limit differs from the persisted policy.")

    def hold_verification(self, hold_id):
        if not isinstance(hold_id,str) or not hold_id.strip():
            raise RecoveryError('Invalid verification hold identity.')
        with self.store.transaction() as connection:
            row = connection.execute("SELECT state FROM model_verification_holds WHERE id=?",(hold_id,)).fetchone()
            if row is not None:
                return row[0] == 'active'
            if connection.execute("SELECT 1 FROM model_verification_holds WHERE state='active'").fetchone():
                raise RecoveryError('Another verification hold is already active.')
            used = connection.execute("SELECT COUNT(*) FROM model_call_reservations").fetchone()[0]
            if self.maximum-used < 2:  # At least one generation AND one verification request.
                return False
            connection.execute("INSERT INTO model_verification_holds VALUES (?,'active',NULL)",(hold_id,))
        return True

    def release_verification(self, hold_id):
        # Release an unspent earmark, never a paid/uncertain model reservation.
        with self.store.transaction() as connection:
            connection.execute("UPDATE model_verification_holds SET state='released' WHERE id=? AND state='active'",(hold_id,))

    def admit(self, call_id, role):
        if not isinstance(call_id, str) or not call_id.strip() or role not in {"generation", "context_summarization", "repair_verification"}:
            raise RecoveryError("Invalid model request identity or role.")
        try:
            with self.store.transaction() as connection:
                if connection.execute("SELECT 1 FROM model_call_reservations WHERE call_id=?", (call_id,)).fetchone():
                    raise RecoveryError("Model request ID was already reserved; remote requests cannot be replayed by ID.")
                used = connection.execute("SELECT COUNT(*) FROM model_call_reservations").fetchone()[0]
                holds = list(connection.execute("SELECT id FROM model_verification_holds WHERE state='active'"))
                if used >= self.maximum:
                    raise ModelBudgetExceeded("Model call budget exhausted; recovery does not reset or increase the saved limit.")
                if role != 'repair_verification' and used+len(holds) >= self.maximum:
                    raise VerificationTailProtected('Generation stopped to preserve the final candidate-verification slot.')
                connection.execute("INSERT INTO model_call_reservations VALUES (?,?,'reserved',NULL)", (call_id, role))
                if role == 'repair_verification' and holds:
                    connection.execute("UPDATE model_verification_holds SET state='consumed',call_id=? WHERE id=?",(call_id,holds[0][0]))
        except ModelBudgetExceeded as error:
            counts = self.store.counters()
            self.store.add_event(dict(timestamp=now(), event="model_budget_denied", model_call_id=call_id,
                model_role=role, error_code="verification_tail_protected" if isinstance(error,VerificationTailProtected) else "model_call_budget_exceeded", budget_used=used, budget_limit=self.maximum,
                calls_used=counts["calls"], provider_requests_used=counts["requests"]))
            raise

    def finish(self, call_id, state, usage=None):
        if state not in {"completed", "failed"}:
            raise RecoveryError("Invalid model reservation outcome.")
        valid_usage = isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
            for key in ("input_tokens", "output_tokens", "total_tokens"))
        safe_usage = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")} if valid_usage else None
        with self.store.transaction() as connection:
            row = connection.execute("SELECT state FROM model_call_reservations WHERE call_id=?", (call_id,)).fetchone()
            if row is None or row[0] != "reserved":
                raise RecoveryError("Only a reserved model request may receive one terminal outcome.")
            connection.execute("UPDATE model_call_reservations SET state=?,usage=? WHERE call_id=?",
                               (state, json.dumps(safe_usage), call_id))


def model_budget_for(ledger):
    path = ledger.directory / "run.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    policy = manifest.get("model_policy")
    with ledger.store.lock:
        persisted = budget_snapshot(ledger.store.conn)
    if policy is None:
        if persisted is not None:
            raise RecoveryError("Persisted model budget cannot be disabled by removing its manifest policy.")
        return None
    if (not isinstance(policy, dict) or set(policy) != {"version", "max_calls"} or policy["version"] != VERSION):
        raise RecoveryError("Unsupported model call policy.")
    return ModelCallBudget(ledger.store, policy["max_calls"])


class BudgetCallbacks(BaseCallbackHandler):
    """Instrument the shared model, including framework-owned summarization calls."""
    raise_error = True
    run_inline = True

    def __init__(self, ledger, budget):
        self.ledger, self.budget = ledger, budget
        self.active = {}
        self.lock = threading.RLock()

    def event(self, name, call_id, role, **fields):
        counts = self.ledger.store.counters()
        self.ledger.store.add_event(dict(timestamp=now(), event=name, model_call_id=call_id, model_role=role,
            calls_used=counts["calls"], provider_requests_used=counts["requests"], **fields))

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        call_id = str(run_id)
        metadata = kwargs.get("metadata") or {}
        role = "context_summarization" if metadata.get("lc_source") == "summarization" else "generation"
        if self.budget is not None:
            self.budget.admit(call_id, role)
        with self.lock:
            self.active[call_id] = (role, time.monotonic())
        self.event("model_started", call_id, role)

    def on_llm_end(self, response, *, run_id, **kwargs):
        call_id = str(run_id)
        with self.lock:
            active = self.active.pop(call_id, None)
        if active is None:
            return
        role, started = active
        rows = [getattr(generation.message, "usage_metadata", None)
                for group in response.generations for generation in group if hasattr(generation, "message")]
        valid = bool(rows) and all(isinstance(row, dict) and all(type(row.get(key)) is int and row[key] >= 0
            for key in ("input_tokens", "output_tokens", "total_tokens")) for row in rows)
        usage = {key: sum(row[key] for row in rows) for key in ("input_tokens", "output_tokens", "total_tokens")} if valid else None
        if self.budget is not None:
            self.budget.finish(call_id, "completed", usage)
        self.event("model_completed", call_id, role, usage=usage, elapsed_ms=round((time.monotonic()-started)*1000))

    def on_llm_error(self, error, *, run_id, **kwargs):
        call_id = str(run_id)
        with self.lock:
            active = self.active.pop(call_id, None)
        if active is None:
            return
        role, started = active
        if self.budget is not None:
            self.budget.finish(call_id, "failed")
        self.event("model_failed", call_id, role, error_type=type(error).__name__, elapsed_ms=round((time.monotonic()-started)*1000))
