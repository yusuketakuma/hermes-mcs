"""Durable execution guards for the semantic worker.

The semantic pipeline can spend several seconds outside SQLite while it is
calling Jev or the local model.  A job row may be re-seeded during that time,
and the configuration may be switched off.  This module keeps the small
amount of identity and transition logic needed to make those boundaries
conditional without changing the generic fetch-job contract.
"""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
import math
import socket
import time
import urllib.error
import uuid
from contextlib import suppress

from mcs_requests import payload_hash
from mcs_util import loads_dict


DEFAULT_ATTEMPT_LIMIT = 6
MAX_SQL_INTEGER = 2**63 - 1


class RuntimeGuardError(RuntimeError):
    """An execution boundary is no longer valid for this worker."""

    def __init__(self, stage: str = ""):
        super().__init__(stage or "semantic_runtime_guard")
        self.stage = stage


class RuntimeStale(RuntimeGuardError):
    """The job/config/source generation changed while work was in flight."""


class RuntimeOff(RuntimeGuardError):
    """Semantic execution was switched off before a side effect."""


class RuntimeBudget(RuntimeGuardError):
    """The tick or per-job execution budget was exhausted."""


class RuntimeBudgetShort(RuntimeBudget):
    """Earlier model calls in this pass completed, but too little budget
    is left to START the next one (2026-09-30: a v2 fact document is one
    ~300 s generation, and a pass needs several). The work already
    persisted stands; the job is deferred, not charged an attempt — an
    executed call that overran its deadline still raises RuntimeBudget."""


class LLMNotSent(RuntimeGuardError):
    """The local-model request was never dispatched — admission held or
    deferred it, or the backend refused the connection. Nothing was
    consumed, so the job waits instead of spending a retry attempt."""


class LLMRejected(RuntimeError):
    """The backend definitively refused the request itself — e.g. the
    prompt exceeds the slot's context window. Deterministic: retrying
    the same input can never succeed, so the caller maps it to a
    terminal outcome instead of burning retry attempts. NOT a
    RuntimeGuardError: a refusal must not abort the pass, and must not
    be confused with LLMNotSent (which waits because nothing ran)."""


def parse_payload(row: dict | object) -> dict:
    raw = row.get("payload") if isinstance(row, dict) else row["payload"]
    return loads_dict(raw) or {}


def attempt_limit(payload: dict) -> int:
    """Return the durable retry ceiling for one semantic payload.

    The manual retry budget is deliberately payload-local so it survives a
    process restart and cannot widen a newly seeded input.  Invalid or legacy
    values use the original six-attempt ceiling.
    """
    if not isinstance(payload, dict):
        return DEFAULT_ATTEMPT_LIMIT
    value = payload.get("manual_attempt_limit")
    if (type(value) is int
            and DEFAULT_ATTEMPT_LIMIT <= value <= MAX_SQL_INTEGER):
        return value
    return DEFAULT_ATTEMPT_LIMIT


def payload_generation(payload: dict) -> str:
    """Return the durable input-generation token.

    Older rows do not have a token.  Their canonical payload digest is a
    conservative substitute: any payload edit invalidates the old worker.
    """
    value = payload.get("generation")
    if isinstance(value, str) and value:
        return value
    return payload_hash(payload)


def config_generation(cfg: dict) -> str:
    """Stable generation for the complete config snapshot."""
    return payload_hash(cfg if isinstance(cfg, dict) else {})


@dataclass(frozen=True)
class JobToken:
    job_id: int
    kind: str
    project_id: int
    message_id: int
    state: str
    attempts: int
    payload_raw: str
    payload_digest: str
    generation: str
    source_generation: str | None

    @classmethod
    def from_row(cls, row: dict | object) -> JobToken:
        def get(name, default=None):
            return row.get(name, default) if isinstance(row, dict) \
                else row[name]

        payload_raw = get("payload") or "{}"
        payload = parse_payload({"payload": payload_raw})
        return cls(
            job_id=int(get("job_id")), kind=str(get("kind", "")),
            project_id=int(get("project_id")),
            message_id=int(get("message_id", 0)),
            state=str(get("state", "pending")),
            attempts=int(get("attempts", 0) or 0),
            payload_raw=payload_raw,
            payload_digest=payload_hash(payload),
            generation=payload_generation(payload),
            source_generation=(payload.get("source_generation")
                               if isinstance(payload.get("source_generation"),
                                              str)
                               else None),
        )


def job_matches(ledger, token: JobToken) -> bool:
    """Check the complete identity captured before external work."""
    row = ledger.db.execute(
        "SELECT job_id,kind,project_id,message_id,state,attempts,payload "
        "FROM fetch_jobs WHERE job_id=?", (token.job_id,)).fetchone()
    if row is None:
        return False
    current = JobToken.from_row(row)
    return (current.kind == token.kind
            and current.project_id == token.project_id
            and current.message_id == token.message_id
            and current.state == token.state
            and current.attempts == token.attempts
            and current.generation == token.generation
            and current.payload_digest == token.payload_digest)


def _where_args(token: JobToken) -> tuple:
    # Comparing the serialized payload as well as its parsed generation keeps
    # legacy rows safe when a worker rewrites a payload without a generation
    # field. New semantic rows always carry a UUID generation.
    return (token.job_id, token.kind, token.project_id, token.message_id,
            token.state, token.attempts, token.payload_raw,
            token.payload_raw)


# payload is matched exactly, with one rescue: a stored NULL/empty payload
# (only reachable through out-of-band DB edits — every in-repo writer
# stores json.dumps output) normalizes to "{}" in JobToken, so a strict
# `payload=?` CAS could never fire and the row would wedge pending
# forever (S-6). The rescue only applies when the token itself carries
# the normalized "{}" — real payloads still require an exact match.
_PAYLOAD_MATCH = ("(payload=? OR (COALESCE(payload,'')='' AND ?='{}'))")


def transition_tx(ledger, token: JobToken, action: str,
                  retry_in: float = 300,
                  max_attempts: int = DEFAULT_ATTEMPT_LIMIT) -> bool:
    """Conditionally transition a semantic row, without committing.

    Callers may use this inside the same transaction as result artifacts.
    ``False`` means another generation owns the row; no write is made.
    """
    now = time.time()
    args = _where_args(token)
    if action == "done":
        cur = ledger.db.execute(
            "UPDATE fetch_jobs SET state='done',updated_at=? "
            "WHERE job_id=? AND kind=? AND project_id=? AND message_id=? "
            "AND state=? AND attempts=? AND " + _PAYLOAD_MATCH,
            (now, *args))
    elif action == "defer":
        cur = ledger.db.execute(
            "UPDATE fetch_jobs SET next_try=?,updated_at=? "
            "WHERE job_id=? AND kind=? AND project_id=? AND message_id=? "
            "AND state=? AND attempts=? AND " + _PAYLOAD_MATCH,
            (now + max(0.0, retry_in), now, *args))
    elif action == "retry":
        cur = ledger.db.execute(
            "UPDATE fetch_jobs SET attempts=attempts+1,next_try=?,"
            "state=CASE WHEN attempts+1>=? THEN 'failed' ELSE state END,"
            "updated_at=? WHERE job_id=? AND kind=? AND project_id=? "
            "AND message_id=? AND state=? AND attempts=? AND "
            + _PAYLOAD_MATCH,
            (now + max(0.0, retry_in), max(1, int(max_attempts)), now,
             *args))
    elif action == "failed":
        cur = ledger.db.execute(
            "UPDATE fetch_jobs SET state='failed',updated_at=? "
            "WHERE job_id=? AND kind=? AND project_id=? "
            "AND message_id=? AND state=? AND attempts=? AND "
            + _PAYLOAD_MATCH,
            (now, *args))
    else:
        raise ValueError(f"unknown semantic transition: {action}")
    return cur.rowcount == 1


def transition(ledger, token: JobToken, action: str,
               retry_in: float = 300,
               max_attempts: int = DEFAULT_ATTEMPT_LIMIT) -> bool:
    with ledger.db:
        return transition_tx(ledger, token, action, retry_in, max_attempts)


_CIRCUIT_ARTIFACT_KIND = "semantic_circuit"
_CIRCUIT_FAILURE_LIMIT = 3
_CIRCUIT_COOLDOWN_SECONDS = 300.0
_CIRCUIT_RETRYABLE_KINDS = frozenset({"rate_limited", "transport", "timeout"})
_CIRCUIT_FAILURE_CLASSES = _CIRCUIT_RETRYABLE_KINDS | {
    "http_402", "http_429", "http_529", "http_5xx"
}


def _circuit_now(now) -> float:
    if now is None:
        return time.time()
    try:
        valid = (type(now) in (int, float) and math.isfinite(float(now)))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("circuit_time_invalid")
    return float(now)


def _circuit_status(error) -> int:
    status = getattr(error, "status", None)
    if status is None:
        status = getattr(error, "code", None)
    return status if type(status) is int and 100 <= status <= 599 else 0


def _circuit_failure_class(error) -> str | None:
    """Return a bounded failure label, never the error/detail text."""
    if error is None:
        return None
    if getattr(error, "kind", "") == "payment_required":
        # not retryable within a job, but every call fails until the
        # account is settled: open the circuit like a persistent 5xx
        return "http_402"
    retryable = getattr(error, "retryable", None)
    if retryable is not None and retryable is not True:
        return None
    status = _circuit_status(error)
    if status == 429:
        return "http_429"
    if status == 529:
        return "http_529"
    if 500 <= status <= 599:
        return "http_5xx"
    if status:
        # An HTTP status outside the retryable set is terminal even when a
        # caller attached a misleading ``transport`` kind.
        return None
    kind = getattr(error, "kind", None)
    if kind == "rate_limited":
        return "http_429"
    if kind in ("transport", "timeout"):
        return kind
    if isinstance(error, TimeoutError | socket.timeout):
        return "timeout"
    if isinstance(error, OSError | urllib.error.URLError):
        return "transport"
    return None


def _circuit_state(ledger, now=None) -> dict:
    row = ledger.db.execute(
        "SELECT content,created_at FROM artifacts WHERE kind=? AND project_id IS NULL "
        "AND message_id IS NULL ORDER BY artifact_id DESC LIMIT 1",
        (_CIRCUIT_ARTIFACT_KIND,)).fetchone()
    if row is None:
        return {"consecutive_failures": 0, "open_until": 0.0,
                "failure_class": None}
    try:
        state = json.loads(row["content"] or "{}")
        count = state.get("consecutive_failures")
        open_until = state.get("open_until", 0.0)
        failure_class = state.get("failure_class")
        if (not isinstance(state, dict) or type(count) is not int or count < 0
                or type(open_until) not in (int, float)
                or not math.isfinite(float(open_until))
                or (failure_class is not None
                    and failure_class not in _CIRCUIT_FAILURE_CLASSES)):
            raise ValueError
        return {"consecutive_failures": min(count, _CIRCUIT_FAILURE_LIMIT),
                "open_until": float(open_until),
                "failure_class": failure_class}
    except (AttributeError, OverflowError, TypeError, ValueError, RecursionError,
            json.JSONDecodeError):
        # A malformed circuit record is fail-closed for one bounded cooldown.
        # Its artifact timestamp survives a process restart, so corruption
        # cannot turn into a permanent outage and can be healed by the next
        # successful Jev call after the cooldown.
        try:
            created_at = float(row["created_at"])
            if not math.isfinite(created_at):
                raise ValueError
        except (KeyError, OverflowError, TypeError, ValueError):
            created_at = _circuit_now(now)
        return {"consecutive_failures": _CIRCUIT_FAILURE_LIMIT,
                "open_until": created_at + _CIRCUIT_COOLDOWN_SECONDS,
                "failure_class": "malformed"}


def _circuit_is_open(state: dict, now: float) -> bool:
    return float(state.get("open_until", 0.0)) > now


def _write_circuit_state(ledger, state: dict, now: float) -> None:
    payload = {
        "consecutive_failures": int(state["consecutive_failures"]),
        "open_until": float(state["open_until"]),
        "failure_class": state.get("failure_class"),
        "updated_at": now,
    }
    ledger.artifact_add(
        _CIRCUIT_ARTIFACT_KIND,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                   allow_nan=False),
        model="jev",
        meta={"state": "open" if payload["open_until"] > now else "closed"},
    )


def circuit_open(ledger, now=None) -> bool:
    """Return whether the durable Jev circuit is within its cooldown."""
    current = _circuit_now(now)
    return _circuit_is_open(_circuit_state(ledger, current), current)


def record_circuit_result(ledger, error, now=None) -> bool:
    """Persist one Jev result and return the resulting open state.

    Only retryable rate-limit/5xx/transport/timeout failures count.  Error
    details are intentionally reduced to a fixed class label before storage.
    ``None`` represents a successful Jev call and resets the consecutive
    failure count.
    """
    current = _circuit_now(now)
    state = _circuit_state(ledger, current)
    failure_class = _circuit_failure_class(error)
    if error is None:
        if state["consecutive_failures"] == 0 and not _circuit_is_open(state, current):
            return False
        clean = {"consecutive_failures": 0, "open_until": 0.0,
                 "failure_class": None}
        _write_circuit_state(ledger, clean, current)
        return False
    if failure_class is None or _circuit_is_open(state, current):
        return _circuit_is_open(state, current)
    failures = state["consecutive_failures"]
    if state["open_until"] > 0 and current >= state["open_until"]:
        failures = 0
    failures = min(_CIRCUIT_FAILURE_LIMIT, failures + 1)
    open_until = (current + _CIRCUIT_COOLDOWN_SECONDS
                  if failures >= _CIRCUIT_FAILURE_LIMIT else 0.0)
    updated = {"consecutive_failures": failures, "open_until": open_until,
               "failure_class": failure_class}
    _write_circuit_state(ledger, updated, current)
    return open_until > current


def job_deadline(scfg: dict, tick_deadline: float) -> float:
    """Apply both the shared tick and this job's execution budget."""
    try:
        seconds = float(scfg.get("job_budget_seconds", 45.0))
    except (TypeError, ValueError):
        seconds = 45.0
    return min(float(tick_deadline), time.monotonic() + max(0.0, seconds))


def _call_guard(guard, stage: str):
    guard(stage)


def accepts_timeout(fn) -> bool:
    """Whether an injected callable takes a ``timeout`` kwarg — inspect
    the signature rather than catch a TypeError that could hide a real
    model failure."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "timeout"
               and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                               inspect.Parameter.KEYWORD_ONLY)
               or p.kind is inspect.Parameter.VAR_KEYWORD
               for p in params)


def llm_call(fn, prompt: str, deadline: float,
             timeout_cap: float | None = None):
    """Call an injected local model while propagating remaining timeout.

    Existing test doubles commonly accept only ``prompt``.  We inspect the
    callable before adding the keyword, so compatibility does not require a
    TypeError catch that could hide a model failure.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeBudget("llm")
    timeout = remaining if timeout_cap is None else min(remaining, timeout_cap)
    return (fn(prompt, timeout=timeout) if accepts_timeout(fn)
            else fn(prompt))


# Budget a follow-up local-model call must see before it is dispatched
# — one long canonical generation measured 322 s (2026-09-27); a call
# started with less would overrun the pass and burn a retry attempt on
# work that was progressing.
LLM_CALL_RESERVE_S = 300.0


class _GuardedLLM:
    def __init__(self, fn, guard, deadline, timeout_cap=None,
                 call_reserve=None):
        self._fn = fn
        self._guard = guard
        self._deadline = deadline
        self._timeout_cap = timeout_cap
        self._reserve = (LLM_CALL_RESERVE_S if call_reserve is None
                         else max(0.0, float(call_reserve)))
        self._calls = 0

    def __call__(self, prompt):
        _call_guard(self._guard, "llm_attempt")
        if self._calls and self._deadline - time.monotonic() \
                < self._reserve:
            raise RuntimeBudgetShort("llm_next_call")
        value = llm_call(self._fn, prompt, self._deadline,
                         timeout_cap=self._timeout_cap)
        self._calls += 1
        _call_guard(self._guard, "llm_result")
        return value


class _GuardedJev:
    def __init__(self, client, guard):
        self._client = client
        self._guard = guard

    def evaluate(self, state, questions, deadline):
        _call_guard(self._guard, "jev_attempt")
        try:
            value = self._client.evaluate(state, questions, deadline)
        except Exception as error:
            if hasattr(error, "kind") and hasattr(error, "retryable"):
                with suppress(Exception):
                    self._client.last_error = error
            raise
        _call_guard(self._guard, "jev_result")
        return value

    def __getattr__(self, name):
        return getattr(self._client, name)


class _HookedJev:
    """Temporarily bind hooks around one real adapter evaluation.

    Keeping hooks scoped to the call avoids leaking one job's identity into a
    client reused for the next job in the same drain.
    """
    def __init__(self, client, guard, reserve):
        self._client = client
        self._guard = guard
        self._reserve = reserve

    def evaluate(self, state, questions, deadline):
        old_before = getattr(self._client, "before_attempt", None)
        old_after = getattr(self._client, "after_result", None)
        old_reserve = getattr(self._client, "reserve_fn", None)
        self._client.before_attempt = \
            lambda: _call_guard(self._guard, "jev_attempt")
        self._client.after_result = \
            lambda: _call_guard(self._guard, "jev_result")
        self._client.reserve_fn = self._reserve
        try:
            try:
                return self._client.evaluate(state, questions, deadline)
            except Exception as error:
                if hasattr(error, "kind") and hasattr(error, "retryable"):
                    with suppress(Exception):
                        self._client.last_error = error
                raise
        finally:
            self._client.before_attempt = old_before
            self._client.after_result = old_after
            self._client.reserve_fn = old_reserve

    def __getattr__(self, name):
        return getattr(self._client, name)


def guarded_llm(fn, guard, deadline, timeout_cap=None, call_reserve=None):
    """``call_reserve``: budget a follow-up call needs before dispatch
    (None = LLM_CALL_RESERVE_S; callers scale it to their job budget)."""
    return _GuardedLLM(fn, guard, deadline, timeout_cap, call_reserve)


def bind_jev(client, guard, reserve=None):
    """Guard injected clients and hook real Jev retries at each POST.

    The Jev adapter marks itself hookable.  Other clients are wrapped at the
    evaluate boundary, preserving their public counters through __getattr__.
    Returns ``(client_for_pipeline, cleanup)``.
    """
    if client is None:
        return None, lambda: None
    if not getattr(client, "_mcs_jev_hookable", False):
        return _GuardedJev(client, guard), lambda: None
    return _HookedJev(client, guard, reserve), lambda: None


def usage_reserver(ledger, token: JobToken, *, kind: str,
                   model: str, project_id: int, message_id: int):
    """Build a durable one-request reservation written before POST."""
    def reserve(body: dict, timeout: float):
        request_fp = payload_hash(body)
        ledger.artifact_add(
            kind,
            json.dumps({"job_id": token.job_id, "reserved": True},
                       ensure_ascii=False),
            project_id=project_id, message_id=message_id, model=model,
            meta={"jev_requests": 1, "reserved": True,
                  "job_id": token.job_id, "generation": token.generation,
                  "request_fp": request_fp,
                  "timeout_seconds": float(timeout),
                  "reservation_id": uuid.uuid4().hex})
    return reserve


def guard(ledger, token: JobToken, *, deadline: float,
          expected_config_generation: str | None,
          expected_mode: str, cfg_path: str | None = None,
          load_cfg=None, parse_cfg=None,
          source_fingerprint: str | None = None,
          current_source=None, stage: str = ""):
    """Re-read all mutable execution identity immediately before a side effect."""
    if time.monotonic() >= deadline:
        raise RuntimeBudget(stage)
    from mcs_operations import paused
    if paused(ledger.db, token.project_id):
        raise RuntimeOff(stage)
    if cfg_path is not None and load_cfg is not None and parse_cfg is not None:
        current_cfg = load_cfg(cfg_path)
        current_scfg, _ = parse_cfg(current_cfg)
        if current_scfg.get("mode") == "off":
            raise RuntimeOff(stage)
        if (expected_config_generation is not None
                and config_generation(current_cfg)
                != expected_config_generation):
            raise RuntimeStale(stage)
    elif expected_mode == "off":
        raise RuntimeOff(stage)
    if not job_matches(ledger, token):
        raise RuntimeStale(stage)
    if current_source is not None:
        try:
            current_fp = current_source()
        except Exception:
            current_fp = None
        if current_fp != source_fingerprint:
            raise RuntimeStale(stage + ":source")


__all__ = [
    "JobToken", "LLMNotSent", "LLMRejected", "RuntimeBudget",
    "RuntimeBudgetShort",
    "RuntimeGuardError",
    "RuntimeOff",
    "RuntimeStale", "bind_jev", "config_generation",
    "circuit_open", "record_circuit_result",
    "guard", "guarded_llm", "job_deadline", "job_matches", "llm_call",
    "parse_payload", "attempt_limit", "payload_generation", "transition",
    "transition_tx",
    "usage_reserver",
]
