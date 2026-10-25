"""Independent pipe/process oracles; never spawn a child or access Keychain."""
import asyncio
import base64
import json
import os
from pathlib import Path
import signal
import threading

import pytest

from adapters.sdc import keychain
from adapters.sdc.keychain import KeychainError
from mcs_standalone import restart_credential as carrier

ACCOUNT = '00000000-0000-4000-8000-000000000001'
OTHER_ACCOUNT = '00000000-0000-4000-8000-000000000002'


def raw_credential(number=1):
    return json.dumps({'key': f'{number:064x}'}, separators=(',', ':')).encode()


def frame(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return len(raw).to_bytes(4, 'big') + raw


class PipeChild:
    """Fake Popen with independent framing, synchronous responses and explicit reap."""
    pid = 424242

    def __init__(self, fds):
        self.request = os.fdopen(os.dup(fds[0]), 'rb', buffering=0)
        self.response = os.fdopen(os.dup(fds[1]), 'wb', buffering=0)
        os.set_blocking(self.request.fileno(), False)
        os.set_blocking(self.response.fileno(), False)
        self.returncode = None
        self.exited, self.exit_code, self.reap_allowed = False, 0, True
        self.input, self.output = bytearray(), bytearray()
        self.requests, self.reads = [], []
        self.reply = lambda value, request: frame(value)
        self.chunk = 8192
        self.hold_ready = self.hold_read = self.hold_exit = self.terminal = False
        self.polls = 0

    def step(self):
        self.polls += 1
        if self.exited:
            return
        while not self.request.closed:
            try:
                part = os.read(self.request.fileno(), 8192)
            except BlockingIOError:
                break
            if not part:
                break
            self.input.extend(part)
        while len(self.input) >= 4:
            size = int.from_bytes(self.input[:4], 'big')
            if len(self.input) < size + 4:
                break
            request = json.loads(self.input[4:size + 4])
            del self.input[:size + 4]
            assert request['sequence'] == len(self.requests)
            assert request['nonce'] not in [r['nonce'] for r in self.requests]
            assert len(request['nonce']) == 32
            self.requests.append(request)
            response = {'sequence': request['sequence'], 'nonce': request['nonce']}
            if request['kind'] == 'prepare':
                assert request['account'] == ACCOUNT
                assert set(request) == {'kind', 'sequence', 'nonce', 'account'}
                response['kind'] = 'ready'
                if self.hold_ready:
                    continue
            else:
                assert set(request) == {'kind', 'sequence', 'nonce'}
                assert request['kind'] in ('read', 'read_and_exit')
                self.reads.append(request)
                self.terminal = request['kind'] == 'read_and_exit'
                if self.terminal:
                    assert os.read(self.request.fileno(), 1) == b''
                if self.hold_read:
                    continue
                response.update(kind='value', raw=base64.b64encode(raw_credential(len(self.reads))).decode())
            reply = self.reply(response, request)
            if reply is not None:
                self.output.extend(reply)
        if self.output:
            try:
                count = os.write(self.response.fileno(), self.output[:self.chunk])
                del self.output[:count]
            except BlockingIOError:
                pass
            except BrokenPipeError:
                self.finish(1)
        if self.terminal and not self.output and not self.hold_read and not self.hold_exit:
            self.finish()

    def finish(self, code=0):
        self.exited, self.exit_code = True, code
        self.request.close()
        self.response.close()


@pytest.fixture
def world(monkeypatch):
    children, spawns, signals = [], [], []

    def forbidden_native(*args, **kwargs):
        pytest.fail('the parent and synthetic child must never invoke native Keychain')

    def popen(argv, **kwargs):
        child = PipeChild(kwargs['pass_fds'])
        children.append(child)
        spawns.append((argv, kwargs))
        return child

    def waitpid(pid, flags):
        assert flags == os.WNOHANG and len(children) == 1 and pid == children[0].pid
        child = children[0]
        assert child.returncode is None, 'no second wait after actual reap'
        child.step()
        return (pid, child.exit_code << 8) if child.exited and child.reap_allowed else (0, 0)

    def killpg(pid, sig):
        assert len(children) == 1 and pid == children[0].pid
        assert children[0].returncode is None, 'never signal a reaped process group'
        signals.append(sig)
        children[0].finish()

    monkeypatch.setattr(carrier.subprocess, 'Popen', popen)
    monkeypatch.setattr(keychain, '_MacKeychain', forbidden_native)
    monkeypatch.setattr(carrier.os, 'waitpid', waitpid)
    monkeypatch.setattr(carrier.os, 'killpg', killpg)
    monkeypatch.setattr(carrier, 'POLL_S', 0.001)
    monkeypatch.setattr(carrier, 'TERM_GRACE_S', 0.005)
    yield children, spawns, signals
    for child in children:
        child.finish()


async def until(predicate):
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError('synthetic ownership observation did not arrive')


def new_owner(check=lambda: None, seconds=2):
    return carrier.CredentialCarrier(check=check, deadline=asyncio.get_running_loop().time() + seconds)


def test_fresh_reads_terminal_actual_reap_and_no_secret_spawn_or_cache(world, monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'synthetic-never-inherited')
    monkeypatch.setenv('SYNTHETIC_SECRET', 'synthetic-never-inherited')

    async def scenario():
        owner = new_owner()
        assert not owner.closed
        await owner.prepare(ACCOUNT)
        child = world[0][0]
        assert child.reads == [] and owner.busy and not owner.closed
        assert owner.copy_current(ACCOUNT) == raw_credential(1)
        assert owner.copy_current(ACCOUNT) == raw_credential(2)
        assert owner.copy_current(ACCOUNT, terminal=True) == raw_credential(3)
        assert child.returncode == 0 and owner.closed and not owner.busy
        assert owner._request.closed and owner._response.closed
        assert [r['kind'] for r in child.requests] == ['prepare', 'read', 'read', 'read_and_exit']
        assert len({r['nonce'] for r in child.requests}) == 4
        assert raw_credential(3).decode() not in repr(vars(owner))
        with pytest.raises(carrier.CredentialCarrierError, match='_CLOSED$'):
            owner.copy_current(ACCOUNT)
        await owner.close()
        assert owner.closed and len(world[1]) == 1 and world[2] == []
        argv, options = world[1][0]
        assert argv == [carrier.PYTHON, '-I', carrier.ENTRY, *map(str, options['pass_fds'])]
        assert Path(argv[0]).is_absolute() and Path(argv[2]).is_absolute()
        assert options == {'stdin': carrier.subprocess.DEVNULL, 'stdout': carrier.subprocess.DEVNULL,
                           'stderr': carrier.subprocess.DEVNULL, 'close_fds': True,
                           'pass_fds': options['pass_fds'], 'start_new_session': True,
                           'env': {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}}
        assert len(options['pass_fds']) == 2
        assert ACCOUNT not in repr(world[1]) and 'synthetic-never-inherited' not in repr(world[1])
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['unknown', 'sequence', 'bool-sequence', 'nonce', 'base64',
                                  'invalid-credential', 'raw-overflow', 'duplicate-key',
                                  'oversized-header', 'truncated', 'extra-frame', 'trailing'])
def test_invalid_frames_poison_without_reusing_late_response(world, kind, monkeypatch):
    monkeypatch.setattr(carrier, 'READ_BUDGET_S', 0.04)

    async def scenario():
        owner = new_owner()
        await owner.prepare(ACCOUNT)
        child = world[0][0]

        def reply(value, request):
            if kind == 'unknown':
                value['unexpected'] = 'synthetic-canary'
            elif kind == 'sequence':
                value['sequence'] += 1
            elif kind == 'bool-sequence':
                value['sequence'] = True
            elif kind == 'nonce':
                value['nonce'] = 'f' * 32
            elif kind == 'base64':
                value['raw'] = '**synthetic-canary**'
            elif kind == 'invalid-credential':
                value['raw'] = base64.b64encode(b'{"key":"synthetic-canary"}').decode()
            elif kind == 'raw-overflow':
                value['raw'] = base64.b64encode(b' ' * 4097).decode()
            elif kind == 'duplicate-key':
                raw = b'{"kind":"value","kind":"value"}'
                return len(raw).to_bytes(4, 'big') + raw
            elif kind == 'oversized-header':
                return (8193).to_bytes(4, 'big')
            elif kind == 'truncated':
                return frame(value)[:-1]
            elif kind == 'extra-frame':
                return frame(value) * 2
            elif kind == 'trailing':
                return frame(value) + b'X'
            return frame(value)

        child.reply = reply
        with pytest.raises((carrier.CredentialCarrierError, KeychainError)) as caught:
            owner.copy_current(ACCOUNT)
        assert 'synthetic-canary' not in str(caught.value)
        assert not owner.closed and len(child.reads) == 1
        child.reply = lambda value, request: frame(value)
        with pytest.raises(carrier.CredentialCarrierError, match='_FAILED$'):
            owner.copy_current(ACCOUNT, terminal=True)
        assert len(child.reads) == 1
        await owner.close()
        assert owner.closed and child.returncode == 0
    asyncio.run(scenario())


def test_partial_frames_and_writes_preserve_exact_request(world, monkeypatch):
    real_write = os.write

    def short_write(fd, raw):
        return real_write(fd, raw[:7])

    monkeypatch.setattr(carrier.os, 'write', short_write)

    async def scenario():
        owner = new_owner()
        await owner.prepare(ACCOUNT)
        world[0][0].chunk = 3
        assert owner.copy_current(ACCOUNT, terminal=True) == raw_credential()
        assert owner.closed and len(world[0][0].reads) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('deadline', [float('inf'), float('nan'), True, -1])
def test_invalid_deadline_never_spawns(world, deadline):
    async def scenario():
        with pytest.raises(carrier.CredentialCarrierError, match='_INVALID$'):
            carrier.CredentialCarrier(check=lambda: None, deadline=deadline)
    asyncio.run(scenario())
    assert world[0] == []


@pytest.mark.parametrize('account', [None, '', 'not-a-uuid', '00000000-0000-1000-8000-000000000001'])
def test_invalid_account_never_spawns(world, account):
    async def scenario():
        owner = new_owner()
        with pytest.raises(carrier.CredentialCarrierError, match='_INVALID$'):
            await owner.prepare(account)
        assert owner.closed
    asyncio.run(scenario())
    assert world[0] == []


def test_account_change_poisons_before_new_read(world):
    async def scenario():
        owner = new_owner()
        await owner.prepare(ACCOUNT)
        with pytest.raises(carrier.CredentialCarrierError, match='_INVALID$'):
            owner.copy_current(OTHER_ACCOUNT)
        with pytest.raises(carrier.CredentialCarrierError, match='_FAILED$'):
            owner.copy_current(ACCOUNT)
        assert world[0][0].reads == []
        await owner.close()
    asyncio.run(scenario())


def test_reentrant_current_is_not_another_native_read(world):
    active, owner = False, None

    def check():
        if active:
            owner.copy_current(ACCOUNT)

    async def scenario():
        nonlocal active, owner
        owner = new_owner(check)
        await owner.prepare(ACCOUNT)
        active = True
        with pytest.raises(carrier.CredentialCarrierError, match='_CHECK_FAILED$'):
            owner.copy_current(ACCOUNT)
        assert world[0][0].reads == []
        await owner.close()
        assert owner.closed
    asyncio.run(scenario())


@pytest.mark.parametrize('terminal', [False, True])
def test_no_response_or_exit_overruns_original_deadline(world, monkeypatch, terminal):
    monkeypatch.setattr(carrier, 'READ_BUDGET_S', 0.04)

    async def scenario():
        owner = new_owner()
        await owner.prepare(ACCOUNT)
        child = world[0][0]
        child.hold_exit, child.hold_read = terminal, not terminal
        owner._deadline = asyncio.get_running_loop().time() + 0.015
        before = asyncio.get_running_loop().time()
        with pytest.raises(carrier.CredentialCarrierError, match='_DEADLINE$'):
            owner.copy_current(ACCOUNT, terminal=terminal)
        assert asyncio.get_running_loop().time() - before < 0.2
        assert not owner.closed and child.returncode is None
        await owner.close()
        assert owner.closed
    asyncio.run(scenario())


def test_terminal_reply_is_not_success_before_reap_and_eof(world, monkeypatch):
    monkeypatch.setattr(carrier, 'READ_BUDGET_S', 0.02)

    async def scenario():
        owner = new_owner()
        await owner.prepare(ACCOUNT)
        child = world[0][0]
        child.reap_allowed = False
        with pytest.raises(carrier.CredentialCarrierError, match='_DEADLINE$'):
            owner.copy_current(ACCOUNT, terminal=True)
        assert child.exited and child.returncode is None and not owner.closed
        closer = asyncio.create_task(owner.close())
        for _ in range(4):
            await asyncio.sleep(0.001)
            assert owner.busy and not owner.closed and not closer.done()
        child.reap_allowed = True
        await closer
        assert owner.closed and child.returncode == 0
        with pytest.raises(carrier.CredentialCarrierError):
            owner.copy_current(ACCOUNT)
    asyncio.run(scenario())


def test_cleanup_ignores_failing_check_repeated_cancel_and_term_until_reap(world, monkeypatch):
    active = False

    def check():
        if active:
            raise RuntimeError('synthetic-secret-canary')

    def ignore_term(pid, sig):
        child = world[0][0]
        assert pid == child.pid and child.returncode is None
        world[2].append(sig)
        if sig == signal.SIGKILL:
            child.finish()

    monkeypatch.setattr(carrier.os, 'killpg', ignore_term)

    async def scenario():
        nonlocal active
        owner = new_owner(check)
        await owner.prepare(ACCOUNT)
        child = world[0][0]
        active, child.reap_allowed = True, False
        with pytest.raises(carrier.CredentialCarrierError, match='^SDC_RESTART_CREDENTIAL_CHECK_FAILED$'):
            owner.copy_current(ACCOUNT)
        closer = asyncio.create_task(owner.close())
        await until(lambda: signal.SIGKILL in world[2])
        for _ in range(4):
            closer.cancel()
            await asyncio.sleep(0.001)
            assert not closer.done() and not owner.closed and owner.busy
        child.reap_allowed = True
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert owner.closed and child.returncode == 0
        assert world[2][0] == signal.SIGTERM and signal.SIGKILL in world[2]
    asyncio.run(scenario())


def test_cancel_during_executor_spawn_keeps_handles_until_spawn_settles(world, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    spawn = carrier._spawn

    def delayed_spawn():
        entered.set()
        assert release.wait(5), 'test must release its synthetic executor'
        return spawn()

    monkeypatch.setattr(carrier, '_spawn', delayed_spawn)

    async def scenario():
        owner = new_owner()
        preparing = asyncio.create_task(owner.prepare(ACCOUNT))
        try:
            await until(entered.is_set)
            for _ in range(4):
                preparing.cancel()
                await asyncio.sleep(0.001)
                assert owner.busy and not owner.closed and not preparing.done()
            assert world[0] == []
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await preparing
        assert len(world[0]) == 1 and owner.closed and world[0][0].returncode == 0
        assert world[2] == [signal.SIGTERM]
    asyncio.run(scenario())


def test_spawn_failure_closes_every_created_pipe_and_is_fixed(world, monkeypatch):
    opened = []
    real_pipe = os.pipe

    def pipe():
        pair = real_pipe()
        opened.extend(pair)
        return pair

    def fail(*args, **kwargs):
        raise OSError('synthetic-secret-canary')

    monkeypatch.setattr(carrier.os, 'pipe', pipe)
    monkeypatch.setattr(carrier.subprocess, 'Popen', fail)

    async def scenario():
        owner = new_owner()
        with pytest.raises(carrier.CredentialCarrierError, match='^SDC_RESTART_CREDENTIAL_SPAWN$'):
            await owner.prepare(ACCOUNT)
        assert owner.closed and not owner.busy
        assert len(opened) == 4
        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)
    asyncio.run(scenario())


def test_pending_same_task_cancellation_during_final_check_never_returns_raw(world):
    owner = None
    cancel_now = False

    def check():
        if cancel_now:
            asyncio.current_task().cancel()

    async def attempt():
        nonlocal owner, cancel_now
        owner = new_owner(check)
        await owner.prepare(ACCOUNT)
        cancel_now = True
        try:
            owner.copy_current(ACCOUNT, terminal=True)
            pytest.fail('pending cancellation must never become credential success')
        finally:
            await owner.close()

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(attempt())
        assert owner.closed and world[0][0].reads == []
    asyncio.run(scenario())


def test_guard_after_terminal_reap_still_rejects_revoked_authority(world):
    async def scenario():
        def check():
            # Reap alone can precede EOF drainage. Revoke at the guard after
            # terminal cleanup, before the credential response may be returned.
            if (world[0] and world[0][0].returncode == 0
                    and owner._request.closed and owner._response.closed):
                raise RuntimeError('synthetic-secret-canary')

        owner = new_owner(check)
        await owner.prepare(ACCOUNT)
        with pytest.raises(carrier.CredentialCarrierError, match='_CHECK_FAILED$'):
            owner.copy_current(ACCOUNT, terminal=True)
        assert world[0][0].returncode == 0
        assert owner._request.closed and owner._response.closed
        await owner.close()
        assert owner.closed and world[2] == []
    asyncio.run(scenario())


def test_prepare_wait_leaves_event_loop_running_and_cancel_reaps(world, monkeypatch):
    popen = carrier.subprocess.Popen

    def waiting_child(*args, **kwargs):
        child = popen(*args, **kwargs)
        child.hold_ready = True
        return child

    monkeypatch.setattr(carrier.subprocess, 'Popen', waiting_child)

    async def scenario():
        owner = new_owner()
        preparing = asyncio.create_task(owner.prepare(ACCOUNT))
        await until(lambda: world[0] and world[0][0].requests)
        for _ in range(4):
            await asyncio.sleep(0.001)
            assert not preparing.done() and owner.busy and not owner.closed
        preparing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await preparing
        assert world[0][0].reads == [] and world[0][0].returncode == 0 and owner.closed
    asyncio.run(scenario())


def test_exact_maximum_raw_is_returned_without_normalizing_or_caching(world):
    raw = raw_credential() + b' ' * (4096 - len(raw_credential()))

    async def scenario():
        owner = new_owner()
        await owner.prepare(ACCOUNT)
        child = world[0][0]
        child.reply = lambda value, request: frame({**value, 'raw': base64.b64encode(raw).decode()})
        assert owner.copy_current(ACCOUNT, terminal=True) == raw
        assert owner.closed and len(child.reads) == 1
        assert raw not in vars(owner).values()
    asyncio.run(scenario())


def test_terminal_nonzero_exit_rejects_a_complete_credential_response(world):
    async def scenario():
        owner = new_owner()
        await owner.prepare(ACCOUNT)
        child = world[0][0]
        finish = child.finish
        child.finish = lambda code=0: finish(7)
        with pytest.raises(carrier.CredentialCarrierError, match='_CHILD$'):
            owner.copy_current(ACCOUNT, terminal=True)
        assert child.returncode == 7
        await owner.close()
        assert owner.closed and world[2] == []
    asyncio.run(scenario())


def test_unknown_reaper_never_signals_a_potentially_reused_pid(world, monkeypatch):
    async def scenario():
        owner = new_owner()
        await owner.prepare(ACCOUNT)
        child = world[0][0]

        def not_our_child(*args):
            raise ChildProcessError

        monkeypatch.setattr(carrier.os, 'waitpid', not_our_child)
        with pytest.raises(carrier.CredentialCarrierError, match='_CHILD$'):
            owner.copy_current(ACCOUNT)
        assert owner.busy and not owner.closed
        # Advance one cleanup iteration without leaving a permanently retained task
        # in asyncio.run. The oracle has deliberately withheld actual exit evidence.
        closer = owner.close()
        try:
            closer.send(None)
            assert not owner.closed and owner.busy and world[2] == []
        finally:
            closer.close()
            child.finish()
            owner._close_pipes()
        assert child.returncode is None and not owner.closed
    asyncio.run(scenario())


def test_credential_raw_uses_shared_shape_and_preserves_bytes():
    from ops.sdc_restart_credential import credential_raw

    raw = b'{ "key" : "' + b'1' * 64 + b'" }'
    assert credential_raw(raw) is raw
    with pytest.raises(KeychainError, match='SDC_KEYCHAIN_INVALID_VALUE'):
        credential_raw(b'{"key":"invalid"}')
