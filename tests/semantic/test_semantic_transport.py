"""Synthetic local transport checks for the bounded Jev HTTP path."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import mcs_test_guard as test_guard
import semantic_jev as jev


def _guard_original(owner, name):
    for module, attr, original in reversed(test_guard._ORIGINALS):
        if module is owner and attr == name:
            return original
    raise AssertionError(f"missing guarded original {owner!r}.{name}")


@pytest.fixture
def local_http(monkeypatch):
    """Run only loopback servers after explicitly lifting the socket guard."""
    # tests/conftest.py blocks all socket connects so an accidental live
    # call fails.  These three originals are restored only for this fixture;
    # the endpoint is a temporary loopback server created below.
    monkeypatch.setattr(socket, "create_connection",
                        _guard_original(socket, "create_connection"))
    monkeypatch.setattr(socket.socket, "connect",
                        _guard_original(socket.socket, "connect"))
    monkeypatch.setattr(socket.socket, "connect_ex",
                        _guard_original(socket.socket, "connect_ex"))
    servers = []

    def start(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  name="semantic-test-http", daemon=True)
        thread.start()
        servers.append((server, thread))
        host, port = server.server_address
        return f"http://{host}:{port}/v1/systemone"

    yield start

    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def _questions():
    return {"q": jev.noul_question("inspect state.target", "yes", "no")}


def test_slow_trickle_cannot_extend_real_attempt_deadline(local_http,
                                                           monkeypatch):
    started = threading.Event()
    payload = b"{" + (b"x" * 127) + b"}"

    class SlowHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            started.set()
            try:
                for byte in payload:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            return

    endpoint = local_http(SlowHandler)
    monkeypatch.setattr(jev, "JEV_ALLOWED_ENDPOINTS",
                        frozenset({endpoint}))
    events = []
    client = jev.JevClient(api_key="synthetic", endpoint=endpoint,
                           attempt_timeout=1.0, max_attempts=1)
    client.before_attempt = lambda: events.append("before")
    client.reserve_fn = lambda _body, _timeout: events.append("reserve")
    client.after_result = lambda: events.append("after")

    started_at = time.monotonic()
    with pytest.raises(jev.JevError) as exc_info:
        client.evaluate({"target": {"text": "x"}, "context": []},
                        _questions(), time.monotonic() + 0.35)
    elapsed = time.monotonic() - started_at

    assert exc_info.value.kind == "timeout"
    assert elapsed < 1.5
    assert events == ["before", "reserve"]
    assert client.requests_made == 1


def test_real_transport_keeps_hooks_and_fixed_wire_contract(local_http,
                                                              monkeypatch):
    received = []
    auth_headers = []
    answer = {
        "model": jev.JEV_MODEL,
        "answers": {"q": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    response = json.dumps(answer).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers["Content-Length"])
            auth_headers.append(self.headers.get("Authorization"))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()

        def log_message(self, *_args):
            return

    endpoint = local_http(Handler)
    monkeypatch.setattr(jev, "JEV_ALLOWED_ENDPOINTS",
                        frozenset({endpoint}))
    events = []
    client = jev.JevClient(api_key="synthetic", endpoint=endpoint,
                           attempt_timeout=1.0, max_attempts=1)
    client.before_attempt = lambda: events.append("before")
    client.reserve_fn = lambda _body, _timeout: events.append("reserve")
    client.after_result = lambda: events.append("after")

    result = client.evaluate({"target": {"text": "x"}, "context": []},
                             _questions(), time.monotonic() + 2.0)

    assert result["answers"]["q"]["noul"] == 0.9
    assert result["usage"] == {"input_tokens": 2, "output_tokens": 1}
    assert events == ["before", "reserve", "after"]
    assert auth_headers == ["Bearer synthetic"]
    assert received[0]["model"] == jev.JEV_MODEL
    assert "criteria" in received[0]["questions"]["q"]
    assert received[0]["questions"]["q"]["type"] == "noul"


def test_real_transport_does_not_follow_redirect_and_caps_body(local_http,
                                                                 monkeypatch):
    payload = b"{" + (b"x" * jev.MAX_RESPONSE_BYTES) + b"}"
    hits = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            hits.append(1)
            if len(hits) == 1:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/redirect")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

        def log_message(self, *_args):
            return

    endpoint = local_http(Handler)
    monkeypatch.setattr(jev, "JEV_ALLOWED_ENDPOINTS",
                        frozenset({endpoint}))
    client = jev.JevClient(api_key="synthetic", endpoint=endpoint,
                           attempt_timeout=1.0, max_attempts=1)
    with pytest.raises(jev.JevError) as redirect_error:
        client.evaluate({"target": {"text": "x"}, "context": []},
                        _questions(), time.monotonic() + 2.0)
    assert redirect_error.value.kind == "protocol_error"
    assert len(hits) == 1

    with pytest.raises(jev.JevError) as size_error:
        client.evaluate({"target": {"text": "x"}, "context": []},
                        _questions(), time.monotonic() + 2.0)
    assert size_error.value.detail == "response_too_large"
    assert len(hits) == 2
