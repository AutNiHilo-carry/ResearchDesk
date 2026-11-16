"""Durable application receipts and budgets above Deep Agents / LangGraph."""
import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4
from pydantic import BaseModel, ValidationError
from .ledger import now
from .storage import RecoveryStore, RecoveryError


class ToolFailure(Exception):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass(frozen=True)
class ToolLimits:
    max_calls: int = 20
    max_provider_requests: int = 8
    timeout_seconds: float = 30
    max_retries: int = 1
    max_duration_seconds: float = 180

    def __post_init__(self):
        if self.max_calls < 0 or self.max_provider_requests < 0:
            raise ValueError("Call and request budgets must be nonnegative")
        if (not math.isfinite(self.timeout_seconds) or not math.isfinite(self.max_duration_seconds)
                or self.timeout_seconds <= 0 or self.max_duration_seconds <= 0):
            raise ValueError("Timeouts must be positive")
        if not 0 <= self.max_retries <= 3:
            raise ValueError("Retries must be between zero and three")


@dataclass(frozen=True)
class ToolSpec:
    schema: type[BaseModel]
    run: Callable
    read_only: bool
    cacheable: bool = False


class ToolExecutor:
    def __init__(self, directory, specs, limits=None, allowed_tools=None, *,
                 clock=time.monotonic, sleeper=time.sleep, store=None):
        self.directory, self.specs = directory, specs
        self.limits = limits or ToolLimits()
        self.allowed_tools = frozenset(specs if allowed_tools is None else allowed_tools)
        self.clock, self.sleeper, self.started = clock, sleeper, clock()
        self.store = store or RecoveryStore(directory)
        self.lock = threading.RLock()
        self._refresh()

    def _refresh(self):
        counts = self.store.counters()
        self.calls, self.provider_requests = counts["calls"], counts["requests"]

    def audit(self, event, **fields):
        self._refresh()
        self.store.add_event(dict(timestamp=now(), event=event, calls_used=self.calls,
                                  provider_requests_used=self.provider_requests, **fields))

    def remaining(self):
        # Per-active-process admission window. Persisted count budgets never reset.
        return self.limits.max_duration_seconds - (self.clock() - self.started)

    def provider_timeout(self):
        remaining = self.remaining()
        if remaining <= 0:
            raise ToolFailure("deadline_exceeded", "Tool execution admission deadline exceeded.")
        if not self.store.reserve_request(self.limits.max_provider_requests):
            raise ToolFailure("request_budget_exceeded", "External request budget exhausted.")
        self._refresh()
        return min(self.limits.timeout_seconds, remaining)

    def denied(self, name):
        self.audit("rejected", tool=name, error_code="permission_denied")
        return {"error": "Tool is not permitted.", "error_code": "permission_denied"}

    def _finish(self, fields, result, success):
        # Commit before returning ToolMessage; replay can close the graph checkpoint gap.
        self.store.finish(fields["call_id"], fields["fingerprint"], result, success)
        return result

    def execute(self, name, arguments, *, operation_id=None):
        with self.lock:
            started = self.clock()
            call_id = operation_id or uuid4().hex
            if name not in self.specs or name not in self.allowed_tools:
                return self.denied(name)
            fingerprint = hashlib.sha256(json.dumps(
                [name, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            fields = dict(tool=name, call_id=call_id, fingerprint=fingerprint)
            try:
                dispatch, replay = self.store.begin_call(call_id, name, fingerprint, self.limits.max_calls)
            except RecoveryError:
                self.audit("rejected", **fields, error_code="operation_id_conflict")
                return {"error": "Operation ID has different arguments.", "error_code": "operation_id_conflict"}
            self._refresh()
            if dispatch is None:
                self.audit("rejected", **fields, error_code="call_budget_exceeded")
                return {"error": "Tool call budget exhausted.", "error_code": "call_budget_exceeded"}
            if dispatch["result"] is not None:
                self.audit("replayed", **fields)
                return json.loads(dispatch["result"])

            def fail(code, message):
                self.audit("rejected", **fields, error_code=code)
                return self._finish(fields, {"error": message, "error_code": code}, False)

            spec = self.specs[name]
            try:
                args = spec.schema.model_validate(arguments).model_dump()
            except ValidationError:
                return fail("invalid_arguments", "Arguments do not match the tool schema.")
            cached = self.store.cached(fingerprint) if spec.cacheable or replay else None
            if cached is not None:
                self.audit("cache_hit", **fields, elapsed_ms=round((self.clock()-started)*1000))
                return self._finish(fields, cached, True)
            # Local evidence writes have stable content IDs; unknown side effects do not.
            if replay and not spec.read_only and name != "record_evidence":
                return fail("ambiguous_operation", "Pending side effect requires manual reconciliation.")
            if self.remaining() <= 0:
                return fail("deadline_exceeded", "Tool execution admission deadline exceeded.")
            attempts = dispatch["attempts"]
            maximum = self.limits.max_retries + 1 if spec.read_only else 1
            if replay and name == "record_evidence":
                maximum = attempts + 1  # Recovery replay is safe; automatic write retry stays off.
            if attempts >= maximum:
                return fail("retry_budget_exceeded", "Previous attempt outcome is uncertain; retry budget exhausted.")
            self.audit("started", **fields, recovery_replay=replay)
            while attempts < maximum:
                self.store.attempt(call_id)
                attempts += 1
                try:
                    result = spec.run(**args)
                    if not isinstance(result, dict) or "error" in result:
                        raise ToolFailure("invalid_result", "Adapter returned an invalid result.")
                except ToolFailure as error:
                    code, message = error.code, error.message
                    retry = (spec.read_only and error.retryable and attempts < maximum
                             and self.store.counters()["requests"] < self.limits.max_provider_requests
                             and self.remaining() > 0)
                except Exception:
                    code, message, retry = "execution_failed", "Tool execution failed.", False
                else:
                    # Persistence errors deliberately propagate: never misclassify a committed
                    # effect as an adapter failure or automatically repeat it.
                    result = self._finish(fields, result, True)
                    self.audit("completed", **fields, retry_count=max(0, attempts-1),
                               elapsed_ms=round((self.clock()-started)*1000))
                    return result
                self.audit("attempt_failed", **fields, attempt=attempts, error_code=code, will_retry=retry)
                if not retry:
                    self.audit("failed", **fields, error_code=code, retry_count=max(0, attempts-1),
                               elapsed_ms=round((self.clock()-started)*1000))
                    return self._finish(fields, {"error": message, "error_code": code}, False)
                self.sleeper(min(0.25 * (2 ** (attempts-1)), max(0, self.remaining())))
                if self.remaining() <= 0:
                    return fail("deadline_exceeded", "Tool execution admission deadline exceeded.")

