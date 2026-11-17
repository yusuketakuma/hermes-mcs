"""Anthropic auth cannot widen the existing keyed or anonymous transport policy."""
import base64
import io
import json
from types import SimpleNamespace

import pytest

import bounded_http as b


@pytest.mark.parametrize("overrides", [{"endpoint": "https://api.anthropic.com/v1/messages?x=1"},
                                       {"endpoint": "https://other.invalid/v1/messages"},
                                       {"method": "GET"}, {"api_key": None},
                                       {"auth": "unknown"}])
def test_parent_rejects_wrong_auth_endpoint_before_spawn(monkeypatch, overrides):
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    args = {"endpoint": b.ANTHROPIC_ENDPOINT, "method": "POST", "body": {}, "timeout": 1,
            "api_key": "synthetic", "auth": "anthropic", "allowed_endpoints": (b.ANTHROPIC_ENDPOINT,)}
    args.update(overrides)
    with pytest.raises(ValueError):
        b.bounded_http_request(**args)


def _worker(monkeypatch, envelope, opener):
    output = io.StringIO()
    monkeypatch.setattr(b.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(envelope).encode())))
    monkeypatch.setattr(b.sys, "stdout", output)
    monkeypatch.setattr(b, "no_proxy_opener", opener)
    b._http_worker_main()
    return json.loads(output.getvalue())


def _envelope(**kw):
    return {"endpoint": b.ANTHROPIC_ENDPOINT, "method": "POST", "body": {}, "timeout": 1,
            "api_key": "synthetic", "auth": "anthropic", "allowed_endpoints": [b.ANTHROPIC_ENDPOINT], **kw}


def test_worker_builds_only_fixed_headers_and_returns_safe_request_id(monkeypatch):
    seen = []

    class Response:
        status = 200
        headers = {"Request-Id": "req_synthetic", "Set-Cookie": "private"}

        def read(self, count):
            assert count == b.MAX_RESPONSE_BYTES + 1
            return b"{}"

        def close(self):
            pass

    class Opener:
        def open(self, request, timeout):
            seen.append({k.lower(): v for k, v in request.header_items()})
            return Response()

    def opener(redirect):
        assert redirect is b.NoRedirect
        return Opener()

    result = _worker(monkeypatch, _envelope(), opener)
    assert seen[0]["x-api-key"] == "synthetic"
    assert seen[0]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in seen[0]
    assert result["headers"] == {"Request-Id": "req_synthetic"}


@pytest.mark.parametrize("overrides", [{"endpoint": "https://other.invalid/v1/messages"},
                                       {"endpoint": "https://api.anthropic.com/v1/messages/"},
                                       {"method": "GET"}, {"api_key": None}, {"auth": "unknown"}])
def test_worker_independently_checks_auth_policy(monkeypatch, overrides):
    result = _worker(monkeypatch, _envelope(**overrides), lambda *a: pytest.fail("must not open"))
    assert result == {"ok": False, "error": "transport"}


def _reading_opener(observed, payload=b"{}"):
    class Response:
        status = 200
        headers = {}

        def read(self, count):
            observed.append(count)
            return payload

        def close(self):
            pass

    class Opener:
        def open(self, request, timeout):
            return Response()

    return lambda redirect: Opener()


@pytest.mark.parametrize("limit", [b.MAX_RESPONSE_BYTES, b.MAX_RESPONSE_BYTES + 1, b.MAX_RESPONSE_BYTES_CEILING])
def test_worker_reads_one_byte_past_a_raised_response_limit(monkeypatch, limit):
    observed = []
    result = _worker(monkeypatch, _envelope(max_bytes=limit), _reading_opener(observed))
    assert result["ok"] is True and observed == [limit + 1]


def test_worker_without_a_declared_limit_keeps_the_default(monkeypatch):
    observed = []
    assert "max_bytes" not in _envelope()
    assert _worker(monkeypatch, _envelope(), _reading_opener(observed))["ok"] is True
    assert observed == [b.MAX_RESPONSE_BYTES + 1]


_INVALID_LIMITS = [0, -1, b.MAX_RESPONSE_BYTES - 1, b.MAX_RESPONSE_BYTES_CEILING + 1,
                   float(b.MAX_RESPONSE_BYTES), str(b.MAX_RESPONSE_BYTES), True]


@pytest.mark.parametrize("limit", _INVALID_LIMITS)
def test_worker_refuses_a_response_limit_outside_the_bounds(monkeypatch, limit):
    result = _worker(monkeypatch, _envelope(max_bytes=limit), lambda *a: pytest.fail("must not open"))
    assert result == {"ok": False, "error": "transport"}


@pytest.mark.parametrize("limit", _INVALID_LIMITS)
def test_parent_refuses_a_response_limit_outside_the_bounds_before_spawn(monkeypatch, limit):
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="max_bytes_invalid"):
        b.bounded_http_request(b.ANTHROPIC_ENDPOINT, "POST", {}, 1, api_key="synthetic", auth="anthropic",
                               allowed_endpoints=(b.ANTHROPIC_ENDPOINT,), max_bytes=limit)


class _Worker:
    """Synthetic worker process: records the stdin envelope and answers with a fixed body."""
    returncode = 0

    def __init__(self, sent, body):
        self.sent, self.body = sent, body

    def communicate(self, input=None, timeout=None):
        self.sent.append(json.loads(input.decode("utf-8")))
        reply = {"ok": True, "status": 200, "headers": {}, "body": base64.b64encode(self.body).decode("ascii")}
        return json.dumps(reply).encode("utf-8"), b""

    def poll(self):
        return 0


def _parent(monkeypatch, body, **kw):
    sent = []
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: _Worker(sent, body))
    result = b.bounded_http_request(b.ANTHROPIC_ENDPOINT, "POST", {}, 1, api_key="synthetic", auth="anthropic",
                                    allowed_endpoints=(b.ANTHROPIC_ENDPOINT,), **kw)
    return sent[0], result


def test_parent_declares_a_raised_limit_to_the_worker_and_accepts_up_to_one_byte_past_it(monkeypatch):
    limit = b.MAX_RESPONSE_BYTES + 4096
    envelope, (status, _headers, raw) = _parent(monkeypatch, b"x" * (limit + 1), max_bytes=limit)
    assert envelope["max_bytes"] == limit and status == 200 and len(raw) == limit + 1
    with pytest.raises(OSError, match="http worker response invalid"):
        _parent(monkeypatch, b"x" * (limit + 2), max_bytes=limit)


def test_parent_default_request_declares_and_keeps_the_default_limit(monkeypatch):
    envelope, (_status, _headers, raw) = _parent(monkeypatch, b"x" * (b.MAX_RESPONSE_BYTES + 1))
    assert envelope["max_bytes"] == b.MAX_RESPONSE_BYTES and len(raw) == b.MAX_RESPONSE_BYTES + 1
    with pytest.raises(OSError, match="http worker response invalid"):
        _parent(monkeypatch, b"x" * (b.MAX_RESPONSE_BYTES + 2))
