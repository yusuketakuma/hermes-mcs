"""Synthetic daemon shutdown, callback expiry and durable claim regressions."""
import asyncio
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.lineworks import __main__ as cli
from adapters.lineworks import server
from adapters.lineworks.client import ClientError
from test_lineworks_adapter import NOW, SECRET, event, raw, signature, world


def run_once(monkeypatch, tmp_path, w, box, *, invalid_scope=False):
    observed = []
    callbacks = {}
    calls = 0

    def settings(_root):
        nonlocal calls
        calls += 1
        if invalid_scope and calls > 1:
            raise ClientError("lineworks_configuration_invalid")
        return w.settings

    async def reconcile():
        pass

    async def tick():
        observed.append("tick")
        callbacks[signal.SIGTERM]()

    async def handle(value):
        observed.append(value)

    worker = SimpleNamespace(acquire_scope_lock=lambda: True, reconcile=reconcile, tick=tick,
                             stop=lambda: observed.append("worker_stopped"),
                             release_scope_lock=lambda: observed.append("lock_released"))
    http = SimpleNamespace(serve_forever=lambda: None,
                           shutdown=lambda: observed.append("server_stopped"),
                           server_close=lambda: observed.append("server_closed"))
    thread = SimpleNamespace(start=lambda: None, join=lambda **kw: None)
    monkeypatch.setattr(cli, "settings", settings)
    monkeypatch.setattr(cli, "load_credentials", lambda *_: (w.client, SECRET))
    monkeypatch.setattr(cli, "DeliveryWorker", lambda **kw: worker)
    monkeypatch.setattr(cli, "CallbackInbox", lambda *_: box)
    monkeypatch.setattr(cli, "Actions", lambda *_: SimpleNamespace(
        handle=handle, sweep_followups=reconcile))
    monkeypatch.setattr(cli, "callback_server", lambda *a, **kw: http)
    monkeypatch.setattr(cli, "threading", SimpleNamespace(Thread=lambda **kw: thread))

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: callbacks.__setitem__(sig, cb))
        monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)
        await asyncio.wait_for(cli.serve(tmp_path, 8788), timeout=1)

    asyncio.run(run())
    assert observed[-4:] == ["worker_stopped", "server_stopped", "server_closed", "lock_released"]
    assert w.client.calls == []
    return observed


def queued_callback(tmp_path):
    w = world(tmp_path)
    box = server.CallbackInbox(Path(w.dirs["state"]) / "callbacks", w.settings,
                               SECRET, clock=lambda: NOW)
    body = raw(event())
    assert box.accept(body, signature(body), w.settings["application_id"]) == 200
    return w, box, box.pending()[0]


@pytest.mark.parametrize("fsync_fails", [False, True])
def test_callback_processing_requires_a_durable_claim(monkeypatch, tmp_path, fsync_fails):
    w, box, pending = queued_callback(tmp_path)
    synced = []

    def sync(directory):
        assert not pending.exists() and pending.with_suffix(".working").exists()
        if fsync_fails:
            raise OSError("synthetic_disk_failure")
        synced.append(directory)

    monkeypatch.setattr(server, "fsync_dir", sync)
    observed = run_once(monkeypatch, tmp_path, w, box)
    if fsync_fails:
        assert event() not in observed
        assert pending.with_suffix(".working").exists()
        assert box.pending() == []
    else:
        assert synced == [str(box.directory)]
        assert event() in observed
        assert json.loads(pending.with_suffix(".done").read_text()) == {"result": "processed"}


def test_disabling_lineworks_scope_stops_the_old_callback_server(monkeypatch, tmp_path):
    w, box, pending = queued_callback(tmp_path)
    observed = run_once(monkeypatch, tmp_path, w, box, invalid_scope=True)
    assert "tick" not in observed and event() not in observed
    assert pending.exists()


def test_expired_pending_callback_is_discarded_before_processing(monkeypatch, tmp_path):
    w, box, pending = queued_callback(tmp_path)
    os.utime(pending, (NOW - 1201, NOW - 1201))
    observed = run_once(monkeypatch, tmp_path, w, box)
    assert event() not in observed and not pending.exists()
    done = pending.with_suffix(".done").read_text()
    assert json.loads(done) == {"result": "unknown"}
    assert "合成入力" not in done


def test_poll_timeout_uses_asyncio_exception_across_supported_python_versions(monkeypatch, tmp_path):
    w, box, _ = queued_callback(tmp_path)
    wait_for = asyncio.wait_for

    class LegacyTimeout(Exception):
        pass  # Python 3.10's asyncio.TimeoutError was distinct from builtin TimeoutError.

    async def wait(value, *, timeout):
        if timeout == 2:
            value.close()
            raise LegacyTimeout
        return await wait_for(value, timeout=timeout)

    monkeypatch.setattr(asyncio, "TimeoutError", LegacyTimeout)
    monkeypatch.setattr(asyncio, "wait_for", wait)
    observed = run_once(monkeypatch, tmp_path, w, box)
    assert event() in observed
