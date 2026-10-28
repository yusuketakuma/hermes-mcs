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
import os
import time
import uuid
from contextlib import suppress

from mcs_util import loads_dict, payload_hash, reject_constant, unique_pairs, valid_hash


DEFAULT_ATTEMPT_LIMIT = 6
MAX_SQL_INTEGER = 2**63 - 1


@dataclass(frozen=True)
class JevReservation:
    """The accounting day and artifact of one committed Jev reservation."""
    artifact_id: int
    jst_day: int


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
    is left to START the next one. A pass may need several generations.
    The work already persisted stands; the job is deferred, not charged
    an attempt — an executed call that overran its deadline still raises
    RuntimeBudget."""


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


# Run-window and spending settings decide when new requests may start, not how a
# job's answer is interpreted: editing them must not discard results already received.
_GENERATION_EXEMPT = {"semantic": ("until",),
                      "cloud_llm": ("until", "window_start", "window_budget_usd",
                                    "daily_cost_limit_usd", "monthly_cost_limit_usd")}


def config_generation(cfg: dict) -> str:
    """Stable generation for the config snapshot, apart from run-window and spending settings."""
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    for section, keys in _GENERATION_EXEMPT.items():
        if isinstance(cfg.get(section), dict):
            cfg[section] = {k: v for k, v in cfg[section].items() if k not in keys}
    return payload_hash(cfg)


def execution_generation(scfg: dict) -> str:
    """Interpretation identity, independent of runtime budgets, scope and mode."""
    from inference_version import CURRENT_VERSION, VERSION_KEY
    from semantic_policy import policy_fingerprint, SCHEMA_VERSION
    from semantic_extraction import v2_generation
    # Candidate approval still fences execution/publication separately. A code
    # approval or operational cap change is not a fresh retry budget.
    policy = {**scfg, 'candidate_fingerprint': None}
    return payload_hash({VERSION_KEY: CURRENT_VERSION, 'schema': SCHEMA_VERSION,
                         'policy': policy_fingerprint(policy), 'extraction': v2_generation()})


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
    from inference_version import is_current
    if not is_current(parse_payload({'payload': token.payload_raw})):
        return False
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
    from inference_version import is_current
    if not is_current(parse_payload({'payload': token.payload_raw})):
        return False
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
    if cur.rowcount == 1:
        new_state = ('done' if action == 'done' else 'failed' if action == 'failed'
                     or action == 'retry' and token.attempts + 1 >= max(1, int(max_attempts)) else token.state)
        if new_state != token.state:
            from ledger import bump_semantic_job_state_epoch_tx
            bump_semantic_job_state_epoch_tx(ledger.db)
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
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):      # URLError included
        return "transport"
    return None


def _circuit_state(ledger, now=None, *, kind=_CIRCUIT_ARTIFACT_KIND) -> dict:
    row = ledger.db.execute(
        "SELECT content,created_at FROM artifacts WHERE kind=? AND project_id IS NULL "
        "AND message_id IS NULL ORDER BY artifact_id DESC LIMIT 1",
        (kind,)).fetchone()
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
                    and failure_class not in (_CIRCUIT_FAILURE_CLASSES |
                        ({"auth_error", "payment_required", "pricing_unverified", "contract_error"}
                         if kind == "cloud_circuit" else set())))):
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
                "open_until": 1e30 if kind == "cloud_circuit" else created_at + _CIRCUIT_COOLDOWN_SECONDS,
                "failure_class": "pricing_unverified" if kind == "cloud_circuit" else "malformed"}


def _circuit_is_open(state: dict, now: float) -> bool:
    return float(state.get("open_until", 0.0)) > now


def _write_circuit_state(ledger, state: dict, now: float, *, kind=_CIRCUIT_ARTIFACT_KIND) -> None:
    payload = {
        "consecutive_failures": int(state["consecutive_failures"]),
        "open_until": float(state["open_until"]),
        "failure_class": state.get("failure_class"),
        "updated_at": now,
    }
    write = ledger.artifact_add_tx if kind == "cloud_circuit" else ledger.artifact_add
    write(
        kind,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                   allow_nan=False),
        model="jev" if kind == _CIRCUIT_ARTIFACT_KIND else "claude-haiku-5-5",
        meta={"state": "open" if payload["open_until"] > now else "closed"},
    )


def circuit_open(ledger, now=None, *, kind=_CIRCUIT_ARTIFACT_KIND) -> bool:
    """Return whether the durable Jev circuit is within its cooldown."""
    current = _circuit_now(now)
    return _circuit_is_open(_circuit_state(ledger, current, kind=kind), current)


def record_circuit_result(ledger, error, now=None, *, kind=_CIRCUIT_ARTIFACT_KIND, reset=False) -> bool:
    """Persist one result; cloud read/write owns one immediate transaction.

    Cloud rejects a borrowed active transaction without committing caller work.
    Terminal cloud failures survive successful results until explicit reset;
    the existing Jev cooldown and transaction behavior remain unchanged.
    """
    if kind != "cloud_circuit":
        return _record_circuit_result(ledger, error, now, kind=kind, reset=reset)
    if ledger.db.in_transaction:
        raise RuntimeError("cloud_circuit_transaction_active")
    ledger.db.execute("BEGIN IMMEDIATE")
    with ledger.db:
        return _record_circuit_result(ledger, error, now, kind=kind, reset=reset)


def _record_circuit_result(ledger, error, now=None, *, kind=_CIRCUIT_ARTIFACT_KIND, reset=False) -> bool:
    """Persist one Jev result and return the resulting open state.

    Only retryable rate-limit/5xx/transport/timeout failures count.  Error
    details are intentionally reduced to a fixed class label before storage.
    ``None`` represents a successful Jev call and resets the consecutive
    failure count.
    """
    current = _circuit_now(now)
    state = _circuit_state(ledger, current, kind=kind)
    terminal = {"auth_error", "payment_required", "pricing_unverified"}
    if (kind == "cloud_circuit" and not reset
            and state["failure_class"] in terminal):
        return True
    if kind == "cloud_circuit" and getattr(error, "kind", "") in terminal:
        updated = {"consecutive_failures": _CIRCUIT_FAILURE_LIMIT,
                   "open_until": 1e30, "failure_class": error.kind}
        _write_circuit_state(ledger, updated, current, kind=kind)
        return True
    failure_class = _circuit_failure_class(error)
    if kind == "cloud_circuit" and getattr(error, "kind", "") == "contract_error":
        failure_class = "contract_error"
    if error is None:
        if state["consecutive_failures"] == 0 and not _circuit_is_open(state, current):
            return False
        clean = {"consecutive_failures": 0, "open_until": 0.0,
                 "failure_class": None}
        _write_circuit_state(ledger, clean, current, kind=kind)
        return False
    if failure_class is None or _circuit_is_open(state, current):
        return _circuit_is_open(state, current)
    failures = state["consecutive_failures"]
    if kind == "cloud_circuit" and state["failure_class"] != failure_class:
        failures = 0
    if state["open_until"] > 0 and current >= state["open_until"]:
        failures = 0
    failures = min(_CIRCUIT_FAILURE_LIMIT, failures + 1)
    open_until = (current + _CIRCUIT_COOLDOWN_SECONDS
                  if failures >= _CIRCUIT_FAILURE_LIMIT else 0.0)
    updated = {"consecutive_failures": failures, "open_until": open_until,
               "failure_class": failure_class}
    _write_circuit_state(ledger, updated, current, kind=kind)
    return open_until > current


def job_deadline(scfg: dict, tick_deadline: float) -> float:
    """Apply both the shared tick and this job's execution budget."""
    try:
        seconds = float(scfg.get("job_budget_seconds", 45.0))
    except (TypeError, ValueError):
        seconds = 45.0
    return min(float(tick_deadline), time.monotonic() + max(0.0, seconds))


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


# Budget a follow-up local-model call must see before it is dispatched.
# Starting with too little remaining time can overrun the pass and burn
# a retry attempt on work that was progressing.
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
        self._guard("llm_attempt")
        if self._calls and self._deadline - time.monotonic() \
                < self._reserve:
            raise RuntimeBudgetShort("llm_next_call")
        value = llm_call(self._fn, prompt, self._deadline,
                         timeout_cap=self._timeout_cap)
        self._calls += 1
        self._guard("llm_result")
        return value






def guarded_llm(fn, guard, deadline, timeout_cap=None, call_reserve=None):
    """``call_reserve``: budget a follow-up call needs before dispatch
    (None = LLM_CALL_RESERVE_S; callers scale it to their job budget)."""
    return _GuardedLLM(fn, guard, deadline, timeout_cap, call_reserve)


def _jev_cutoff(cutoff, attempt_s: float = 0.0) -> None:
    """A Jev request (or internal retry) may not start when it could still
    be in flight at ``cutoff`` — a free defer of the pass, like a follow-up
    model call that cannot fit. The answer itself is accepted until the
    caller's deadline, so waiting to retake the run lock never voids it."""
    if cutoff is not None and time.monotonic() + attempt_s >= cutoff:
        raise RuntimeBudgetShort("jev_next_call")




def reserve_artifact(ledger, check, write):
    """Check and write one usage reservation in the same immediate transaction."""
    if not ledger.db.in_transaction:
        ledger.db.execute("BEGIN IMMEDIATE")
    with ledger.db:
        check()
        return write()


def _daily_cap_check(ledger, limit: int, retryable: bool = False, *, config_path=None):
    """Refuse a reservation once today's durable count reaches `limit`."""
    from semantic_jev import JevError
    from semantic_store import jev_usage_today
    try:
        used = jev_usage_today(ledger)
    except ValueError:
        raise JevError("budget_exceeded", "semantic_usage_invalid", retryable=retryable) from None
    if used >= limit:
        raise JevError("budget_exceeded", "daily_cap", retryable=retryable)
    _jev_window_check(retryable, config_path=config_path)


def _jev_window_check(retryable: bool = False, *, config_path=None):
    """Refuse a new Jev request after semantic.until in that installation's live config.

    Checked with each attempt's reservation (the step right before it is
    sent), or at the attempt itself for callers that do not reserve."""
    from semantic_jev import JevError
    import mcs_util
    path = mcs_util.CONF_PATH if config_path is None else str(config_path)
    if not os.path.lexists(path):
        return
    cfg = mcs_util.load_config_checked(path)
    block = cfg.get("semantic") if isinstance(cfg, dict) else None
    if cfg is None or (block is not None and not isinstance(block, dict)):
        raise JevError("budget_exceeded", "semantic_config_unreadable", retryable=retryable)
    until = (block or {}).get("until")
    if until is None:
        return
    try:
        ended = time.time() >= mcs_util.until_epoch(until)
    except (ValueError, OverflowError):
        raise JevError("budget_exceeded", "semantic_until_invalid", retryable=retryable) from None
    if ended:
        raise JevError("budget_exceeded", "semantic_until_expired", retryable=retryable)


def _check_reservation_day(ledger, artifact_id, day):
    """Reject a reservation whose check and durable timestamp span JST days."""
    from semantic_jev import JevError
    from semantic_store import _jst_day_start
    row = ledger.db.execute("SELECT created_at FROM artifacts WHERE artifact_id=?",
                            (artifact_id,)).fetchone()
    if (row is None or int(_jst_day_start(row["created_at"])) != day
            or int(_jst_day_start(time.time())) != day):
        raise JevError("budget_exceeded", "reservation_day_changed", retryable=True)


def usage_reserver(ledger, token: JobToken, *, kind: str,
                   model: str, project_id: int, message_id: int,
                   daily_request_budget: int | None = None):
    """Build a durable one-request reservation written before POST."""
    if daily_request_budget is not None and (
            type(daily_request_budget) is not int or daily_request_budget < 0):
        raise ValueError("semantic_daily_budget_invalid")
    from semantic_jev import DAILY_REQUEST_LIMIT
    daily_request_budget = (DAILY_REQUEST_LIMIT if daily_request_budget is None
                            else min(daily_request_budget, DAILY_REQUEST_LIMIT))

    def reserve(body: dict, timeout: float):
        request_fp = payload_hash(body)
        from semantic_store import _jst_day_start
        day = None
        def check():
            nonlocal day
            day = int(_jst_day_start(time.time()))
            _daily_cap_check(ledger, daily_request_budget, retryable=True)
            if int(_jst_day_start(time.time())) != day:
                from semantic_jev import JevError
                raise JevError("budget_exceeded", "reservation_day_changed", retryable=True)
        def write():
            artifact_id = ledger.artifact_add_tx(
                kind,
                json.dumps({"job_id": token.job_id, "reserved": True},
                           ensure_ascii=False),
                project_id=project_id, message_id=message_id, model=model,
                meta={"jev_requests": 1, "reserved": True,
                      "job_id": token.job_id, "generation": token.generation,
                      "request_fp": request_fp,
                      "timeout_seconds": float(timeout),
                      "reservation_id": uuid.uuid4().hex})
            _check_reservation_day(ledger, artifact_id, day)
            return JevReservation(artifact_id, day)
        return reserve_artifact(ledger, check, write)
    return reserve


def manual_usage_reserver(ledger, *, operation: str, daily_request_budget=None, config_path=None):
    """Reserve every explicit live-evaluation HTTP against the same MCS daily count."""
    from semantic_jev import DAILY_REQUEST_LIMIT, JEV_MODEL, JevError
    if operation not in {"wire_smoke", "jev_value", "semantic_bench", "semantic_detect", "semantic_shadow_e2e"}:
        raise ValueError("manual_jev_operation_invalid")
    if daily_request_budget is None:
        daily_request_budget = DAILY_REQUEST_LIMIT
    if type(daily_request_budget) is not int or daily_request_budget < 0:
        raise JevError("budget_exceeded", "semantic_daily_budget_invalid")
    limit = min(daily_request_budget, DAILY_REQUEST_LIMIT)

    def reserve(body, timeout):
        from semantic_store import _jst_day_start
        day = None
        def check():
            nonlocal day
            day = int(_jst_day_start(time.time()))
            _daily_cap_check(ledger, limit, config_path=config_path)
            if int(_jst_day_start(time.time())) != day:
                raise JevError("budget_exceeded", "reservation_day_changed", retryable=True)

        def write():
            artifact_id = ledger.artifact_add_tx("semantic_usage", "{}", model=JEV_MODEL,
                meta={"jev_requests": 1, "reserved": True, "operation": operation,
                      "request_fp": payload_hash(body), "timeout_seconds": float(timeout),
                      "reservation_id": uuid.uuid4().hex})
            _check_reservation_day(ledger, artifact_id, day)
            return JevReservation(artifact_id, day)
        if ledger.db.in_transaction:
            raise RuntimeError("manual_jev_transaction_active")
        return reserve_artifact(ledger, check, write)
    return reserve


_QUALITY_ID_KEYS = ("input_key", "candidate_key", "failure_key", "audit_key")
# Exact artifact kind owned by patient_context_refresh. The value is a storage contract.
_QUALITY_PURPOSES = {
    "summary": ("patient_summary_quality", "patient_context_summary_quality_v1"),
    "source": ("patient_source_quality", "patient_context_source_quality_v1"),
}
_QUALITY_QIDS = frozenset(f"q{i}" for i in range(12))


def _quality_purpose(purpose):
    if not isinstance(purpose, str) or purpose not in _QUALITY_PURPOSES:
        raise ValueError("patient_summary_purpose_invalid")
    return _QUALITY_PURPOSES[purpose]


def _quality_json(raw):
    if not isinstance(raw, str) or len(raw) > 32768:
        return None
    try:
        def finite_float(text):
            value = float(text)
            if not math.isfinite(value):
                raise ValueError("invalid_number")
            return value
        value = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant,
                           parse_float=finite_float)
    except (TypeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _quality_identity(identity, *, purpose="summary"):
    if not isinstance(identity, dict) or any(not valid_hash(identity.get(key)) for key in _QUALITY_ID_KEYS):
        raise ValueError("patient_summary_identity_invalid")
    keys = {key: identity[key] for key in _QUALITY_ID_KEYS}
    if purpose == "summary":
        return keys
    _quality_purpose(purpose)
    source = identity.get("source_input_key")
    assessment = identity.get("assessment")
    questions, passing = identity.get("question_keys"), identity.get("passing_choices")
    if (not valid_hash(source) or not isinstance(assessment, dict) or set(assessment) != {"code", "policy"}
            or any(not valid_hash(value) for value in assessment.values())
            or not isinstance(questions, dict) or not questions
            or any(not valid_hash(fid) or not valid_hash(key) for fid, key in questions.items())
            or len(set(questions.values())) != len(questions)
            or not isinstance(passing, dict) or set(passing) != set(questions)
            or any(choice not in ("supports", "complete") for choice in passing.values())):
        raise ValueError("patient_source_identity_invalid")
    return {**keys, "source_input_key": source, "assessment": dict(assessment),
            "question_keys": dict(questions), "passing_choices": dict(passing)}


def _quality_request(identity, body, *, purpose="summary"):
    keys = _quality_identity(identity, purpose=purpose)
    try:
        questions = body["questions"]
        facts = body["state"]["target"]["facts"]
        if (not isinstance(questions, dict) or not 1 <= len(questions) <= 12
                or not isinstance(facts, list) or not 1 <= len(facts) <= 12):
            raise ValueError
        indexed = {}
        for fact in facts:
            qid, fid = fact["id"], fact["fact"]["id"]
            if qid not in _QUALITY_QIDS or not valid_hash(fid) or qid in indexed:
                raise ValueError
            indexed[qid] = fid
        mapping = {qid: indexed[qid] for qid in questions}
        if len(set(mapping.values())) != len(mapping):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("patient_summary_request_invalid") from None
    if purpose == "source":
        from semantic_audit import patient_source_question_keys
        computed = patient_source_question_keys(body, source_input_key=keys["source_input_key"],
                                                assessment=keys["assessment"])
        passing = "complete" if body["state"]["target"]["id"] == "source_coverage" else "supports"
        if (set(computed) != set(mapping.values()) or any(
                keys["question_keys"].get(fid) != key or keys["passing_choices"].get(fid) != passing
                for fid, key in computed.items())):
            raise ValueError("patient_source_request_invalid")
        return {"v": 2, **{key: keys[key] for key in _QUALITY_ID_KEYS},
                "source_input_key": keys["source_input_key"], "assessment_hash": payload_hash(keys["assessment"]),
                "questions": {qid: {"fact_id": fid, "question_key": computed[fid]} for qid, fid in mapping.items()}}
    return {"v": 1, **keys, "questions": mapping}


def _quality_bound(meta):
    if (not isinstance(meta, dict) or meta.get("operation") not in {v[0] for v in _QUALITY_PURPOSES.values()}
            or type(meta.get("jev_requests")) is not int or meta["jev_requests"] != 1
            or meta.get("reserved") is not True or not valid_hash(meta.get("request_fp"))
            or not isinstance(meta.get("reservation_id"), str) or len(meta["reservation_id"]) != 32
            or any(c not in "0123456789abcdef" for c in meta["reservation_id"])
            or type(meta.get("timeout_seconds")) not in (int, float)
            or not 0 < meta["timeout_seconds"] <= 1.7976931348623157e308
            or not math.isfinite(meta["timeout_seconds"])):
        return None
    request = meta.get("quality_request")
    if not isinstance(request, dict) or type(request.get("v")) is not int or request["v"] not in (1, 2):
        return None
    extra = {"source_input_key", "assessment_hash"} if request["v"] == 2 else set()
    if (set(request) != {"v", *_QUALITY_ID_KEYS, "questions", *extra}
            or extra and (meta["operation"] != "patient_source_quality"
                          or any(not valid_hash(request.get(key)) for key in extra))):
        return None
    try:
        _quality_identity(request)
        questions = request["questions"]
        if not isinstance(questions, dict) or not 1 <= len(questions) <= 12 or any(q not in _QUALITY_QIDS for q in questions):
            return None
        if request["v"] == 2:
            if any(not isinstance(value, dict) or set(value) != {"fact_id", "question_key"}
                   or any(not valid_hash(item) for item in value.values()) for value in questions.values()):
                return None
            if len({value["question_key"] for value in questions.values()}) != len(questions):
                return None
            ids = [value["fact_id"] for value in questions.values()]
        else:
            ids = list(questions.values())
        if any(not valid_hash(fid) for fid in ids) or len(set(ids)) != len(ids):
            return None
    except (TypeError, ValueError):
        return None
    return request


def _quality_result_parts(meta, request):
    result = meta.get("quality_result")
    if not isinstance(result, dict) or set(result) != {"answers", "invalid"}:
        return None
    answers, invalid = result["answers"], result["invalid"]
    if (not isinstance(answers, dict) or not isinstance(invalid, list)
            or any(not valid_hash(fid) for fid in invalid) or invalid != sorted(set(invalid))
            or any(not valid_hash(fid) for fid in answers)):
        return None
    if request["v"] == 2:
        ids = {value["fact_id"] for value in request["questions"].values()}
        if any(not isinstance(ref, dict) or set(ref) != {"answer_artifact_id", "answer_hash"}
               or type(ref["answer_artifact_id"]) is not int or not 0 < ref["answer_artifact_id"] <= MAX_SQL_INTEGER
               or not valid_hash(ref["answer_hash"]) for ref in answers.values()):
            return None
    else:
        ids = set(request["questions"].values())
        if any(not valid_hash(value) for value in answers.values()):
            return None
    if set(answers) & set(invalid) or set(answers) | set(invalid) != ids:
        return None
    return answers, invalid


def _quality_answer_evidence(db, project_id, request, answers, *, purpose="summary"):
    from inference_version import CURRENT_VERSION
    _, answer_kind = _quality_purpose(purpose)
    choices = {"supports", "contradicts", "not_supported", "ambiguous"}
    choices |= {"omission_ok", "important_missing"} if purpose == "summary" else {"complete", "missing"}
    found = {}
    marks = ",".join("?" for _ in answers) or "NULL"
    # Include malformed or duplicate-key metadata even if its SQL keys do
    # not match. Otherwise an ambiguous second key could hide a receipt.
    query = ("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND CASE "
             "WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
             "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
             "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
             "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND "
             "abs(value)>1.7976931348623157e308) THEN 1 "
             "ELSE json_extract(meta,'$.input_key')=? AND json_extract(meta,'$.audit_key')=? "
             f"AND json_extract(meta,'$.fact_id') IN ({marks}) END")
    for row in db.execute(query, (answer_kind, project_id, request["input_key"],
                                  request["audit_key"], *answers)):
        answer_meta = _quality_json(row[1])
        if answer_meta is None:
            return False
        fid = answer_meta.get("fact_id")
        if (answer_meta.get("input_key") != request["input_key"]
                or answer_meta.get("audit_key") != request["audit_key"]
                or answer_meta.get("inference_engine_version") != CURRENT_VERSION or fid not in answers):
            continue
        value = _quality_json(row[0])
        if fid in found or value is None or set(value) != {"choice", "confidence"}:
            return False
        confidence = value.get("confidence")
        if (not isinstance(value.get("choice"), str)
                or value.get("choice") not in choices
                or type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1
                or (answers[fid] is not None and payload_hash(value) != answers[fid])):
            return False
        found[fid] = value
    return set(found) == set(answers)


def _source_quality_rows(db, pid, source_input_key, question_keys):
    """Narrow normal rows by logical question, while retaining malformed scope evidence."""
    def hash_sql(value, size=64):
        return f"(typeof({value})='text' AND length({value})={size} AND {value} NOT GLOB '*[^0-9a-f]*')"

    def get(path):
        return f"json_extract(meta,'$.{path}')"
    version = get("quality_request.v")
    source_operation = get("operation") + "='patient_source_quality'"
    question = "json_extract(q.value,'$.question_key')"
    field_checks = [hash_sql(get("quality_request." + key)) for key in _QUALITY_ID_KEYS]
    field_checks += [hash_sql(get("request_fp")), hash_sql(get("reservation_id"), 32)]
    source_fields = " AND ".join(hash_sql(get("quality_request." + key))
                                 for key in ("source_input_key", "assessment_hash"))
    legacy_question = hash_sql("q.value")
    source_question = ("CASE q.type WHEN 'object' THEN (SELECT COUNT(*) FROM json_each(q.value))=2 AND "
        + hash_sql("json_extract(q.value,'$.fact_id')") + " AND " + hash_sql(question) + " ELSE 0 END")
    qids = ",".join("'q" + str(i) + "'" for i in range(12))
    marks = ",".join("?" for _ in question_keys) or "NULL"
    query = ("SELECT artifact_id,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END "
        "FROM artifacts WHERE kind='semantic_usage' AND project_id=? AND CASE "
        "WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN json_type(meta)!='object' THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
        "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 "
        "WHEN json_extract(meta,'$.operation') NOT IN ('patient_source_quality','patient_summary_quality') "
        "OR json_extract(meta,'$.operation') IS NULL THEN "
        "json_type(meta,'$.quality_request') IS NOT NULL OR json_type(meta,'$.quality_result') IS NOT NULL "
        "WHEN NOT COALESCE((" + " AND ".join(field_checks) + ") AND "
        "json_type(meta,'$.jev_requests')='integer' AND json_extract(meta,'$.jev_requests')=1 AND "
        "json_type(meta,'$.reserved')='true' AND "
        "json_type(meta,'$.timeout_seconds') IN ('integer','real') AND json_extract(meta,'$.timeout_seconds')>0 AND "
        "json_type(meta,'$.quality_request')='object' AND json_type(meta,'$.quality_request.v')='integer' AND "
        f"(({source_operation} AND {version} IN (1,2)) OR (NOT ({source_operation}) AND {version}=1)) AND "
        f"(SELECT COUNT(*) FROM json_each(meta,'$.quality_request'))=CASE {version} WHEN 1 THEN 6 ELSE 8 END AND "
        "json_type(meta,'$.quality_request.questions')='object' AND "
        "(SELECT COUNT(*) FROM json_each(meta,'$.quality_request.questions')) BETWEEN 1 AND 12 AND "
        f"({version}=1 OR ({source_fields})) AND NOT EXISTS "
        "(SELECT 1 FROM json_each(meta,'$.quality_request.questions') q WHERE "
        f"q.key NOT IN ({qids}) OR NOT COALESCE(CASE {version} WHEN 1 THEN {legacy_question} "
        f"ELSE {source_question} END,0)),0) THEN 1 "
        f"WHEN {source_operation} AND {version}=1 THEN 1 "
        f"ELSE {source_operation} AND json_extract(meta,'$.quality_request.source_input_key')=? "
        "AND EXISTS (SELECT 1 FROM json_each(meta,'$.quality_request.questions') q "
        f"WHERE {question} IN ({marks})) END")
    return db.execute(query, (pid, source_input_key, *question_keys))


def _source_usage_artifact(db, project_id, meta, artifact_id=None):
    """Read the actual reservation; result sealing may add only its quality_result."""
    if artifact_id is None:
        rows = db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='semantic_usage' "
            "AND project_id=? AND CASE WHEN json_valid(meta) THEN "
            "json_extract(meta,'$.reservation_id') END=?", (project_id, meta.get("reservation_id"))).fetchmany(2)
        if len(rows) != 1:
            return None
        artifact_id, raw = rows[0]
    else:
        row = db.execute("SELECT meta FROM artifacts WHERE kind='semantic_usage' AND project_id=? AND artifact_id=?",
                         (project_id, artifact_id)).fetchone()
        if row is None:
            return None
        raw = row[0]
    stored = _quality_json(raw)
    if (stored is None or {k: v for k, v in stored.items() if k != "quality_result"}
            != {k: v for k, v in meta.items() if k != "quality_result"}):
        return None
    return artifact_id


def _source_answer_record(db, project_id, usage_id, meta, request, fid, ref):
    """Validate one original answer by primary key and its exact original reservation."""
    from inference_version import CURRENT_VERSION
    if (not isinstance(ref, dict) or set(ref) != {"answer_artifact_id", "answer_hash"}
            or type(ref["answer_artifact_id"]) is not int or not 0 < ref["answer_artifact_id"] <= MAX_SQL_INTEGER
            or not valid_hash(ref["answer_hash"])):
        return None
    row = db.execute("SELECT content,meta FROM artifacts WHERE kind='patient_context_source_quality_v1' "
                     "AND project_id=? AND artifact_id=?", (project_id, ref["answer_artifact_id"])).fetchone()
    answer_meta = _quality_json(row[1]) if row else None
    value = _quality_json(row[0]) if row else None
    qid = next((qid for qid, question in request["questions"].items() if question["fact_id"] == fid), None)
    if (qid is None or answer_meta is None or value is None or set(value) != {"choice", "confidence"}
            or answer_meta.get("inference_engine_version") != CURRENT_VERSION
            or any(answer_meta.get(key) != request[key] for key in ("input_key", "audit_key", "source_input_key"))
            or answer_meta.get("fact_id") != fid
            or answer_meta.get("question_key") != request["questions"][qid]["question_key"]
            or type(answer_meta.get("reservation_artifact_id")) is not int
            or answer_meta["reservation_artifact_id"] != usage_id
            or answer_meta.get("reservation_id") != meta["reservation_id"] or answer_meta.get("qid") != qid
            or value.get("choice") not in ("supports", "contradicts", "not_supported", "ambiguous", "complete", "missing")
            or type(value.get("confidence")) not in (int, float) or not 0 <= value["confidence"] <= 1
            or not math.isfinite(value["confidence"]) or payload_hash(value) != ref["answer_hash"]):
        return None
    return value


def _source_new_answer_refs(db, project_id, usage_id, meta, request, answers):
    """Find newly received answer records without borrowing another reservation's receipt."""
    query = ("SELECT artifact_id,content,meta FROM artifacts WHERE kind='patient_context_source_quality_v1' "
        "AND project_id=? AND CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
        "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
        "ELSE json_extract(meta,'$.reservation_artifact_id')=? OR json_extract(meta,'$.reservation_id')=? END")
    found = {}
    for artifact_id, content, raw in db.execute(query, (project_id, usage_id, meta["reservation_id"])):
        answer_meta, value = _quality_json(raw), _quality_json(content)
        fid = answer_meta.get("fact_id") if answer_meta else None
        if fid not in answers or fid in found or value != answers[fid]:
            raise ValueError("patient_source_result_invalid")
        ref = {"answer_artifact_id": artifact_id, "answer_hash": payload_hash(value)}
        if _source_answer_record(db, project_id, usage_id, meta, request, fid, ref) is None:
            raise ValueError("patient_source_result_invalid")
        found[fid] = ref
    if set(found) != set(answers):
        raise ValueError("patient_source_result_invalid")
    return found


def patient_source_answer_binding(db, pid, identity, reservation, fact_ids):
    """Bind a new response only to its live, unsealed source reservation in the caller's write transaction."""
    if not db.in_transaction or not isinstance(reservation, JevReservation):
        raise RuntimeError("patient_source_transaction_required")
    keys = _quality_identity(identity, purpose="source")
    if (type(pid) is not int or pid <= 0 or not isinstance(fact_ids, (list, tuple, set, frozenset))
            or len(fact_ids) > 12 or any(not valid_hash(fid) for fid in fact_ids)):
        raise ValueError("patient_source_result_invalid")
    row = db.execute("SELECT meta FROM artifacts WHERE kind='semantic_usage' AND project_id=? AND artifact_id=?",
                     (pid, reservation.artifact_id)).fetchone()
    meta = _quality_json(row[0]) if row else None
    request = _quality_bound(meta)
    if (request is None or request["v"] != 2 or meta["operation"] != "patient_source_quality"
            or "quality_result" in meta or any(request[key] != keys[key] for key in _QUALITY_ID_KEYS)
            or request["source_input_key"] != keys["source_input_key"]
            or request["assessment_hash"] != payload_hash(keys["assessment"])):
        raise ValueError("patient_source_request_invalid")
    questions = {item["fact_id"]: (qid, item["question_key"]) for qid, item in request["questions"].items()}
    if any(fid not in questions or keys["question_keys"].get(fid) != questions[fid][1] for fid in fact_ids):
        raise ValueError("patient_source_request_invalid")
    return {fid: {"source_input_key": keys["source_input_key"], "question_key": questions[fid][1],
                  "reservation_artifact_id": reservation.artifact_id, "reservation_id": meta["reservation_id"],
                  "qid": questions[fid][0]} for fid in fact_ids}


def patient_source_question_evidence(db, pid, identity, fact_ids):
    """Reuse original source answers, unknowns and invalid counts across candidate audits."""
    keys = _quality_identity(identity, purpose="source")
    if (type(pid) is not int or pid <= 0 or not isinstance(fact_ids, (list, tuple, set, frozenset))
            or len(fact_ids) > 12
            or any(not valid_hash(fid) or fid not in keys["question_keys"] for fid in fact_ids)):
        raise ValueError("patient_source_question_invalid")
    requested = set(fact_ids)
    result = {"answers": {}, "references": {}, "problems": {}}
    if not requested:
        return result
    invalid_counts = dict.fromkeys(requested, 0)
    qkeys = [keys["question_keys"][fid] for fid in sorted(requested)]
    for row in _source_quality_rows(db, pid, keys["source_input_key"], qkeys):
        usage_id, raw = row
        meta = _quality_json(raw)
        request = _quality_bound(meta)
        if request is None or meta.get("operation") != "patient_source_quality" or request["v"] != 2:
            result["problems"].update(dict.fromkeys(requested, "unknown"))
            continue
        matching = {item["fact_id"] for item in request["questions"].values()
                    if item["fact_id"] in requested and item["question_key"] == keys["question_keys"][item["fact_id"]]}
        if request["source_input_key"] != keys["source_input_key"]:
            continue
        if request['assessment_hash'] != payload_hash(keys['assessment']):
            result['problems'].update(dict.fromkeys(matching, 'unknown'))
            continue
        parts = _quality_result_parts(meta, request)
        if parts is None:
            result["problems"].update(dict.fromkeys(matching, "unknown"))
            continue
        answers, invalid = parts
        checked = {fid: _source_answer_record(db, pid, usage_id, meta, request, fid, ref)
                   for fid, ref in answers.items()}
        if any(value is None for value in checked.values()):
            result["problems"].update(dict.fromkeys(matching, "unknown"))
            continue
        for fid in matching:
            if fid in invalid:
                invalid_counts[fid] += 1
                continue
            answer = checked[fid]
            choices = ({"supports", "contradicts", "not_supported", "ambiguous"}
                       if keys["passing_choices"][fid] == "supports" else {"complete", "missing", "ambiguous"})
            if fid in result["answers"] or answer["choice"] not in choices:
                result["problems"][fid] = "unknown"
                continue
            result["answers"][fid] = answer
            result["references"][fid] = {"usage_artifact_id": usage_id, "reservation_id": meta["reservation_id"],
                "answer_artifact_id": answers[fid]["answer_artifact_id"], "answer_hash": answers[fid]["answer_hash"]}
    marks = ",".join("?" for _ in qkeys)
    answer_query = ("SELECT artifact_id,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END "
        "FROM artifacts WHERE kind='patient_context_source_quality_v1' AND project_id=? "
        "AND CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
        "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 "
        "WHEN json_type(meta)!='object' OR json_type(meta,'$.source_input_key') IS NOT 'text' "
        "OR length(json_extract(meta,'$.source_input_key'))!=64 "
        "OR json_extract(meta,'$.source_input_key') GLOB '*[^0-9a-f]*' "
        "OR json_type(meta,'$.question_key') IS NOT 'text' OR length(json_extract(meta,'$.question_key'))!=64 "
        "OR json_extract(meta,'$.question_key') GLOB '*[^0-9a-f]*' THEN 1 "
        "ELSE json_extract(meta,'$.source_input_key')=? "
        f"AND json_extract(meta,'$.question_key') IN ({marks}) END")
    qkey_facts = {keys["question_keys"][fid]: fid for fid in requested}
    for artifact_id, raw in db.execute(answer_query, (pid, keys["source_input_key"], *qkeys)):
        meta = _quality_json(raw)
        if meta is None or not valid_hash(meta.get("source_input_key")) or not valid_hash(meta.get("question_key")):
            result["problems"].update(dict.fromkeys(requested, "unknown"))
            continue
        fid = qkey_facts.get(meta["question_key"])
        ref = result["references"].get(fid)
        if fid is not None and (meta.get("fact_id") != fid or not ref or ref["answer_artifact_id"] != artifact_id):
            # An orphan or copied answer cannot turn a missing original reservation into an unasked question.
            result["problems"][fid] = "unknown"
    for fid, count in invalid_counts.items():
        if count >= 2 and fid not in result["answers"]:
            result["problems"].setdefault(fid, "invalid_exhausted")
    for fid in result["problems"]:
        result["answers"].pop(fid, None)
        result["references"].pop(fid, None)
    return result


def _quality_result(db, project_id, meta, request):
    parts = _quality_result_parts(meta, request)
    if request["v"] == 2:
        usage_id = _source_usage_artifact(db, project_id, meta)
        return bool(parts is not None and usage_id is not None and all(
            _source_answer_record(db, project_id, usage_id, meta, request, fid, ref) is not None
            for fid, ref in parts[0].items()))
    purpose = next((key for key, (operation, _) in _QUALITY_PURPOSES.items()
                    if meta.get("operation") == operation), None)
    return bool(purpose is not None and parts is not None
                and _quality_answer_evidence(db, project_id, request, parts[0], purpose=purpose))


def patient_summary_usage_resolved(db, project_id, meta):
    """Check a bound request and its persisted answer receipts without changing the DB."""
    request = _quality_bound(meta)
    return bool(type(project_id) is int and project_id > 0 and request
                and _quality_result(db, project_id, meta, request))


def patient_summary_usage_prunable(db, project_id, raw):
    """Keep unknown, malformed or unresolved quality and invalid-attempt evidence."""
    meta = _quality_json(raw)
    if meta is None:
        return False
    purpose = next((key for key, (operation, _) in _QUALITY_PURPOSES.items()
                    if meta.get("operation") == operation), None)
    if purpose is None:
        return not ("quality_request" in meta or "quality_result" in meta)
    if purpose == "source":
        # Source questions outlive a candidate audit; their original evidence is never a TTL cache.
        return False
    request = _quality_bound(meta)
    if not request or not patient_summary_usage_resolved(db, project_id, meta):
        return False
    _, invalid = _quality_result_parts(meta, request)
    return _quality_answer_evidence(db, project_id, request, dict.fromkeys(invalid), purpose=purpose)


def patient_summary_request_problem(db, pid, identity, fact_ids, *, purpose="summary"):
    """Return a durable unknown or exhausted-invalid reason for logical questions."""
    if purpose == "source":
        keys = _quality_identity(identity, purpose="source")
        if (type(pid) is not int or pid <= 0 or not isinstance(fact_ids, (list, tuple, set, frozenset))
                or any(not valid_hash(fid) or fid not in keys["question_keys"] for fid in fact_ids)):
            raise ValueError("patient_source_question_invalid")
        ordered, problems = sorted(set(fact_ids)), set()
        for start in range(0, len(ordered), 12):
            ids = ordered[start:start + 12]
            page = {**keys, "question_keys": {fid: keys["question_keys"][fid] for fid in ids},
                    "passing_choices": {fid: keys["passing_choices"][fid] for fid in ids}}
            problems.update(patient_source_question_evidence(db, pid, page, ids)["problems"].values())
        if "unknown" in problems:
            return "patient_summary_request_unknown"
        return "patient_summary_invalid_exhausted" if problems else None
    keys = _quality_identity(identity)
    operation, _ = _quality_purpose(purpose)
    if type(pid) is not int or pid <= 0 or not isinstance(fact_ids, (list, tuple, set, frozenset)) or any(not valid_hash(fid) for fid in fact_ids):
        raise ValueError("patient_summary_request_invalid")
    requested = set(fact_ids)
    invalid_counts = dict.fromkeys(requested, 0)
    for row in db.execute("SELECT meta FROM artifacts WHERE kind='semantic_usage' AND project_id=?", (pid,)):
        meta = _quality_json(row[0])
        if meta is None:
            return "patient_summary_request_unknown"
        if meta.get("operation") not in {v[0] for v in _QUALITY_PURPOSES.values()}:
            if "quality_request" in meta or "quality_result" in meta:
                return "patient_summary_request_unknown"
            continue
        request = _quality_bound(meta)
        if request is None:
            return "patient_summary_request_unknown"
        resolved = "quality_result" in meta and patient_summary_usage_resolved(db, pid, meta)
        if "quality_result" in meta and not resolved:
            return "patient_summary_request_unknown"
        if meta["operation"] != operation:
            continue
        if (request["failure_key"] == keys["failure_key"] and requested.intersection(request["questions"].values())
                and not resolved):
            return "patient_summary_request_unknown"
        if request["failure_key"] == keys["failure_key"] and "quality_result" in meta:
            for fid in _quality_result_parts(meta, request)[1]:
                if fid in invalid_counts:
                    invalid_counts[fid] += 1
    for fid, count in invalid_counts.items():
        if count >= 2 and not _quality_answer_evidence(db, pid, keys, {fid: None}, purpose=purpose):
            return "patient_summary_invalid_exhausted"
    return None


def patient_summary_resolve_request_tx(ledger, reservation, identity, answers, invalid_ids, *, project_id, purpose="summary"):
    """Seal one request only with its already-persisted answer evidence."""
    if not ledger.db.in_transaction or not isinstance(reservation, JevReservation):
        raise RuntimeError("patient_summary_transaction_required")
    keys = _quality_identity(identity, purpose=purpose)
    operation, _ = _quality_purpose(purpose)
    if not isinstance(answers, dict) or not isinstance(invalid_ids, set):
        raise ValueError("patient_summary_result_invalid")
    row = ledger.db.execute("SELECT project_id,meta FROM artifacts WHERE kind='semantic_usage' AND artifact_id=?",
                            (reservation.artifact_id,)).fetchone()
    meta = _quality_json(row[1]) if row else None
    request = _quality_bound(meta)
    if (type(project_id) is not int or project_id <= 0 or not row or row[0] != project_id
            or not request or any(request[k] != keys[k] for k in _QUALITY_ID_KEYS)
            or meta.get("operation") != operation):
        raise ValueError("patient_summary_request_invalid")
    try:
        if purpose == "source":
            if (request["v"] != 2 or request["source_input_key"] != keys["source_input_key"]
                    or request["assessment_hash"] != payload_hash(keys["assessment"])
                    or any(keys["question_keys"].get(value["fact_id"]) != value["question_key"]
                           for value in request["questions"].values())):
                raise ValueError("patient_source_request_invalid")
            for fid, value in answers.items():
                choices = ({"supports", "contradicts", "not_supported", "ambiguous"}
                           if keys["passing_choices"].get(fid) == "supports" else {"complete", "missing", "ambiguous"})
                if not isinstance(value, dict) or value.get("choice") not in choices:
                    raise ValueError("patient_source_result_invalid")
            refs = _source_new_answer_refs(ledger.db, project_id, reservation.artifact_id, meta, request, answers)
            result = {"answers": refs, "invalid": sorted(invalid_ids)}
        else:
            result = {"answers": {fid: payload_hash(value) for fid, value in answers.items()}, "invalid": sorted(invalid_ids)}
    except (TypeError, ValueError, RecursionError):
        raise ValueError("patient_summary_result_invalid") from None
    candidate = {**meta, "quality_result": result}
    if not _quality_result(ledger.db, row[0], candidate, request):
        raise ValueError("patient_summary_result_invalid")
    if "quality_result" in meta:
        if meta["quality_result"] != result:
            raise ValueError("patient_summary_result_conflict")
        return 0
    updated = ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=? AND kind='semantic_usage' AND meta=?",
                                (json.dumps(candidate, separators=(",", ":")), reservation.artifact_id, row[1])).rowcount
    saved = ledger.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (reservation.artifact_id,)).fetchone()
    if updated != 1 or not saved or _quality_json(saved[0]) != candidate:
        raise ValueError("patient_summary_result_unverified")
    return updated


def patient_summary_usage_reserver(ledger, *, project_id, guard, daily_request_budget, identity, purpose="summary"):
    """Reserve one patient-summary Jev request under the shared daily budget."""
    from semantic_jev import DAILY_REQUEST_LIMIT, JEV_MODEL, JevError
    from semantic_policy import BACKLOG_BUDGET_RESERVE
    if (type(project_id) is not int or project_id <= 0 or not callable(guard)
            or type(daily_request_budget) is not int or daily_request_budget < 0):
        raise ValueError("patient_summary_reservation_invalid")
    identity = _quality_identity(identity, purpose=purpose)
    operation, _ = _quality_purpose(purpose)
    limit = min(daily_request_budget, DAILY_REQUEST_LIMIT)
    limit -= math.ceil(limit * BACKLOG_BUDGET_RESERVE)

    def reserve(body, timeout):
        if ledger.db.in_transaction:
            raise RuntimeError("patient_summary_transaction_active")
        if (not isinstance(body, dict) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("patient_summary_reservation_invalid")
        request = _quality_request(identity, body, purpose=purpose)
        from semantic_store import _jst_day_start
        day = None
        def check():
            nonlocal day
            guard("jev_reserve")
            ids = ([value["fact_id"] for value in request["questions"].values()] if purpose == "source"
                   else list(request["questions"].values()))
            evidence = patient_source_question_evidence(ledger.db, project_id, identity, ids) if purpose == "source" else None
            problem = ("patient_summary_request_unknown" if "unknown" in evidence["problems"].values()
                       else "patient_summary_invalid_exhausted" if evidence["problems"] else None) if evidence is not None else (
                           patient_summary_request_problem(ledger.db, project_id, identity, ids))
            if problem:
                raise JevError("budget_exceeded", problem, retryable=problem == "patient_summary_request_unknown")
            if evidence is not None and evidence["answers"]:
                raise JevError("contract_error", "patient_source_question_answered")
            day = int(_jst_day_start(time.time()))
            _daily_cap_check(ledger, limit, retryable=True)
            if int(_jst_day_start(time.time())) != day:
                raise JevError("budget_exceeded", "reservation_day_changed", retryable=True)
        def write():
            artifact_id = ledger.artifact_add_tx("semantic_usage", "{}", project_id=project_id,
                model=JEV_MODEL, meta={"jev_requests": 1, "reserved": True,
                    "operation": operation, "request_fp": payload_hash(body),
                    "timeout_seconds": float(timeout), "reservation_id": uuid.uuid4().hex,
                    "quality_request": request})
            _check_reservation_day(ledger, artifact_id, day)
            guard("jev_reserved")
            return JevReservation(artifact_id, day)
        return reserve_artifact(ledger, check, write)
    return reserve


def guard(ledger, token: JobToken, *, deadline: float,
          expected_config_generation: str | None,
          expected_mode: str, cfg_path: str | None = None,
          load_cfg=None, parse_cfg=None,
          source_fingerprint: str | None = None,
          current_source=None, stage: str = "", expected_execution_generation=None):
    """Re-read all mutable execution identity immediately before a side effect."""
    if time.monotonic() >= deadline:
        raise RuntimeBudget(stage)
    from mcs_queries import paused
    if paused(ledger.db, token.project_id):
        raise RuntimeOff(stage)
    execution = parse_payload({'payload': token.payload_raw}).get('execution_generation')
    if (execution is not None and expected_execution_generation is not None
            and execution != expected_execution_generation):
        raise RuntimeStale(stage + ':execution')
    scope = ledger.db.execute('SELECT fetch_reason FROM patients WHERE project_id=?',
                              (token.project_id,)).fetchone()
    if scope is not None and scope[0] in ('forbidden', 'permission_denied', 'access_revoked'):
        raise RuntimeOff(stage + ':permission')
    if cfg_path is not None and load_cfg is not None and parse_cfg is not None:
        current_cfg = load_cfg(cfg_path)
        current_scfg, _ = parse_cfg(current_cfg)
        if current_scfg.get("mode") == "off":
            raise RuntimeOff(stage)
        projects = current_scfg.get('project_ids')
        if projects is not None and token.project_id not in projects:
            raise RuntimeOff(stage + ':scope')
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
    "RuntimeStale", "JevEvaluation", "bind_jev", "config_generation", "execution_generation",
    "circuit_open", "record_circuit_result",
    "guard", "guarded_llm", "job_deadline", "job_matches", "llm_call",
    "parse_payload", "attempt_limit", "payload_generation", "transition",
    "transition_tx",
    "usage_reserver", "llm_request_was_dispatched",
]


PRE_DISPATCH_STAGES = ("llm_attempt", "llm_next_call", "llm")


def llm_request_was_dispatched(error: RuntimeGuardError) -> bool:
    """A pre-dispatch refusal must not spend the one-shot repair reservation."""
    return not (isinstance(error, LLMNotSent) or error.stage in PRE_DISPATCH_STAGES)


def _accepts_explicit_keyword(fn, keyword):
    """An old **kwargs hook must explicitly promise the guarded protocol."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    param = params.get(keyword)
    if param is not None and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                             inspect.Parameter.KEYWORD_ONLY):
        return True
    declared = getattr(fn, "__mcs_guarded_keywords__", ())
    return (isinstance(declared, (tuple, frozenset)) and keyword in declared
            and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()))


def _can_bind_call(fn, *args, **kwargs):
    try:
        inspect.signature(fn).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return False
    return True


class JevEvaluation:
    """Bind one job's evaluation guards, reservation and timing without client mutation."""
    def __init__(self, client, guard, reserve, cutoff, transport, timing, config_path=None):
        self._client = client

        def windowed(stage):
            guard(stage)
            if stage == "jev_attempt" and reserve is None:
                # Reserved paths check semantic.until in the same transaction as each
                # attempt's reservation, immediately before it is sent; refusing after
                # that reservation would only strand it unresolved. Unreserved callers
                # are checked here instead.
                _jev_window_check(config_path=config_path)
        self._guard = windowed
        self._reserve = reserve
        self._cutoff = cutoff
        self._transport = transport
        self._timing = timing

    @property
    def requests_made(self):
        return self._client.requests_made

    @property
    def usage_totals(self):
        return getattr(self._client, "usage_totals", {})

    @property
    def last_error(self):
        return getattr(self._client, "last_error", None)

    @last_error.setter
    def last_error(self, error):
        self._client.last_error = error

    def models(self, timeout=10.0):
        def before_attempt():
            self._guard("jev_attempt")
            _jev_cutoff(self._cutoff, timeout)
        before_attempt()
        models = self._client.models
        if (_accepts_explicit_keyword(models, "before_attempt")
                and _can_bind_call(models, timeout, reserve_fn=self._reserve,
                                   before_attempt=before_attempt)):
            result = models(timeout, reserve_fn=self._reserve, before_attempt=before_attempt)
        else:
            if self._reserve is not None:
                from semantic_jev import JevError
                raise JevError("budget_exceeded", "models_guard_unsupported", retryable=True)
            result = models(timeout, reserve_fn=self._reserve)
        self._guard("jev_result")
        return result

    def _evaluate(self, state, questions, deadline, **hooks):
        started = time.perf_counter()
        try:
            return self._client.evaluate(state, questions, deadline, **hooks)
        finally:
            if self._timing is not None:
                self._timing["s"] += time.perf_counter() - started

    def evaluate(self, state, questions, deadline):
        try:
            if not getattr(self._client, "supports_attempt_hooks", False):
                if self._reserve is not None:
                    # Without attempt hooks the reservation could not precede each POST.
                    from semantic_jev import JevError
                    raise JevError("budget_exceeded", "evaluate_guard_unsupported", retryable=True)
                self._guard("jev_attempt")
                _jev_cutoff(self._cutoff)
                value = self._evaluate(state, questions, deadline)
                self._guard("jev_result")
                return value
            attempt_s = float(getattr(self._client, "attempt_timeout", 0) or 0)
            def before_attempt():
                self._guard("jev_attempt")
                _jev_cutoff(self._cutoff, attempt_s)
            return self._evaluate(state, questions, deadline,
                before_attempt=before_attempt, after_result=lambda: self._guard("jev_result"),
                reserve_fn=self._reserve, transport=self._transport)
        except Exception as error:
            if hasattr(error, "kind") and hasattr(error, "retryable"):
                with suppress(Exception):
                    self._client.last_error = error
            raise


def bind_jev(client, guard, reserve=None, cutoff=None, *, transport=None, timing=None, config_path=None):
    """Bind the finite per-call Jev execution protocol; own no client-global hooks.

    config_path names the installation whose semantic.until gates each attempt
    (default: this MCS root)."""
    if client is None:
        return None
    return JevEvaluation(client, guard, reserve, cutoff, transport, timing, config_path)


_SOURCE_PASS_POLICY_KEYS = ('model', 'match_threshold', 'nomatch_threshold',
                          'calibration_version', 'fact_source', 'candidate_fingerprint')


def patient_source_assessment_descriptor(assessment, policy):
    """Describe the interpretation policy without credentials, scope lists or spending settings."""
    return {'v': 1, 'assessment': dict(assessment),
            'policy': {key: policy.get(key) for key in _SOURCE_PASS_POLICY_KEYS}}


def patient_source_pass_content(db, pid, identity, descriptor):
    """Rebuild a source PASS from its bounded policy and original Jev receipts."""
    import hashlib
    import extract_llm
    import semantic_candidate
    import semantic_policy
    from semantic_jev import JEV_MODEL

    if not db.in_transaction:
        raise ValueError('patient_source_snapshot_required')
    keys = _quality_identity(identity, purpose='source')
    if (type(pid) is not int or pid <= 0 or not isinstance(descriptor, dict)
            or set(descriptor) != {'v', 'assessment', 'policy'} or type(descriptor['v']) is not int
            or descriptor['v'] != 1 or not isinstance(descriptor['policy'], dict)
            or set(descriptor['policy']) != set(_SOURCE_PASS_POLICY_KEYS)):
        raise ValueError('patient_source_descriptor_invalid')
    policy = descriptor['policy']
    lo, hi = policy['nomatch_threshold'], policy['match_threshold']
    if (policy['model'] != JEV_MODEL or type(lo) not in (int, float) or type(hi) not in (int, float)
            or not math.isfinite(lo) or not math.isfinite(hi) or not 0 <= lo < hi <= 1
            or policy['fact_source'] not in ('legacy', 'shadow', 'canonical')
            or policy['calibration_version'] is not None and (
                not isinstance(policy['calibration_version'], str)
                or not policy['calibration_version'].strip() or len(policy['calibration_version']) > 128)
            or policy['fact_source'] == 'legacy' and policy['candidate_fingerprint'] is not None
            or policy['fact_source'] != 'legacy' and not valid_hash(policy['candidate_fingerprint'])):
        raise ValueError('patient_source_descriptor_invalid')
    assessment = descriptor['assessment']
    code = payload_hash([extract_llm._LOADED_SOURCE_DIGESTS, semantic_candidate.loaded_source_contract()])
    if (assessment != keys['assessment'] or not isinstance(assessment, dict)
            or set(assessment) != {'code', 'policy'} or assessment['code'] != code
            or assessment['policy'] != semantic_policy.policy_fingerprint(policy)):
        raise ValueError('patient_source_descriptor_invalid')
    ids = identity.get('fact_ids')
    if (not isinstance(ids, list) or not ids or len(ids) != len(set(ids))
            or set(ids) != set(keys['question_keys']) or type(identity.get('source_fact_count')) is not int
            or identity['source_fact_count'] != len(ids) - 1):
        raise ValueError('patient_source_identity_invalid')
    references = hashlib.sha256()
    for start in range(0, len(ids), 12):
        page = ids[start:start + 12]
        evidence = patient_source_question_evidence(db, pid, identity, page)
        if (evidence['problems'] or set(evidence['answers']) != set(page)
                or set(evidence['references']) != set(page)
                or any(evidence['answers'][fid]['choice'] != keys['passing_choices'][fid]
                       or evidence['answers'][fid]['confidence'] < hi for fid in page)):
            return None
        for fid in page:
            references.update(bytes.fromhex(payload_hash([fid, evidence['references'][fid]])))
    return {'question_count': len(ids), 'source_fact_count': identity['source_fact_count'],
            'answer_refs_sig': references.hexdigest(), 'descriptor': descriptor}


def patient_source_pass_descriptor(db, pid, audit_key):
    """Read only a unique, strictly parsed source PASS descriptor by audit identity."""
    from inference_version import CURRENT_VERSION, current_meta_pred

    if not db.in_transaction or type(pid) is not int or pid <= 0 or not valid_hash(audit_key):
        raise ValueError('patient_source_descriptor_invalid')
    kinds = ('patient_context_source_quality_fail_v1', 'patient_context_source_quality_pass_v1')
    bad = db.execute("SELECT 1 FROM artifacts WHERE project_id=? AND kind IN (?,?) AND CASE "
        "WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN json_type(meta)!='object' THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 "
        "ELSE 0 END LIMIT 1", (pid, *kinds)).fetchone()
    if bad:
        return None
    rows = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND "
        + current_meta_pred('meta', target=CURRENT_VERSION)
        + " AND json_extract(meta,'$.audit_key')=? ORDER BY artifact_id LIMIT 2",
        (kinds[1], pid, audit_key)).fetchall()
    if len(rows) != 1:
        return None
    content, meta = _quality_json(rows[0][0]), _quality_json(rows[0][1])
    if content is None or meta is None or meta.get('audit_key') != audit_key:
        return None
    return content.get('descriptor')


def patient_source_pass_verified(db, pid, identity, descriptor):
    """Verify recorded policy and original receipts, not current config or publication authority."""
    from inference_version import CURRENT_VERSION, current_meta_pred

    if patient_source_pass_descriptor(db, pid, identity['audit_key']) != descriptor:
        return False
    expected = patient_source_pass_content(db, pid, identity, descriptor)
    if expected is None:
        return False
    for kind, key, value in (('patient_context_source_quality_fail_v1', 'failure_key', identity['failure_key']),
                             ('patient_context_source_quality_pass_v1', 'audit_key', identity['audit_key'])):
        rows = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND "
            + current_meta_pred('meta', target=CURRENT_VERSION)
            + " AND json_extract(meta,?)=? ORDER BY artifact_id LIMIT 2",
            (kind, pid, '$.' + key, value)).fetchall()
        if kind.endswith('_fail_v1'):
            if rows:
                return False
        elif (len(rows) != 1 or _quality_json(rows[0][0]) != expected
              or not isinstance(_quality_json(rows[0][1]), dict)
              or any(_quality_json(rows[0][1]).get(key) != identity[key]
                     for key in ('input_key', 'candidate_key', 'audit_key'))):
            return False
    return True
