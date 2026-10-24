"""Restart reservations and shutdown use actual synthetic delivery ownership."""
import asyncio
import fcntl

import pytest

from mcs_standalone import runtime


@pytest.fixture
def host(tmp_path, monkeypatch):
    cfg = {"runtime_mode": "standalone", "notify": {"interactive": "slack"}}
    (tmp_path / "data").mkdir()
    owner = runtime.Runtime(tmp_path, cfg)
    monkeypatch.setattr(runtime.config, "load", lambda _root: cfg)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no real child"))
    return owner


def ready(host):
    identity = host.delivery_admission.begin_connector()
    host._delivery_identity = identity
    host.connector_started(100)
    host.delivery_admission.connector_ready(identity)
    return identity


@pytest.mark.parametrize("transport", ["slack", "discord"])
def test_ready_idle_connector_reserves_both_gates_and_reopens_same_owner(host, transport):
    host.cfg["notify"]["interactive"] = transport
    identity = ready(host)
    handle = host.reserve_restart()
    assert not host.native_admission.snapshot()["open"]
    assert not host.delivery_admission.send_allowed(identity)
    assert not host.delivery_admission.begin_pass(identity)
    assert not host.restarting
    assert host.release_restart(handle)
    assert host.native_admission.snapshot()["open"]
    assert host.delivery_admission.send_allowed(identity)
    assert host.delivery_admission.begin_pass(identity)
    host.delivery_admission.end_pass(identity)


def test_running_task_without_readiness_does_not_authorize_reservation(host):
    host._delivery_identity = host.delivery_admission.begin_connector()
    host.connector_started(100)
    with pytest.raises(RuntimeError, match="^native_owner_unknown$"):
        host.reserve_restart()
    assert not (host.data / "update.lock").exists()


@pytest.mark.parametrize("state", ["starting", "closing"])
def test_lost_ready_witness_rejects_reservation(host, state):
    identity = ready(host)
    getattr(host.delivery_admission, "connector_" + state)(identity)
    with pytest.raises(RuntimeError, match="^native_owner_unknown$"):
        host.reserve_restart()
    assert host.delivery_admission.snapshot()["open"]


def test_active_pass_is_busy_and_can_finish_after_admission_closes(host):
    identity = ready(host)
    assert host.delivery_admission.begin_pass(identity)
    try:
        with pytest.raises(RuntimeError, match="^standalone_restart_reservation_busy$"):
            host.reserve_restart()
        assert host.delivery_admission.snapshot()["active_passes"] == 1
        assert host.native_admission.snapshot()["open"]
        host.restarting = True
        assert host.tick(100) is False
        assert not host.delivery_admission.send_allowed(identity)
    finally:
        host.delivery_admission.end_pass(identity)
    assert host.tick(101) is True


def test_old_reservation_cannot_reopen_replacement_connector(host):
    identity = ready(host)
    handle = host.reserve_restart()
    host.delivery_admission.connector_closing(identity)
    host.delivery_admission.end_connector(identity)
    # Even an unexpected in-process owner replacement cannot inherit authority.
    host._delivery_identity = None
    assert host.release_restart(handle) is False
    assert host.restarting
    assert not host.native_admission.snapshot()["open"]
    assert not host.delivery_admission.snapshot()["open"]


@pytest.mark.parametrize("active_pass", [False, True])
def test_shutdown_keeps_singleton_until_actual_connector_cleanup(host, monkeypatch, active_pass):
    monkeypatch.setattr(runtime, "Runtime", lambda *_a, **_k: host)
    monkeypatch.setattr(host, "tick", lambda **_k: False)

    async def scenario():
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        identities = []

        async def connector(root, stopping, *, native_admission, delivery_admission, delivery_identity):
            assert root == host.root and native_admission is host.native_admission
            assert delivery_admission is host.delivery_admission
            identities.append(delivery_identity)
            delivery_admission.connector_ready(delivery_identity)
            if active_pass:
                assert delivery_admission.begin_pass(delivery_identity)
            entered.set()
            await stopping.wait()
            delivery_admission.connector_closing(delivery_identity)
            cleaning.set()
            await release.wait()
            if active_pass:
                delivery_admission.end_pass(delivery_identity)

        task = asyncio.create_task(runtime.serve(host.root, host.cfg, connector))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not host.delivery_admission.snapshot()["open"]
            assert host.delivery_admission.snapshot()["state"] == "closing"
            assert host.delivery_admission.snapshot()["active_passes"] == int(active_pass)
            with (host.data / "standalone.lock").open("rb") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        assert len(identities) == 1
        assert host._delivery_identity is None
        assert host.delivery_admission.snapshot()["state"] == "off"
        with (host.data / "standalone.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    asyncio.run(scenario())
