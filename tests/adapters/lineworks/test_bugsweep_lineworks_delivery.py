"""Synthetic regressions: LINE WORKS parts that provably never reached the room
settle not_sent (or are delivered), never stranded as unknown."""
import asyncio
import io
import json
import os
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


def test_cooldown_deadline_far_beyond_the_official_reset_is_discarded(tmp_path):
    w = world(tmp_path)
    cooldown = Path(w.dirs["state"], "rate-limit.json")
    cooldown.write_text(json.dumps({"until": time.time() + 365 * 86400}))
    out = asyncio.run(w.sender.perform(overflow_spec()))
    assert out["result"] == "delivered" and len(w.client.calls) == 1
    assert not cooldown.exists()
