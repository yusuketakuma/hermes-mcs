#!/usr/bin/env python3
"""Bounded HTTP transport — one short-lived worker per request.

Stdlib-only request mechanism shared by the two HTTP surfaces that need
process-level isolation: the TypeSafe Jev API (semantic_jev) and the
loopback llama.cpp server (local_llm). Each request runs in a fresh
interpreter: the JSON body and credential travel over stdin (never
argv), the worker follows no proxy and no redirect, reads at most
``MAX_RESPONSE_BYTES + 1`` bytes, and the parent enforces an absolute
deadline with kill/reap so a wedged socket can never outlive the
caller's budget.

This module owns the MECHANISM only — which endpoints a credential may
be sent to is the caller's policy, passed in as ``allowed_endpoints``
(and re-declared to the worker through the stdin envelope). An
``api_key=None`` request is pinned to an unauthenticated loopback URL
regardless of the allowlist.
"""
import base64
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
from urllib.parse import urlsplit

from mcs_util import NoRedirect, no_proxy_opener

MAX_RESPONSE_BYTES = 262144

_HTTP_WORKER_ARG = "--http-worker"
_WORKER_HEADER_NAMES = frozenset({
    "content-length", "content-type", "retry-after", "transfer-encoding",
})


def _loopback_endpoint_allowed(endpoint: str) -> bool:
    """Allow only an unauthenticated HTTP endpoint on loopback."""
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    return (parts.scheme == "http" and parts.hostname in
            {"127.0.0.1", "localhost", "::1"}
            and parts.username is None and parts.password is None
            and not parts.fragment)


def _worker_endpoint_allowed(endpoint: str, allowed) -> bool:
    """Worker-side check: the caller-declared allowlist (already
    enforced parent-side) plus unauthenticated loopback."""
    return endpoint in allowed or _loopback_endpoint_allowed(endpoint)


def _worker_headers(headers) -> dict:
    return {str(key): str(value) for key, value in headers.items()
            if str(key).lower() in _WORKER_HEADER_NAMES}


def _http_worker_main() -> int:
    """Read one request from stdin and write one bounded response to stdout.

    The parent process supplies the credential, JSON body, and endpoint
    allowlist through stdin; none of them is present in this worker's
    argv or diagnostics.  The worker is short-lived so urllib DNS and
    socket timeouts cannot outlive the parent's absolute deadline.
    """
    try:
        envelope = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(envelope, dict):
            raise ValueError("request_invalid")
        endpoint = envelope.get("endpoint")
        method = envelope.get("method", "POST")
        api_key = envelope.get("api_key")
        timeout = envelope.get("timeout")
        body = envelope.get("body")
        allowed = envelope.get("allowed_endpoints") or []
        if (not isinstance(endpoint, str)
                or not isinstance(allowed, list)
                or not all(isinstance(e, str) for e in allowed)
                or not _worker_endpoint_allowed(endpoint, allowed)
                or method not in ("GET", "POST")
                or not isinstance(timeout, int | float)
                or isinstance(timeout, bool) or not math.isfinite(timeout)
                or timeout <= 0
                or (api_key is not None and not isinstance(api_key, str))
                or (api_key is None
                    and not _loopback_endpoint_allowed(endpoint))):
            raise ValueError("request_invalid")
        raw = None if body is None else json.dumps(
            body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"User-Agent": "mcs-adapter-semantic/1.0"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        if raw is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(endpoint, data=raw, method=method,
                                         headers=headers)
        opener = no_proxy_opener(NoRedirect)
        try:
            response = opener.open(request, timeout=float(timeout))
        except urllib.error.HTTPError as error:
            response = error
        try:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            result = {
                "ok": True,
                "status": int(getattr(response, "status",
                                       getattr(response, "code", 0))),
                "headers": _worker_headers(response.headers),
                "body": base64.b64encode(payload).decode("ascii"),
            }
        finally:
            response.close()
    except Exception as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, ConnectionRefusedError):
            kind = "connection_refused"
        elif isinstance(reason, TimeoutError):
            kind = "timeout"
        else:
            kind = "transport"
        result = {"ok": False, "error": kind}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    sys.stdout.flush()
    return 0


def _worker_environment() -> dict:
    """Pass only runtime essentials; credentials remain stdin-only."""
    allowed = {"PATH", "PYTHONPATH", "PYTHONHOME", "SYSTEMROOT",
               "LANG", "LC_ALL", "VIRTUAL_ENV"}
    return {key: value for key, value in os.environ.items()
            if key in allowed}


def bounded_http_request(endpoint: str, method: str, body,
                         timeout: float, api_key: str | None = None,
                         deadline: float | None = None,
                         allowed_endpoints=()):
    """Make one bounded request through a short-lived, reaped worker.

    An ``api_key`` request must target ``allowed_endpoints`` — the
    caller's own allowlist (semantic_jev passes its fixed Jev set).  An
    ``api_key=None`` request is restricted to an unauthenticated
    loopback URL.  Request bodies and credentials are sent through stdin
    only; the worker follows neither proxies nor redirects and returns
    at most ``MAX_RESPONSE_BYTES + 1`` bytes so callers can reject an
    oversized response without retaining an unbounded body.
    """
    if not isinstance(endpoint, str) or method not in ("GET", "POST"):
        raise ValueError("request_invalid")
    allowed = {e for e in allowed_endpoints if isinstance(e, str)}
    if api_key is None:
        if not _loopback_endpoint_allowed(endpoint):
            raise ValueError("local_endpoint_not_allowed")
    elif not isinstance(api_key, str) or endpoint not in allowed:
        raise ValueError("endpoint_not_allowed")
    if (isinstance(timeout, bool) or not isinstance(timeout, int | float)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout_invalid")
    operation_deadline = time.monotonic() + float(timeout)
    if deadline is not None:
        if (isinstance(deadline, bool)
                or not isinstance(deadline, int | float)
                or not math.isfinite(deadline)):
            raise ValueError("deadline_invalid")
        operation_deadline = min(operation_deadline, float(deadline))
    remaining = operation_deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("http worker deadline exceeded")
    envelope = {
        "endpoint": endpoint,
        "method": method,
        "api_key": api_key,
        "body": body,
        "timeout": remaining,
        "allowed_endpoints": sorted(allowed),
    }
    payload = json.dumps(envelope, ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    command = [sys.executable, os.path.abspath(__file__), _HTTP_WORKER_ARG]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, close_fds=True, env=_worker_environment())
    try:
        remaining = operation_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("http worker deadline exceeded")
        stdout, _stderr = process.communicate(input=payload, timeout=remaining)
    except subprocess.TimeoutExpired as error:
        try:
            process.kill()
        finally:
            process.communicate()
        raise TimeoutError("http worker deadline exceeded") from error
    except BaseException:
        if process.poll() is None:
            process.kill()
        with suppress(Exception):
            process.communicate()
        raise
    if time.monotonic() >= operation_deadline:
        raise TimeoutError("http worker deadline exceeded")
    if process.returncode != 0:
        raise OSError("http worker failed")
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise OSError("http worker protocol failed") from error
    if not isinstance(result, dict) or not result.get("ok"):
        if isinstance(result, dict) and result.get("error") == "timeout":
            raise TimeoutError("http worker timeout")
        if isinstance(result, dict) and result.get("error") == "connection_refused":
            raise ConnectionRefusedError("http worker connection refused")
        raise OSError("http worker transport failed")
    try:
        status = int(result["status"])
        headers = result["headers"]
        raw = base64.b64decode(result["body"], validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise OSError("http worker response invalid") from error
    if (status < 100 or status > 599 or not isinstance(headers, dict)
            or len(raw) > MAX_RESPONSE_BYTES + 1):
        raise OSError("http worker response invalid")
    return status, headers, raw


if __name__ == "__main__":
    # spawned per request by bounded_http_request: one envelope on
    # stdin -> one JSON result on stdout; anything else is misuse
    if len(sys.argv) > 1 and sys.argv[1] == _HTTP_WORKER_ARG:
        sys.exit(_http_worker_main())
    sys.exit(2)
