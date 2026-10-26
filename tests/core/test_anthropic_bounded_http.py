"""Anthropic auth cannot widen the existing keyed or anonymous transport policy."""
import io
import json
from types import SimpleNamespace

import pytest

import bounded_http as b


@pytest.mark.parametrize("overrides", [{"endpoint": "https://api.anthropic.com/v1/messages?x=1"},
                                       {"endpoint": "https://other.invalid/v1/messages"},
                                       {"method": "GET"}, {"api_key": None},
                                       {"auth": "unknown"}, {"_public_mcp": True}])
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
