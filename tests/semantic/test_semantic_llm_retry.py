"""Bounded retry contracts for unsuccessful local semantic-model calls."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import conftest as test_guard
import semantic
from semantic_testkit import _FakeJev, _cfg, _seeded


@pytest.fixture(autouse=True)
def isolated_format_marks(monkeypatch, tmp_path):
    monkeypatch.setattr(semantic, "_FMT_PATH", str(tmp_path / "format.json"))


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
    monkeypatch.setattr(semantic, "_probe_format",
                        lambda *a: "plain")   # no format probe
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


def _response(text, finish="stop"):
    return {"choices": [{"message": {"content": text},
                         "finish_reason": finish}]}


def test_semantic_llm_chat_requests_json_object_after_probe(monkeypatch):
    """Every semantic prompt expects JSON: once the probe accepts
    json_object, the constraint rides every call. The probe spends the
    caller's timeout instead of adding up to 10 s on top of it."""
    import local_llm
    monkeypatch.setattr(semantic, "_FMT_MODE", None)
    monkeypatch.setattr(semantic, "_FMT_TS", 0.0)
    bodies, timeouts = [], []

    def send(endpoint, method, body, timeout, deadline=None):
        bodies.append(body)
        timeouts.append(timeout)
        text = '{"ok": true}' if "Reply with" in body["messages"][0]["content"] \
            else '{"facts": []}'
        return 200, {}, json.dumps(_response(text)).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    assert semantic.llm_chat("抽出 JSON:", timeout=4) == '{"facts": []}'
    assert semantic._FMT_MODE == "object"
    assert bodies[-1]["response_format"] == {"type": "json_object"}
    assert bodies[-1]["id_slot"] == local_llm.BACKGROUND_SLOT
    assert len(timeouts) == 2 and all(t <= 4 for t in timeouts)
    # cached: a second call does not re-probe
    semantic.llm_chat("次 JSON:")
    assert sum("Reply with" in b["messages"][0]["content"] for b in bodies) == 1


def test_semantic_llm_chat_degrades_to_plain_when_format_rejected(monkeypatch):
    import local_llm
    monkeypatch.setattr(semantic, "_FMT_MODE", "object")
    monkeypatch.setattr(semantic, "_FMT_TS", 10.0 ** 9)
    formats = []

    def send(endpoint, method, body, timeout, deadline=None):
        formats.append(body.get("response_format"))
        if body.get("response_format"):
            return 422, {}, b""
        return 200, {}, json.dumps(_response("{}")).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    assert semantic.llm_chat("p JSON:") == "{}"
    assert formats == [{"type": "json_object"}, None]
    assert semantic._FMT_MODE == "plain"
    # the module cooldown restarts at the rejection (not a local binding)
    assert semantic._FMT_TS != 10.0 ** 9
    assert semantic._FMT_TS <= semantic.time.monotonic()


def test_semantic_llm_chat_reject_retry_shares_remaining_budget(monkeypatch):
    """The degrade-to-plain retry spends what is left of the caller's
    timeout — not a fresh one — so the absolute deadline holds."""
    import local_llm
    monkeypatch.setattr(semantic, "_FMT_MODE", "object")
    monkeypatch.setattr(semantic, "_FMT_TS", 10.0 ** 9)
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    timeouts = []

    def send(endpoint, method, body, timeout, deadline=None):
        timeouts.append(timeout)
        clock[0] += 1.5
        if body.get("response_format"):
            return 422, {}, b""
        return 200, {}, json.dumps(_response("{}")).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    assert semantic.llm_chat("p JSON:", timeout=4) == "{}"
    assert timeouts == [4, 2.5]


def _long_send(bodies, long_ok):
    def send(endpoint, method, body, timeout, deadline=None):
        bodies.append(body)
        if body["max_tokens"] > semantic.LLM_MAX_TOKENS and long_ok:
            return 200, {}, json.dumps(_response('{"facts": []}')).encode()
        return 200, {}, json.dumps(_response('{"facts": [', "length")).encode()
    return send


def _long_env(monkeypatch, tmp_path, bodies, long_ok):
    import local_llm
    monkeypatch.setattr(semantic, "_FMT_MODE", "plain")
    monkeypatch.setattr(semantic, "_FMT_TS", 10.0 ** 9)
    monkeypatch.setattr(semantic, "_LONG_PATH",
                        str(tmp_path / "data" / "long.json"))
    monkeypatch.setattr(local_llm, "bounded_request",
                        _long_send(bodies, long_ok))


def _ceilings(bodies):
    return [b["max_tokens"] for b in bodies]


@pytest.mark.parametrize("status", [400, 404, 422])
@pytest.mark.parametrize("spent,expected_calls", [(51.0, 1), (50.0, 2)])
def test_remembered_long_output_format_retry_rechecks_remaining_budget(
        monkeypatch, tmp_path, spent, expected_calls, status):
    import local_llm
    _long_env(monkeypatch, tmp_path, [], True)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic, "_probe_format", lambda *a: "object")
    semantic._long_mark(semantic._long_key(semantic.llm_conf()[1], "synthetic"), 1)
    clock = [0.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            clock[0] += spent
            return {"status": status}
        return {"status": 200, "text": '{"facts": []}', "finish_reason": "stop"}

    monkeypatch.setattr(local_llm, "chat", chat)
    if status == 400 and expected_calls == 1:
        with pytest.raises(semantic.runtime.LLMRejected):
            semantic.llm_chat("synthetic", timeout=450)
        out = None
    else:
        out = semantic.llm_chat("synthetic", timeout=450)
    assert len(calls) == expected_calls
    assert out == (None if expected_calls == 1 else '{"facts": []}')
    assert all(c["max_tokens"] == semantic.LLM_LONG_MAX_TOKENS for c in calls)
    if expected_calls == 2:
        assert calls[1]["timeout"] == 400.0
        assert calls[1]["response_format"] is None
    assert semantic._long_marks()[semantic._long_key(
        semantic.llm_conf()[1], "synthetic")] == 1


@pytest.mark.parametrize("long_ok", [True, False])
def test_length_stop_retries_once_at_long_ceiling_then_stops_spending(
        monkeypatch, tmp_path, long_ok):
    """temperature 0 stops a fact-rich doc at the same max_tokens on
    every retry: a length stop is retried once at the long ceiling, and
    a prompt that stops even there fails fast later without a call."""
    bodies = []
    _long_env(monkeypatch, tmp_path, bodies, long_ok)
    short, long_ = semantic.LLM_MAX_TOKENS, semantic.LLM_LONG_MAX_TOKENS
    out = semantic.llm_chat("抽出 JSON:", timeout=900)
    assert _ceilings(bodies) == [short, long_]
    assert out == ('{"facts": []}' if long_ok else None)
    bodies.clear()
    out = semantic.llm_chat("抽出 JSON:", timeout=450)
    if long_ok:
        # remembered: the next attempt starts at the long ceiling
        assert _ceilings(bodies) == [long_]
        assert out == '{"facts": []}'
    else:
        assert bodies == [] and out is None
    bodies.clear()
    semantic.llm_chat("別 JSON:", timeout=300)     # other prompts untouched
    assert _ceilings(bodies)[0] == short


def test_length_stop_without_time_left_uses_long_ceiling_next_attempt(
        monkeypatch, tmp_path):
    bodies = []
    _long_env(monkeypatch, tmp_path, bodies, True)
    assert semantic.llm_chat("抽出 JSON:", timeout=30) is None
    assert len(bodies) == 1          # < _LONG_MIN_CALL_S left
    # a marked prompt is never sent into a budget it cannot finish in
    # (a tick lane): free defer, the background lane takes it
    with pytest.raises(semantic.runtime.LLMNotSent):
        semantic.llm_chat("抽出 JSON:", timeout=300)
    assert len(bodies) == 1
    assert semantic.llm_chat("抽出 JSON:", timeout=450) == '{"facts": []}'
    assert _ceilings(bodies)[-1] == semantic.LLM_LONG_MAX_TOKENS


def test_llm_chat_context_reject_is_terminal(monkeypatch):
    """A plain-mode HTTP 400 (e.g. the prompt exceeds the slot's context
    window) can never succeed on retry — llm_chat raises LLMRejected
    instead of returning a retryable model failure."""
    import local_llm
    import semantic_runtime as runtime
    monkeypatch.setattr(semantic, "_FMT_MODE", "plain")
    monkeypatch.setattr(semantic, "_FMT_TS", 10.0 ** 9)
    bodies = []

    def send(endpoint, method, body, timeout, deadline=None):
        bodies.append(body)
        return 400, {}, json.dumps(
            {"error": {"message": "request exceeds the available "
                       "context size",
                       "type": "exceed_context_size_error"}}).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    with pytest.raises(runtime.LLMRejected):
        semantic.llm_chat("synthetic prompt", timeout=10)
    assert len(bodies) == 1        # deterministic: no retry burn


def test_summarize_maps_llm_rejection_to_input_oversize(tmp_path):
    """A backend context refusal lands on the designed oversize path —
    a NEEDS_REVIEW stub, not a burned attempt."""
    import semantic_runtime as runtime
    db = _seeded(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        calls = []

        def rejected(_prompt):
            calls.append(_prompt)
            raise runtime.LLMRejected("prompt_rejected")

        summary = semantic.summarize(rejected, bundle, 1, [], {})
        assert summary["_input_oversize"] is True
        assert summary["claims"] == []
        assert len(calls) == 1
    finally:
        db.close()


def test_format_rejection_plain_survives_worker_restart(monkeypatch, tmp_path):
    import local_llm
    _long_env(monkeypatch, tmp_path, [], True)
    monkeypatch.setattr(semantic, "_FMT_PATH", str(tmp_path / "format"), raising=False)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(local_llm, "probe_format", lambda *a, **kw: "object")
    model = semantic.llm_conf()[1]
    semantic._long_mark(semantic._long_key(model, "synthetic"), 1)
    monkeypatch.setattr(semantic, "_FMT_MODE", None)
    clock = [0.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["response_format"] is not None:
            clock[0] += 51.0
            return {"status": 422}
        return {"status": 200, "text": "{}", "finish_reason": "stop"}

    monkeypatch.setattr(local_llm, "chat", chat)
    assert semantic.llm_chat("synthetic", timeout=450) is None
    assert len(calls) == 1
    # A separate worker has no in-memory probe result. The durable mark
    # must avoid spending another constrained request on the same server.
    monkeypatch.setattr(semantic, "_FMT_MODE", None)
    monkeypatch.setattr(semantic, "_FMT_TS", 0.0)
    assert semantic.llm_chat("synthetic", timeout=450) == "{}"
    assert len(calls) == 2
    assert calls[-1]["response_format"] is None
    assert calls[-1]["max_tokens"] == semantic.LLM_LONG_MAX_TOKENS


def test_durable_format_cooldown_scope_expiry_and_corruption(monkeypatch, tmp_path):
    import local_llm
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "time", lambda: clock[0])
    monkeypatch.setattr(semantic, "_FMT_MODE", None)
    probes = []
    monkeypatch.setattr(local_llm, "probe_format",
                        lambda *a, **kw: probes.append(a) or "object")
    semantic._remember_plain("http://127.0.0.1:1", "synthetic-model")
    assert semantic._probe_format("http://127.0.0.1:1", "synthetic-model") == "plain"
    assert not probes
    assert semantic._probe_format("http://127.0.0.1:2", "synthetic-model") == "object"
    monkeypatch.setattr(semantic, "_FMT_MODE", None)
    assert semantic._probe_format("http://127.0.0.1:1", "other-model") == "object"
    clock[0] += semantic._PROBE_RETRY_S
    monkeypatch.setattr(semantic, "_FMT_MODE", None)
    assert semantic._probe_format("http://127.0.0.1:1", "synthetic-model") == "object"
    from pathlib import Path
    Path(semantic._FMT_PATH).write_text("broken")
    assert semantic._format_marks() == {}
    key = semantic._format_key("http://127.0.0.1:1", "synthetic-model")
    Path(semantic._FMT_PATH).write_text(json.dumps({key: clock[0] + 1000}))
    assert semantic._format_marks() == {}


def test_fresh_process_reads_format_cooldown(monkeypatch, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    semantic._remember_plain("http://127.0.0.1:1", "synthetic-model")
    code = """
import sys
sys.path.insert(0, sys.argv[1])
import _mcs_path
import semantic
semantic._FMT_PATH = sys.argv[2]
semantic.local_llm.probe_format = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("probe must not run"))
assert semantic._probe_format("http://127.0.0.1:1", "synthetic-model") == "plain"
"""
    root = Path(__file__).resolve().parents[2] / "mcs"
    result = subprocess.run([sys.executable, "-c", code, str(root),
                             semantic._FMT_PATH],
                            env={"HOME": os.environ["HOME"],
                                 "PATH": os.environ["PATH"]},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_constrained_long_rejection_returns_terminal_summary(monkeypatch, tmp_path):
    import local_llm
    _long_env(monkeypatch, tmp_path, [], True)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic, "_probe_format", lambda *a: "object")
    # All generated synthetic summary prompts are remembered as long.
    monkeypatch.setattr(semantic, "_long_marks", lambda: {"synthetic-key": 1})
    monkeypatch.setattr(semantic, "_long_key", lambda *a: "synthetic-key")
    clock = [0.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        clock[0] += 51.0
        return {"status": 400}

    monkeypatch.setattr(local_llm, "chat", chat)
    db = _seeded(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        summary, reason = semantic.summarize(
            lambda prompt: semantic.llm_chat(prompt, timeout=450),
            bundle, 1, [], {}, return_reason=True)
        assert summary["_input_oversize"] is True
        assert summary["claims"] == []
        assert reason is None  # not the retryable "model" failure
        assert len(calls) == 1
    finally:
        db.close()


def test_format_persistence_time_is_charged_to_retry_budget(monkeypatch, tmp_path):
    import local_llm
    _long_env(monkeypatch, tmp_path, [], True)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic, "_probe_format", lambda *a: "object")
    semantic._long_mark(semantic._long_key(semantic.llm_conf()[1], "synthetic"), 1)
    clock = [0.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        clock[0] += 50.0
        return {"status": 422}

    def remember(*args):
        clock[0] += 1.0

    monkeypatch.setattr(local_llm, "chat", chat)
    monkeypatch.setattr(semantic, "_remember_plain", remember)
    assert semantic.llm_chat("synthetic", timeout=450) is None
    assert len(calls) == 1  # 399s, not the pre-persistence 400s


def test_format_persistence_io_failure_keeps_same_call_fallback(monkeypatch, tmp_path):
    import local_llm
    # An existing regular file cannot become the state directory.
    blocked = tmp_path / "blocked"
    blocked.write_text("synthetic")
    monkeypatch.setattr(semantic, "_FMT_PATH", str(blocked / "format.json"))
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic, "_probe_format", lambda *a: "object")
    monkeypatch.setattr(semantic, "_long_marks", lambda: {})
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["response_format"] is not None:
            return {"status": 422}
        return {"status": 200, "text": "{}", "finish_reason": "stop"}

    monkeypatch.setattr(local_llm, "chat", chat)
    assert semantic.llm_chat("synthetic", timeout=10) == "{}"
    assert len(calls) == 2


def test_format_mark_lock_contention_preserves_previous_state(monkeypatch, tmp_path):
    import fcntl
    from pathlib import Path
    semantic._remember_plain("http://127.0.0.1:1", "synthetic-model")
    original = Path(semantic._FMT_PATH).read_bytes()
    with open(semantic._FMT_PATH + ".lock", "a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        semantic._remember_plain("http://127.0.0.1:2", "synthetic-model")
    assert Path(semantic._FMT_PATH).read_bytes() == original


def test_format_mark_retention_is_bounded(monkeypatch, tmp_path):
    from pathlib import Path
    now = semantic.time.time()
    marks = {f"synthetic-{i}": now for i in range(500)}
    Path(semantic._FMT_PATH).write_text(json.dumps(marks))
    semantic._remember_plain("http://127.0.0.1:1", "synthetic-model")
    kept = semantic._format_marks()
    assert len(kept) == 500
    assert "synthetic-0" not in kept
    assert semantic._format_key("http://127.0.0.1:1", "synthetic-model") in kept
