"""Synthetic process/pipe oracles; no HTTP, SDK, Keychain, real child or runtime data."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import signal
import threading

import pytest

from adapters.sdc import restart_control
from mcs_standalone import restart_transport as carrier
import sdc_restart_transport as entry

ENDPOINT = {'origin': 'https://synthetic.invalid', 'audience': 'synthetic', 'environment': 'test'}
HEADERS = {'X-SDC-Device': '00000000-0000-4000-8000-000000000001',
           'X-SDC-Key-ID': '00000000-0000-4000-8000-000000000002',
           'X-SDC-Time': '1', 'X-SDC-Nonce': 'a' * 32, 'X-SDC-Signature': 'b' * 64}
RAW = b'{"synthetic":"exact signed bytes"}'


class PipeChild:
    """Popen oracle with real bounded anonymous pipes, but no operating-system child."""
    pid = 424242

    def __init__(self):
        request_read, request_write = os.pipe()
        response_read, response_write = os.pipe()
        self.stdin = os.fdopen(request_write, 'wb', buffering=0)
        self.stdout = os.fdopen(response_read, 'rb', buffering=0)
        self.request = os.fdopen(request_read, 'rb', buffering=0)
        self.response = os.fdopen(response_write, 'wb', buffering=0)
        os.set_blocking(request_read, False)
        os.set_blocking(response_write, False)
        self.returncode = None
        self.exited = False
        self.polls = 0
        self.received = asyncio.Event()
        self.input = bytearray()

    def poll(self):
        self.polls += 1
        if self.exited:
            self.returncode = 0
        return self.returncode

    def finish(self):
        self.exited = True
        self.response.close()
        self.request.close()

    def close(self):
        for stream in (self.stdin, self.stdout, self.request, self.response):
            stream.close()

    async def exchange(self, reply=b'R\n{"answer":"synthetic grant"}', release=None):
        while True:
            try:
                part = os.read(self.request.fileno(), 4096)
            except BlockingIOError:
                part = None
            if part == b'':
                break
            if part:
                self.input.extend(part)
            await asyncio.sleep(0)
        self.received.set()
        if release is not None:
            await release.wait()
        offset = 0
        while offset < len(reply):
            if self.response.closed:
                return
            try:
                offset += os.write(self.response.fileno(), reply[offset:offset + 4096])
            except BlockingIOError:
                pass
            except BrokenPipeError:
                return
            await asyncio.sleep(0)
        self.finish()


@pytest.fixture
def world(monkeypatch):
    children, spawns, signals = [], [], []

    def popen(argv, **kwargs):
        child = PipeChild()
        children.append(child)
        spawns.append((argv, kwargs))
        return child

    def killpg(pid, sig):
        assert len(children) == 1 and pid == children[0].pid
        assert children[0].returncode is None, 'never signal a reaped group'
        signals.append(sig)
        children[0].finish()

    monkeypatch.setattr(carrier.subprocess, 'Popen', popen)
    monkeypatch.setattr(carrier.os, 'killpg', killpg)
    monkeypatch.setattr(carrier, 'POLL_S', 0.001)
    monkeypatch.setattr(carrier, 'TERM_GRACE_S', 0.005)
    yield children, spawns, signals
    for child in children:
        child.close()


async def until(condition):
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.001)
    raise AssertionError('synthetic observation did not arrive')


def request(owner, *, deadline=None):
    return owner.send(ENDPOINT, restart_control.GRANT, RAW, HEADERS,
                      deadline=asyncio.get_running_loop().time() + 1 if deadline is None else deadline)


def test_exact_one_request_bounded_pipes_clean_spawn_and_loop_progress(world, monkeypatch):
    children, spawns, signals = world
    monkeypatch.setenv('HTTPS_PROXY', 'synthetic-never-inherited')
    monkeypatch.setenv('SYNTHETIC_SECRET', 'never-inherited')

    async def scenario():
        owner = carrier.RestartCarrier()
        send = asyncio.create_task(request(owner))
        await until(lambda: children)
        child = children[0]
        release = asyncio.Event()
        server = asyncio.create_task(child.exchange(release=release))
        await child.received.wait()
        heartbeats = []
        for _ in range(5):
            heartbeats.append(owner.busy)
            await asyncio.sleep(0.001)
        assert heartbeats == [True] * 5 and not send.done()
        with pytest.raises(carrier.RestartCarrierError, match='^SDC_RESTART_CARRIER_BUSY$'):
            await request(owner)
        packet = json.loads(child.input)
        assert set(packet) == {'endpoint', 'path', 'raw', 'headers'}
        assert packet['endpoint'] == ENDPOINT and packet['headers'] == HEADERS
        assert packet['path'] == restart_control.GRANT
        assert base64.b64decode(packet['raw']) == RAW
        release.set()
        assert await send == {'answer': 'synthetic grant'}
        await server
        assert owner.outcome == 'response' and not owner.busy
        assert await owner.wait_closed() is None
        assert child.returncode == 0 and child.stdin.closed and child.stdout.closed
        assert len(spawns) == 1 and not signals
        argv, options = spawns[0]
        assert argv == [carrier.PYTHON, '-I', carrier.ENTRY]
        assert Path(argv[0]).is_absolute() and Path(argv[2]).is_absolute()
        assert options == {'stdin': carrier.subprocess.PIPE, 'stdout': carrier.subprocess.PIPE,
                           'stderr': carrier.subprocess.DEVNULL, 'bufsize': 0,
                           'close_fds': True, 'start_new_session': True,
                           'env': {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}}
        assert 'synthetic grant' not in repr(vars(owner))
        assert RAW.decode() not in repr(spawns)
    asyncio.run(scenario())


@pytest.mark.parametrize('reply', [b'', b'R\n{"unfinished":', b'R\n[]', b'R\n{}\n{}',
                                    b'R\n{"same":1,"same":2}', b'U\n',
                                    b'R\n' + b'x' * entry.MAX_OUTPUT])
def test_invalid_truncated_and_overflow_never_return_a_response(world, reply):
    children, spawns, _ = world

    async def scenario():
        owner = carrier.RestartCarrier()
        send = asyncio.create_task(request(owner))
        await until(lambda: children)
        server = asyncio.create_task(children[0].exchange(reply))
        with pytest.raises(carrier.RestartCarrierError) as caught:
            await send
        await server
        assert caught.value.outcome == 'unknown' and caught.value.status == 503
        assert not owner.busy and children[0].returncode == 0
        assert len(spawns) == 1
        if reply:
            assert reply.decode(errors='replace') not in str(caught.value)
    asyncio.run(scenario())


@pytest.mark.parametrize('deadline', [float('nan'), float('inf'), -float('inf'), True, -1])
def test_invalid_or_elapsed_deadline_never_spawns(world, deadline):
    async def scenario():
        owner = carrier.RestartCarrier()
        with pytest.raises(carrier.RestartCarrierError) as caught:
            await request(owner, deadline=deadline)
        assert caught.value.outcome == 'not_sent' and not owner.busy
    asyncio.run(scenario())
    assert not world[0]


def test_parent_deadline_while_child_does_not_read_stdin(world, monkeypatch):
    children, spawns, signals = world
    monkeypatch.setattr(carrier, 'MAX_SEND_S', 0.05)

    async def scenario():
        owner = carrier.RestartCarrier()
        with pytest.raises(carrier.RestartCarrierError, match='^SDC_RESTART_CARRIER_DEADLINE$') as caught:
            await owner.send(ENDPOINT, restart_control.GRANT,
                             b'{"synthetic":"' + b'a' * 16000 + b'"}', HEADERS,
                             deadline=asyncio.get_running_loop().time() + 1000)
        assert caught.value.outcome == 'unknown' and not owner.busy
        assert children[0].returncode == 0 and signals == [signal.SIGTERM]
        assert len(spawns) == 1
    asyncio.run(scenario())


def test_repeated_cancel_retains_owned_group_until_delayed_reap(world, monkeypatch):
    children, spawns, signals = world

    def ignore_signals(pid, sig):
        assert pid == children[0].pid and children[0].returncode is None
        signals.append(sig)

    monkeypatch.setattr(carrier.os, 'killpg', ignore_signals)

    async def scenario():
        owner = carrier.RestartCarrier()
        send = asyncio.create_task(request(owner))
        await until(lambda: owner.outcome == 'unknown')
        send.cancel()
        await until(lambda: signal.SIGKILL in signals)
        closer = asyncio.create_task(owner.wait_closed())
        await asyncio.sleep(0)
        for _ in range(5):
            send.cancel()
            closer.cancel()
            await asyncio.sleep(0.001)
            assert owner.busy and not send.done() and not closer.done()
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert len(spawns) == 1 and children[0].returncode is None
        children[0].finish()
        with pytest.raises(asyncio.CancelledError):
            await send
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert not owner.busy and children[0].returncode == 0
        assert children[0].stdin.closed and children[0].stdout.closed
    asyncio.run(scenario())


def test_cancel_during_spawn_keeps_child_and_never_writes_request(world, monkeypatch):
    children, spawns, signals = world
    entered, release = threading.Event(), threading.Event()
    spawn = carrier._spawn

    def slow_spawn():
        entered.set()
        assert release.wait(2)
        return spawn()

    monkeypatch.setattr(carrier, '_spawn', slow_spawn)

    async def scenario():
        owner = carrier.RestartCarrier()
        send = asyncio.create_task(request(owner))
        await until(entered.is_set)
        for _ in range(5):
            send.cancel()
            await asyncio.sleep(0.001)
            assert owner.busy and not send.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await send
        assert not owner.busy and owner.outcome == 'not_sent'
        assert len(spawns) == 1 and signals == [signal.SIGTERM]
        assert children[0].returncode == 0
    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.parametrize('cancel_outer', [False, True])
def test_cancel_before_owner_first_step_retires_without_spinning(world, monkeypatch, cancel_outer):
    create_task, shield = asyncio.create_task, asyncio.shield
    waits = []

    def cancel_before_start(coroutine):
        task = create_task(coroutine)
        task.cancel()
        if cancel_outer:
            asyncio.current_task().cancel()
        return task

    def bounded_shield(task):
        waits.append(task)
        assert len(waits) <= 2, 'cancelled owner must not spin on an already-cancelled task'
        return shield(task)

    monkeypatch.setattr(carrier.asyncio, 'create_task', cancel_before_start)
    monkeypatch.setattr(carrier.asyncio, 'shield', bounded_shield)

    async def scenario():
        owner = carrier.RestartCarrier()
        with pytest.raises(asyncio.CancelledError):
            await request(owner)
        assert waits and not owner.busy and owner._closed.is_set()
        assert owner.outcome == 'not_sent'
        assert await owner.wait_closed() is None
    asyncio.run(scenario())
    assert not any(world)


def test_owner_task_creation_failure_closes_coroutine_without_spawn(world, monkeypatch):
    coroutines = []

    def refuse(coroutine):
        coroutines.append(coroutine)
        raise RuntimeError('synthetic private task factory error')

    monkeypatch.setattr(carrier.asyncio, 'create_task', refuse)

    async def scenario():
        owner = carrier.RestartCarrier()
        with pytest.raises(carrier.RestartCarrierError, match='^SDC_RESTART_CARRIER_UNKNOWN$') as caught:
            await request(owner)
        assert caught.value.outcome == 'not_sent'
        assert len(coroutines) == 1 and coroutines[0].cr_frame is None
        assert not owner.busy and owner._closed.is_set()
        assert await owner.wait_closed() is None
    asyncio.run(scenario())
    assert not any(world)


def test_explicit_stop_discards_late_response_and_reaped_group_is_never_signalled(world):
    children, _, signals = world

    async def scenario():
        owner = carrier.RestartCarrier()
        send = asyncio.create_task(request(owner))
        await until(lambda: owner.outcome == 'unknown')
        # A late complete grant exists, but stop wins before the next owner turn.
        os.write(children[0].response.fileno(), b'R\n{"grant":"synthetic discarded"}')
        children[0].finish()
        owner.cancel()
        with pytest.raises(carrier.RestartCarrierError, match='^SDC_RESTART_CARRIER_CANCELLED$'):
            await send
        assert not owner.busy and not signals
        assert 'synthetic discarded' not in repr(vars(owner))
    asyncio.run(scenario())


def test_owner_loop_cannot_be_transferred(world):
    async def make_owner():
        return carrier.RestartCarrier()
    owner = asyncio.run(make_owner())
    async def foreign():
        with pytest.raises(carrier.RestartCarrierError, match='^SDC_RESTART_CARRIER_OWNER_REQUIRED$'):
            await request(owner)
    asyncio.run(foreign())
    assert not world[0]


def test_reaped_leader_with_stdout_holder_is_unknown_without_group_signal(world, monkeypatch):
    children, _, signals = world
    monkeypatch.setattr(carrier, 'MAX_SEND_S', 0.05)

    async def scenario():
        owner = carrier.RestartCarrier()
        send = asyncio.create_task(request(owner))
        await until(lambda: owner.outcome == 'unknown')
        os.write(children[0].response.fileno(), b'R\n{}')
        children[0].exited = True
        with pytest.raises(carrier.RestartCarrierError, match='^SDC_RESTART_CARRIER_DEADLINE$'):
            await send
        assert not owner.busy and not signals
        assert children[0].returncode == 0 and children[0].stdout.closed
    asyncio.run(scenario())


@pytest.mark.parametrize('path', restart_control.PATHS)
def test_child_uses_existing_transport_once_on_main_thread(monkeypatch, path):
    calls = []
    transport = entry.RestartTransport

    class Reply:
        code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            assert limit == 16385
            return b'{"synthetic":"response"}'

    class Opener:
        def open(self, request, timeout):
            assert threading.current_thread() is threading.main_thread()
            assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
            assert timeout == 10
            calls.append((request.full_url, request.data))
            return Reply()

    def create(approved):
        assert approved == ENDPOINT
        client = transport(approved)
        client.opener = Opener()
        return client

    monkeypatch.setattr(entry, 'RestartTransport', create)
    target = io.BytesIO()
    entry._serve(io.BytesIO(entry.encode_request(ENDPOINT, path, RAW, HEADERS)), target)
    assert calls == [(ENDPOINT['origin'] + path, RAW)]
    assert target.getvalue() == b'R\n{"synthetic":"response"}'


@pytest.mark.parametrize('packet', [b'{}', b'x' * (entry.MAX_INPUT + 1), b'{"endpoint":1}',
                                     b'{"raw":"x","raw":"y"}'])
def test_child_rejects_bounded_bad_input_without_dispatch(monkeypatch, packet):
    monkeypatch.setattr(entry, 'RestartTransport', lambda *_: pytest.fail('must not dispatch'))
    target = io.BytesIO()
    entry._serve(io.BytesIO(packet), target)
    assert target.getvalue() == b'N\n'


def test_child_exception_never_exposes_error_text_or_request(monkeypatch):
    def fail(_):
        raise RuntimeError('synthetic private exception text')
    monkeypatch.setattr(entry, 'RestartTransport', fail)
    target = io.BytesIO()
    entry._serve(io.BytesIO(entry.encode_request(ENDPOINT, restart_control.GRANT, RAW, HEADERS)), target)
    assert target.getvalue() == b'U\n'
