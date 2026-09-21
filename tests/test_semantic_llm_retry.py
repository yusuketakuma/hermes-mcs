"""Bounded retry contracts for unsuccessful local semantic-model calls."""
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "mcs"))

import conftest as test_guard
import semantic
from test_mcs_semantic import _FakeJev, _cfg, _seeded


def _guard_original(owner, name):
    for module, attr, original in reversed(test_guard._ORIGINALS):
        if module is owner and attr == name:
            return original
    raise AssertionError(f"missing guarded original {owner!r}.{name}")


@pytest.fixture
def local_http(monkeypatch):
    """Permit only the synthetic loopback server for this test."""
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
                                  name="semantic-llm-test-http", daemon=True)
        thread.start()
        servers.append((server, thread))
        host, port = server.server_address
        return f"http://{host}:{port}/v1/chat/completions"

    yield start

    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def _run_with_missing_model(db):
    return semantic.run_due(
        db, _cfg("shadow"), {"errors": []}, time.monotonic() + 300,
        jev_client=_FakeJev(), llm_fn=lambda _prompt: None)


def _run_with_missing_summary(db):
    def llm(prompt):
        if "事実候補抽出器" in prompt:
            return json.dumps({"facts": []})
        return None

    return semantic.run_due(
        db, _cfg("shadow"), {"errors": []}, time.monotonic() + 300,
        jev_client=_FakeJev(), llm_fn=llm)


def test_missing_local_model_response_reaches_attempt_limit(tmp_path):
    db = _seeded(tmp_path)
    try:
        for expected_attempts in range(1, 7):
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            _run_with_missing_model(db)
            row = db.db.execute(
                "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'") \
                .fetchone()
            assert row["attempts"] == expected_attempts
        assert row["state"] == "failed"
    finally:
        db.close()


def test_replay_same_source_keeps_summary_retry_attempts(tmp_path):
    db = _seeded(tmp_path)
    try:
        _run_with_missing_summary(db)
        first = db.db.execute(
            "SELECT attempts,payload FROM fetch_jobs WHERE kind='semantic'") \
            .fetchone()
        assert first["attempts"] == 1

        # A replay changes provenance and the job generation, but not the
        # source generation.  The accumulated retry budget must survive.
        db.semantic_seed(1, [1], {"source": "replay"})
        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        _run_with_missing_summary(db)
        row = db.db.execute(
            "SELECT state,attempts,payload FROM fetch_jobs WHERE kind='semantic'") \
            .fetchone()
        assert row["attempts"] == 2
        assert row["state"] == "pending"
    finally:
        db.close()


def test_llm_chat_worker_enforces_absolute_deadline_and_no_auth(local_http,
                                                                 monkeypatch):
    received = []
    started = threading.Event()
    response = json.dumps({
        "choices": [{"message": {"content": "synthetic"}}],
    }).encode()

    class SlowHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers["Content-Length"])
            received.append((dict(self.headers),
                             json.loads(self.rfile.read(length))))
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            started.set()
            try:
                for byte in response:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            return

    endpoint = local_http(SlowHandler)
    monkeypatch.setattr(semantic, "LLM_ENDPOINT", endpoint)
    started_at = time.monotonic()
    assert semantic.llm_chat("synthetic prompt", timeout=0.35) is None
    elapsed = time.monotonic() - started_at

    assert started.wait(0.5)
    assert elapsed < 1.5
    headers, body = received[0]
    assert headers.get("Authorization") is None
    assert body["model"] == semantic.LLM_MODEL
    assert body["messages"] == [{"role": "user", "content":
                                  "synthetic prompt"}]
    assert body["max_tokens"] == 1400
    assert body["temperature"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
