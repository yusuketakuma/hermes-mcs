"""Local semantic inference transport and its process-local/durable retry state."""
import safe_io
import json
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial

import local_llm
import semantic_runtime as runtime
from mcs_util import HOME, load_config, json_object

LLM_ENDPOINT = local_llm.ENDPOINT
LLM_MODEL = local_llm.MODEL
_LLM_EPIN, _LLM_MPIN = LLM_ENDPOINT, LLM_MODEL
_job_model = ContextVar("semantic_job_model", default=None)
_inference_trace = ContextVar("semantic_inference_trace", default=None)
_inference_target = ContextVar("semantic_inference_target", default=None)
_inference_generation = ContextVar("semantic_inference_generation", default=None)
_inference_purpose = ContextVar("semantic_inference_purpose", default="backlog")
_job_providers = ContextVar("semantic_job_providers", default=None)


@contextmanager
def quality_repair():
    """Select the repair profile only while executing a reserved QC repair."""
    purpose = _inference_purpose.set("quality_repair")
    model = _job_model.set(_job_model.get())
    try:
        yield
    finally:
        _job_model.reset(model)
        _inference_purpose.reset(purpose)


@contextmanager
def inference_targets(generation=None):
    token = _inference_target.set(None)
    generation_token = _inference_generation.set(generation)
    try:
        yield
    finally:
        _inference_generation.reset(generation_token)
        _inference_target.reset(token)


def set_inference_target(message_id, phase="facts"):
    _inference_target.set((message_id, phase))


@contextmanager
def trace_inference():
    """Capture accepted responses inside one source-bound stage."""
    providers = []
    token = _inference_trace.set(providers)
    try:
        yield providers
    finally:
        _inference_trace.reset(token)


def _record_inference(provider):
    providers = _inference_trace.get()
    if providers is not None and provider is not None:
        providers.append(provider)


def llm_conf(cfg: dict | None = None) -> tuple[str, str]:
    """(endpoint, model) — local_llm.url/local_llm.model over the pinned
    defaults; patched module constants take precedence (test seam)."""
    ep, mdl = local_llm.resolve(
        cfg if cfg is not None else load_config())
    if LLM_ENDPOINT != _LLM_EPIN:
        ep = LLM_ENDPOINT
    if LLM_MODEL != _LLM_MPIN:
        mdl = LLM_MODEL
    return ep, mdl


def llm_model(cfg: dict | None = None) -> str:
    return llm_conf(cfg)[1]


def inference_model() -> str:
    """Identify the actual backend without changing the shared source/cache contract."""
    provider = _job_model.get()
    return provider.model if provider is not None else llm_model()


def inference_contract():
    """Return the fixed request contract of the active cloud producer."""
    provider = _job_model.get()
    return dict(provider.contract) if provider is not None else None


def inference_receipt():
    """Return the last completed cloud reservation for the active producer."""
    provider = _job_model.get()
    if provider is None or not provider.completed_reservations:
        return None
    return dict(provider.completed_reservations[-1])


def inference_provider() -> str:
    """Identify the current inference backend without inferring old provenance."""
    provider = _job_model.get()
    if provider is None:
        return 'local'
    return provider.selection['provider']


def inference_requests() -> list:
    """Keep actual paid calls distinct from reused local or cloud stage results."""
    providers = _job_providers.get()
    if providers is None:
        provider = _job_model.get()
        providers = [provider] if provider is not None else []
    else:
        current = _job_model.get()
        providers = [p for p in providers if p is not current] + ([current] if current is not None else [])
    return [r["reservation_id"] for provider in providers for r in provider.completed_reservations]


# LLMNotSent stages that prove nothing was dispatched — every CloudNotSent
# subclass lands here through not_sent_reason; settled chunks keep receipts.
# The local path only runs after every provider is proven not possibly_sent,
# so its refusals cannot taint cloud receipts either.  Admission verdicts are
# dynamic (llm_admission:<state>) and checked by prefix at the discard site.
_PRESEND_REFUSAL_STAGES = frozenset({
    "cloud_not_sent", "cloud_unit_unresolved", "cloud_owner_transaction",
    "cloud_deadline", "cloud_budget_blocked", "cloud_pricing_unverified",
    "cloud_capacity", "cloud_profile_unavailable", "realtime_target",
    "probe_unsent", "long_output_needs_budget", "llm_turn_busy",
    "llm_unreachable", "llm_unavailable"})


@contextmanager
def backlog_inference(ledger, cfg, job, deadline, current, transport, *, local_fallback=None,
                      fallback_current=None):
    """Route historical semantic inference through the existing cloud budget guards."""
    import extract_llm
    import cloud_inference
    import cloud_client
    import cloud_policy
    import writer_lock
    pl = runtime.parse_payload(job)
    if not cloud_policy.history_target_ready(ledger.db, job["project_id"], job["message_id"]) or not cloud_policy.history_thread_allowed(
            ledger.db, job["project_id"], job["message_id"]) or extract_llm._cloud_mode(
            ledger, cfg, "semantic_backlog", job["project_id"]) != "on":
        yield None
        return
    scope_snapshot = cloud_policy.history_thread_snapshot(ledger.db, job['project_id'], job['message_id'])
    if scope_snapshot is None:
        yield None
        return
    lock_lost = None

    def dispatch(call, **unused):
        # A lost run lock leaves the reservation held; callbacks must not write.
        if lock_lost is not None:
            raise lock_lost
        return call()

    def request(call, *args, **kwargs):
        nonlocal lock_lost
        try:
            return transport(call, *args, **kwargs)
        except writer_lock.RunLockLost as error:
            lock_lost = error
            raise

    def scoped_current():
        return current() and cloud_policy.history_thread_snapshot(
            ledger.db, job["project_id"], job["message_id"]) == scope_snapshot

    providers = {}

    def get_provider(purpose):
        if purpose not in providers:
            providers[purpose] = cloud_inference.CloudInferenceUnit(
                ledger, cfg, route="semantic_backlog", purpose=purpose, project_id=job["project_id"],
                message_id=job["message_id"], unit_key=f"semantic:{job['job_id']}:{pl.get('generation', '')}",
                deadline=deadline, current=scoped_current, dispatch=dispatch, transport=request)
        return providers[purpose]

    try:
        provider = get_provider("backlog")
    except cloud_inference.CloudFallback:
        raise runtime.LLMNotSent('cloud_profile_unavailable') from None

    switched = False

    def call_local(prompt, timeout):
        if not scoped_current():
            raise runtime.LLMNotSent('cloud_fallback_source_changed')
        result = local_fallback(prompt) if timeout is None else local_fallback(prompt, timeout=timeout)
        if not scoped_current():
            raise runtime.LLMNotSent('cloud_fallback_source_changed')
        return result

    def local(prompt, timeout):
        nonlocal switched
        if (local_fallback is None or any(p.possibly_sent for p in providers.values()) or not scoped_current()
                or fallback_current is not None and not fallback_current()):
            raise runtime.LLMNotSent('cloud_fallback_unavailable')
        switched = True
        _job_model.set(None)
        return call_local(prompt, timeout)

    def chat(prompt, timeout=None):
        if switched:
            _job_model.set(None)
            return call_local(prompt, timeout)
        try:
            provider = get_provider(_inference_purpose.get())
        except cloud_inference.CloudFallback:
            raise runtime.LLMNotSent('cloud_profile_unavailable') from None
        _job_model.set(provider)
        _failed(None)
        target = _inference_target.get()
        provider.message_id = target[0] if target else job["message_id"]
        if not cloud_policy.history_target_ready(ledger.db, job["project_id"], provider.message_id):
            raise runtime.LLMNotSent('realtime_target')
        provider.owner = {"semantic_target_id": provider.message_id, "semantic_thread_root": job["message_id"]}
        if target and _inference_generation.get():
            from inference_dependency_loss import source_binding
            from mcs_util import payload_hash
            members = ledger.db.execute("SELECT message_id FROM messages WHERE project_id=? "
                "AND (message_id=? OR parent_id=?) ORDER BY message_id",
                (job["project_id"], job["message_id"], job["message_id"])).fetchall()
            occurrences = [(row[0], source_binding(ledger.db, job["project_id"], row[0])) for row in members]
            if not occurrences or any(binding is None for _, binding in occurrences):
                raise runtime.LLMNotSent('cloud_source_unverified')
            source_occurrences = ([item for item in occurrences if item[0] == provider.message_id]
                                  if target[1] == 'facts' else occurrences)
            if not source_occurrences:
                raise runtime.LLMNotSent('cloud_target_unverified')
            provider.owner.update(semantic_source=payload_hash(source_occurrences),
                                  semantic_generation=_inference_generation.get(), semantic_phase=target[1])
        try:
            response = provider(prompt, deadline=min(deadline, time.monotonic() + (timeout or LLM_TIMEOUT)),
                                max_tokens=provider.selection['max_tokens'] or LLM_LONG_MAX_TOKENS)
        except cloud_client.CloudNotSent as error:
            reason = cloud_client.not_sent_reason(error)
            raise runtime.LLMNotSent("cloud_not_sent" if reason == "not_sent" else reason) from error
        except cloud_inference.CloudBusy:
            if local_fallback is not None:
                return local(prompt, timeout)
            raise runtime.LLMNotSent("cloud_capacity") from None
        except cloud_inference.CloudFallback as error:
            if (local_fallback is not None and str(error) != 'cloud_unit_changed'
                    and not any(p.possibly_sent for p in providers.values())):
                return local(prompt, timeout)
            current()
            return _failed("cloud_failed")
        error = local_llm.acceptance_error(response)
        if error:
            return _failed(error)
        _record_inference(provider.selection['provider'])
        return response["text"]

    token = _job_model.set(provider)
    providers_token = _job_providers.set(providers.values())
    try:
        yield chat
    except runtime.RuntimeBudgetShort:
        # The next call did not start; previously persisted chunks keep their receipts.
        raise
    except runtime.RuntimeGuardError as error:
        # A definite pre-send refusal is a harmless budget wait: already
        # settled chunks/receipts stay. Scope or lease loss and every other
        # guard failure still discards the unit's dispositions.
        stage = getattr(error, "stage", None)
        if (lock_lost is None and stage not in _PRESEND_REFUSAL_STAGES
                and not (isinstance(stage, str) and stage.startswith("llm_admission:"))):
            for selected in providers.values():
                try:
                    selected.discard()
                except Exception:
                    # An undiscardable receipt stays settled and visible; a
                    # bookkeeping failure must not replace the guard error.
                    pass
        raise
    finally:
        _job_providers.reset(providers_token)
        _job_model.reset(token)
# The per-call timeout allows bounded fact documents to finish under
# shared load. The drain also uses this as timeout_cap, except in the
# backlog lane, where the job deadline bounds the call instead.
LLM_TIMEOUT = 600

# Fact documents and summaries can exceed the shared output default.
# A length-truncated document fails acceptance and must not become an
# endless model retry. The ceiling bounds output; timeout bounds wall time.
LLM_MAX_TOKENS = 4096


# ---------- local LLM (existing endpoint, same isolation) ----------

# Constrained JSON output for every semantic prompt (facts v2, legacy
# facts, summary, repairs all end in "JSON:"): the server's json_object
# grammar removes prose-wrapped/unterminated replies that have no
# parseable summary. Same probe/cooldown/degrade ladder as
# extract_llm, minus the schema rung; a rejected format degrades to
# plain for one retry and re-probes after the cooldown.
_FMT_MODE = None      # None=unprobed | "object" | "plain"
_FMT_TS = 0.0
_PROBE_RETRY_S = 600
_FMT_REJECT_STATUSES = (400, 404, 422)
_FMT_PATH = os.path.join(HOME, "data", "semantic_format_rejections.json")


def _format_key(endpoint: str, model: str) -> str:
    import hashlib
    return hashlib.sha256(f"{endpoint}\0{model}".encode("utf-8")).hexdigest()


def _format_marks() -> dict:
    try:
        with open(_FMT_PATH, encoding="utf-8") as f:
            marks = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(marks, dict):
        return {}
    now = time.time()
    return {k: v for k, v in marks.items()
            if type(v) in (int, float) and 0 <= now - v < _PROBE_RETRY_S}


def _remember_plain(endpoint: str, model: str) -> None:
    # Hashes + rejection times only, never prompts or server error bodies.
    # Serialize the read/merge/publish across workers; atomic replacement
    # alone would lose another endpoint's cooldown on a concurrent write.
    import fcntl
    try:
        os.makedirs(os.path.dirname(_FMT_PATH), exist_ok=True)
        with open(_FMT_PATH + ".lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marks = _format_marks()
            key = _format_key(endpoint, model)
            marks.pop(key, None)
            marks[key] = time.time()
            while len(marks) > 500:
                marks.pop(next(iter(marks)))
            safe_io.atomic_publish_text(_FMT_PATH, json.dumps(marks))
    except OSError:
        # Persistence is an optimization; the same-call retry and the
        # existing in-memory cooldown still work on read-only/full disks.
        pass


def _probe_format(endpoint: str, model: str, timeout: float = 10) -> str:
    global _FMT_MODE, _FMT_TS
    if _format_key(endpoint, model) in _format_marks():
        return "plain"
    if _FMT_MODE == "object":
        return _FMT_MODE
    if _FMT_MODE is not None \
            and time.monotonic() - _FMT_TS < _PROBE_RETRY_S:
        return _FMT_MODE
    try:
        kw = dict(timeout=min(10, timeout),
                  request_fn=local_llm.bounded_request,
                  slot=local_llm.request_slot(),
                  verify=lambda text: json_object(text) is not None)
        if local_llm.admission_enabled():
            # T20: the probe sends real inference POSTs — it takes a
            # class permit (a held/deferred verdict yields None)
            mode = local_llm.admitted_probe_format(
                "mcs.semantic", endpoint, model, None, **kw)
        else:
            mode = local_llm.probe_format(endpoint, model, None, **kw)
        if mode is None:
            return _FMT_MODE  # GPU turn busy: nothing sent, probe again next call
        _FMT_MODE = mode
    except Exception:
        # an unobserved probe is not a plain verdict — stay unprobed
        return _FMT_MODE
    _FMT_TS = time.monotonic()
    return _FMT_MODE


# Fact-rich messages may exceed the normal output ceiling. Repeating
# the same request can reproduce the same length stop and waste a slot.
# A length stop retries ONCE with LLM_LONG_MAX_TOKENS in the remaining
# budget. The prompt is remembered (1 = start at the long ceiling,
# 2 = even that stopped on length: fail fast without a model call), so
# later attempts spend the long call only once. Prompt and ceiling fit
# one slot.
LLM_LONG_MAX_TOKENS = 8192
_LONG_PATH = os.path.join(HOME, "data", "semantic_long_output.json")
_LONG_MAX = 500
# A long-ceiling call is deferred when its remaining budget is too
# short to complete. Deferral consumes no attempt; background lanes
# with sufficient time may take it, while a short tick lane cannot.
LONG_MIN_CALL_S = 620.0
# Least time a send may have left after the backlog GPU turn is won;
# capped at half the live window so a short job budget never reports
# busy uncontended, and a turn won with scraps is deferred, not sent.
_TURN_MIN_CALL_S = 60.0


def _long_key(model: str, prompt: str) -> str:
    import hashlib
    return hashlib.sha256(f"{model}\0{prompt}".encode("utf-8")).hexdigest()


def _long_marks() -> dict:
    try:
        with open(_LONG_PATH, encoding="utf-8") as f:
            marks = json.load(f)
    except (OSError, ValueError, RecursionError):
        marks = {}
    if not isinstance(marks, dict):
        marks = {}
    # hand-edited values (non-int) would raise at `level >= 2`
    out = {k: v for k, v in marks.items() if type(v) is int}
    # marks this process could not persist still apply here (T8)
    for k, v in _LONG_UNSAVED.items():
        out[k] = max(v, out.get(k, 0))
    return out


# Marks whose durable save failed (lock busy, disk/permission error):
# kept in-process so this worker does not repeat the doomed base-ceiling
# call, bounded like the file. Counted for the run record instead of
# being dropped silently (T8).
_LONG_UNSAVED: dict = {}
_LONG_MARK_FAILURES = {"busy": 0, "error": 0}


def long_mark_failures() -> dict:
    """Cumulative unsaved long-output marks of this process by cause."""
    return dict(_LONG_MARK_FAILURES)


def _long_mark(key: str, level: int) -> None:
    """Keep retry suppression monotonic without waiting for another worker.
    A failed save stays non-fatal but is logged, counted and remembered
    in-process."""
    import fcntl
    try:
        os.makedirs(os.path.dirname(_LONG_PATH), exist_ok=True)
        with open(_LONG_PATH + ".lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marks = _long_marks()
            level = max(level, marks.pop(key, 0))
            marks[key] = level
            while len(marks) > _LONG_MAX:
                marks.pop(next(iter(marks)))
            safe_io.atomic_publish_text(_LONG_PATH, json.dumps(marks))
        _LONG_UNSAVED.clear()   # the merged write persisted them all
    except Exception as e:
        cause = "busy" if isinstance(e, BlockingIOError) else "error"
        _LONG_MARK_FAILURES[cause] += 1
        _LONG_UNSAVED[key] = max(level, _LONG_UNSAVED.pop(key, 0))
        while len(_LONG_UNSAVED) > _LONG_MAX:
            _LONG_UNSAVED.pop(next(iter(_LONG_UNSAVED)))
        # code and exception class only — never the prompt or its key
        print(json.dumps({"event": "semantic_long_mark_unsaved",
                          "cause": cause, "error": type(e).__name__,
                          "level": level}),
              file=sys.stderr, flush=True)


_LLM_FAILURE: str | None = None


def last_llm_failure() -> str | None:
    """Failure code of the latest llm_chat call, or None after a success.

    Run records carry the code only — never model text."""
    return _LLM_FAILURE


def _failed(code: str | None):
    global _LLM_FAILURE
    _LLM_FAILURE = code
    return None


def llm_chat(prompt: str, timeout: int = LLM_TIMEOUT,
             max_tokens: int = LLM_MAX_TOKENS) -> str | None:
    """One local-llama.cpp chat call via the shared loopback adapter.
    The model gets no tools and no send capability; worker-isolated
    request, no proxy, no redirect, bounded bytes (INV-13, §15.3).
    Returns raw text or None (a model failure); raises
    ``semantic_runtime.LLMNotSent`` when the request never left — an
    admission hold/deferral or a refused connection — so callers can
    wait without consuming an attempt (mirrors extract_llm's
    ``_DEFERRED``)."""
    endpoint, model = llm_conf()
    err_out: dict = {}
    _failed(None)
    global _FMT_MODE, _FMT_TS
    # T20: under admission each send (retries included) takes its own
    # class permit; the loop below is otherwise identical
    send = partial(local_llm.admitted_chat, "mcs.semantic") \
        if local_llm.admission_enabled() else local_llm.chat
    # the probe spends the caller's budget, never adds to it: the
    # runtime sized ``timeout`` to the job deadline (absolute)
    call_deadline = time.monotonic() + timeout
    fmt = _probe_format(endpoint, model, timeout)
    if fmt is None:
        # the probe never left — an unobserved format defers the real
        # call like any other admission hold, never a silent plain send
        _failed("probe_unsent")
        raise runtime.LLMNotSent("probe_unsent")
    rf = {"type": "json_object"} if fmt == "object" else None
    key = _long_key(model, prompt)
    level = _long_marks().get(key, 0)
    if level >= 2:
        return _failed("long_ceiling")   # even the long ceiling stopped on length
    timeout = call_deadline - time.monotonic()
    if timeout <= 0:
        return _failed("deadline")
    if level and timeout < LONG_MIN_CALL_S:
        _failed("long_output_needs_budget")
        raise runtime.LLMNotSent("long_output_needs_budget")
    while True:
        timeout = call_deadline - time.monotonic()
        if timeout <= 0:
            return _failed("deadline")
        response = send(
            prompt, endpoint=endpoint, model=model,
            timeout=timeout, deadline=call_deadline,
            max_tokens=max(max_tokens, LLM_LONG_MAX_TOKENS)
            if level else max_tokens,
            response_format=rf,
            extra_payload={"id_slot": local_llm.request_slot()},
            request_fn=local_llm.bounded_request, error_out=err_out,
            turn_min_s=LONG_MIN_CALL_S if level
            else min(_TURN_MIN_CALL_S, timeout / 2))
        if response is not None and response.get("admission"):
            # a denial/deferral never left — the verdict, never model text
            raise runtime.LLMNotSent(
                f"llm_admission:{response['admission']}")
        timeout = call_deadline - time.monotonic()
        if rf is not None and response is not None \
                and response.get("status") in _FMT_REJECT_STATUSES:
            # the server rejected the constraint (restart / model
            # swap): degrade to plain for this and later calls —
            # the retry shares what is left of the caller's budget
            _FMT_MODE, _FMT_TS = "plain", time.monotonic()
            _remember_plain(endpoint, model)
            timeout = call_deadline - time.monotonic()
            rf = None
            if timeout <= 0:
                # A format refusal is not a plain-prompt rejection.
                # Exhausted work stays retryable without another send.
                return _failed("format_rejected")
            if level and timeout < LONG_MIN_CALL_S:
                # The rejected request already left: do not report a
                # free not-sent defer, or send a long call that cannot
                # fit. A format refusal says nothing about the prompt,
                # so this is a retryable model failure, never the plain
                # HTTP 400 terminal rejection; plain is remembered and
                # the mark kept for a later adequately sized try.
                return _failed("format_rejected")
            continue
        if response is not None \
                and response.get("finish_reason") == "length":
            level += 1
            _long_mark(key, level)
            timeout = call_deadline - time.monotonic()
            if level == 1 and timeout >= LONG_MIN_CALL_S:
                continue  # one long-ceiling retry in what is left
        break
    if response is None and err_out.get("kind") == "busy":
        # the other backlog lane held the GPU turn past the deadline:
        # nothing sent, endpoint healthy — a short defer, no backoff
        _failed("busy")
        raise runtime.LLMNotSent("llm_turn_busy")
    if response is None and err_out.get("kind") in ("unreachable", "not_sent"):
        _failed("unreachable")
        raise runtime.LLMNotSent("llm_unreachable")
    if response is not None and response.get("status") == 503:
        # llama-server answers 503 while it is still loading the model
        # (daily restart): no work was done, so defer like a refused connection
        _failed("unavailable")
        raise runtime.LLMNotSent("llm_unavailable")
    if response is not None and response.get("status") == 400:
        # the server definitively rejected the request (context
        # oversize under a fixed slot, or a malformed body) — retrying
        # the same input can never succeed, so this is terminal, not a
        # retryable model failure
        _failed("prompt_rejected")
        raise runtime.LLMRejected("prompt_rejected")
    # canonical acceptance: a length-truncated or empty completion is an
    # incomplete result, never a success payload — even when its text
    # happens to parse (C05)
    acceptance = local_llm.acceptance_error(response)
    if acceptance is not None:
        return _failed(acceptance)
    _record_inference("local")
    return response["text"]
