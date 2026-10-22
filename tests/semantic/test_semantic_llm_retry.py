"""Bounded retry contracts for unsuccessful local semantic-model calls."""
import llm_availability
import semantic_drain
import semantic_llm
import semantic_runtime
import semantic_store
import semantic_transport
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import mcs_test_guard as test_guard
import semantic
from semantic_testkit import _FakeJev, _cfg, _seeded


@pytest.fixture(autouse=True)
def isolated_format_marks(monkeypatch, tmp_path):
    monkeypatch.setattr(semantic_transport, '_FMT_PATH', str(tmp_path / "format.json"))


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
    return semantic_drain.run_due(
        db, _cfg("shadow"), {"errors": []}, time.monotonic() + 300,
        jev_client=_FakeJev(), llm_fn=lambda _prompt: None)


def _run_with_missing_summary(db):
    def llm(prompt):
        if "事実候補抽出器" in prompt:
            return json.dumps({"facts": []})
        return None

    return semantic_drain.run_due(
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
    import bounded_http

    received = []
    started = threading.Event()
    landed_at = []
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
            landed_at.append(time.monotonic())
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
    monkeypatch.setattr(semantic_transport, 'LLM_ENDPOINT', endpoint)
    monkeypatch.setattr(semantic_transport, '_probe_format',
                        lambda *a: "plain")   # no format probe
    trickle_s = 0.25 * len(response)
    spawn = bounded_http.subprocess.Popen
    workers, timed_out = [], []

    def popen_after_post(*args, **kwargs):
        # Worker interpreter start is not under test and slows down under
        # suite load: send the request, then hand the parent's real-time
        # communicate window over only once the POST has landed.
        process = spawn(*args, **kwargs)
        workers.append(process)
        communicate = process.communicate

        def communicate_after_post(input=None, timeout=None):
            if input is None:          # the reap after kill: unchanged
                return communicate(timeout=timeout)
            process.stdin.write(input)
            process.stdin.close()
            process.stdin = None
            startup_deadline = time.monotonic() + 60
            while (not started.wait(0.05) and process.poll() is None
                   and time.monotonic() < startup_deadline):
                pass
            try:
                return communicate(timeout=timeout)
            except bounded_http.subprocess.TimeoutExpired:
                timed_out.append(process)
                raise
        process.communicate = communicate_after_post
        return process

    monkeypatch.setattr(bounded_http.subprocess, "Popen", popen_after_post)
    # The byte-trickled response takes ~20 s; returning None far below that
    # after the POST landed proves the absolute deadline cut the in-flight
    # response instead of a per-read idle timeout.
    assert semantic_transport.llm_chat("synthetic prompt", timeout=2.0) is None
    returned_at = time.monotonic()

    assert started.is_set(), semantic_transport.last_llm_failure()
    assert returned_at - landed_at[0] < trickle_s / 2
    # the parent's absolute deadline fired, then the worker was killed and reaped
    assert len(workers) == 1 and timed_out == workers
    assert workers[0].returncode is not None and workers[0].returncode < 0
    headers, body = received[0]
    assert headers.get("Authorization") is None
    assert body["model"] == semantic_transport.LLM_MODEL
    assert body["messages"] == [{"role": "user", "content":
                                  "synthetic prompt"}]
    assert body["max_tokens"] == semantic_transport.LLM_MAX_TOKENS
    assert body["temperature"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def _held_admission(monkeypatch, calls):
    import local_llm
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: True)
    # A held admission also yields the plain probe; stubbing it keeps the
    # test from opening a real broker at the unset ("") admission path.
    monkeypatch.setattr(local_llm, "admitted_probe_format",
                        lambda *a, **kw: "plain")

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


def _turn_busy(monkeypatch, calls):
    import local_llm
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)

    def busy(prompt, error_out=None, **kw):
        calls.append(prompt)
        if error_out is not None:
            error_out["kind"] = "busy"
        return None
    monkeypatch.setattr(local_llm, "chat", busy)


def test_turn_wait_expiry_is_not_an_outage(monkeypatch):
    """The other backlog lane holding the GPU turn past the deadline is a
    not-sent class outside the LLM-down reasons: no breaker, no hourly
    backoff."""
    import semantic_drain
    import semantic_runtime
    calls = []
    _turn_busy(monkeypatch, calls)
    with pytest.raises(semantic_runtime.LLMNotSent) as info:
        semantic_transport.llm_chat("synthetic prompt")
    assert info.value.stage == "llm_turn_busy"
    assert info.value.stage not in semantic_drain._LLM_DOWN_REASONS
    assert calls[-1] == "synthetic prompt"  # a format probe may precede it


@pytest.mark.parametrize("not_sent", [_held_admission, _refused_connection, _turn_busy])
def test_request_never_sent_does_not_consume_attempts(tmp_path, monkeypatch,
                                                      not_sent):
    """U07-F01: an admission hold or a refused connection means the local
    model never received the request — the job waits (deferred) instead
    of burning its bounded retry attempts until it fails."""
    import semantic_runtime
    calls = []
    not_sent(monkeypatch, calls)
    with pytest.raises(semantic_runtime.LLMNotSent):
        semantic_transport.llm_chat("synthetic prompt")
    db = _seeded(tmp_path)
    try:
        for _ in range(7):
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            # a refused connection now feeds the shared endpoint breaker;
            # let each cooldown lapse so every pass reaches the model
            llm_availability.circuit_state_path(db.db).unlink(missing_ok=True)
            out = semantic_drain.run_due(
                db, _cfg("shadow"), {"errors": []}, time.monotonic() + 300,
                jev_client=_FakeJev(), llm_fn=semantic_transport.llm_chat)
            assert out["deferred"] == 1 and not out["failed"]
        row = db.db.execute(
            "SELECT state,attempts,next_try FROM fetch_jobs "
            "WHERE kind='semantic'").fetchone()
        assert row["state"] == "pending" and row["attempts"] == 0
        assert row["next_try"] > time.time()
        assert len(calls) >= 8
    finally:
        db.close()


@pytest.mark.parametrize("stage, jev_spent, expected", [
    ("llm_unreachable", 0, "deferred"),
    ("llm_unreachable", 1, "deferred_backoff"),
    ("llm_turn_busy", 1, "deferred"),
])
def test_not_sent_llm_defers_only_when_no_jev_was_spent(
        monkeypatch, stage, jev_spent, expected):
    """A held/unreachable local LLM costs no attempt, but a pass that
    already spent Jev requests backs off hourly so the job cannot
    re-spend the shared Jev budget every minute while the LLM is down.
    A lost GPU turn is not an outage: short defer even after Jev spend."""
    import semantic_drain
    import semantic_runtime

    class Client:
        requests_made = 0

    client = Client()

    def inner(*args, **kwargs):
        client.requests_made += jev_spent
        raise semantic_runtime.LLMNotSent(stage)

    monkeypatch.setattr(semantic_drain, "_process_job_inner", inner)
    assert semantic_drain._process_job(
        None, None, None, client, lambda *a, **k: None, None, diagnostics=semantic_drain._job_diagnostics()) == expected


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
            out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
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


def test_not_sent_repair_resumes_and_keeps_completed_siblings(tmp_path):
    """An undispatched repair waits without spending its one-shot budget;
    the sibling already audited in the pass stays committed."""
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
        semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                         time.monotonic() + 300, jev_client=_FakeJev(),
                         llm_fn=llm)
        audits = dict(db.db.execute(
            "SELECT message_id, json_extract(meta,'$.audit_status') "
            "FROM artifacts WHERE kind='semantic_audit'").fetchall())
        assert audits.get(1) == "PASS"
        assert audits.get(2) is None
        assert not db.artifacts("semantic_repair", message_id=2)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                               time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=llm)
        assert out["done"] == 1
        assert len(db.artifacts("semantic_repair", message_id=2)) == 1
        assert len(db.artifacts("semantic_summary", message_id=1)) == 1
        latest = db.artifacts("semantic_audit", message_id=2)[-1]
        assert json.loads(latest["meta"])["audit_status"] == "PASS"
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
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', None)
    monkeypatch.setattr(semantic_transport, '_FMT_TS', 0.0)
    bodies, timeouts = [], []

    def send(endpoint, method, body, timeout, deadline=None):
        bodies.append(body)
        timeouts.append(timeout)
        text = '{"ok": true}' if "Reply with" in body["messages"][0]["content"] \
            else '{"facts": []}'
        return 200, {}, json.dumps(_response(text)).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    assert semantic_transport.llm_chat("抽出 JSON:", timeout=4) == '{"facts": []}'
    assert semantic_transport._FMT_MODE == "object"
    assert bodies[-1]["response_format"] == {"type": "json_object"}
    assert bodies[-1]["id_slot"] == local_llm.BACKGROUND_SLOT
    assert len(timeouts) == 2 and all(t <= 4 for t in timeouts)
    # cached: a second call does not re-probe
    semantic_transport.llm_chat("次 JSON:")
    assert sum("Reply with" in b["messages"][0]["content"] for b in bodies) == 1


def test_semantic_llm_chat_degrades_to_plain_when_format_rejected(monkeypatch):
    import local_llm
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', "object")
    monkeypatch.setattr(semantic_transport, '_FMT_TS', 10.0 ** 9)
    formats = []

    def send(endpoint, method, body, timeout, deadline=None):
        formats.append(body.get("response_format"))
        if body.get("response_format"):
            return 422, {}, b""
        return 200, {}, json.dumps(_response("{}")).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    assert semantic_transport.llm_chat("p JSON:") == "{}"
    assert formats == [{"type": "json_object"}, None]
    assert semantic_transport._FMT_MODE == "plain"
    # the module cooldown restarts at the rejection (not a local binding)
    assert semantic_transport._FMT_TS != 10.0 ** 9
    assert semantic_transport._FMT_TS <= semantic.time.monotonic()


def test_semantic_llm_chat_reject_retry_shares_remaining_budget(monkeypatch):
    """The degrade-to-plain retry spends what is left of the caller's
    timeout — not a fresh one — so the absolute deadline holds."""
    import local_llm
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', "object")
    monkeypatch.setattr(semantic_transport, '_FMT_TS', 10.0 ** 9)
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
    assert semantic_transport.llm_chat("p JSON:", timeout=4) == "{}"
    assert timeouts == [4, 2.5]


def _long_send(bodies, long_ok):
    def send(endpoint, method, body, timeout, deadline=None):
        bodies.append(body)
        if body["max_tokens"] > semantic_transport.LLM_MAX_TOKENS and long_ok:
            return 200, {}, json.dumps(_response('{"facts": []}')).encode()
        return 200, {}, json.dumps(_response('{"facts": [', "length")).encode()
    return send


def _long_env(monkeypatch, tmp_path, bodies, long_ok):
    import local_llm
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', "plain")
    monkeypatch.setattr(semantic_transport, '_FMT_TS', 10.0 ** 9)
    monkeypatch.setattr(semantic_transport, '_LONG_PATH',
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
    monkeypatch.setattr(semantic_transport, '_probe_format', lambda *a: "object")
    semantic_transport._long_mark(semantic_transport._long_key(semantic_transport.llm_conf()[1], "synthetic"), 1)
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
    # every format refusal, 400 included, is retryable — never prompt_rejected
    out = semantic_transport.llm_chat("synthetic", timeout=670)
    assert len(calls) == expected_calls
    assert out == (None if expected_calls == 1 else '{"facts": []}')
    assert all(c["max_tokens"] == semantic_transport.LLM_LONG_MAX_TOKENS for c in calls)
    if expected_calls == 2:
        assert calls[1]["timeout"] == 620.0
        assert calls[1]["response_format"] is None
    assert semantic_transport._long_marks()[semantic_transport._long_key(
        semantic_transport.llm_conf()[1], "synthetic")] == 1


@pytest.mark.parametrize("long_ok", [True, False])
def test_length_stop_retries_once_at_long_ceiling_then_stops_spending(
        monkeypatch, tmp_path, long_ok):
    """temperature 0 stops a fact-rich doc at the same max_tokens on
    every retry: a length stop is retried once at the long ceiling, and
    a prompt that stops even there fails fast later without a call."""
    bodies = []
    _long_env(monkeypatch, tmp_path, bodies, long_ok)
    short, long_ = semantic_transport.LLM_MAX_TOKENS, semantic_transport.LLM_LONG_MAX_TOKENS
    out = semantic_transport.llm_chat("抽出 JSON:", timeout=900)
    assert _ceilings(bodies) == [short, long_]
    assert out == ('{"facts": []}' if long_ok else None)
    bodies.clear()
    out = semantic_transport.llm_chat("抽出 JSON:", timeout=670)
    if long_ok:
        # remembered: the next attempt starts at the long ceiling
        assert _ceilings(bodies) == [long_]
        assert out == '{"facts": []}'
    else:
        assert bodies == [] and out is None
    bodies.clear()
    semantic_transport.llm_chat("別 JSON:", timeout=300)     # other prompts untouched
    assert _ceilings(bodies)[0] == short


def test_length_stop_without_time_left_uses_long_ceiling_next_attempt(
        monkeypatch, tmp_path):
    bodies = []
    _long_env(monkeypatch, tmp_path, bodies, True)
    assert semantic_transport.llm_chat("抽出 JSON:", timeout=30) is None
    assert len(bodies) == 1          # < _LONG_MIN_CALL_S left
    # a marked prompt is never sent into a budget it cannot finish in
    # (a tick lane): free defer, the background lane takes it
    with pytest.raises(semantic_runtime.LLMNotSent):
        semantic_transport.llm_chat("抽出 JSON:", timeout=300)
    assert len(bodies) == 1
    assert semantic_transport.llm_chat("抽出 JSON:", timeout=670) == '{"facts": []}'
    assert _ceilings(bodies)[-1] == semantic_transport.LLM_LONG_MAX_TOKENS


def test_llm_chat_context_reject_is_terminal(monkeypatch):
    """A plain-mode HTTP 400 (e.g. the prompt exceeds the slot's context
    window) can never succeed on retry — llm_chat raises LLMRejected
    instead of returning a retryable model failure."""
    import local_llm
    import semantic_runtime as runtime
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', "plain")
    monkeypatch.setattr(semantic_transport, '_FMT_TS', 10.0 ** 9)
    bodies = []

    def send(endpoint, method, body, timeout, deadline=None):
        bodies.append(body)
        return 400, {}, json.dumps(
            {"error": {"message": "request exceeds the available "
                       "context size",
                       "type": "exceed_context_size_error"}}).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    with pytest.raises(runtime.LLMRejected):
        semantic_transport.llm_chat("synthetic prompt", timeout=10)
    assert len(bodies) == 1        # deterministic: no retry burn


def test_summarize_maps_llm_rejection_to_input_oversize(tmp_path):
    """A backend context refusal lands on the designed oversize path —
    a NEEDS_REVIEW stub, not a burned attempt."""
    import semantic_runtime as runtime
    db = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        calls = []

        def rejected(_prompt):
            calls.append(_prompt)
            raise runtime.LLMRejected("prompt_rejected")

        summary = semantic_llm.summarize(rejected, bundle, 1, [], {})
        assert summary["_input_oversize"] is True
        assert summary["claims"] == []
        assert len(calls) == 1
    finally:
        db.close()


def test_format_rejection_plain_survives_worker_restart(monkeypatch, tmp_path):
    import local_llm
    _long_env(monkeypatch, tmp_path, [], True)
    monkeypatch.setattr(semantic_transport, '_FMT_PATH', str(tmp_path / "format"), raising=False)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(local_llm, "probe_format", lambda *a, **kw: "object")
    model = semantic_transport.llm_conf()[1]
    semantic_transport._long_mark(semantic_transport._long_key(model, "synthetic"), 1)
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', None)
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
    assert semantic_transport.llm_chat("synthetic", timeout=670) is None
    assert len(calls) == 1
    # A separate worker has no in-memory probe result. The durable mark
    # must avoid spending another constrained request on the same server.
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', None)
    monkeypatch.setattr(semantic_transport, '_FMT_TS', 0.0)
    assert semantic_transport.llm_chat("synthetic", timeout=670) == "{}"
    assert len(calls) == 2
    assert calls[-1]["response_format"] is None
    assert calls[-1]["max_tokens"] == semantic_transport.LLM_LONG_MAX_TOKENS


def test_durable_format_cooldown_scope_expiry_and_corruption(monkeypatch, tmp_path):
    import local_llm
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "time", lambda: clock[0])
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', None)
    probes = []
    monkeypatch.setattr(local_llm, "probe_format",
                        lambda *a, **kw: probes.append(a) or "object")
    semantic_transport._remember_plain("http://127.0.0.1:1", "synthetic-model")
    assert semantic_transport._probe_format("http://127.0.0.1:1", "synthetic-model") == "plain"
    assert not probes
    assert semantic_transport._probe_format("http://127.0.0.1:2", "synthetic-model") == "object"
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', None)
    assert semantic_transport._probe_format("http://127.0.0.1:1", "other-model") == "object"
    clock[0] += semantic_transport._PROBE_RETRY_S
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', None)
    assert semantic_transport._probe_format("http://127.0.0.1:1", "synthetic-model") == "object"
    from pathlib import Path
    Path(semantic_transport._FMT_PATH).write_text("broken")
    assert semantic_transport._format_marks() == {}
    key = semantic_transport._format_key("http://127.0.0.1:1", "synthetic-model")
    Path(semantic_transport._FMT_PATH).write_text(json.dumps({key: clock[0] + 1000}))
    assert semantic_transport._format_marks() == {}


def test_fresh_process_reads_format_cooldown(monkeypatch, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    semantic_transport._remember_plain("http://127.0.0.1:1", "synthetic-model")
    code = """
import sys
sys.path.insert(0, sys.argv[1])
import _mcs_path
import semantic_transport
semantic_transport._FMT_PATH = sys.argv[2]
semantic_transport.local_llm.probe_format = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("probe must not run"))
assert semantic_transport._probe_format("http://127.0.0.1:1", "synthetic-model") == "plain"
"""
    root = Path(__file__).resolve().parents[2] / "mcs"
    result = subprocess.run([sys.executable, "-c", code, str(root),
                             semantic_transport._FMT_PATH],
                            env={"HOME": os.environ["HOME"],
                                 "PATH": os.environ["PATH"]},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_budget_short_format_refusal_retries_plain_on_the_next_attempt(monkeypatch, tmp_path):
    """A json_object refusal is not a refusal of the prompt: without budget
    for the plain long call this attempt is a retryable model failure, and
    the next adequately funded attempt sends the plain request."""
    import local_llm
    _long_env(monkeypatch, tmp_path, [], True)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic_transport, '_FMT_PATH', str(tmp_path / "data" / "fmt.json"))
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', "object")
    semantic_transport._long_mark(semantic_transport._long_key(semantic_transport.llm_conf()[1], "synthetic"), 1)
    clock = [0.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["response_format"] is not None:
            clock[0] += 51.0
            return {"status": 400}
        return {"status": 200, "text": '{"facts": []}', "finish_reason": "stop"}

    monkeypatch.setattr(local_llm, "chat", chat)
    assert semantic_transport.llm_chat("synthetic", timeout=670) is None
    assert semantic_transport.llm_chat("synthetic", timeout=670) == '{"facts": []}'
    assert [c["response_format"] for c in calls] == [{"type": "json_object"}, None]
    assert all(c["max_tokens"] == semantic_transport.LLM_LONG_MAX_TOKENS for c in calls)


def test_constrained_long_rejection_is_a_retryable_summary_failure(monkeypatch, tmp_path):
    import local_llm
    _long_env(monkeypatch, tmp_path, [], True)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic_transport, '_probe_format', lambda *a: "object")
    # All generated synthetic summary prompts are remembered as long.
    monkeypatch.setattr(semantic_transport, '_long_marks', lambda: {"synthetic-key": 1})
    monkeypatch.setattr(semantic_transport, '_long_key', lambda *a: "synthetic-key")
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
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        summary, reason = semantic_llm.summarize(
            lambda prompt: semantic_transport.llm_chat(prompt, timeout=670),
            bundle, 1, [], {}, return_reason=True)
        # a json_object refusal is not an oversize prompt: no terminal stub
        assert summary is None
        assert reason == "model"
        assert len(calls) == 1
    finally:
        db.close()


def test_format_persistence_time_is_charged_to_retry_budget(monkeypatch, tmp_path):
    import local_llm
    _long_env(monkeypatch, tmp_path, [], True)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic_transport, '_probe_format', lambda *a: "object")
    semantic_transport._long_mark(semantic_transport._long_key(semantic_transport.llm_conf()[1], "synthetic"), 1)
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
    monkeypatch.setattr(semantic_transport, '_remember_plain', remember)
    assert semantic_transport.llm_chat("synthetic", timeout=670) is None
    assert len(calls) == 1  # 399s, not the pre-persistence 400s


def test_format_persistence_io_failure_keeps_same_call_fallback(monkeypatch, tmp_path):
    import local_llm
    # An existing regular file cannot become the state directory.
    blocked = tmp_path / "blocked"
    blocked.write_text("synthetic")
    monkeypatch.setattr(semantic_transport, '_FMT_PATH', str(blocked / "format.json"))
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic_transport, '_probe_format', lambda *a: "object")
    monkeypatch.setattr(semantic_transport, '_long_marks', lambda: {})
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["response_format"] is not None:
            return {"status": 422}
        return {"status": 200, "text": "{}", "finish_reason": "stop"}

    monkeypatch.setattr(local_llm, "chat", chat)
    assert semantic_transport.llm_chat("synthetic", timeout=10) == "{}"
    assert len(calls) == 2


def test_format_mark_lock_contention_preserves_previous_state(monkeypatch, tmp_path):
    import fcntl
    from pathlib import Path
    semantic_transport._remember_plain("http://127.0.0.1:1", "synthetic-model")
    original = Path(semantic_transport._FMT_PATH).read_bytes()
    with open(semantic_transport._FMT_PATH + ".lock", "a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        semantic_transport._remember_plain("http://127.0.0.1:2", "synthetic-model")
    assert Path(semantic_transport._FMT_PATH).read_bytes() == original


def test_long_mark_cannot_downgrade_a_completed_long_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(tmp_path / "long.json"))
    semantic_transport._long_mark("synthetic-prompt", 2)
    semantic_transport._long_mark("synthetic-other", 1)
    semantic_transport._long_mark("synthetic-prompt", 1)
    assert semantic_transport._long_marks() == {"synthetic-other": 1, "synthetic-prompt": 2}


def test_long_mark_contention_does_not_overwrite_another_worker(monkeypatch, tmp_path):
    import fcntl
    from pathlib import Path
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(tmp_path / "long.json"))
    semantic_transport._long_mark("synthetic-prompt", 2)
    original = Path(semantic_transport._LONG_PATH).read_bytes()
    with open(semantic_transport._LONG_PATH + ".lock", "a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        semantic_transport._long_mark("synthetic-other", 1)
    assert Path(semantic_transport._LONG_PATH).read_bytes() == original


def test_format_mark_retention_is_bounded(monkeypatch, tmp_path):
    from pathlib import Path
    now = semantic.time.time()
    marks = {f"synthetic-{i}": now for i in range(500)}
    Path(semantic_transport._FMT_PATH).write_text(json.dumps(marks))
    semantic_transport._remember_plain("http://127.0.0.1:1", "synthetic-model")
    kept = semantic_transport._format_marks()
    assert len(kept) == 500
    assert "synthetic-0" not in kept
    assert semantic_transport._format_key("http://127.0.0.1:1", "synthetic-model") in kept


@pytest.mark.parametrize("stage", ["probe", "long_marks"])
def test_format_preflight_cannot_dispatch_after_budget_expires(monkeypatch, stage):
    import local_llm
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    calls = []

    def probe(*args):
        if stage == "probe":
            clock[0] += 4.0
        return "plain"

    def marks():
        if stage == "long_marks":
            clock[0] += 4.0
        return {}

    monkeypatch.setattr(semantic_transport, '_probe_format', probe)
    monkeypatch.setattr(semantic_transport, '_long_marks', marks)
    monkeypatch.setattr(local_llm, "chat", lambda *a, **kw: calls.append(kw) or {
        "status": 200, "text": "{}", "finish_reason": "stop"})
    assert semantic_transport.llm_chat("synthetic expired preflight", timeout=4) is None
    assert calls == []


@pytest.mark.parametrize("status", [400, 404, 422])
@pytest.mark.parametrize("stage", ["response", "persistence"])
def test_format_rejection_cannot_retry_after_budget_expires(monkeypatch, status, stage):
    import local_llm
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic_transport, '_probe_format', lambda *a: "object")
    monkeypatch.setattr(semantic_transport, '_long_marks', lambda: {})
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            clock[0] += 4.0 if stage == "response" else 3.0
            return {"status": status}
        return {"status": 200, "text": "{}", "finish_reason": "stop"}

    def remember(*args):
        if stage == "persistence":
            clock[0] += 1.0

    monkeypatch.setattr(local_llm, "chat", chat)
    monkeypatch.setattr(semantic_transport, '_remember_plain', remember)
    assert semantic_transport.llm_chat("synthetic expired format retry", timeout=4) is None
    assert len(calls) == 1
    assert calls[0]["deadline"] == 1004.0


@pytest.mark.parametrize("limitations", [0, 1, False, True,
                                         {}, {"synthetic": "limitation"}, ["valid", 1]])
def test_summarize_rejects_malformed_limitations(tmp_path, limitations):
    db = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        response = json.dumps({"claims": [], "limitations": limitations})
        assert semantic_llm.summarize(lambda _prompt: response, bundle, 1, [], {},
                                  return_reason=True) == (None, "model")
    finally:
        db.close()


@pytest.mark.parametrize("limitation", ["", "完全合成の補足。\n数量は記載されていません。"])
def test_summarize_preserves_a_single_limitation_string_and_claims(tmp_path, limitation):
    db = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        claim = {"section": "other", "text": "完全合成の確認事項",
                 "claim_kind": "limitation", "fact_refs": [],
                 "status": "not_stated", "polarity": "uncertain"}
        response = json.dumps({"claims": [claim], "limitations": limitation})
        errors = {}
        summary, reason = semantic_llm.summarize(lambda _: response, bundle, 1, [], {},
                                            return_reason=True, error_out=errors)
        assert reason is None and errors == {}
        assert summary["limitations"] == [limitation]
        assert len(summary["claims"]) == 1
        assert all(summary["claims"][0][k] == v for k, v in claim.items())
    finally:
        db.close()


@pytest.mark.parametrize("claim", [None, {}, {"text": ""}, {"text": " "}, {"text": 1}])
def test_summarize_rejects_malformed_claims_without_silent_drop(tmp_path, claim):
    db = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        response = json.dumps({"claims": [claim], "limitations": []})
        assert semantic_llm.summarize(lambda _prompt: response, bundle, 1, [], {},
                                  return_reason=True) == (None, "model")
    finally:
        db.close()


def _admitted_env(monkeypatch, tmp_path, replies):
    """Admission on: the probe and every send go through stubbed permits."""
    import local_llm
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', None)
    monkeypatch.setattr(semantic_transport, '_FMT_TS', 0.0)
    monkeypatch.setattr(semantic_transport, '_LONG_PATH',
                        str(tmp_path / "data" / "long.json"))
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: True)
    probes, calls = [], []

    def probe(route, endpoint, model, schema, **kw):
        probes.append(route)
        return "object"

    def admitted(route, prompt, **kw):
        calls.append(dict(kw, route=route))
        return replies.pop(0)

    def direct(*a, **kw):
        raise AssertionError("admission bypassed")

    monkeypatch.setattr(local_llm, "admitted_probe_format", probe)
    monkeypatch.setattr(local_llm, "probe_format", direct)
    monkeypatch.setattr(local_llm, "admitted_chat", admitted)
    monkeypatch.setattr(local_llm, "chat", direct)
    return probes, calls


def test_admitted_llm_chat_keeps_json_object_and_long_ceiling_retry(
        monkeypatch, tmp_path):
    """F20: with admission enabled the probe and every send take a
    permit, the json_object constraint rides along, and a length stop
    retries once at the long ceiling with a fresh permit."""
    probes, calls = _admitted_env(monkeypatch, tmp_path, [
        {"status": 200, "text": '{"facts": [', "finish_reason": "length"},
        {"status": 200, "text": '{"facts": []}', "finish_reason": "stop"}])
    assert semantic_transport.llm_chat("抽出 JSON:", timeout=900) == '{"facts": []}'
    assert probes == ["mcs.semantic"]
    assert [c["route"] for c in calls] == ["mcs.semantic"] * 2
    assert all(c["response_format"] == {"type": "json_object"} for c in calls)
    assert [c["max_tokens"] for c in calls] == [
        semantic_transport.LLM_MAX_TOKENS, semantic_transport.LLM_LONG_MAX_TOKENS]


def test_admitted_long_ceiling_retry_held_is_not_sent(monkeypatch, tmp_path):
    """A held verdict on the retry's own permit is a free deferral."""
    _, calls = _admitted_env(monkeypatch, tmp_path, [
        {"status": 200, "text": '{"facts": [', "finish_reason": "length"},
        {"text": None, "finish_reason": None, "usage": None,
         "status": None, "admission": "held"}])
    with pytest.raises(semantic_runtime.LLMNotSent,
                       match="llm_admission:held"):
        semantic_transport.llm_chat("抽出 JSON:", timeout=900)
    assert len(calls) == 2


def test_probe_blocked_by_turn_is_not_cached_as_plain(monkeypatch):
    """A probe that never left (GPU turn busy) leaves the format mode
    unprobed so the next call probes again instead of sending plain
    for the whole cooldown."""
    import local_llm
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', None)
    monkeypatch.setattr(semantic_transport, '_FMT_TS', 0.0)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(local_llm, "probe_format", lambda *a, **k: None)
    assert semantic_transport._probe_format("http://127.0.0.1:8080", "synthetic", 4) is None
    assert semantic_transport._FMT_MODE is None and semantic_transport._FMT_TS == 0.0


def _turn_lock_env(monkeypatch, tmp_path, calls):
    import local_llm
    path = str(tmp_path / "turn.lock")
    monkeypatch.setenv(local_llm.TURN_LOCK_ENV, path)
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', "plain")
    monkeypatch.setattr(semantic_transport, '_FMT_TS', time.monotonic())

    def send(endpoint, method, body, timeout, deadline=None):
        calls.append(timeout)
        return 200, {}, json.dumps(_response('{"facts": []}')).encode()
    monkeypatch.setattr(local_llm, "bounded_request", send)
    return path


def test_turn_floor_never_exceeds_job_window(tmp_path, monkeypatch):
    """The turn floor is capped by the live window: an uncontended call
    on a job budget shorter than the floor is sent, not reported busy
    (the real lock path, not a stubbed chat)."""
    calls = []
    _turn_lock_env(monkeypatch, tmp_path, calls)
    assert semantic_transport.llm_chat("synthetic JSON:", timeout=45) == '{"facts": []}'
    assert len(calls) == 1


def test_turn_won_with_scraps_of_window_is_busy(tmp_path, monkeypatch):
    """A turn won with less than half the window left is deferred as
    busy: the lock, not the model, ate the budget, so no attempt is
    charged for a request that would be reaped mid-decode."""
    import fcntl
    import local_llm
    import os
    import semantic_runtime
    calls = []
    path = _turn_lock_env(monkeypatch, tmp_path, calls)
    holder = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    # release 1.4 s after the FIRST lock attempt, not after an arbitrary
    # point before the call: a slow test thread must not shift the hold
    # into the 2 s window and let the request through
    real_flock = fcntl.flock
    timer = []

    def flock(fd, operation):
        if not timer and operation == (fcntl.LOCK_EX | fcntl.LOCK_NB):
            timer.append(threading.Timer(1.4, os.close, [holder]))
            timer[0].start()
        return real_flock(fd, operation)

    monkeypatch.setattr(local_llm.fcntl, "flock", flock)
    try:
        with pytest.raises(semantic_runtime.LLMNotSent) as info:
            semantic_transport.llm_chat("synthetic JSON:", timeout=2)
        assert info.value.stage == "llm_turn_busy"
        assert calls == []
    finally:
        if timer:
            timer[0].join()
        else:
            os.close(holder)
