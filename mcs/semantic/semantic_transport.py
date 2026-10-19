"""Local semantic inference transport and its process-local/durable retry state."""
import json
import os
import sys
import time
from functools import partial

import local_llm
import semantic_runtime as runtime
from mcs_util import HOME, load_config, json_object

LLM_ENDPOINT = local_llm.ENDPOINT
LLM_MODEL = local_llm.MODEL
_LLM_EPIN, _LLM_MPIN = LLM_ENDPOINT, LLM_MODEL


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
    import maintenance
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
            maintenance.atomic_publish_text(_FMT_PATH, json.dumps(marks))
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
            # class permit (a held verdict yields "plain")
            mode = local_llm.admitted_probe_format(
                "mcs.semantic", endpoint, model, None, **kw)
        else:
            mode = local_llm.probe_format(endpoint, model, None, **kw)
        if mode is None:
            return _FMT_MODE  # GPU turn busy: nothing sent, probe again next call
        _FMT_MODE = mode
    except Exception:
        _FMT_MODE = "plain"
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
_LONG_MIN_CALL_S = 620.0
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
        import maintenance
        os.makedirs(os.path.dirname(_LONG_PATH), exist_ok=True)
        with open(_LONG_PATH + ".lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marks = _long_marks()
            level = max(level, marks.pop(key, 0))
            marks[key] = level
            while len(marks) > _LONG_MAX:
                marks.pop(next(iter(marks)))
            maintenance.atomic_publish_text(_LONG_PATH, json.dumps(marks))
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
    rf = {"type": "json_object"} \
        if _probe_format(endpoint, model, timeout) == "object" else None
    key = _long_key(model, prompt)
    level = _long_marks().get(key, 0)
    if level >= 2:
        return _failed("long_ceiling")   # even the long ceiling stopped on length
    timeout = call_deadline - time.monotonic()
    if timeout <= 0:
        return _failed("deadline")
    if level and timeout < _LONG_MIN_CALL_S:
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
            turn_min_s=_LONG_MIN_CALL_S if level
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
            if level and timeout < _LONG_MIN_CALL_S:
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
            if level == 1 and timeout >= _LONG_MIN_CALL_S:
                continue  # one long-ceiling retry in what is left
        break
    if response is None and err_out.get("kind") == "busy":
        # the other backlog lane held the GPU turn past the deadline:
        # nothing sent, endpoint healthy — a short defer, no backoff
        _failed("busy")
        raise runtime.LLMNotSent("llm_turn_busy")
    if response is None and err_out.get("kind") == "unreachable":
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
    return response["text"]

