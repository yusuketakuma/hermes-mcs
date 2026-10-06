"""Failure of one shutdown step must not retain the delivery scope lock."""
import asyncio
import signal
from types import SimpleNamespace

import pytest

from adapters.lineworks import __main__ as cli
from test_lineworks_adapter import SECRET, world


@pytest.mark.parametrize("failure", ["signal", "start", "shutdown", "close", "join"])
def test_shutdown_error_still_releases_lock_and_signal_handlers(monkeypatch, tmp_path, failure):
    w = world(tmp_path)
    observed, callbacks = [], {}

    def cleanup(name):
        observed.append(name)
        if name == failure:
            raise RuntimeError("synthetic_shutdown_failure")

    async def reconcile():
        return {}

    async def tick():
        callbacks[signal.SIGTERM]()

    worker = SimpleNamespace(acquire_scope_lock=lambda: True, reconcile=reconcile, tick=tick,
                             stop=lambda: cleanup("stop"),
                             release_scope_lock=lambda: cleanup("release"))
    http = SimpleNamespace(serve_forever=lambda: None, shutdown=lambda: cleanup("shutdown"),
                           server_close=lambda: cleanup("close"))
    thread = SimpleNamespace(start=lambda: cleanup("start"), join=lambda **_kw: cleanup("join"))
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

        def add_handler(sig, cb):
            if failure == "signal":
                raise RuntimeError("synthetic_shutdown_failure")
            callbacks[sig] = cb

        monkeypatch.setattr(loop, "add_signal_handler", add_handler)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: callbacks.pop(sig, None))
        with pytest.raises(RuntimeError, match="synthetic_shutdown_failure"):
            await cli.serve(tmp_path, 8788)

    asyncio.run(run())
    assert observed == (["stop", "release"] if failure == "signal"
                        else ["start", "stop", "close", "release"] if failure == "start"
                        else ["start", "stop", "shutdown", "close", "join", "release"])
    assert callbacks == {}
    assert w.client.calls == []
