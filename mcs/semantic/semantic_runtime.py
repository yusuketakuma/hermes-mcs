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

from mcs_util import loads_dict, payload_hash, strict_json
from semantic_usage_receipts import (
    MAX_SQL_INTEGER as MAX_SQL_INTEGER,
    JevReservation as JevReservation,
    _QUALITY_ID_KEYS as _QUALITY_ID_KEYS,
    _QUALITY_PURPOSES as _QUALITY_PURPOSES,
    _QUALITY_QIDS as _QUALITY_QIDS,
    _quality_purpose as _quality_purpose,
    _quality_json as _quality_json,
    _quality_identity as _quality_identity,
    _quality_request as _quality_request,
    _quality_bound as _quality_bound,
    _NOT_SENT_REASONS as _NOT_SENT_REASONS,
    _quality_result_parts as _quality_result_parts,
    _quality_answer_evidence as _quality_answer_evidence,
    _source_quality_rows as _source_quality_rows,
    _source_usage_artifact as _source_usage_artifact,
    _source_answer_record as _source_answer_record,
    _source_new_answer_refs as _source_new_answer_refs,
    patient_source_answer_binding as patient_source_answer_binding,
    patient_source_question_evidence as patient_source_question_evidence,
    _quality_result as _quality_result,
    patient_summary_usage_resolved as patient_summary_usage_resolved,
    patient_summary_usage_prunable as patient_summary_usage_prunable,
    patient_summary_request_problem as patient_summary_request_problem,
    patient_summary_resolve_request_tx as patient_summary_resolve_request_tx,
    _SOURCE_PASS_POLICY_KEYS as _SOURCE_PASS_POLICY_KEYS,
    patient_source_assessment_descriptor as patient_source_assessment_descriptor,
    patient_source_pass_content as patient_source_pass_content,
    patient_source_pass_descriptor as patient_source_pass_descriptor,
    patient_source_pass_verified as patient_source_pass_verified,
)


DEFAULT_ATTEMPT_LIMIT = 6


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
CLOUD_OPERATING_LIMITS = ("daily_request_budget", "daily_cost_limit_usd", "monthly_cost_limit_usd",
                          "max_concurrency", "usd_jpy", "window_budget_usd", "window_start", "until",
                          "provider_budgets")
_GENERATION_EXEMPT = {"semantic": ("until",), "cloud_llm": CLOUD_OPERATING_LIMITS}


def config_generation(cfg: dict) -> str:
    """Stable generation for the config snapshot, apart from run-window and spending settings."""
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    for section, keys in _GENERATION_EXEMPT.items():
        if isinstance(cfg.get(section), dict):
            cfg[section] = {k: v for k, v in cfg[section].items() if k not in keys}
            if not cfg[section]:
                # first adding only an exempt key must not look like a new section
                del cfg[section]
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
    if token.kind == 'semantic':
        import semantic_intake_hold
        semantic_intake_hold.validate_indexes(ledger.db)
        semantic_intake_hold._unstored_unique_index(ledger.db)
    row = ledger.db.execute(
        "SELECT job_id,kind,project_id,message_id,state,attempts,payload, "
        "CASE WHEN kind='semantic' AND EXISTS (SELECT 1 FROM main.artifacts "
        "INDEXED BY idx_semantic_intake_hold_root WHERE kind='semantic_intake_hold_v1' "
        "AND project_id=fetch_jobs.project_id AND model=CAST(fetch_jobs.message_id AS TEXT)) "
        "OR kind='semantic' AND (EXISTS (SELECT 1 FROM main.fetch_jobs u "
        "WHERE u.kind='semantic_intake_unstored_root_v1' AND u.project_id=fetch_jobs.project_id "
        "AND u.message_id IN (0,fetch_jobs.message_id)) OR EXISTS (SELECT 1 FROM main.fetch_jobs p "
        "WHERE p.kind='semantic_intake_unstored_v1' AND p.project_id=fetch_jobs.project_id AND p.message_id=0 "
        "AND (p.state IS NOT 'pending' OR typeof(p.attempts)!='integer' OR p.attempts<1 "
        "OR p.payload IS NOT '{}' OR p.reason_code IS NOT NULL "
        "OR NOT EXISTS (SELECT 1 FROM main.fetch_jobs u WHERE u.kind='semantic_intake_unstored_root_v1' "
        "AND u.project_id=fetch_jobs.project_id)))) "
        "THEN 1 ELSE 0 END AS intake_held "
        "FROM fetch_jobs WHERE job_id=?", (token.job_id,)).fetchone()
    if row is None or row['intake_held']:
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
_NO_INTAKE_HOLD = (
    " AND (kind!='semantic' OR NOT EXISTS (SELECT 1 FROM main.artifacts "
    "INDEXED BY idx_semantic_intake_hold_root WHERE kind='semantic_intake_hold_v1' "
    "AND project_id=fetch_jobs.project_id AND model=CAST(fetch_jobs.message_id AS TEXT)))"
    " AND (kind!='semantic' OR NOT EXISTS (SELECT 1 FROM main.fetch_jobs u "
    "WHERE u.kind='semantic_intake_unstored_root_v1' AND u.project_id=fetch_jobs.project_id "
    "AND u.message_id IN (0,fetch_jobs.message_id)))"
    " AND (kind!='semantic' OR NOT EXISTS (SELECT 1 FROM main.fetch_jobs p "
    "WHERE p.kind='semantic_intake_unstored_v1' AND p.project_id=fetch_jobs.project_id AND p.message_id=0 "
    "AND (p.state IS NOT 'pending' OR typeof(p.attempts)!='integer' OR p.attempts<1 "
    "OR p.payload IS NOT '{}' OR p.reason_code IS NOT NULL "
    "OR NOT EXISTS (SELECT 1 FROM main.fetch_jobs u WHERE u.kind='semantic_intake_unstored_root_v1' "
    "AND u.project_id=fetch_jobs.project_id))))")


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
    if token.kind == 'semantic':
        import semantic_intake_hold
        semantic_intake_hold.validate_indexes(ledger.db)
        semantic_intake_hold._unstored_unique_index(ledger.db)
    now = time.time()
    args = _where_args(token)
    if action == "done":
        cur = ledger.db.execute(
            "UPDATE fetch_jobs SET state='done',updated_at=? "
            "WHERE job_id=? AND kind=? AND project_id=? AND message_id=? "
            "AND state=? AND attempts=? AND " + _PAYLOAD_MATCH + _NO_INTAKE_HOLD,
            (now, *args))
    elif action == "defer":
        cur = ledger.db.execute(
            "UPDATE fetch_jobs SET next_try=?,updated_at=? "
            "WHERE job_id=? AND kind=? AND project_id=? AND message_id=? "
            "AND state=? AND attempts=? AND " + _PAYLOAD_MATCH + _NO_INTAKE_HOLD,
            (now + max(0.0, retry_in), now, *args))
    elif action == "retry":
        cur = ledger.db.execute(
            "UPDATE fetch_jobs SET attempts=attempts+1,next_try=?,"
            "state=CASE WHEN attempts+1>=? THEN 'failed' ELSE state END,"
            "updated_at=? WHERE job_id=? AND kind=? AND project_id=? "
            "AND message_id=? AND state=? AND attempts=? AND "
            + _PAYLOAD_MATCH + _NO_INTAKE_HOLD,
            (now + max(0.0, retry_in), max(1, int(max_attempts)), now,
             *args))
    elif action == "failed":
        cur = ledger.db.execute(
            "UPDATE fetch_jobs SET state='failed',updated_at=? "
            "WHERE job_id=? AND kind=? AND project_id=? "
            "AND message_id=? AND state=? AND attempts=? AND "
            + _PAYLOAD_MATCH + _NO_INTAKE_HOLD,
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
        state = strict_json(row["content"] or "{}")
        count = state.get("consecutive_failures")
        open_until = state.get("open_until", 0.0)
        failure_class = state.get("failure_class")
        if (not isinstance(state, dict) or type(count) is not int or count < 0
                or type(open_until) not in (int, float)
                or not math.isfinite(float(open_until))
                or (failure_class is not None
                    and failure_class not in (_CIRCUIT_FAILURE_CLASSES |
                        ({"auth_error", "payment_required", "pricing_unverified", "contract_error"}
                         if kind == "cloud_circuit" else set())))
                or (kind == "cloud_circuit"
                    and failure_class in {"auth_error", "payment_required", "pricing_unverified"}
                    and open_until <= _circuit_now(now))):
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
        return _record_cloud_circuit_result_tx(ledger, error, now, reset=reset)


def _record_cloud_circuit_result_tx(ledger, error, now=None, *, reset=False) -> bool:
    """Update the cloud circuit inside its caller's active transaction."""
    if not ledger.db.in_transaction:
        raise RuntimeError("cloud_circuit_transaction_required")
    return _record_circuit_result(ledger, error, now, kind="cloud_circuit", reset=reset)


def _record_circuit_result(ledger, error, now=None, *, kind=_CIRCUIT_ARTIFACT_KIND, reset=False) -> bool:
    """Persist one Jev result and return the resulting open state.

    Retryable rate-limit/5xx/transport/timeout failures count; cloud circuits
    also count contract errors and hold auth/payment/pricing failures.
    Error details are reduced to a fixed class label before storage.
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
        if kind == "cloud_circuit" and not reset and _circuit_is_open(state, current):
            return True
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
    absolute_path = os.path.abspath(path)
    try:
        # Explicit config paths can name another MCS root. Never inspect the
        # current installation when the caller bound this request elsewhere.
        observed = None
        if os.path.basename(absolute_path) == "config.json":
            import provider_settings
            root = os.path.dirname(absolute_path)
            observed = provider_settings.probe(root)
            if observed["state"] == "pending":
                raise ValueError("provider_settings_pending")
        if observed is not None and observed["state"] == "requires_checked_read":
            import cloud_policy
            import semantic_jev
            owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
            state = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
            if state["mode"] != "split":
                raise ValueError("provider_settings_unverified")
            until = state["providers"]["jev"].get("until")
        else:
            if not os.path.lexists(path):
                if observed is not None and provider_settings.probe(root) != observed:
                    raise ValueError("provider_settings_changed")
                return
            cfg = mcs_util.load_config_checked(path)
            if observed is not None and provider_settings.probe(root) != observed:
                raise ValueError("provider_settings_changed")
            block = cfg.get("semantic") if isinstance(cfg, dict) else None
            if cfg is None or (block is not None and not isinstance(block, dict)):
                raise ValueError("semantic_config_unreadable")
            until = (block or {}).get("until")
    except (OSError, ValueError, TypeError, RecursionError):
        raise JevError("budget_exceeded", "semantic_config_unreadable", retryable=retryable) from None
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
                   daily_request_budget: int | None = None, config_path=None):
    """Build a durable one-request reservation written before POST; config_path
    names the config whose semantic.until gates it (default: this MCS root)."""
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
            if token.kind == 'semantic' and not job_matches(ledger, token):
                raise RuntimeStale('jev_reserve')
            day = int(_jst_day_start(time.time()))
            _daily_cap_check(ledger, daily_request_budget, retryable=True, config_path=config_path)
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


# Exact artifact kind owned by patient_context_refresh. The value is a storage contract.


# A reservation whose POST provably never started (the run window ended between
# its commit and the send) is sealed with one of these reasons and no answers.


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
    payload_pl = parse_payload({'payload': token.payload_raw})
    execution = payload_pl.get('execution_generation')
    contract = payload_pl.get('execution_contract')
    if ((execution is not None and expected_execution_generation is not None
            and execution != expected_execution_generation)
            or (type(contract) is int and contract == 1
                and (expected_execution_generation is None
                     or execution != expected_execution_generation))):
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
    def __init__(self, client, guard, reserve, cutoff, transport, timing, config_path=None, not_sent=None):
        self._client = client
        unsent = [None]   # committed by reserve, not yet past its pre-POST attempt hook

        def reserving(body, timeout):
            unsent[0] = reserve(body, timeout)
            return unsent[0]

        def windowed(stage):
            guard(stage)
            if stage != "jev_attempt":
                return
            if reserve is None:
                _jev_window_check(config_path=config_path)
                return
            # The client calls this hook after each reservation and before its POST,
            # so the window can still end while BEGIN waited; that request is provably unsent.
            pending, unsent[0] = unsent[0], None
            if pending is None:
                return
            from semantic_jev import JevError
            try:
                _jev_window_check(True, config_path=config_path)
            except JevError as error:
                if not_sent is not None:
                    with suppress(Exception):   # unsealed, it stays unresolved: fail closed
                        not_sent(pending, error.detail)
                raise
        self._guard = windowed
        self._reserve = None if reserve is None else reserving
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


def bind_jev(client, guard, reserve=None, cutoff=None, *, transport=None, timing=None, config_path=None,
             not_sent=None):
    """Bind the finite per-call Jev execution protocol; own no client-global hooks.

    config_path names the installation whose semantic.until gates each attempt
    (default: this MCS root); not_sent(reservation, reason) settles a reservation
    stopped before its POST."""
    if client is None:
        return None
    return JevEvaluation(client, guard, reserve, cutoff, transport, timing, config_path, not_sent)
