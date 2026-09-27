"""Workers with absolute deadlines for MCS API, attachments, and local Chrome I/O."""
from __future__ import annotations

import base64
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class WorkerError(Exception):
    def __init__(self, kind: str, status=None, retryable=False):
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.retryable = retryable


def _worker_command():
    return [sys.executable, os.path.abspath(__file__)]


def bounded_call(payload: dict, *, timeout: float, deadline=None) -> dict:
    """Run one operation; credentials and bodies travel only through stdin.

    Killing and reaping the worker also ends a DNS lookup, header read, or
    trickling response that would otherwise keep renewing a socket timeout.
    The worker never renames an attachment into its final destination.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, int | float)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout_invalid")
    end = time.monotonic() + timeout
    if deadline is not None:
        if (isinstance(deadline, bool)
                or not isinstance(deadline, int | float)
                or not math.isfinite(deadline)):
            raise ValueError("deadline_invalid")
        end = min(end, deadline)
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("mcs worker deadline exceeded")
    envelope = dict(payload, timeout=remaining)
    raw = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode()
    allowed_env = {"PATH", "PYTHONPATH", "PYTHONHOME", "SYSTEMROOT",
                   "LANG", "LC_ALL", "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE"}
    env = {key: value for key, value in os.environ.items() if key in allowed_env}
    process = subprocess.Popen(
        _worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, close_fds=True, env=env)
    try:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("mcs worker deadline exceeded")
        stdout, _ = process.communicate(input=raw, timeout=remaining)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise TimeoutError("mcs worker deadline exceeded") from None
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise
    if time.monotonic() >= end:
        raise TimeoutError("mcs worker deadline exceeded")
    if process.returncode != 0:
        raise WorkerError("network_error", retryable=True)
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError
        if result["ok"]:
            if not isinstance(result.get("value"), dict):
                raise ValueError
            return result["value"]
        kind = result.get("kind")
        if kind not in {"http_error", "network_error", "download_failed",
                        "download_empty", "download_too_large", "no_token",
                        "url_not_allowed", "bootstrap_error",
                        "deadline_exceeded"}:
            raise ValueError
        status = result.get("status")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ValueError
    except (ValueError, TypeError):
        raise WorkerError("network_error", retryable=True) from None
    raise WorkerError(kind, status, result.get("retryable") is True)


def _loopback_url(url: str, scheme: str):
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != scheme or parsed.hostname not in
            {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment):
        raise WorkerError("url_not_allowed")
    # Access validates malformed or out-of-range ports as well.
    parsed.port  # noqa: B018


def _execute(envelope: dict) -> dict:
    import mcs_adapter as adapter_module
    from mcs_util import NoRedirect, no_proxy_opener

    timeout = envelope["timeout"]
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or timeout <= 0):
        raise WorkerError("network_error", retryable=True)
    operation = envelope.get("operation")
    if operation == "api":
        url = envelope["url"]
        if not url.startswith(adapter_module.API + "/"):
            raise WorkerError("url_not_allowed")
        method = envelope["method"]
        if method not in ("GET", "POST"):
            raise WorkerError("url_not_allowed")
        data = envelope.get("data")
        request = urllib.request.Request(
            url, data=None if data is None else base64.b64decode(data, validate=True),
            headers=envelope["headers"], method=method)
        try:
            response = no_proxy_opener(NoRedirect).open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            # Error bodies are unused and may themselves trickle indefinitely.
            try:
                return {"status": error.code, "headers": dict(error.headers or {}),
                        "body": ""}
            finally:
                error.close()
        with response:
            return {"status": response.status, "headers": dict(response.headers),
                    "body": base64.b64encode(response.read()).decode("ascii")}
    if operation == "download":
        adapter = adapter_module.MCSAdapter()
        adapter._token = envelope.get("token")
        adapter.set_deadline(time.monotonic() + timeout)
        return adapter._download_to_part(envelope["url"], envelope["partial"])
    if operation == "cdp_json":
        url = envelope["url"]
        _loopback_url(url, "http")
        method = envelope.get("method", "GET")
        if method not in ("GET", "PUT"):
            raise WorkerError("url_not_allowed")
        request = urllib.request.Request(url, method=method)
        with no_proxy_opener(NoRedirect).open(request, timeout=timeout) as response:
            return {"value": json.load(response)}
    if operation == "cdp_eval":
        _loopback_url(envelope["url"], "ws")
        return {"value": adapter_module._ws_eval(
            envelope["url"], envelope["expression"], timeout)}
    raise WorkerError("url_not_allowed")


def worker_main() -> int:
    # No exception text, request URL, response body, or credential reaches stderr.
    import mcs_adapter as adapter_module
    try:
        envelope = json.loads(sys.stdin.buffer.read())
        result = {"ok": True, "value": _execute(envelope)}
    except (WorkerError, adapter_module.MCSError) as error:
        result = {"ok": False, "kind": error.kind, "status": error.status,
                  "retryable": error.retryable}
    except urllib.error.HTTPError as error:
        result = {"ok": False, "kind": "http_error", "status": error.code,
                  "retryable": error.code in (408, 429) or error.code >= 500}
        error.close()
    except Exception:
        result = {"ok": False, "kind": "network_error", "retryable": True}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import _mcs_path  # noqa: F401
    raise SystemExit(worker_main())
