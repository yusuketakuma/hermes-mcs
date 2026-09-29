"""Bounded retry contracts for unsuccessful local semantic-model calls."""
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import conftest as test_guard
import semantic
from semantic_testkit import _FakeJev, _cfg, _seeded


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
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            return

    endpoint = local_http(SlowHandler)
    monkeypatch.setattr(semantic, "LLM_ENDPOINT", endpoint)
    trickle_s = 0.25 * len(response)
    started_at = time.monotonic()
    # The worker is a fresh interpreter process whose spawn can take a
    # while under suite load, so timeout=2.0 leaves headroom for the POST
    # to land.  The byte-trickled response takes ~20 s; returning None far
    # below that proves the absolute deadline fired instead of a per-read
    # idle timeout — with a wide margin rather than a tight wall-clock
    # bound that flakes on a loaded machine.
    assert semantic.llm_chat("synthetic prompt", timeout=2.0) is None
    elapsed = time.monotonic() - started_at

    assert started.wait(5.0)
    assert elapsed < trickle_s / 2
    headers, body = received[0]
    assert headers.get("Authorization") is None
    assert body["model"] == semantic.LLM_MODEL
    assert body["messages"] == [{"role": "user", "content":
                                  "synthetic prompt"}]
    assert body["max_tokens"] == semantic.LLM_MAX_TOKENS
    assert body["temperature"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def _held_admission(monkeypatch, calls):
    import local_llm
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: True)

    def held(route, prompt, **kw):
        calls.append(route)
        return {"text": None, "finish_reason": None, "usage": None,
                "status": None, "admission": "held", "permit_id": 1,
                "epoch": 1}
    monkeypatch.setattr(local_llm, "admitted_chat", held)


def _refused_connection(monkeypatch, calls):
    import local_llm
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)

    def refused(prompt, error_out=None, **kw):
        calls.append(prompt)
        if error_out is not None:
            error_out["kind"] = "unreachable"
        return None
    monkeypatch.setattr(local_llm, "chat", refused)


@pytest.mark.parametrize("not_sent", [_held_admission, _refused_connection])
def test_request_never_sent_does_not_consume_attempts(tmp_path, monkeypatch,
                                                      not_sent):
    """U07-F01: an admission hold or a refused connection means the local
    model never received the request — the job waits (deferred) instead
    of burning its bounded retry attempts until it fails."""
    import semantic_runtime
    calls = []
    not_sent(monkeypatch, calls)
    with pytest.raises(semantic_runtime.LLMNotSent):
        semantic.llm_chat("synthetic prompt")
    db = _seeded(tmp_path)
    try:
        for _ in range(7):
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            out = semantic.run_due(
                db, _cfg("shadow"), {"errors": []}, time.monotonic() + 300,
                jev_client=_FakeJev(), llm_fn=semantic.llm_chat)
            assert out["deferred"] == 1 and not out["failed"]
        row = db.db.execute(
            "SELECT state,attempts,next_try FROM fetch_jobs "
            "WHERE kind='semantic'").fetchone()
        assert row["state"] == "pending" and row["attempts"] == 0
        assert row["next_try"] > time.time()
        assert len(calls) >= 8
    finally:
        db.close()


@pytest.mark.parametrize("jev_spent, expected", [(0, "deferred"),
                                                 (1, "deferred_backoff")])
def test_not_sent_llm_defers_only_when_no_jev_was_spent(
        monkeypatch, jev_spent, expected):
    """A held/unreachable local LLM costs no attempt, but a pass that
    already spent Jev requests backs off hourly so the job cannot
    re-spend the shared Jev budget every minute while the LLM is down."""
    import semantic_drain
    import semantic_runtime

    class Client:
        requests_made = 0

    client = Client()

    def inner(*args, **kwargs):
        client.requests_made += jev_spent
        raise semantic_runtime.LLMNotSent("llm_unreachable")

    monkeypatch.setattr(semantic_drain, "_process_job_inner", inner)
    assert semantic_drain._process_job(
        None, None, None, client, lambda *a, **k: None, None) == expected


def test_not_sent_target_summary_keeps_completed_siblings(tmp_path):
    """LLMNotSent on target 2's summary must still commit target 1's
    audited result — otherwise every pass re-spends its Jev audit."""
    import semantic_runtime
    from semantic_testkit import _FakeJev, _llm
    db = _seeded(tmp_path)
    jev = _FakeJev()
    calls = {"summary": 0}

    def llm(prompt):
        if "要約器" in prompt:
            calls["summary"] += 1
            if calls["summary"] >= 2:
                raise semantic_runtime.LLMNotSent("llm_admission:held")
        return _llm(prompt)

    try:
        spent = []
        for _ in range(3):
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            before = jev.requests_made
            out = semantic.run_due(db, _cfg("shadow"), {"errors": []},
                                   time.monotonic() + 300, jev_client=jev,
                                   llm_fn=llm)
            assert out["deferred"] == 1 and not out["failed"]
            spent.append(jev.requests_made - before)
        audits = dict(db.db.execute(
            "SELECT message_id, json_extract(meta,'$.audit_status') "
            "FROM artifacts WHERE kind='semantic_audit'").fetchall())
        assert audits.get(1) == "PASS"
        assert spent[1:] == [0, 0]
        assert db.db.execute(
            "SELECT attempts FROM fetch_jobs WHERE kind='semantic'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_not_sent_repair_is_an_unavailable_repair(tmp_path):
    """LLMNotSent from the one-shot repair call ends like an unavailable
    repair (NEEDS_REVIEW) — the sibling target audited earlier in the
    pass is still committed, never discarded."""
    import json
    import semantic_runtime
    from semantic_testkit import _FakeJev, _llm
    db = _seeded(tmp_path)
    calls = {"n": 0}

    def llm(prompt):
        if "事実候補抽出器" in prompt:
            return _llm(prompt)
        calls["n"] += 1
        if calls["n"] == 2:        # target 2: claim with no evidence
            return json.dumps({"claims": [{
                "section": "medication", "text": "無根拠の断定",
                "claim_kind": "reported_fact", "fact_refs": []}],
                "limitations": []})
        if calls["n"] == 3:        # its repair never leaves
            raise semantic_runtime.LLMNotSent("llm_admission:held")
        return _llm(prompt)

    try:
        semantic.run_due(db, _cfg("shadow"), {"errors": []},
                         time.monotonic() + 300, jev_client=_FakeJev(),
                         llm_fn=llm)
        audits = dict(db.db.execute(
            "SELECT message_id, json_extract(meta,'$.audit_status') "
            "FROM artifacts WHERE kind='semantic_audit'").fetchall())
        assert audits.get(1) == "PASS"
        assert audits.get(2) == "NEEDS_REVIEW"
    finally:
        db.close()
