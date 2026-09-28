"""Loopback local-LLM transport and response metadata.

One stdlib-only adapter shared by the canonical semantic path and the
legacy ``extract_llm`` path.  It returns the response text plus
``finish_reason`` and token ``usage`` so callers can apply their own
acceptance policy: canonical callers reject ``length`` stops and empty
or malformed structured output, while legacy callers keep their current
behaviour and only record bounded integrity metadata.

Transport rules are inherited unchanged: loopback-only endpoint, no
proxy, no redirects, bounded response size, absolute deadlines, and no
tool access.  There is no cloud fallback and no service change.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
from contextlib import suppress

import bounded_http

ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "Qwen3.5-9B"
MAX_TOKENS = 1400
TIMEOUT = 90


def resolve(cfg: dict | None) -> tuple[str, str]:
    """(endpoint, model) — ``local_llm.url``/``local_llm.model`` keys in
    config.json override the pinned defaults (a self-hosted
    OpenAI-compatible server on another port, a different GGUF), while
    absent or malformed values keep the built-in llama.cpp pin. The
    loopback-only wire gate in ``chat``/``bounded_http`` still applies
    to whatever the config points at."""
    endpoint, model = ENDPOINT, MODEL
    ll = (cfg or {}).get("local_llm")
    if isinstance(ll, dict):
        url = ll.get("url")
        if isinstance(url, str) and url.strip():
            endpoint = url.strip()
        m = ll.get("model")
        if isinstance(m, str) and m.strip():
            model = m.strip()
    return endpoint, model


def probe_urls(endpoint: str) -> tuple[str, str]:
    """(models, slots) probe URLs for an OpenAI-compatible endpoint —
    derived from the endpoint's authority so a configured port is
    honoured."""
    try:
        parts = urllib.parse.urlsplit(endpoint)
    except ValueError:
        parts = None
    if parts and parts.netloc:
        base = f"{parts.scheme}://{parts.netloc}"
    else:  # malformed endpoint — degrade to a still-probeable default
        base = ENDPOINT.split("/v1/", 1)[0]
    return f"{base}/v1/models", f"{base}/slots"

# Slot reservation on the shared -np 2 llama-server: every MCS
# background call pins slot 1 so slot 2 stays free for interactive
# clients (queue wait is the dominant latency for them; decode speed
# is shared compute either way).
# Slot naming is 1-based for humans: "slot 1" = background/batch traffic
# (MCS semantic drain, extract_llm), "slot 2" = real-time/interactive traffic
# (Hermes agent + auxiliary calls). The llama-server wire protocol (id_slot)
# is 0-based, so logical slot N is sent as id_slot N-1.
SLOT_1 = 1  # background
SLOT_2 = 2  # real-time
BACKGROUND_SLOT = SLOT_1 - 1  # wire id_slot 0
REALTIME_SLOT = SLOT_2 - 1    # wire id_slot 1

# T19: the selected safe parallel width of the deployed server — the
# checked-in ``-np`` value of ai.mcs.llamaserver.plist. This is the
# measured deployment choice, not a promise: the selection record lives
# in .omo/evidence/.../task-19/slot-capacity-selection.json and the
# rollback count below is the configuration reverted to on regression.
# A server advertising FEWER slots than this is a mismatch — flag it,
# never let a call ride an out-of-range (unpinned) id_slot.
SLOT_COUNT = 2
ROLLBACK_SLOT_COUNT = 1


def request_slot() -> int:
    """Wire id_slot for a background call. ``MCS_LLM_SLOT`` (decimal)
    overrides the default — scoped to whatever process the operator
    sets it on (the nightly QC drainer exports it to borrow the RT
    slot inside its window); unset everywhere else keeps slot 1's
    real-time reservation. An override at or beyond the deployed slot
    count is invalid — llama.cpp treats out-of-range id_slot as
    UNPINNED, which could land on the real-time slot; fall back to the
    background slot instead."""
    v = os.environ.get("MCS_LLM_SLOT")
    if v is not None:
        digits = v.strip()
        if digits and digits.isascii() and digits.isdecimal():
            try:
                slot = int(digits)
            except ValueError:
                return BACKGROUND_SLOT
            if 0 <= slot < SLOT_COUNT:
                return slot
    return BACKGROUND_SLOT


# ---------- cross-client admission boundary (T20, default off) ----------

def admission_enabled() -> bool:
    """The shared RT/BACKLOG admission boundary is opt-in until a
    staged rollout proves direct-connection rejection on the deployed
    topology. ``MCS_LLM_ADMISSION`` carries the broker SQLite path (or
    ``1`` for the default location)."""
    v = os.environ.get("MCS_LLM_ADMISSION")
    return bool(v and v != "0")


def _admission_db_path() -> str:
    v = os.environ.get("MCS_LLM_ADMISSION") or ""
    if v == "1":
        return os.path.join(os.path.expanduser("~/.mcs"), "data",
                            "llm_admission.db")
    return v


_BROKERS: dict = {}


def _broker(path: str | None = None):
    """Process-cached broker handle — one SQLite connection per path."""
    import llm_admission
    p = path or _admission_db_path()
    b = _BROKERS.get(p)
    if b is None or getattr(b, "_closed", False):
        b = llm_admission.Broker(p, slots=SLOT_COUNT)
        _BROKERS[p] = b
    return b


def admitted_chat(client_route: str, prompt: str, *,
                  broker_path: str | None = None,
                  wait_s: float = 0, deadline: float | None = None,
                  error_out: dict | None = None,
                  **kw) -> dict | None:
    """One chat call through the RT/BACKLOG admission boundary.

    ``client_route`` is an authenticated route name registered with
    the broker — its bound class (never a caller value) decides
    RT vs BACKLOG. Returns ``chat()``'s response on a sent request;
    on admission denial/deferral returns
    ``{"admission": <reason>, "status": None, "text": None, ...}`` —
    a distinct, honest outcome the caller must not confuse with a
    model answer. A transport failure is ``mark_unknown`` — the
    permit keeps occupying its class until reconciled, never
    optimistically retired."""
    broker = _broker(broker_path)
    cls = broker.routes.get(client_route)
    if cls is None:
        return {"text": None, "finish_reason": None, "usage": None,
                "status": None, "admission": "unknown_client"}
    # invalid arguments fail before a permit exists — never recorded
    # as a backend-uncertain ``unknown`` permit that holds a slot
    _validate_chat_args(prompt, kw.get("endpoint", ENDPOINT),
                        kw.get("timeout", TIMEOUT),
                        kw.get("max_tokens", MAX_TOKENS))
    acq = broker.acquire(client_route, cls)
    pid = acq.get("permit_id")
    if not acq.get("admitted") and acq.get("reason") == "waiting" \
            and wait_s > 0 and pid is not None:
        # an RT caller may wait out the protected-BG window — bounded
        # by wait_s AND the caller deadline; the permit stays waiting
        # (occupied) the whole time and never overlaps BACKLOG
        until = time.monotonic() + wait_s
        if deadline is not None:
            until = min(until, deadline)
        state = "waiting"
        try:
            while time.monotonic() < until:
                state = broker.poll(pid).get("state", "waiting")
                if state != "waiting":
                    break
                time.sleep(0.05)
        except BaseException:
            # never let a failing cancel mask the original interruption
            with suppress(Exception):
                broker.cancel(pid)
            raise
        if state == "admitted":
            acq = {"admitted": True, "permit_id": pid,
                   "epoch": acq.get("epoch")}
        else:
            if state == "waiting":
                # wait timed out — retire the never-sent intent so the
                # RT-waiting flag does not hold BACKLOG shut
                broker.cancel(pid)
            return {"text": None, "finish_reason": None,
                    "usage": None, "status": None,
                    "admission": f"wait_{state}",
                    "permit_id": pid, "epoch": acq.get("epoch")}
    if not acq.get("admitted"):
        # waiting RT or held backlog — honest deferral, no send; a
        # waiting RT intent the caller will not wait out is retired
        if acq.get("reason") == "waiting" and pid is not None:
            broker.cancel(pid)
        return {"text": None, "finish_reason": None, "usage": None,
                "status": None,
                "admission": acq.get("reason", "held"),
                "permit_id": acq.get("permit_id"),
                "epoch": acq.get("epoch")}
    pid = acq["permit_id"]
    sent = broker.sent(pid)
    if not sent.get("sent"):
        broker.terminal(pid, "not_sent",
                        proof=sent.get("reason"))
        return {"text": None, "finish_reason": None, "usage": None,
                "status": None, "admission": sent.get("reason"),
                "permit_id": pid}
    extra = dict(kw.pop("extra_payload", None) or {})
    extra["admission_token"] = sent["token"]
    extra.setdefault("id_slot", request_slot())
    # the caller's error_out still receives unreachable/transport so
    # its deferral policy survives the gate
    err_out = error_out if error_out is not None else {}
    try:
        response = chat(prompt, deadline=deadline, extra_payload=extra,
                        error_out=err_out, **kw)
    except BaseException:
        broker.mark_unknown(pid, "interrupted")
        raise
    if response is None:
        if err_out.get("kind") == "unreachable":
            # connection refused — provably never reached the backend
            broker.terminal(pid, "not_sent", proof="unreachable")
        else:
            # transport failure — the backend may still be decoding;
            # unknown, never assumed free
            broker.mark_unknown(pid, "transport")
    else:
        broker.terminal(pid,
                        "done" if response.get("status") == 200
                        else f"http_{response.get('status')}")
    return response


def admitted_probe_format(client_route: str, endpoint: str, model: str,
                          schema: dict | None, *,
                          broker_path: str | None = None,
                          **kw) -> str | None:
    """``probe_format`` through the admission boundary — the probe's
    ladder sends real inference POSTs, so it acquires one class permit
    for the whole bounded probe (≤2 sequential sends, same class) and
    injects the token into each. Returns the probe mode, or
    ``"plain"`` — a denied/held admission also yields ``"plain"``
    (callers cache with a bounded cooldown, and the verdict is
    visible in the permit ledger)."""
    broker = _broker(broker_path)
    cls = broker.routes.get(client_route)
    if cls is None:
        return "plain"
    acq = broker.acquire(client_route, cls)
    if not acq.get("admitted"):
        return "plain"
    pid = acq["permit_id"]
    sent = broker.sent(pid)
    if not sent.get("sent"):
        broker.terminal(pid, "not_sent", proof=sent.get("reason"))
        return "plain"
    err_out = kw.pop("error_out", None)
    if err_out is None:
        err_out = {}
    try:
        return probe_format(endpoint, model, schema,
                            admission_token=sent["token"], error_out=err_out, **kw)
    except BaseException:
        broker.mark_unknown(pid, "probe_error")
        raise
    finally:
        # Plain mode can also mean an interrupted transport. It proves
        # neither completion nor that the backend stopped decoding.
        p = broker._permit(pid)
        if p is not None and p["state"] == "sent":
            if err_out.get("kind") == "unreachable":
                broker.terminal(pid, "not_sent", proof="unreachable")
            elif err_out.get("kind"):
                broker.mark_unknown(pid, "probe_transport")
            else:
                broker.terminal(pid, "done", proof="probe_returned")


def bounded_request(endpoint: str, method: str, body, timeout: float,
                    deadline: float | None = None):
    """Pre-wired ``bounded_http.bounded_http_request`` for
    unauthenticated loopback calls — worker isolation, byte bound,
    absolute deadline."""
    return bounded_http.bounded_http_request(
        endpoint, method, body, timeout, api_key=None, deadline=deadline)


def _default_request(endpoint: str, method: str, body, timeout: float,
                     deadline: float | None):
    """Use a reaped worker so a slow response cannot extend the deadline."""
    return bounded_request(endpoint, method, body, timeout, deadline)


def _usage_dict(usage) -> dict | None:
    if not isinstance(usage, dict):
        return None
    out = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if type(value) is int and value >= 0:
            out[key] = value
    return out or None


def _timings_dict(timings) -> dict | None:
    """llama.cpp `timings` block — numeric fields only. prompt_n/
    cache_n expose the per-slot prefix-cache hit (cache_n tokens were
    already in KV and skipped re-evaluation); predicted_ms/prompt_ms
    split decode time from prompt evaluation so throughput tuning is
    measured, not guessed. Missing/absent fields stay absent."""
    if not isinstance(timings, dict):
        return None
    out = {}
    for key in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms",
                "cache_n"):
        value = timings.get(key)
        if (type(value) in (int, float)
                and 0 <= value <= sys.float_info.max and math.isfinite(value)):
            out[key] = value
    return out or None


def _validate_chat_args(prompt, endpoint, timeout, max_tokens) -> None:
    """Pre-send argument checks shared by ``chat`` and ``admitted_chat``
    — a request rejected here provably never reached the backend."""
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt_invalid")
    if not bounded_http._loopback_endpoint_allowed(endpoint):
        raise ValueError("local_endpoint_not_allowed")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) \
            or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_invalid")
    if max_tokens is not None and (type(max_tokens) is not int
                                 or max_tokens <= 0):
        raise ValueError("max_tokens_invalid")


def chat(prompt: str, *, endpoint: str = ENDPOINT, model: str = MODEL,
         max_tokens: int = MAX_TOKENS, timeout: float = TIMEOUT,
         deadline: float | None = None, response_format=None,
         extra_payload: dict | None = None,
         request_fn=None, error_out: dict | None = None) -> dict | None:
    """One chat-completions round trip.

    Returns ``{"text", "finish_reason", "usage", "status"}`` on a parsed
    HTTP 200, or ``None`` on transport/protocol failure.  ``request_fn``
    is the ``(endpoint, method, body, timeout, deadline) ->
    (status, headers, raw)`` seam — tests inject a fake loopback here
    and ``semantic.llm_chat`` passes ``bounded_request`` to keep
    worker isolation.  ``error_out``, when given, receives
    ``{"kind": "unreachable"|"transport"}`` on the exception path —
    "unreachable" means the server refused the connection outright
    (never started / down), which callers may treat as free-of-cost
    unlike a timeout that consumed real server work.
    """
    _validate_chat_args(prompt, endpoint, timeout, max_tokens)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
        # No tools: the model cannot act, only answer.
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if response_format is not None:
        body["response_format"] = response_format
    if extra_payload:
        body.update(extra_payload)
    send = request_fn or _default_request
    try:
        status, _headers, raw = send(endpoint, "POST", body, timeout,
                                     deadline)
    except urllib.error.HTTPError as error:
        status, raw = error.code, b""
    except Exception as error:
        # Transport seam failures fail closed; runtime guards still
        # propagate so budget/circuit stops are never swallowed.
        if error.__class__.__name__ == "RuntimeGuardError":
            raise
        if error_out is not None:
            reason = getattr(error, "reason", error)
            error_out["kind"] = (
                "unreachable"
                if isinstance(reason, ConnectionRefusedError)
                else "transport")
        return None
    if status != 200 or len(raw) > bounded_http.MAX_RESPONSE_BYTES:
        return {"text": None, "finish_reason": None, "usage": None,
                "status": status}
    try:
        out = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(out, dict) \
            or not isinstance(out.get("choices"), list) \
            or not out["choices"] \
            or not isinstance(out["choices"][0], dict):
        return None
    choice = out["choices"][0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("content")
    if text is not None and not isinstance(text, str):
        return None
    finish = choice.get("finish_reason")
    if finish is not None and not isinstance(finish, str):
        finish = str(finish)
    return {"text": text, "finish_reason": finish,
            "usage": _usage_dict(out.get("usage")),
            "timings": _timings_dict(out.get("timings")),
            "status": status}


def acceptance_error(response: dict | None) -> str | None:
    """Canonical acceptance policy — ``None`` means acceptable.

    ``length`` stops and empty text are explicit incompletes, never a
    success payload.  Structural validation stays with the caller; this
    layer only rules out transport-level and truncation outcomes.
    """
    if response is None:
        return "transport"
    if response.get("status") != 200:
        return "http_status"
    text = response.get("text")
    if not isinstance(text, str) or not text.strip():
        return "empty"
    if response.get("finish_reason") == "length":
        return "length_stop"
    return None


def probe_format(endpoint: str, model: str, schema: dict | None,
                 timeout: float = 10, verify=None,
                 deadline: float | None = None, request_fn=None,
                 slot: int | None = None,
                 admission_token: str | None = None,
                 error_out: dict | None = None) -> str | None:
    """Detect the best ``response_format`` the server accepts.

    Ladder: json_schema (if *schema* given) -> json_object -> plain.
    A mode is accepted only when the probe reply parses as a JSON object
    — a 200 with prose means the constraint silently failed open.
    Returns the accepted mode, or ``"plain"`` when every constraint was
    rejected; transport failure aborts probing and also yields
    ``"plain"`` (callers cache the result for a bounded cooldown).
    *slot* pins the probe's wire ``id_slot``; ``None`` resolves it via
    :func:`request_slot`, so the per-process ``MCS_LLM_SLOT`` override —
    or the caller's own slot decision — governs probes exactly as it
    does the calls they precede.
    """
    verify = verify or (lambda text: isinstance(_json_obj(text), dict))
    # A malformed OR out-of-range caller slot must not become an
    # unpinned request — llama.cpp treats out-of-range id_slot as
    # unpinned, which could land the probe on the real-time slot.
    # Fall back to the default resolver.
    id_slot = slot if type(slot) is int \
        and 0 <= slot < SLOT_COUNT else request_slot()
    candidates = []
    if schema is not None:
        # A server that ignores json_schema will echo the prompt's "ok"
        # instead of this marker; that response must not select schema.
        candidates.append(("schema", {"type": "json_schema",
                                      "json_schema": {
                                          "name": "mcs_format_probe",
                                          "schema": {
                                              "type": "object",
                                              "properties": {"probe": {
                                                  "type": "string",
                                                  "enum": ["schema"]}},
                                              "required": ["probe"],
                                              "additionalProperties": False}}}))
    candidates.append(("object", {"type": "json_object"}))
    operation_deadline = time.monotonic() + timeout
    if deadline is not None:
        operation_deadline = min(operation_deadline, deadline)
    for mode, rf in candidates:
        remaining = operation_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            payload = {"id_slot": id_slot}
            if admission_token is not None:
                payload["admission_token"] = admission_token
            response = chat(
                'Reply with {"ok": true}', endpoint=endpoint, model=model,
                max_tokens=20, timeout=remaining, deadline=operation_deadline,
                response_format=rf, extra_payload=payload,
                request_fn=request_fn, error_out=error_out)
            if response is None:
                if error_out is not None:
                    error_out.setdefault("kind", "transport")
                break
            if response["status"] != 200:
                continue
            if acceptance_error(response) is not None:
                continue
            content = response["text"]
            if (isinstance(content, str) and verify(content)
                    and (mode != "schema"
                         or _json_obj(content) == {"probe": "schema"})):
                return mode
        except (OSError, ValueError):
            if error_out is not None:
                error_out.setdefault("kind", "transport")
            break                   # transport dead — stop probing
    return "plain"


def _json_obj(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
