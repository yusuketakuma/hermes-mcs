"""Synthetic Runtime ownership of a bounded restart exchange, without remote authority."""
import asyncio
import fcntl
import json
import os

import pytest

from adapters.common.worker import await_settled
from mcs_standalone import runtime


class Carrier:
    def __init__(self):
        self.busy = False
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0

    async def send(self, *args, **kwargs):
        assert not self.busy
        self.busy = True
        self.calls += 1
        async def io():
            self.entered.set()
            try:
                await self.finished.wait()
                return {'synthetic': 'untrusted response'}
            finally:
                self.busy = False
        return await await_settled(io(), on_cancel=self.cancel)

    def cancel(self):
        self.cancelled.set()

    async def wait_closed(self):
        await self.finished.wait()
        while self.busy:
            await asyncio.sleep(0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg = {'runtime_mode': 'standalone'}
    (tmp_path / 'data').mkdir(mode=0o700)
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    host = runtime.Runtime(tmp_path, cfg)
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('no real child'))
    monkeypatch.setattr(runtime, 'RestartCarrier', Carrier)
    return host


def exchange(host, handle):
    # Transport and signature tests own request validation; this fixture owns no network.
    return host.restart_exchange(handle, {}, '/synthetic', b'{}', {},
                                 deadline=asyncio.get_running_loop().time() + 10)


def assert_locked(path):
    fd = os.open(path, os.O_WRONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_exchange_requires_own_reservation_before_allocating_carrier(world):
    async def scenario():
        for wrong in (None, object()):
            with pytest.raises(RuntimeError, match='^standalone_restart_reservation_invalid$'):
                await exchange(world, wrong)
        assert world._restart_carrier is None
        handle = world.reserve_restart()
        try:
            with pytest.raises(RuntimeError, match='^standalone_restart_reservation_invalid$'):
                await exchange(world, object())
            assert world._restart_carrier is None
        finally:
            world.release_restart(handle)
    asyncio.run(scenario())


def test_pending_exchange_keeps_gate_and_update_lock_while_loop_and_tick_progress(world):
    async def scenario():
        handle = world.reserve_restart()
        task = asyncio.create_task(exchange(world, handle))
        await asyncio.sleep(0)
        carrier = world._restart_carrier
        await carrier.entered.wait()
        with pytest.raises(RuntimeError, match='^standalone_restart_transport_busy$'):
            world.release_restart(handle)
        assert world._restart_reservation['handle'] is handle
        assert_locked(world.data / 'update.lock')
        assert not world.tick(6000)
        assert not world.native_admission.snapshot()['open']
        assert not world.delivery_admission.snapshot()['open']
        assert not world.children and not world.restarting
        carrier.finished.set()
        assert await task == {'synthetic': 'untrusted response'}
        assert carrier.calls == 1 and not carrier.busy
        assert not world.restarting
        assert world.release_restart(handle)
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['stop', 'generation', 'config', 'native', 'delivery', 'identity', 'gate_open'])
@pytest.mark.parametrize('after_send', [False, True])
def test_changed_owner_cannot_send_or_receive_execution_evidence(world, change, after_send):
    async def scenario():
        handle = world.reserve_restart()
        task = None
        if after_send:
            task = asyncio.create_task(exchange(world, handle))
            await asyncio.sleep(0)
            await world._restart_carrier.entered.wait()
        if change == 'stop':
            world.restarting = True
        elif change == 'generation':
            world.generation = 'changed'
        elif change == 'config':
            (world.root / 'config.json').write_text('{"runtime_mode":"standalone","notify":{}}')
        elif change == 'native':
            world.native_admission = runtime.NativeAdmission()
            world.native_admission.close()
        elif change == 'delivery':
            world.delivery_admission = runtime.DeliveryAdmission()
            world.delivery_admission.close()
        elif change == 'identity':
            world._delivery_identity = object()
        else:
            world.native_admission.open()
        if after_send:
            world._restart_carrier.finished.set()
        try:
            with pytest.raises(RuntimeError, match='^standalone_restart_owner_changed$'):
                await (task if after_send else exchange(world, handle))
            if not after_send:
                assert world._restart_carrier is None
        finally:
            world.release_restart(handle)
    asyncio.run(scenario())


def test_repeated_waiter_cancellation_does_not_release_actual_exchange(world):
    async def scenario():
        handle = world.reserve_restart()
        task = asyncio.create_task(exchange(world, handle))
        await asyncio.sleep(0)
        carrier = world._restart_carrier
        await carrier.entered.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert carrier.busy and not task.done()
            with pytest.raises(RuntimeError, match='^standalone_restart_transport_busy$'):
                world.release_restart(handle)
            assert_locked(world.data / 'update.lock')
        carrier.finished.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not carrier.busy and carrier.calls == 1
        assert world.release_restart(handle)
    asyncio.run(scenario())


def test_serve_reaps_exchange_before_releasing_singleton_and_reservation(world, monkeypatch):
    async def scenario():
        handle = world.reserve_restart()
        sending = asyncio.create_task(exchange(world, handle))
        await asyncio.sleep(0)
        carrier = world._restart_carrier
        await carrier.entered.wait()
        monkeypatch.setattr(runtime, 'Runtime', lambda *_a: world)
        monkeypatch.setattr(world, 'adopt_orphans', lambda: None)
        monkeypatch.setattr(world, 'recover_inflight', lambda: None)
        original = world.tick
        def stop(*args, **kwargs):
            world.restarting = True
            return original(6000, **kwargs)
        monkeypatch.setattr(world, 'tick', stop)
        serving = asyncio.create_task(runtime.serve(world.root, world.cfg))
        await carrier.cancelled.wait()
        for _ in range(2):
            serving.cancel()
            await asyncio.sleep(0)
            assert not serving.done() and carrier.busy
            assert_locked(world.data / 'standalone.lock')
            assert_locked(world.data / 'update.lock')
        carrier.finished.set()
        with pytest.raises(RuntimeError, match='^standalone_restart_owner_changed$'):
            await sending
        with pytest.raises(asyncio.CancelledError):
            await serving
        assert world._restart_reservation is None and not carrier.busy
        assert not world.native_admission.snapshot()['open']
        assert not world.delivery_admission.snapshot()['open']
        fd = os.open(world.data / 'standalone.lock', os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    asyncio.run(scenario())
