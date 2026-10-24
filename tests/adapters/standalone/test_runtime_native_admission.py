"""Runtime admission uses actual synthetic worker lifetimes, not cancelled waiters."""
import asyncio
import fcntl
import threading
from types import SimpleNamespace

import pytest

from adapters.common import commands
from mcs_standalone import runtime


def test_start_failure_closes_native_gate_in_the_same_tick(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})

    def fail(*_args, **_kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(runtime.subprocess, "Popen", fail)
    assert host.tick(100) is True
    assert host.native_admission.snapshot() == {"open": False, "queued": 0, "running": 0}


def test_closed_admission_preserves_pending_calendar_and_command_work(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "cmd_int").mkdir(parents=True)
    (data / "cmd_int" / "synthetic.json").write_text("{}")
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    host.last_minute = 100
    host.due = {"mcs_check": 6000}
    host.fired = {"mcs_check": 5940}
    host.drained["cmd_int"] = set()
    starts = []

    def start(job, kind, now, names=None):
        starts.append(job)
        host.children[job] = {"kind": kind, "stopping_at": None,
                              "process": SimpleNamespace(pid=1001, poll=lambda: None)}

    monkeypatch.setattr(host, "start", start)
    monkeypatch.setattr(host, "_start_identity", lambda: None)
    host.native_admission.close()
    assert not host.tick(6000)
    assert starts == [] and host.fired == {"mcs_check": 5940}
    assert host.inflight == {} and host.drained["cmd_int"] == set()
    host.native_admission.open()
    assert not host.tick(6000)
    assert starts == [*runtime.BACKGROUND, "cmd_int", "mcs_check"]
    assert host.fired["mcs_check"] == 6000
    assert host.inflight["mcs_check"] == 5940


def test_restart_tick_does_not_finish_while_cancelled_native_work_runs(tmp_path):
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})

    async def scenario():
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def native():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "synthetic work was not released"

        task = asyncio.create_task(commands.run_native(native, admission=host.native_admission))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            host.restarting = True
            assert host.tick(100) is False
            assert host.native_admission.snapshot() == {"open": False, "queued": 0, "running": 1}
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert host.tick(101) is True

    asyncio.run(scenario())


def test_connector_reconnect_borrows_same_closed_runtime_gate(tmp_path, monkeypatch):
    cfg = {"runtime_mode": "standalone"}
    host = runtime.Runtime(tmp_path, cfg)
    monkeypatch.setattr(runtime, "Runtime", lambda *_a, **_k: host)
    monkeypatch.setattr(runtime.config, "load", lambda _root: cfg)
    owners = []

    def tick(*_a, **_k):
        return host.restarting

    monkeypatch.setattr(host, "tick", tick)

    async def scenario():
        sleep = asyncio.sleep

        async def yield_once(*_a):
            await sleep(0)

        monkeypatch.setattr(runtime.asyncio, "sleep", yield_once)

        async def connector(root, stopping, *, native_admission, delivery_admission, delivery_identity):
            owners.append(native_admission)
            assert root == tmp_path and not stopping.is_set()
            if len(owners) == 1:
                native_admission.close()
                raise ValueError("slack_configuration_changed_restart_required")
            assert len(owners) == 2
            assert native_admission.snapshot() == {"open": False, "queued": 0, "running": 0}
            host.restarting = True
            await stopping.wait()

        assert await asyncio.wait_for(runtime.serve(tmp_path, cfg, connector), 2) == 0

    asyncio.run(scenario())
    assert owners == [host.native_admission, host.native_admission]


@pytest.mark.parametrize("repeated_cancel", [False, True])
def test_cancelled_serve_holds_singleton_until_native_actual_exit(tmp_path, monkeypatch, repeated_cancel):
    cfg = {"runtime_mode": "standalone"}
    host = runtime.Runtime(tmp_path, cfg)
    monkeypatch.setattr(runtime, "Runtime", lambda *_a, **_k: host)
    monkeypatch.setattr(runtime.config, "load", lambda _root: cfg)
    monkeypatch.setattr(host, "tick", lambda **_k: False)

    async def scenario():
        entered = asyncio.Event()
        connector_ended = asyncio.Event()
        release = threading.Event()
        native_tasks = []
        loop = asyncio.get_running_loop()

        def native():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "synthetic work was not released"

        async def connector(root, stopping, *, native_admission, delivery_admission, delivery_identity):
            # A failed connector can leave real work even after its task ended.
            native_tasks.append(asyncio.create_task(commands.run_native(
                native, admission=native_admission)))
            await entered.wait()
            connector_ended.set()
            raise RuntimeError("synthetic_connector_end")

        task = asyncio.create_task(runtime.serve(tmp_path, cfg, connector))
        try:
            await asyncio.wait_for(connector_ended.wait(), 2)
            task.cancel()
            for _ in range(20):
                await asyncio.sleep(0)
                if not host.native_admission.snapshot()["open"]:
                    break
            assert host.native_admission.snapshot() == {"open": False, "queued": 0, "running": 1}
            if repeated_cancel:
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            with (host.data / "standalone.lock").open("rb") as fd:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            release.set()
            await asyncio.gather(*native_tasks, return_exceptions=True)
            with pytest.raises(asyncio.CancelledError):
                await task
        with (host.data / "standalone.lock").open("rb") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    asyncio.run(scenario())
