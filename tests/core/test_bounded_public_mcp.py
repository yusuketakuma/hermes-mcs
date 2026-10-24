"""Public MCP mode preserves anonymous loopback and keyed transport boundaries."""
import base64
import io
import json
from types import SimpleNamespace

import pytest

import bounded_http

URL = "https://mcp.exa.ai/mcp"


@pytest.mark.parametrize("endpoint", [
    "http://mcp.exa.ai/mcp", "https://localhost/mcp", "https://127.0.0.1/mcp",
    "https://192.168.1.1/mcp", "https://[::1]/mcp", "https://name.local/mcp",
    "https://user@mcp.exa.ai/mcp", "https://mcp.exa.ai/mcp?token=private",
    "https://mcp.exa.ai/mcp#fragment", "https://mcp.exa.ai:444/mcp",
])
def test_parent_refuses_nonpublic_or_decorated_mcp_endpoints(monkeypatch, endpoint):
    monkeypatch.setattr(bounded_http.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="public_mcp_endpoint_not_allowed"):
        bounded_http.public_mcp_request(endpoint, {}, 1, allowed_endpoints=(endpoint,))


def test_allowlist_and_existing_anonymous_default_remain_required(monkeypatch):
    monkeypatch.setattr(bounded_http.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="public_mcp_endpoint_not_allowed"):
        bounded_http.public_mcp_request(URL, {}, 1)
    with pytest.raises(ValueError, match="local_endpoint_not_allowed"):
        bounded_http.bounded_http_request(URL, "POST", {}, 1, allowed_endpoints=(URL,))
    with pytest.raises(ValueError, match="public_mcp_endpoint_not_allowed"):
        bounded_http.bounded_http_request(URL, "POST", {}, 1, api_key="private",
                                          allowed_endpoints=(URL,), _public_mcp=True)
    with pytest.raises(ValueError, match="public_mcp_endpoint_not_allowed"):
        bounded_http.bounded_http_request(URL, "GET", None, 1, allowed_endpoints=(URL,), _public_mcp=True)


def _worker(monkeypatch, envelope, opener):
    output = io.StringIO()
    monkeypatch.setattr(bounded_http.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(envelope).encode())))
    monkeypatch.setattr(bounded_http.sys, "stdout", output)
    monkeypatch.setattr(bounded_http, "no_proxy_opener", opener)
    assert bounded_http._http_worker_main() == 0
    return json.loads(output.getvalue())


def test_worker_uses_fixed_accept_no_authorization_and_bounded_read(monkeypatch):
    observed = []

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, count):
            observed.append(count)
            return b"synthetic"

        def close(self):
            observed.append("closed")

    class Opener:
        def open(self, request, timeout):
            observed.append((request.full_url, dict(request.header_items()), request.data, timeout))
            return Response()

    def opener(redirect):
        assert redirect is bounded_http.NoRedirect
        return Opener()

    result = _worker(monkeypatch, {"endpoint": URL, "method": "POST", "api_key": None,
                     "allowed_endpoints": [URL], "public_mcp": True, "timeout": 3,
                     "body": {"jsonrpc": "2.0"}}, opener)
    endpoint, headers, body, timeout = observed[0]
    assert endpoint == URL and timeout == 3
    assert headers["Accept"] == "application/json, text/event-stream"
    assert headers["Content-type"] == "application/json"
    assert not any(key.lower() == "authorization" for key in headers)
    assert json.loads(body) == {"jsonrpc": "2.0"}
    assert observed[1:] == [bounded_http.MAX_RESPONSE_BYTES + 1, "closed"]
    assert result["ok"] and base64.b64decode(result["body"]) == b"synthetic"


@pytest.mark.parametrize("overrides", [
    {"api_key": "private"}, {"method": "GET"}, {"allowed_endpoints": []},
    {"endpoint": "https://127.0.0.1/mcp", "allowed_endpoints": ["https://127.0.0.1/mcp"]},
    {"endpoint": "http://mcp.exa.ai/mcp", "allowed_endpoints": ["http://mcp.exa.ai/mcp"]},
    {"public_mcp": "yes"}, {"public_mcp": False},
])
def test_worker_independently_refuses_invalid_public_mode(monkeypatch, overrides):
    envelope = {"endpoint": URL, "method": "POST", "api_key": None,
                "allowed_endpoints": [URL], "public_mcp": True, "timeout": 1, "body": {}}
    envelope.update(overrides)
    result = _worker(monkeypatch, envelope, lambda *args: pytest.fail("must not open"))
    assert result == {"ok": False, "error": "transport"}


def test_parent_propagates_mcp_mode_allowlist_and_deadline(monkeypatch):
    observed = []
    monkeypatch.setattr(bounded_http.time, "monotonic", lambda: 10.0)

    class Process:
        returncode = 0

        def communicate(self, input, timeout):
            observed.append((json.loads(input), timeout))
            return json.dumps({"ok": True, "status": 200, "headers": {}, "body": "eA=="}).encode(), b""

    monkeypatch.setattr(bounded_http.subprocess, "Popen", lambda *args, **kwargs: Process())
    assert bounded_http.public_mcp_request(URL, {}, 30, deadline=12, allowed_endpoints=(URL,)) == (200, {}, b"x")
    envelope, timeout = observed[0]
    assert envelope["public_mcp"] is True and envelope["api_key"] is None
    assert envelope["allowed_endpoints"] == [URL]
    assert timeout == envelope["timeout"] == 2
