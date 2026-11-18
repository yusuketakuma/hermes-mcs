"""Synthetic regressions: LINE WORKS parts that provably never reached the room
settle not_sent (or are delivered), never stranded as unknown."""
import asyncio
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from adapters.common import envelopes
from adapters.lineworks import client as module
from adapters.lineworks import delivery
from adapters.lineworks.client import ClientError
from lineworks_client_testkit import TOKEN, client
from lineworks_testkit import _worker, overflow_spec, world

CTX = {"card_message_id": "lw:" + "c" * 32}
STORAGE = "https://apis-storage.worksmobile.com/k/emsg/r/jp1/synthetic/file.bin"
ATTACHMENT = {"part_id": "attachment:1", "kind": "attachment_part", "name": "合成.bin"}


def _published(w):
    spec = overflow_spec()
    # A real worker retries only while the runner's sealed spec remains published.
    Path(w.dirs["render"], spec["delivery_id"] + ".json").write_bytes(envelopes.canonical(spec))
    return spec


def _wire(fail_at):
    """Token, attachment slot, storage upload, file message — raising at ``fail_at``."""
    calls = []

    def wire(method, url, headers, body, timeout):
        calls.append((url, timeout))
        stage = ("token" if url == module.AUTH_URL else "slot" if url.endswith("/attachments")
                 else "storage" if url == STORAGE else "message")
        if stage == fail_at:
            raise OSError("synthetic timeout")
        payload = {"token": TOKEN, "slot": {"uploadUrl": STORAGE, "fileId": "file-synthetic"},
                   "storage": {"fileId": "file-synthetic"}}.get(stage)
        return (201 if stage == "message" else 200), {}, json.dumps(payload or {}).encode()
    wire.calls = calls
    return wire


@pytest.mark.parametrize("fail_at, result", [("storage", "not_sent"), ("slot", "not_sent"),
                                             ("message", "unknown")])
def test_attachment_upload_failure_before_the_file_post_is_not_sent(tmp_path, monkeypatch,
                                                                    fail_at, result):
    w = world(tmp_path)
    wire = _wire(fail_at)
    sender = delivery.Sender(client(wire), w.settings, str(w.data))
    worker = _worker(w)
    worker._sender = sender
    blob = b"s" * (2 * module.UPLOAD_MIN_BYTES_PER_S)
    monkeypatch.setattr(delivery.paths, "read_verified_attachment", lambda *args: blob)
    out = asyncio.run(worker._perform_part({"spec": _published(w)}, ATTACHMENT, CTX))
    # Only a failure once the room-visible file message began may stay unknown.
    assert out == {"result": result, "error_code": "transport_unknown"}
    if fail_at != "message":
        assert not any(url.endswith("/messages") for url, _ in wire.calls)
    if fail_at == "storage":
        # The storage upload gets time for its size, not the fixed 20 s base.
        assert dict(wire.calls)[STORAGE] >= 22


def test_large_upload_timeout_passes_the_real_http_worker(monkeypatch):
    """A 13 MiB body gets a sized timeout above 120 s; the worker must accept it."""
    seen = []

    class Response:
        code = 200
        headers = {}

        def read(self, limit):
            return b"{}"

        def close(self):
            pass

    class Opener:
        def open(self, request, timeout):
            seen.append((request.full_url, len(request.data), timeout))
            return Response()

    def run(argv, *, input, timeout, **kwargs):
        # In-process stand-in for the child: a real one would bypass the stubs.
        assert argv[-1] == "--http-worker"
        output = io.StringIO()
        monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(input)))
        monkeypatch.setattr(module.sys, "stdout", output)
        module._http_worker()
        return SimpleNamespace(returncode=0, stdout=output.getvalue().encode())

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(module.subprocess, "run", run)
    body = b"s" * (13 * 1024 * 1024)
    status, raw = client(module._http_request)._request(STORAGE, body, "application/octet-stream")
    assert (status, raw) == (200, b"{}")
    assert seen == [(STORAGE, len(body), 20 + len(body) // module.UPLOAD_MIN_BYTES_PER_S)]
    assert seen[0][2] > 120


def test_upload_5xx_keeps_its_own_code(tmp_path):
    def wire(method, url, headers, body, timeout):
        if url == module.AUTH_URL:
            return 200, {}, json.dumps(TOKEN).encode()
        return 503, {}, b"{}"
    with pytest.raises(ClientError) as caught:
        client(wire).upload_file(b"synthetic", "synthetic.bin")
    assert delivery.failure(caught.value) == {"result": "not_sent", "error_code": "http_error"}


@pytest.mark.parametrize("token", [{**TOKEN, "expires_in": "10"}, {**TOKEN, "token_type": "MAC"},
                                   {k: v for k, v in TOKEN.items() if k != "access_token"}])
def test_invalid_token_response_is_not_sent(token):
    urls = []

    def wire(method, url, headers, body, timeout):
        urls.append(url)
        return 200, {}, json.dumps(token).encode()
    with pytest.raises(ClientError) as caught:
        client(wire).send_message({"type": "text", "text": "x"}, channel_id="room")
    assert delivery.failure(caught.value) == {"result": "not_sent", "error_code": "response_invalid"}
    assert urls == [module.AUTH_URL]


def test_body_part_outwaits_a_ten_second_cli_lock_hold(tmp_path, monkeypatch):
    w = world(tmp_path)
    spec = _published(w)
    held, release = threading.Event(), threading.Event()

    def cli_hold():
        with delivery.api_lock(str(w.data)):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=cli_hold)
    holder.start()
    assert held.wait(5)
    waited = []

    async def sleep(seconds):
        waited.append(seconds)
        if sum(waited) >= 10:  # the CLI's text + upload hold ends after 10 s
            release.set()
            await asyncio.to_thread(holder.join, 5)

    monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
    try:
        out = asyncio.run(_worker(w)._perform_part({"spec": spec}, spec["parts"]["manifest"][1], CTX))
    finally:
        release.set()
        holder.join(5)
    assert out["result"] == "delivered" and len(w.client.calls) == 1
    assert len(waited) > delivery.SENDER_BUSY_TRIES


def test_part_busy_wait_ends_at_part_busy_limit(monkeypatch):
    clock = [0.0]
    tries = []

    def busy():
        tries.append(clock[0])
        raise ClientError("sender_busy")

    async def sleep(seconds):
        clock[0] += seconds

    async def allowed():
        return True

    monkeypatch.setattr(delivery, "time", SimpleNamespace(time=lambda: clock[0],
                                                          monotonic=lambda: clock[0]))
    monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
    with pytest.raises(ClientError, match="sender_busy"):
        asyncio.run(delivery.locked(busy, wait_cooldown=True, busy_tries=None,
                                    retry_allowed=allowed))
    # A wedged lock holder ends the part's wait at the bound, not before and not never.
    assert delivery.PART_BUSY_LIMIT == 1800
    assert delivery.PART_BUSY_LIMIT <= tries[-1] < delivery.PART_BUSY_LIMIT + 1


def test_stop_during_cooldown_journals_not_sent_promptly(tmp_path):
    w = world(tmp_path)
    spec = _published(w)
    with open(os.path.join(w.dirs["state"], "rate-limit.json"), "w") as stream:
        json.dump({"until": time.time() + 59}, stream)
    worker = _worker(w)
    part = spec["parts"]["manifest"][1]

    async def run():
        attempt = asyncio.create_task(worker._attempt_part(
            {"spec": spec, "payload_hash": "a" * 64}, part, CTX))
        await asyncio.sleep(0.2)
        worker.stop()
        # launchd's 20 s ExitTimeOut: the journal result must land well before it.
        await asyncio.wait_for(attempt, 5)

    asyncio.run(run())
    rows = worker._jview.refresh()[envelopes.part_attempt_id(spec["delivery_id"], part["part_id"])]
    result = next(r for r in rows if r["phase"] == "result")
    assert (result["result"], result["error_code"]) == ("not_sent", "rate_limited")
    assert w.client.calls == []


def test_signal_stops_worker_immediately_and_once_across_finally(tmp_path, monkeypatch):
    from adapters.lineworks import __main__ as cli
    from lineworks_testkit import SECRET

    w = world(tmp_path)
    stops, callbacks = [], {}
    saw_stop_before_finally = []

    async def reconcile():
        return {}

    async def tick():
        callbacks[signal.SIGTERM]()
        saw_stop_before_finally.append(len(stops))

    worker = SimpleNamespace(acquire_scope_lock=lambda: True, reconcile=reconcile, tick=tick,
                             stop=lambda: stops.append("stop"), release_scope_lock=lambda: None)
    http = SimpleNamespace(serve_forever=lambda: None, shutdown=lambda: None, server_close=lambda: None)
    thread = SimpleNamespace(start=lambda: None, join=lambda **_kw: None)
    monkeypatch.setattr(cli, "settings", lambda _root: w.settings)
    monkeypatch.setattr(cli, "load_credentials", lambda *_args: (w.client, SECRET))
    monkeypatch.setattr(cli, "DeliveryWorker", lambda **_kw: worker)
    monkeypatch.setattr(cli, "CallbackInbox", lambda *_args: SimpleNamespace(
        expire=lambda: None, pending=lambda: []))
    monkeypatch.setattr(cli, "Actions", lambda *_args: SimpleNamespace(sweep_followups=reconcile))
    monkeypatch.setattr(cli, "callback_server", lambda *_args, **_kw: http)
    monkeypatch.setattr(cli, "threading", SimpleNamespace(Thread=lambda **_kw: thread))

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: callbacks.setdefault(sig, cb))
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: callbacks.pop(sig, None))
        await cli.serve(tmp_path, 8788)

    asyncio.run(run())
    assert saw_stop_before_finally == [1]
    assert stops == ["stop"]


def test_api_lock_shares_429_cooldown_across_processes(tmp_path):
    w = world(tmp_path)
    repo = Path(__file__).resolve().parents[3]
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + '/mcs'); "
        "import _mcs_path; "
        "from adapters.lineworks import delivery; "
        "from adapters.lineworks.client import ClientError\n"
        "try:\n"
        "    with delivery.api_lock(sys.argv[2]):\n"
        "        raise ClientError('rate_limited', 429)\n"
        "except ClientError:\n"
        "    pass\n"
    )
    child = subprocess.run([sys.executable, "-I", "-c", script, str(repo), str(w.data)],
                           capture_output=True, text=True, timeout=15, check=False)
    assert child.returncode == 0, child.stderr
    cooldown = Path(w.dirs["state"], "rate-limit.json")
    assert cooldown.is_file()
    out = asyncio.run(w.sender.perform(overflow_spec()))
    assert out == {"result": "not_sent", "error_code": "rate_limited"}
    assert w.client.calls == []


def test_api_lock_clamps_backward_clock_and_far_future_cooldown_then_releases(tmp_path, monkeypatch):
    w = world(tmp_path)
    now = [1000.0]
    monkeypatch.setattr(delivery.time, "time", lambda: now[0])
    cooldown = Path(w.dirs["state"], "rate-limit.json")

    with pytest.raises(ClientError, match="rate_limited"):
        with delivery.api_lock(str(w.data)):
            raise ClientError("rate_limited", 429)
    assert json.loads(cooldown.read_text())["until"] == 1060.0

    # A 5 s backward wall-clock step makes until > now + 60; it must clamp to now + 60
    # inside the lock and still refuse this call instead of deleting the active cooldown.
    now[0] = 995.0
    out = asyncio.run(w.sender.perform(overflow_spec()))
    assert out == {"result": "not_sent", "error_code": "rate_limited"}
    assert w.client.calls == []
    assert json.loads(cooldown.read_text())["until"] == 1055.0

    # A far-future deadline from an old clock jump is also clamped to now + 60 and refused once,
    # never blocking forever and never letting the immediate call bypass cooldown.
    cooldown.write_text(json.dumps({"until": now[0] + 365 * 86400}))
    out = asyncio.run(w.sender.perform(overflow_spec()))
    assert out == {"result": "not_sent", "error_code": "rate_limited"}
    assert w.client.calls == []
    assert json.loads(cooldown.read_text())["until"] == 1055.0

    # Once the clamped official window expires, the lock clears the file and admits sends.
    now[0] = 1055.0
    out = asyncio.run(w.sender.perform(overflow_spec()))
    assert out["result"] == "delivered" and len(w.client.calls) == 1
    assert not cooldown.exists()


def test_card_attachment_part_24mib_budget_passes_http_worker(tmp_path, monkeypatch):
    """Worker card parts bypass notify_sender's 180 s CLI cap and get 20 + 192 = 212 s."""
    w = world(tmp_path)
    seen = []

    class Response:
        def __init__(self, code, payload):
            self.code = code
            self.headers = {}
            self._raw = json.dumps(payload).encode()

        def read(self, limit):
            return self._raw[:limit]

        def close(self):
            pass

    class Opener:
        def open(self, request, timeout):
            url = request.full_url
            seen.append((url, len(request.data), timeout))
            if url == module.AUTH_URL:
                return Response(200, TOKEN)
            if url.endswith("/attachments"):
                return Response(200, {"uploadUrl": STORAGE, "fileId": "file-24m"})
            if url == STORAGE:
                return Response(200, {"fileId": "file-24m"})
            return Response(201, {})

    def run(argv, *, input, timeout, **kwargs):
        assert argv[-1] == "--http-worker"
        output = io.StringIO()
        monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(input)))
        monkeypatch.setattr(module.sys, "stdout", output)
        module._http_worker()
        return SimpleNamespace(returncode=0, stdout=output.getvalue().encode())

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(module.subprocess, "run", run)
    blob = b"s" * (24 * 1024 * 1024)
    monkeypatch.setattr(delivery.paths, "read_verified_attachment", lambda *args: blob)
    worker = _worker(w)
    worker._sender = delivery.Sender(client(module._http_request), w.settings, str(w.data))
    out = asyncio.run(worker._perform_part({"spec": _published(w)}, ATTACHMENT, CTX))
    assert out == {"result": "delivered", "remote_id": "file-24m"}
    storage_calls = [(size, timeout) for url, size, timeout in seen if url == STORAGE]
    assert len(storage_calls) == 1
    size, timeout = storage_calls[0]
    assert 24 * 1024 * 1024 < size <= module.MAX_REQUEST_BYTES
    assert timeout == 20 + size // module.UPLOAD_MIN_BYTES_PER_S == 212
    assert 180 < timeout <= module.REQUEST_TIMEOUT_MAX


def test_text_cli_obeys_outer_send_budget_and_never_resends_partial_timeout(tmp_path, monkeypatch):
    """Text CLI sends obey notify_sender.send_budget (<=180 s or caller deadline);
    a timeout after the text post can be partial, so it holds unknown without resending."""
    import ledger as ledger_mod
    import notify_flush
    import notify_sender

    cfg = {
        "notify_target": "lineworks:room-synthetic",
        "notify": {"lineworks": {"channel_id": "room-synthetic"}},
    }
    clock = [100.0]
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(notify_flush, "CONF_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(notify_flush.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(notify_sender.time, "monotonic", lambda: clock[0])

    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    att_file = att_dir / "synthetic.bin"
    payload_bytes = b"x" * 1024
    att_file.write_bytes(payload_bytes)
    digest = hashlib.sha256(payload_bytes).hexdigest()

    led = ledger_mod.Ledger(str(tmp_path / "mcs.db"))
    try:
        led.db.execute(
            "INSERT INTO patients(project_id, patient_name) VALUES (1, '合成患者')")
        led.db.execute(
            "INSERT INTO messages(message_id, project_id, sender_name, posted_at, body_text, body_state) "
            "VALUES (10, 1, '合成送信者', '2026-11-18T10:00:00+09:00', '合成本文', 'present')")
        led.db.execute(
            "INSERT INTO attachments(attachment_id, message_id, file_id, name, state, local_path, bytes, sha256) "
            "VALUES (1, 10, 'f1', 'synthetic.bin', 'downloaded', ?, ?, ?)",
            (str(att_file), len(payload_bytes), digest))
        led.db.commit()
        eid = led.outbox_add("new_messages", 1, {"message_ids": [10]})

        calls = []
        mode = ["tempfail"]

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs["timeout"]))
            if mode[0] == "tempfail":
                return SimpleNamespace(returncode=75, stdout="", stderr="rate_limited\n")
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(notify_sender.subprocess, "run", fake_run)

        # Insufficient caller deadline (<30 s) defers without starting CLI or marking sending.
        res_low = notify_flush.flush(led, deadline=129.0)
        assert res_low["send_budget_insufficient"] == 1 and calls == []

        # Caller deadline of 60 s passes timeout=60; pre-send EX_TEMPFAIL refunds in-flight.
        res_pre = notify_flush.flush(led, deadline=160.0)
        assert res_pre["failed"] == 1 and calls[-1][1] == 60.0
        row = led.db.execute("SELECT state, progress FROM notify_outbox WHERE event_id=?", (eid,)).fetchone()
        assert row["state"] == "failed" and json.loads(row["progress"]).get("sending") is None
        led.db.execute("UPDATE notify_outbox SET next_try=0 WHERE event_id=?", (eid,))
        led.db.commit()

        # Ample caller deadline (500 s remaining) still caps CLI timeout at 180 s (<192 s for 24 MiB@128 KiB/s);
        # once CLI times out, outcome is unknown and held without automatic resend.
        mode[0] = "timeout"
        res_timeout = notify_flush.flush(led, deadline=600.0)
        assert res_timeout["uncertain"] == 1 and calls[-1][1] == 180.0
        held = led.db.execute(
            "SELECT state, next_try, progress FROM notify_outbox WHERE event_id=?", (eid,)).fetchone()
        assert held["state"] == "failed" and held["next_try"] is None
        assert json.loads(held["progress"])["hold_reason"] == "send_outcome_unknown"

        count_before = len(calls)
        notify_flush.flush(led, deadline=600.0)
        assert len(calls) == count_before
    finally:
        led.close()
