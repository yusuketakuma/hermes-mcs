"""T20 — one cross-client RT/BACKLOG admission boundary.

All synthetic: temp SQLite broker store + a fake backend that enforces
the admission token (the "backend access control unavailable to
callers"). No network, no real llama, no patient data. The invariant
under test: O_BACKLOG * O_RT = 0 at every observable instant,
counting pre-send reservations through confirmed terminal retirement.
"""
import json
import sqlite3
from contextlib import suppress
import threading
import time

import pytest

import llm_admission as adm


@pytest.fixture
def broker(tmp_path):
    b = adm.Broker(str(tmp_path / "adm.db"))
    b.open_epoch(lambda: True)
    yield b
    b.close()


class FakeBackend:
    """The shared model backend. Accepts a request only with a live
    'sent' permit token of the current epoch for the claimed class —
    direct POSTs, stale epochs, cross-class tokens all rejected.
    Tracks which classes had a request in flight."""
    def __init__(self, broker):
        self.broker = broker
        self.log = []          # (cls, request_id) accepted sends
        self.active = set()    # cls currently decoding

    def post(self, token, cls, request_id):
        v = self.broker.token_valid(token, cls, request_id)
        if not v["ok"]:
            self.log.append(("REJECTED", v["reason"]))
            return {"status": 403, "reason": v["reason"]}
        self.active.add(cls)
        self.log.append((cls, request_id))
        return {"status": 200, "id": f"cmpl-{v['permit_id']}"}

    def finish(self, cls):
        """The backend's terminal retire for one request."""
        self.active.discard(cls)


def _send(backend, broker, client, cls):
    """Helper: acquire → sent → backend POST. Returns the pieces."""
    acq = broker.acquire(client, cls)
    if not acq.get("admitted"):
        return {"acq": acq}
    sent = broker.sent(acq["permit_id"])
    if not sent.get("sent"):
        return {"acq": acq, "sent": sent}
    resp = backend.post(sent["token"], cls, f"req-{acq['permit_id']}")
    return {"acq": acq, "sent": sent, "resp": resp,
            "permit_id": acq["permit_id"]}


def test_rt_waits_for_backlog_terminal(broker):
    backend = FakeBackend(broker)
    bg = _send(backend, broker, "mcs.extract", "BACKLOG")
    assert bg["resp"]["status"] == 200
    assert not broker.overlap()
    # RT arrives mid-decode: registered waiting, never sent
    rt = broker.acquire("hermes.interactive", "RT")
    assert rt["admitted"] is False and rt["reason"] == "waiting"
    sent = broker.sent(rt["permit_id"])
    assert sent["sent"] is False
    assert not broker.overlap()
    # new BACKLOG is closed while RT waits
    bg2 = broker.acquire("mcs.semantic", "BACKLOG")
    assert bg2["admitted"] is False and bg2["reason"] == "rt_pending"
    # backlog terminal → waiting RT promotes, sends, and holds RT alone
    broker.terminal(bg["permit_id"], "done")
    backend.finish("BACKLOG")
    polled = broker.poll(rt["permit_id"])
    assert polled["state"] == "admitted"
    sent = broker.sent(rt["permit_id"])
    assert sent["sent"] is True
    resp = backend.post(sent["token"], "RT", "req-rt")
    assert resp["status"] == 200
    assert not broker.overlap()
    broker.terminal(rt["permit_id"], "done")


def test_unknown_completion_never_frees_class(broker):
    backend = FakeBackend(broker)
    bg = _send(backend, broker, "mcs.extract", "BACKLOG")
    assert bg["resp"]["status"] == 200
    # transport timeout: response lost — backend may still decode
    broker.mark_unknown(bg["permit_id"], "timeout")
    rt = broker.acquire("gbrain.query", "RT")
    assert rt["admitted"] is False       # unknown still occupies
    assert rt["reason"] == "waiting"
    assert broker.overlap() is False
    # timeout/lease/idle-sample is NOT retirement — only confirmed
    # terminal evidence is
    broker.terminal(bg["permit_id"], "confirmed_retired",
                    proof="backend-ack")
    assert broker.poll(rt["permit_id"])["state"] == "admitted"


def test_cancel_is_not_an_ack(broker):
    backend = FakeBackend(broker)
    bg = _send(backend, broker, "mcs.extract", "BACKLOG")
    assert bg["resp"]["status"] == 200
    broker.cancel(bg["permit_id"])
    # cancel verb alone does not retire — class still occupied
    rt = broker.acquire("hermes.interactive", "RT")
    assert rt["admitted"] is False
    st = broker.db.execute(
        "SELECT state FROM permits WHERE permit_id=?",
        (bg["permit_id"],)).fetchone()[0]
    assert st == "cancel_pending"
    broker.terminal(bg["permit_id"], "terminated",
                    proof="backend-empty")
    assert broker.poll(rt["permit_id"])["state"] == "admitted"


def test_direct_backend_post_rejected(broker):
    backend = FakeBackend(broker)
    # raw POST with no token, a guessed token, a stale-epoch token
    assert backend.post(None, "BACKLOG", "x")["status"] == 403
    assert backend.post("1:1:fake", "BACKLOG", "x")["status"] == 403
    acq = broker.acquire("mcs.extract", "BACKLOG")
    sent = broker.sent(acq["permit_id"])
    # cross-class replay of a live token fails too
    assert backend.post(sent["token"], "RT", "x")["status"] == 403
    assert not broker.overlap()


def test_class_is_bound_to_route_not_payload(broker):
    # a BACKLOG-registered client cannot claim RT by asking for it
    denied = broker.acquire("mcs.extract", "RT")
    assert denied["admitted"] is False
    assert denied["reason"] == "class_not_bound"
    # an unknown client is rejected outright
    denied = broker.acquire("rogue.crawler", "RT")
    assert denied["reason"] == "unknown_client"
    # the bound class always wins — asking for BACKLOG on the RT route
    # still yields RT... no: the route's bound class IS the verdict
    ok = broker.acquire("hermes.interactive", "RT")
    assert ok["admitted"] is True


def test_crash_rotates_epoch_and_fences(tmp_path):
    path = str(tmp_path / "adm.db")
    b1 = adm.Broker(path)
    b1.open_epoch(lambda: True)
    backend = FakeBackend(b1)
    bg = _send(backend, b1, "mcs.extract", "BACKLOG")
    assert bg["resp"]["status"] == 200
    ep1 = b1.epoch()
    # the owner exits without retiring the permit; the OS releases its
    # lifetime lock while the permit stays live on disk
    b1.close()
    b2 = adm.Broker(path)          # restart → fenced + closed epoch
    backend.broker = b2            # the backend validates against the
                                 # live broker, never a dead handle
    assert b2.epoch() == ep1 + 1
    assert not b2.is_open()
    # stale-epoch token is rejected everywhere
    stale = b2.sent(bg["permit_id"])
    assert stale["sent"] is False and stale["reason"] == "stale_epoch"
    assert backend.post(bg["sent"]["token"], "BACKLOG",
                        "replay")["status"] == 403
    # epoch reopens only on verified-empty evidence
    assert not b2.open_epoch(lambda: False)
    assert b2.open_epoch(lambda: True)
    # new epoch admits cleanly
    bg2 = _send(backend, b2, "mcs.extract", "BACKLOG")
    assert bg2["resp"]["status"] == 200
    b2.close()


def test_new_client_joins_live_epoch_without_fencing(tmp_path):
    path = str(tmp_path / "adm.db")
    b1 = adm.Broker(path)
    b1.open_epoch(lambda: True)
    bg = b1.acquire("mcs.extract", "BACKLOG")
    assert bg["admitted"]
    try:
        b2 = adm.Broker(path)
        try:
            assert b2.epoch() == b1.epoch()
            assert b2.is_open()
            rt = b2.acquire("gbrain.query", "RT")
            assert rt["reason"] == "waiting"
            assert not b2.overlap()
        finally:
            b2.close()
    finally:
        b1.close()

    # Once every handle exits, the next owner must fence unfinished
    # requests before it can admit new work.
    restarted = adm.Broker(path)
    try:
        assert restarted.epoch() == bg["epoch"] + 1
        assert not restarted.is_open()
    finally:
        restarted.close()


def test_admitted_at_backfilled_on_old_schema(tmp_path):
    """A permit store from before the admitted_at column migrates in
    place: occupancy timestamps are backfilled for every row that ever
    left 'waiting', and still-waiting rows stay NULL (they hold no
    backend slot)."""
    path = str(tmp_path / "adm.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE admission_meta(singleton INTEGER PRIMARY KEY"
        " CHECK (singleton=1), epoch INTEGER NOT NULL, state TEXT"
        " NOT NULL, rt_waiting INTEGER NOT NULL DEFAULT 0,"
        " rotated_at REAL)")
    con.execute(
        "INSERT INTO admission_meta VALUES(1,1,'open',0,1.0)")
    con.execute(
        "CREATE TABLE permits(permit_id INTEGER PRIMARY KEY"
        " AUTOINCREMENT, epoch INTEGER NOT NULL, client TEXT NOT"
        " NULL, cls TEXT NOT NULL, job_gen TEXT, request_id TEXT,"
        " state TEXT NOT NULL, token TEXT, created_at REAL NOT"
        " NULL, sent_at REAL, terminal_at REAL, outcome TEXT,"
        " proof TEXT)")
    con.execute(
        "INSERT INTO permits(epoch,client,cls,state,created_at,"
        "terminal_at) VALUES(1,'mcs.extract','BACKLOG','terminal',"
        "1.0,2.0)")
    con.execute(
        "INSERT INTO permits(epoch,client,cls,state,created_at)"
        " VALUES(1,'hermes.interactive','RT','waiting',1.5)")
    con.commit()
    con.close()
    b = adm.Broker(path)
    try:
        cols = {r[1] for r in b.db.execute(
            "PRAGMA table_info(permits)")}
        assert "admitted_at" in cols
        rows = b.db.execute(
            "SELECT state,admitted_at FROM permits ORDER BY"
            " permit_id").fetchall()
        assert rows[0]["admitted_at"] == 1.0   # occupied -> backfill
        assert rows[1]["admitted_at"] is None  # waiting: no slot
    finally:
        b.close()


def test_deferred_rt_and_continuous_stream_expose_backlog_age(broker):
    backend = FakeBackend(broker)
    rt1 = _send(backend, broker, "hermes.interactive", "RT")
    assert rt1["resp"]["status"] == 200
    # BACKLOG held while RT occupies
    bg = broker.acquire("mcs.extract", "BACKLOG")
    assert bg["admitted"] is False and bg["reason"] == "rt_pending"
    broker.terminal(rt1["permit_id"], "done")
    # RT terminal → backlog resumes
    bg2 = _send(backend, broker, "mcs.semantic", "BACKLOG")
    assert bg2["resp"]["status"] == 200
    st = broker.status()
    assert st["overlap"] is False
    assert st["occupying"]["BACKLOG"] == 1
    # status exposes the oldest backlog age (capacity signal)
    assert st["oldest_backlog_age_s"] is not None
    assert st["oldest_backlog_age_s"] >= 0


def test_concurrent_arrival_trace_never_overlaps(broker):
    """A shuffled sequence of acquires from every registered route —
    after each step the invariant must hold."""
    backend = FakeBackend(broker)
    trace = []
    # interleave: bg admitted+sent, rt arrives, more bg held
    bg = _send(backend, broker, "mcs.extract", "BACKLOG")
    trace.append(broker.overlap())
    rt = broker.acquire("gbrain.query", "RT")
    trace.append(broker.overlap())
    held = broker.acquire("hermes.cron", "BACKLOG")
    assert held["admitted"] is False
    trace.append(broker.overlap())
    broker.terminal(bg["permit_id"], "done")
    broker.poll(rt["permit_id"])
    rt_sent = broker.sent(rt["permit_id"])
    trace.append(broker.overlap())
    resp = backend.post(rt_sent["token"], "RT", "rt-1")
    assert resp["status"] == 200
    trace.append(broker.overlap())
    broker.terminal(rt["permit_id"], "done")
    bg3 = _send(backend, broker, "mcs.qc", "BACKLOG")
    assert bg3["resp"]["status"] == 200
    trace.append(broker.overlap())
    assert not any(trace)
    # and the backend log shows strict alternation, never both
    kinds = [c for c, _ in backend.log if c != "REJECTED"]
    assert kinds == ["BACKLOG", "RT", "BACKLOG"]


def test_simultaneous_clients_never_reserve_opposite_classes(tmp_path):
    """Two independent broker connections must decide admission atomically."""
    path = str(tmp_path / "adm.db")
    ready = threading.Event()
    rt_checked = threading.Event()
    rt_done = threading.Event()
    results = {}

    def backlog_client():
        b = adm.Broker(path)
        try:
            b.open_epoch(lambda: True)
            assert ready.wait(2)
            original = b._occupancy

            def pause_after_rt_check(conn, epoch, cls):
                count = original(conn, epoch, cls)
                if cls == "RT" and count == 0:
                    rt_checked.set()
                    rt_done.wait(0.5)
                return count

            b._occupancy = pause_after_rt_check
            results["bg"] = b.acquire("mcs.extract", "BACKLOG")
        finally:
            b.close()

    def rt_client():
        b = adm.Broker(path)
        try:
            ready.set()
            assert rt_checked.wait(2)
            results["rt"] = b.acquire("hermes.interactive", "RT")
            rt_done.set()
        finally:
            b.close()

    bg = threading.Thread(target=backlog_client)
    rt = threading.Thread(target=rt_client)
    rt.start()
    bg.start()
    bg.join(3)
    rt.join(3)
    assert not bg.is_alive() and not rt.is_alive()
    assert results["bg"]["admitted"]
    assert results["rt"]["admitted"] is False
    assert results["rt"]["reason"] == "waiting"


# ---------- local_llm admitted_chat / admitted_probe_format ----------

def _open_broker_at(path):
    b = adm.Broker(str(path))
    b.open_epoch(lambda: True)
    b.close()
    return str(path)


@pytest.fixture
def admitted_env(tmp_path, monkeypatch):
    import local_llm
    path = _open_broker_at(tmp_path / "adm.db")
    monkeypatch.setenv("MCS_LLM_ADMISSION", path)
    local_llm._BROKERS.clear()
    yield path
    for b in local_llm._BROKERS.values():
        with suppress(Exception):
            b.close()
    local_llm._BROKERS.clear()


def _live_broker(path):
    import local_llm
    return local_llm._broker(path)


def test_admitted_chat_sends_token_and_terminal(admitted_env):
    import local_llm
    captured = []

    def fake(endpoint, method, body, timeout, deadline):
        captured.append(body)
        return 200, {}, json.dumps({
            "choices": [{"message": {"content": "ok"},
                         "finish_reason": "stop"}]}).encode()

    resp = local_llm.admitted_chat(
        "mcs.semantic", "synthetic",
        endpoint=local_llm.ENDPOINT, request_fn=fake)
    assert resp["status"] == 200 and resp["text"] == "ok"
    token = captured[0].get("admission_token")
    assert token and token.startswith("1:")
    b = _live_broker(admitted_env)
    row = b.db.execute(
        "SELECT state,outcome FROM permits").fetchone()
    assert row["state"] == "terminal" and row["outcome"] == "done"


def test_admitted_chat_unknown_client_fails_closed(admitted_env):
    import local_llm
    calls = []

    def fake(*a):
        calls.append(a)
        return 200, {}, b"{}"

    resp = local_llm.admitted_chat(
        "rogue.route", "p", endpoint=local_llm.ENDPOINT,
        request_fn=fake)
    assert resp["admission"] == "unknown_client"
    assert resp["text"] is None and resp["status"] is None
    assert calls == []          # the request never fired


def test_admitted_chat_held_when_rt_occupies(admitted_env):
    import local_llm
    b = _live_broker(admitted_env)
    rt = b.acquire("hermes.interactive", "RT")
    assert rt["admitted"]
    calls = []

    def fake(*a):
        calls.append(a)
        return 200, {}, b"{}"

    resp = local_llm.admitted_chat(
        "mcs.extract", "p", endpoint=local_llm.ENDPOINT,
        request_fn=fake)
    assert resp["admission"] == "rt_pending"
    assert calls == []
    b.terminal(rt["permit_id"], "done")


def test_admitted_chat_unreachable_is_not_sent(admitted_env):
    import local_llm

    def refused(*a):
        raise ConnectionRefusedError()

    err = {}
    resp = local_llm.admitted_chat(
        "mcs.semantic", "p", endpoint=local_llm.ENDPOINT,
        request_fn=refused, error_out=err)
    assert resp is None
    assert err["kind"] == "unreachable"
    b = _live_broker(admitted_env)
    row = b.db.execute(
        "SELECT state,outcome FROM permits").fetchone()
    # connection refused is PROVABLE non-send — terminal, class freed
    assert row["state"] == "terminal" and row["outcome"] == "not_sent"


def test_admitted_chat_transport_loss_stays_unknown(admitted_env):
    import local_llm

    def dropped(*a):
        raise TimeoutError("synthetic")

    resp = local_llm.admitted_chat(
        "mcs.extract", "p", endpoint=local_llm.ENDPOINT,
        request_fn=dropped)
    assert resp is None
    b = _live_broker(admitted_env)
    row = b.db.execute(
        "SELECT state FROM permits").fetchone()
    assert row["state"] == "unknown"   # still occupies its class
    # the backend may still be decoding — RT stays locked out
    rt = b.acquire("gbrain.query", "RT")
    assert rt["admitted"] is False


def test_admitted_probe_format_injects_token(admitted_env):
    import local_llm
    tokens = []

    def fake(endpoint, method, body, timeout, deadline):
        tokens.append(body.get("admission_token"))
        return 200, {}, json.dumps({
            "choices": [{"message": {"content": '{"ok": true}'},
                         "finish_reason": "stop"}]}).encode()

    mode = local_llm.admitted_probe_format(
        "mcs.extract", local_llm.ENDPOINT, "m", None,
        request_fn=fake)
    assert mode == "object"
    assert tokens and all(t and t.startswith("1:") for t in tokens)
    b = _live_broker(admitted_env)
    row = b.db.execute(
        "SELECT state FROM permits").fetchone()
    assert row["state"] == "terminal"


@pytest.mark.parametrize("failure,state", [(TimeoutError, "unknown"),
                                         (ConnectionRefusedError, "terminal")])
def test_admitted_probe_failure_preserves_backend_uncertainty(admitted_env, failure, state):
    import local_llm

    def unavailable(*args):
        raise failure("synthetic")

    assert local_llm.admitted_probe_format(
        "mcs.extract", local_llm.ENDPOINT, "m", None,
        request_fn=unavailable) == "plain"
    broker = _live_broker(admitted_env)
    row = broker.db.execute("SELECT state,outcome FROM permits").fetchone()
    assert row["state"] == state
    assert row["outcome"] != "done"
    if state == "unknown":
        assert not broker.acquire("gbrain.query", "RT")["admitted"]


def test_admitted_chat_interruption_keeps_permit_unknown(admitted_env):
    import local_llm

    def interrupted(*args):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        local_llm.admitted_chat("mcs.extract", "p", request_fn=interrupted)
    broker = _live_broker(admitted_env)
    assert broker.db.execute("SELECT state FROM permits").fetchone()[0] == "unknown"



def test_admitted_chat_rt_deferral_releases_waiting_flag(admitted_env):
    """An RT intent the caller does not wait out is retired — the
    rt_waiting flag must not keep BACKLOG shut for the whole epoch."""
    import local_llm
    b = _live_broker(admitted_env)
    bg = b.acquire("mcs.extract", "BACKLOG")
    assert bg["admitted"] and b.sent(bg["permit_id"])["sent"]
    resp = local_llm.admitted_chat("gbrain.query", "p", wait_s=0)
    assert resp["admission"] == "waiting" and resp["text"] is None
    b.terminal(bg["permit_id"], "done")
    assert b.status()["rt_waiting"] == 0
    assert b.acquire("mcs.extract", "BACKLOG")["admitted"]


def test_admitted_chat_rt_wait_timeout_releases_waiting_flag(admitted_env):
    import local_llm
    b = _live_broker(admitted_env)
    bg = b.acquire("mcs.extract", "BACKLOG")
    assert bg["admitted"] and b.sent(bg["permit_id"])["sent"]
    resp = local_llm.admitted_chat("gbrain.query", "p", wait_s=0.1)
    assert resp["admission"] == "wait_waiting"
    b.terminal(bg["permit_id"], "done")
    assert b.status()["rt_waiting"] == 0
    assert b.acquire("mcs.extract", "BACKLOG")["admitted"]


def test_mark_unknown_on_waiting_rt_releases_waiting_flag(tmp_path):
    b = adm.Broker(str(tmp_path / "adm.db"))
    try:
        assert b.open_epoch(lambda: True)
        bg = b.acquire("mcs.extract", "BACKLOG")
        assert b.sent(bg["permit_id"])["sent"]
        rt = b.acquire("gbrain.query", "RT")
        assert rt["reason"] == "waiting" and b.status()["rt_waiting"] == 1
        b.mark_unknown(rt["permit_id"], "transport")
        b.terminal(rt["permit_id"], "done")
        assert b.status()["rt_waiting"] == 0
    finally:
        b.close()


def test_admitted_chat_invalid_args_hold_no_permit(admitted_env):
    """A request rejected before send never becomes an ``unknown``
    permit occupying a slot."""
    import local_llm
    with pytest.raises(ValueError, match="local_endpoint_not_allowed"):
        local_llm.admitted_chat("mcs.extract", "p",
                                endpoint="https://example.invalid/v1")
    with pytest.raises(ValueError, match="prompt_invalid"):
        local_llm.admitted_chat("mcs.extract", "")
    b = _live_broker(admitted_env)
    assert b.db.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 0

# ---------- extract_llm wiring under admission ----------

def test_llm_call_defers_on_admission_verdict(admitted_env, monkeypatch):
    import extract_llm
    b = _live_broker(admitted_env)
    rt = b.acquire("gbrain.query", "RT")   # hold the RT class
    assert rt["admitted"]
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "plain")
    monkeypatch.setattr(extract_llm, "_FMT_TS", time.monotonic())
    d = extract_llm._llm_call("synthetic prompt")
    assert d is extract_llm._DEFERRED    # parked, never a silent miss
    b.terminal(rt["permit_id"], "done")


def test_llm_call_carries_token_when_admitted(admitted_env, monkeypatch):
    import extract_llm
    captured = []

    def fake(endpoint, method, body, timeout, deadline):
        captured.append(body)
        return 200, {}, json.dumps({
            "choices": [{"message": {"content": '{"meds": []}'},
                         "finish_reason": "stop"}]}).encode()

    monkeypatch.setattr(extract_llm, "_FMT_MODE", "plain")
    monkeypatch.setattr(extract_llm, "_FMT_TS", time.monotonic())
    monkeypatch.setattr(extract_llm, "_opener_request", fake)
    out = extract_llm._llm_call("synthetic prompt")
    assert out == {"meds": []}
    assert captured[0].get("admission_token")


def test_choose_slot_never_borrows_rt_under_admission(
        admitted_env, monkeypatch):
    import extract_llm
    import local_llm
    # --lend-rt armed, RT slot idle — under the boundary there is no
    # borrow; the class permit IS the authorization
    extract_llm._LEND_RT = True
    try:
        assert extract_llm._choose_slot() == local_llm.BACKGROUND_SLOT
    finally:
        extract_llm._LEND_RT = False


def test_same_class_concurrency_capped_at_slots(tmp_path):
    """T19: backlog concurrency is bounded by the measured slot count —
    excess work defers with class_full, never silently overflows the
    backend's parallel width."""
    b = adm.Broker(str(tmp_path / "adm.db"), slots=2)
    b.open_epoch(lambda: True)
    p1 = b.acquire("mcs.extract", "BACKLOG")
    p2 = b.acquire("mcs.semantic", "BACKLOG")
    p3 = b.acquire("mcs.qc", "BACKLOG")
    assert p1["admitted"] and p2["admitted"]
    assert p3["admitted"] is False and p3["reason"] == "class_full"
    assert not b.overlap()
    b.terminal(p1["permit_id"], "done")
    p4 = b.acquire("mcs.qc", "BACKLOG")   # freed capacity admits
    assert p4["admitted"] is True
    b.close()


def test_waiting_rt_promotion_respects_slot_limit(tmp_path):
    b = adm.Broker(str(tmp_path / "adm.db"), slots=1)
    try:
        b.open_epoch(lambda: True)
        bg = b.acquire("mcs.extract", "BACKLOG")
        rt1 = b.acquire("hermes.interactive", "RT")
        rt2 = b.acquire("gbrain.query", "RT")
        assert rt1["reason"] == rt2["reason"] == "waiting"
        b.terminal(bg["permit_id"], "done")
        assert b.poll(rt1["permit_id"])["state"] == "admitted"
        assert b.poll(rt2["permit_id"])["state"] == "waiting"
        b.terminal(rt1["permit_id"], "done")
        assert b.poll(rt2["permit_id"])["state"] == "admitted"
    finally:
        b.close()


def test_setup_gate_blocks_broker_missing_mcs_routes(
        tmp_path, monkeypatch):
    """Blocked startup: with admission on and a route table lacking an
    MCS caller, check_environment reports an ERROR — the calls would
    otherwise fail closed forever with no surface."""
    import local_llm
    import mcs_setup
    path = str(tmp_path / "adm.db")
    b = adm.Broker(path)
    b.open_epoch(lambda: True)
    b.close()
    # the route registry is code-level — simulate a broker whose table
    # lost an MCS route (edited registry, wrong override)
    monkeypatch.setattr(
        adm, "DEFAULT_ROUTES",
        {k: v for k, v in adm.DEFAULT_ROUTES.items()
         if k != "mcs.extract"})
    monkeypatch.setenv("MCS_LLM_ADMISSION", path)
    local_llm._BROKERS.clear()

    class _Done:
        returncode = 1
        stdout = stderr = ""
    # never let the environment probe touch real keychain/hermes —
    # only the admission audit is under test
    monkeypatch.setattr(mcs_setup.subprocess, "run",
                        lambda *a, **k: _Done())
    try:
        errors, _warnings = mcs_setup.check_environment({})
    finally:
        live = local_llm._BROKERS.pop(path, None)
        if live is not None:
            live.close()
    assert any("mcs.extract" in e for e in errors)
