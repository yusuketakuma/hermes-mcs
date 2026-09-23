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
import time
import urllib.error

import semantic_jev as jev

ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "Qwen3.5-9B"
MAX_TOKENS = 1400
TIMEOUT = 90

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


def request_slot() -> int:
    """Wire id_slot for a background call. ``MCS_LLM_SLOT`` (decimal)
    overrides the default — scoped to whatever process the operator
    sets it on (the nightly QC drainer exports it to borrow the RT
    slot inside its window); unset everywhere else keeps slot 1's
    real-time reservation."""
    import os
    v = os.environ.get("MCS_LLM_SLOT")
    if v is not None and v.strip().isdigit() and int(v) >= 0:
        return int(v)
    return BACKGROUND_SLOT

def bounded_request(endpoint: str, method: str, body, timeout: float,
                    deadline: float | None = None):
    """Pre-wired ``jev.bounded_http_request`` for unauthenticated
    loopback calls — worker isolation, byte bound, absolute deadline."""
    return jev.bounded_http_request(endpoint, method, body, timeout,
                                    api_key=None, deadline=deadline)


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
    and ``semantic.llm_chat`` passes ``jev.bounded_http_request`` to keep
    worker isolation.  ``error_out``, when given, receives
    ``{"kind": "unreachable"|"transport"}`` on the exception path —
    "unreachable" means the server refused the connection outright
    (never started / down), which callers may treat as free-of-cost
    unlike a timeout that consumed real server work.
    """
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt_invalid")
    if not jev._loopback_endpoint_allowed(endpoint):
        raise ValueError("local_endpoint_not_allowed")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_invalid")
    if max_tokens is not None and (type(max_tokens) is not int
                                 or max_tokens <= 0):
        raise ValueError("max_tokens_invalid")
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
    if status != 200 or len(raw) > jev.MAX_RESPONSE_BYTES:
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
            "usage": _usage_dict(out.get("usage")), "status": status}


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
                 slot: int | None = None) -> str | None:
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
    # A malformed caller slot must not become an unpinned request —
    # llama.cpp treats out-of-range id_slot as unpinned, which could land
    # the probe on the real-time slot. Fall back to the default resolver.
    id_slot = slot if type(slot) is int and slot >= 0 else request_slot()
    candidates = []
    if schema is not None:
        candidates.append(("schema", {"type": "json_schema",
                                      "json_schema": schema}))
    candidates.append(("object", {"type": "json_object"}))
    operation_deadline = time.monotonic() + timeout
    if deadline is not None:
        operation_deadline = min(operation_deadline, deadline)
    for mode, rf in candidates:
        remaining = operation_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = chat(
                'Reply with {"ok": true}', endpoint=endpoint, model=model,
                max_tokens=20, timeout=remaining, deadline=operation_deadline,
                response_format=rf, extra_payload={"id_slot": id_slot},
                request_fn=request_fn)
            if response is None:
                break
            if response["status"] != 200:
                continue
            content = response["text"]
            if isinstance(content, str) and verify(content):
                return mode
        except (OSError, ValueError):
            break                   # transport dead — stop probing
    return "plain"


def _json_obj(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def chat_text(prompt: str, **kwargs) -> str | None:
    """``semantic.llm_chat``-compatible helper: text or ``None``."""
    response = chat(prompt, **kwargs)
    if response is None or response.get("status") != 200:
        return None
    text = response.get("text")
    return text if isinstance(text, str) else None
