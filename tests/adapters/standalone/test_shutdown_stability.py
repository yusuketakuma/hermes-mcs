"""Synthetic connector/unload failures cannot skip owned-resource cleanup."""
import asyncio
import fcntl

import pytest

from mcs_standalone import runtime
from mcs_standalone.host import Host


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_connector_failure_removes_handlers_and_releases_runtime_lock(tmp_path, monkeypatch, failure):
    cfg = {"runtime_mode": "standalone"}
    monkeypatch.setattr(runtime.config, "load", lambda root: cfg)
    monkeypatch.setattr(runtime.Runtime, "tick", lambda self, **kwargs: self.restarting)
    original_sleep = asyncio.sleep

    async def yield_once(*args):
        await original_sleep(0)

    monkeypatch.setattr(runtime.asyncio, "sleep", yield_once)
    handlers = {}

    async def connector(root, stopping):
        raise failure("synthetic connector ended")

    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler",
                            lambda sig, *args: handlers.__setitem__(sig, args))
        monkeypatch.setattr(loop, "remove_signal_handler",
                            lambda sig: handlers.pop(sig, None))
        with pytest.raises(failure):
            await runtime.serve(tmp_path, cfg, connector)
        assert handlers == {}

    asyncio.run(scenario())
    with (tmp_path / "data/standalone.lock").open("ab") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_partial_signal_registration_is_removed(tmp_path, monkeypatch):
    handlers = {}

    async def scenario():
        loop = asyncio.get_running_loop()

        def register(sig, *args):
            if handlers:
                raise OSError("synthetic handler registration failure")
            handlers[sig] = args

        monkeypatch.setattr(loop, "add_signal_handler", register)
        monkeypatch.setattr(loop, "remove_signal_handler",
                            lambda sig: handlers.pop(sig, None))
        with pytest.raises(OSError):
            await runtime.serve(tmp_path, {"runtime_mode": "standalone"})
        assert handlers == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
def test_failed_unload_still_finishes_remaining_callbacks_and_owned_tasks(failure):
    async def scenario():
        host = Host({})
        started = asyncio.Event()
        stopped = asyncio.Event()
        callbacks = []

        async def child():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def failed_unload():
            raise failure("synthetic unload failure")

        host.on_unload(lambda: callbacks.append("remaining"))
        host.on_unload(failed_unload)
        task = host.spawn_task(child())
        await started.wait()
        try:
            with pytest.raises(failure) as error:
                await host.close()
            if failure is RuntimeError:
                assert str(error.value) == "standalone_host_unload_failed"
            assert stopped.is_set()
            assert task.done() and not host.tasks
            assert callbacks == ["remaining"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_restart_escalates_background_child_without_interrupting_core_work(tmp_path):
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    host.restarting = True

    class Child:
        pid = 1001
        code = None

        def __init__(self):
            self.terminated = self.killed = 0

        def poll(self):
            return self.code

        def terminate(self):
            self.terminated += 1  # Synthetic child ignores graceful termination.

        def kill(self):
            self.killed += 1
            self.code = -9

    background, core = Child(), Child()
    for job, child, kind in [
        ("extract-0", background, "background"), ("cmd", core, "core"),
    ]:
        host.children[job] = {"process": child, "kind": kind, "stopping_at": None}
    assert not host.tick(100)
    assert background.terminated == 1 and not background.killed
    assert not host.tick(109)
    assert background.terminated == 1 and not background.killed
    assert not host.tick(110)
    assert background.killed == 1 and not core.terminated and not core.killed
    assert not host.tick(111)  # Core work must complete before restart.
    assert "extract-0" not in host.children
    core.code = 0
    assert host.tick(112) and not host.children


def test_repeated_hosts_double_close_and_readmission_rejection_release_owned_tasks():
    async def scenario():
        for _n in range(100):
            host = Host({})
            entered, exited = asyncio.Event(), asyncio.Event()
            callbacks = []

            async def child():
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    exited.set()

            host.on_unload(lambda: callbacks.append("once"))
            task = host.spawn_task(child())
            await entered.wait()
            await asyncio.gather(host.close(), host.close())
            await host.close()
            assert callbacks == ["once"] and exited.is_set() and task.done()
            assert not host.tasks
            blocked = child()
            assert host.spawn_task(blocked) is None and blocked.cr_frame is None
            with pytest.raises(RuntimeError, match="standalone_host_stopping"):
                host.on_unload(lambda: None)
    asyncio.run(scenario())
