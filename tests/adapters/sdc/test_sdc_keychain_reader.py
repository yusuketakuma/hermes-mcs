"""Synthetic process/pipe/clock oracles for normal fresh native read ownership."""
import base64
import fcntl
import hashlib
import json
import os
import signal
import time

import pytest

from adapters.sdc import credential_mutation as mutation, keychain, keychain_owner as reader, storage
from test_sdc_credential_mutation import INTENT, canonical, digest
from test_sdc_credential_mutation import private_marker as private_marker

ACCOUNT = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'
CANARY = 'SYNTHETIC_SECRET_NEVER_LOG'


def credential(number=1):
    return json.dumps({'key': f'{number:02x}' * 32}, separators=(',', ':')).encode()


def frame(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return len(raw).to_bytes(4, 'big') + raw


class Child:
    """An independently specified one-copy peer on temporary anonymous pipes."""
    def __init__(self, number, passed):
        self.pid, self.returncode = 400000 + number, None
        self.source, self.target = map(os.dup, passed[:2])
        self.leases = list(map(os.dup, passed[2:]))
        self.mutation_response = None
        os.set_blocking(self.source, False)
        os.set_blocking(self.target, False)
        self.raw, self.error, self.reply = credential(number), None, frame
        self.input, self.output, self.requests = bytearray(), b'', []
        self.hold_read = self.hold_exit = self.hold_eof = False
        self.exited, self.reap_allowed, self.exit_code = False, True, 0
        self.request_done, self.chunk = False, 8192

    def step(self):
        if self.exited or self.hold_read:
            return
        if not self.request_done:
            try:
                part = os.read(self.source, 8192)
            except BlockingIOError:
                return
            if part:
                self.input.extend(part)
                return
            self.request_done = True
            assert int.from_bytes(self.input[:4], 'big') == len(self.input) - 4
            request = json.loads(self.input[4:])
            if self.mutation_response is not None:
                self.requests.append(request)
                self.output = self.reply(self.mutation_response(request, self.leases))
            else:
                self.output = self.reply(self.read_response(request))
        if self.output:
            try:
                count = os.write(self.target, self.output[:self.chunk])
            except BlockingIOError:
                return
            self.output = self.output[count:]
        if not self.output and not self.hold_exit:
            self.finish(self.exit_code)

    def read_response(self, request):
        assert set(request) == {'kind', 'account', 'sequence', 'nonce'}
        assert request['kind'] == 'read' and request['account'] == ACCOUNT
        self.requests.append(request)
        response = {'kind': 'value', 'sequence': request['sequence'], 'nonce': request['nonce'],
                    'raw': base64.b64encode(self.raw).decode(), 'sha256': hashlib.sha256(self.raw).hexdigest()}
        if self.error:
            response = {'kind': 'error', 'sequence': request['sequence'],
                        'nonce': request['nonce'], 'code': self.error}
        return response

    def finish(self, code):
        self.exited, self.exit_code = True, code
        while self.leases:
            os.close(self.leases.pop())
        if self.source is not None:
            os.close(self.source)
            self.source = None
        if self.target is not None and not self.hold_eof:
            os.close(self.target)
            self.target = None


class InterruptedPipe:
    """Keep a real anonymous pipe owned across one injected close interruption."""
    def __init__(self, stream, error):
        self.stream, self.error = stream, error

    @property
    def closed(self):
        return self.stream.closed

    def fileno(self):
        return self.stream.fileno()

    def close(self):
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.stream.close()


@pytest.fixture
def world(monkeypatch):
    state = {'now': 100.0, 'spawn_after': 100.0, 'children': [], 'spawns': [], 'signals': [],
             'executors': [], 'owners': [], 'sleep_errors': [], 'shutdown_errors': [],
             'result_errors': [], 'exception_errors': [],
             'setup': lambda child: None, 'ignore_term': False, 'lost_reap': False}

    class Future:
        def __init__(self, call, args):
            self.call, self.args, self.finished = call, args, False
            self.value, self.error = None, None

        def done(self):
            if not self.finished and state['now'] >= state['spawn_after']:
                try:
                    self.value = self.call(*self.args)
                except BaseException as error:
                    self.error = error
                self.finished = True
            return self.finished

        def result(self):
            assert self.done(), 'unsettled spawn must stay owned'
            if state['result_errors']:
                raise state['result_errors'].pop(0)
            if self.error:
                raise self.error
            return self.value

        def exception(self):
            assert self.done(), 'unsettled spawn must stay owned'
            if state['exception_errors']:
                raise state['exception_errors'].pop(0)
            return self.error

        def cancel(self):
            pytest.fail('Future cancellation cannot relinquish actual spawn ownership')

    class Executor:
        def __init__(self, *, max_workers, thread_name_prefix):
            assert max_workers == 1 and thread_name_prefix == 'sdc-keychain-spawn'
            self.futures, self.joined = [], False
            state['executors'].append(self)

        def submit(self, call, *args):
            assert call is reader._spawn and len(args) in (1, 2)
            if len(args) == 2:
                assert len(args[1]) == 2 and args[1][0] != args[1][1]
            assert not self.joined and all(future.finished for future in self.futures)
            future = Future(call, args)
            self.futures.append(future)
            return future

        def shutdown(self, *, wait, cancel_futures):
            assert wait is True and cancel_futures is False
            assert all(future.finished for future in self.futures)
            if state['shutdown_errors']:
                raise state['shutdown_errors'].pop(0)
            self.joined = True

    def popen(argv, **kwargs):
        state['spawns'].append((argv, kwargs))
        child = Child(len(state['children']) + 1, kwargs['pass_fds'])
        state['setup'](child)
        state['children'].append(child)
        return child

    def waitpid(pid, flags):
        assert flags == os.WNOHANG
        if state['lost_reap']:
            raise ChildProcessError('synthetic lost ownership')
        child = next(child for child in state['children'] if child.pid == pid)
        assert child.returncode is None
        child.step()
        if child.exited and child.reap_allowed:
            return pid, child.exit_code << 8
        return 0, 0

    def killpg(pid, sig):
        child = next(child for child in state['children'] if child.pid == pid)
        assert child.returncode is None
        state['signals'].append((pid, sig))
        if sig == signal.SIGKILL or not state['ignore_term']:
            child.finish(0)

    def sleep(delay):
        assert 0 <= delay <= 0.005
        state['now'] += max(0.001, delay)
        if state['sleep_errors']:
            raise state['sleep_errors'].pop(0)

    def forbidden(*args, **kwargs):
        pytest.fail('no actual subprocess or native Keychain is allowed')

    monkeypatch.setattr(reader, 'ThreadPoolExecutor', Executor)
    monkeypatch.setattr(reader.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(reader.os, 'waitpid', waitpid)
    monkeypatch.setattr(reader.os, 'killpg', killpg)
    monkeypatch.setattr(reader.time, 'sleep', sleep)
    monkeypatch.setattr(keychain, '_MacKeychain', forbidden)
    state['popen'] = popen
    yield state
    # Test teardown alone repairs the fake OS. Production has no lost-reap recovery.
    state['lost_reap'] = False
    state['sleep_errors'].clear()
    state['shutdown_errors'].clear()
    state['result_errors'].clear()
    state['exception_errors'].clear()
    state['now'] = max(state['now'], state['spawn_after'])
    for owned in state['owners']:
        owned._lost_reap = False
        owned._interruption = None
        if owned._spawning is not None:
            owned._spawning.done()
        for child in state['children']:
            child.reap_allowed, child.hold_eof = True, False
            child.finish(0)
        owned.close()
    # An old reader may drop a settled Future on timeout. The red oracle must
    # still release that fixture's real pipes after detecting the ownership loss.
    for executor in state['executors']:
        for future in executor.futures:
            if future.value is not None:
                for stream in future.value[1:]:
                    stream.close()


def owner(world, **options):
    owned = reader.KeychainOwner(popen=world['popen'], monotonic=lambda: world['now'], **options)
    world['owners'].append(owned)
    return owned


def mutation_peer(world, *, bad=None):
    def setup(child):
        def respond(request, leases):
            assert set(request) == {'kind', 'account', 'sequence', 'nonce', 'deadline', 'intent_id', 'marker_sha256', 'raw'}
            assert request['kind'] in ('write', 'delete') and request['account'] == ACCOUNT
            assert request['intent_id'] == INTENT and request['deadline'] <= 105.0
            assert len(leases) == 2 and all(os.fstat(fd) for fd in leases)
            root_fd, _ = leases
            fd = os.open('credential-store.json', os.O_RDONLY, dir_fd=root_fd)
            with os.fdopen(fd, 'rb') as source:
                marker = json.load(source)
            assert digest(marker) == request['marker_sha256']
            if request['kind'] == 'write':
                assert base64.b64decode(request['raw']) == credential(2)
            else:
                assert request['raw'] is None
            verified = {**marker, 'mutation': {**marker['mutation'], 'phase': 'verified', 'verified_at': 1100}}
            if bad == 'changed-intent':
                verified['mutation']['id'] = OTHER
            if bad != 'no-proof':
                fd = os.open('credential-store.json', os.O_WRONLY | os.O_TRUNC, dir_fd=root_fd)
                with os.fdopen(fd, 'wb') as output:
                    output.write(canonical(verified))
            if bad == 'late':
                world['now'] = 106.0
            return {'kind': 'verified', 'intent_id': INTENT, 'marker_sha256': digest(verified),
                    'sequence': request['sequence'], 'nonce': request['nonce']}
        child.mutation_response = respond
    world['setup'] = setup


def mutate(owned, private_marker, world, *, deadline=105.0, tick_fd=None):
    _, root_fd, original_tick, value = private_marker
    return owned.for_account(ACCOUNT).write(credential(2), intent_id=INTENT,
                    marker_sha256=digest(value), root_fd=root_fd,
                    tick_fd=original_tick if tick_fd is None else tick_fd, deadline=deadline)


def test_mutation_adopts_only_matching_checkpoint_after_eof_and_actual_reap(world, private_marker):
    mutation_peer(world)
    owned = owner(world)
    result = mutate(owned, private_marker, world)
    child = world['children'][0]
    assert result == {'intent_id': INTENT, 'marker_sha256': digest(reader.mutation.read_marker(private_marker[1]))}
    assert child.returncode == 0 and child.exited and child.source is child.target is None
    assert child.leases == [] and owned._lease_fds == []
    argv, options = world['spawns'][0]
    assert len(options['pass_fds']) == 4 and argv[-4:] == list(map(str, options['pass_fds']))
    assert options['env'] == {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
    assert ACCOUNT not in repr(argv) + repr(options) and credential(2).decode() not in repr(argv) + repr(options)
    owned.close()
    assert owned.closed and world['executors'][0].joined


@pytest.mark.parametrize('bad', ['no-proof', 'changed-intent', 'late'])
def test_mutation_cannot_adopt_forged_or_late_completion(world, private_marker, bad):
    mutation_peer(world, bad=bad)
    owned = owner(world)
    with pytest.raises(reader.KeychainReadError):
        mutate(owned, private_marker, world)
    with pytest.raises(reader.KeychainReadError, match='FAILED'):
        owned.for_account(ACCOUNT)
    assert len(world['spawns']) == 1 and owned._lease_fds
    owned.close()
    assert owned.closed and owned._lease_fds == []


@pytest.mark.parametrize('held', ['hold_exit', 'hold_eof', 'reap_allowed'])
def test_verified_bytes_do_not_replace_owned_eof_exit_reap(world, private_marker, held):
    mutation_peer(world)
    setup = world['setup']
    def hold(child):
        setup(child)
        setattr(child, held, held != 'reap_allowed')
    world['setup'] = hold
    owned = owner(world)
    with pytest.raises(reader.KeychainReadError, match='DEADLINE'):
        mutate(owned, private_marker, world, deadline=100.03)
    assert reader.mutation.read_marker(private_marker[1])['mutation']['phase'] == 'verified'
    assert not owned.closed and owned._lease_fds
    child = world['children'][0]
    child.reap_allowed, child.hold_eof, child.hold_exit = True, False, False
    child.finish(0)
    owned.close()
    assert owned.closed


def test_unsettled_mutation_spawn_and_lost_reap_keep_same_ofd_lease(world, private_marker):
    root, root_fd, _, _ = private_marker
    parent_fd = os.open(root / 'tick.lock', os.O_RDWR)
    fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    world['spawn_after'] = 101.0
    mutation_peer(world)
    owned = owner(world)
    try:
        with pytest.raises(reader.KeychainReadError, match='DEADLINE'):
            mutate(owned, private_marker, world, deadline=100.02, tick_fd=parent_fd)
    finally:
        os.close(parent_fd)  # The caller's original reference really disappears.
    probe = os.open(root / 'tick.lock', os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not world['children'] and owned._spawning is not None and len(owned._lease_fds) == 2
        world['now'] = 101.0
        owned._adopt_spawn()
        world['lost_reap'] = True
        owned.close()
        assert owned._lost_reap and not owned.closed and len(owned._lease_fds) == 2
        assert world['signals'] == [] and world['executors'][0].joined
        # Even after the fake child releases every reference, the lost owner keeps
        # its own lease; logical close never invents kernel/process retirement.
        world['children'][0].finish(0)
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert os.fstat(root_fd)
    finally:
        os.close(probe)


@pytest.mark.parametrize('error', [TimeoutError('synthetic'), KeyboardInterrupt(), SystemExit(7)])
def test_mutation_parent_interruption_is_same_instance_through_cleanup(world, private_marker, error):
    mutation_peer(world)
    owned = owner(world)
    world['sleep_errors'] = [error]
    with pytest.raises(type(error)) as caught:
        mutate(owned, private_marker, world)
    assert caught.value is error and owned._lease_fds
    world['shutdown_errors'] = [TimeoutError('later cleanup deadline')]
    with pytest.raises(type(error)) as cleanup:
        owned.close()
    assert cleanup.value is error and owned.closed and not owned._lease_fds


def test_optional_read_deadline_cannot_extend_original_operation_budget(world):
    world['setup'] = lambda child: setattr(child, 'hold_read', True)
    owned = owner(world)
    with pytest.raises(reader.KeychainReadError, match='DEADLINE'):
        owned.for_account(ACCOUNT).read(deadline=100.02)
    assert world['now'] < 100.1 and len(world['spawns']) == 1
    owned.close()


def test_each_read_is_fresh_reaped_and_private_before_adoption(world):
    owned = owner(world)
    proxy = owned.for_account(ACCOUNT)
    assert callable(proxy.write) and callable(proxy.delete)  # Explicit mutation contract; reads still dispatch only read.
    for number in (1, 2):
        assert proxy.read() == credential(number)
        child = world['children'][-1]
        assert child.returncode == 0 and child.target is None and child.source is None
        assert owned._child is None and owned._request is None and owned._response is None
    requests = [child.requests[0] for child in world['children']]
    assert [value['sequence'] for value in requests] == [1, 2]
    assert len({value['nonce'] for value in requests}) == 2
    for argv, kwargs in world['spawns']:
        assert argv == [reader.PYTHON, '-I', reader.ENTRY, *map(str, kwargs['pass_fds'])]
        assert kwargs['stdin'] == kwargs['stdout'] == kwargs['stderr'] == reader.subprocess.DEVNULL
        assert kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
        assert kwargs['close_fds'] is True and kwargs['start_new_session'] is True
        assert ACCOUNT not in repr(argv) and credential(1).decode() not in repr((argv, kwargs))
    assert not any(value == credential(1) or value == credential(2) for value in vars(owned).values())
    owned.close()
    assert owned.closed and world['executors'][0].joined
    owned.close()


@pytest.mark.parametrize('bad', ['nonce', 'sequence', 'bool-sequence', 'hash', 'extra', 'base64',
                                 'schema', 'raw-size', 'duplicate', 'frame-size', 'truncated', 'trailing'])
def test_untrusted_reply_never_becomes_credential_or_reusable_owner(world, bad):
    def reply(value):
        if bad == 'nonce':
            value['nonce'] = 'f' * 32
        elif bad == 'sequence':
            value['sequence'] += 1
        elif bad == 'bool-sequence':
            value['sequence'] = True
        elif bad == 'hash':
            value['sha256'] = '0' * 64
        elif bad == 'extra':
            value['secret'] = CANARY
        elif bad == 'base64':
            value['raw'] = 'bad='
        elif bad in ('schema', 'raw-size'):
            raw = b'{}' if bad == 'schema' else b' ' * 4097
            value['raw'], value['sha256'] = base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()
        packet = frame(value)
        if bad == 'duplicate':
            raw = packet[4:-1] + b',"kind":"value"}'
            return len(raw).to_bytes(4, 'big') + raw
        if bad == 'frame-size':
            return (8193).to_bytes(4, 'big')
        if bad == 'truncated':
            return packet[:-1]
        return packet + b'X' if bad == 'trailing' else packet

    world['setup'] = lambda child: setattr(child, 'reply', reply)
    owned = owner(world)
    proxy = owned.for_account(ACCOUNT)
    with pytest.raises(keychain.KeychainError) as failure:
        proxy.read()
    assert CANARY not in str(failure.value) and owned._failed
    with pytest.raises(reader.KeychainReadError, match='FAILED'):
        proxy.read()
    assert len(world['spawns']) == 1
    owned.close()
    assert owned.closed


@pytest.mark.parametrize('delay', ['native', 'exit', 'eof', 'reap'])
def test_original_deadline_includes_native_eof_exit_and_actual_reap(world, delay):
    def setup(child):
        child.hold_read = delay == 'native'
        child.hold_exit = delay == 'exit'
        child.hold_eof = delay == 'eof'
        child.reap_allowed = delay != 'reap'
    world['setup'] = setup
    owned = owner(world, deadline=100.04)
    with pytest.raises(reader.KeychainReadError, match='DEADLINE'):
        owned.for_account(ACCOUNT).read()
    assert world['now'] < 100.05 and not owned.closed
    child = world['children'][0]
    child.reap_allowed, child.hold_eof = True, False
    child.finish(0)
    owned.close()
    assert owned.closed


def test_fixed_read_cap_applies_without_caller_deadline(world):
    world['setup'] = lambda child: setattr(child, 'hold_read', True)
    owned = owner(world)
    with pytest.raises(reader.KeychainReadError, match='DEADLINE'):
        owned.for_account(ACCOUNT).read()
    assert 105 <= world['now'] < 105.01
    owned.close()
    assert owned.closed


def test_original_deadline_is_not_restarted_by_a_successful_read(world):
    owned = owner(world, deadline=100.05)
    proxy = owned.for_account(ACCOUNT)
    assert proxy.read() == credential(1)
    world['now'] = 100.045
    world['setup'] = lambda child: setattr(child, 'hold_read', True)
    with pytest.raises(reader.KeychainReadError, match='DEADLINE'):
        proxy.read()
    assert 100.05 <= world['now'] < 100.06 and len(world['children']) == 2


def test_late_value_is_rejected_even_after_output_eof_reap_and_pipe_close(world):
    def check():
        if world['children'] and owned._child is None:
            assert owned._request is None and owned._response is None
            assert world['children'][0].returncode == 0
            world['now'] = 100.1
    owned = owner(world, deadline=100.1, check=check)
    with pytest.raises(reader.KeychainReadError, match='DEADLINE'):
        owned.for_account(ACCOUNT).read()
    assert world['children'][0].returncode == 0 and owned._failed
    owned.close()
    assert owned.closed


def test_partial_response_and_last_sequence_preserve_the_exact_raw(world):
    world['setup'] = lambda child: setattr(child, 'chunk', 3)
    owned = owner(world)
    proxy = owned.for_account(ACCOUNT)
    owned._sequence = 2**32 - 2
    assert proxy.read() == credential(1)
    assert world['children'][0].requests[0]['sequence'] == 2**32 - 1
    with pytest.raises(reader.KeychainReadError, match='INVALID'):
        proxy.read()
    assert len(world['children']) == 1


def test_delayed_spawn_stays_owned_until_settled_killed_and_joined(world):
    world['spawn_after'] = 100.05
    owned = owner(world, deadline=100.02)
    with pytest.raises(reader.KeychainReadError, match='DEADLINE'):
        owned.for_account(ACCOUNT).read()
    assert not world['children'] and not owned.closed and owned._spawning is not None
    owned.close()
    assert owned.closed and world['now'] >= 100.05 and world['executors'][0].joined
    assert len(world['children']) == 1 and world['children'][0].returncode == 0
    assert world['children'][0].requests == []
    assert world['signals'] == [(world['children'][0].pid, signal.SIGTERM)]


def test_repeated_cancellation_keeps_first_instance_and_actual_cleanup(world):
    first = KeyboardInterrupt('synthetic original cancellation')
    fired = []

    def check():
        if world['children'] and not fired:
            fired.append(True)
            raise first

    world['ignore_term'] = True
    world['setup'] = lambda child: setattr(child, 'hold_read', True)
    owned = owner(world, check=check)
    with pytest.raises(KeyboardInterrupt) as failure:
        owned.for_account(ACCOUNT).read()
    assert failure.value is first and not owned.closed
    world['sleep_errors'] = [KeyboardInterrupt('second'), SystemExit('third')]
    world['shutdown_errors'] = [KeyboardInterrupt('fourth')]
    with pytest.raises(KeyboardInterrupt) as cleanup:
        owned.close()
    assert cleanup.value is first and owned.closed and world['executors'][0].joined
    assert [sig for _, sig in world['signals']] == [signal.SIGTERM, signal.SIGKILL]
    assert world['children'][0].returncode == 0
    owned.close()


@pytest.mark.parametrize('site', ['check', 'poll', 'response-read', 'request-write', 'request-close',
                                 'response-close', 'spawn-result', 'spawn-exception', 'spawn-worker', 'sleep'])
def test_parent_timeout_during_copy_keeps_instance_and_all_owned_work(world, monkeypatch, site):
    first = TimeoutError('synthetic original parent deadline')
    injected = []

    def check():
        if owned._child is not None and not injected:
            if site == 'check':
                injected.append(site)
                raise first
            if site in ('request-close', 'response-close'):
                attr = '_request' if site == 'request-close' else '_response'
                setattr(owned, attr, InterruptedPipe(getattr(owned, attr), first))
                injected.append(site)

    if site == 'spawn-worker':
        def fail_spawn(*args, **kwargs):
            raise first
        world['popen'] = fail_spawn
    elif site in ('spawn-result', 'spawn-exception'):
        world['result_errors' if site == 'spawn-result' else 'exception_errors'] = [first]
    elif site == 'sleep':
        world['sleep_errors'] = [first]
    owned = owner(world, check=check)
    if site in ('poll', 'response-read', 'request-write'):
        name = {'poll': 'waitpid', 'response-read': 'read', 'request-write': 'write'}[site]
        actual = getattr(reader.os, name)

        def interrupt_once(*args):
            if not injected and (site == 'poll' or site == 'response-read' and args[0] == owned._response.fileno()
                                 or site == 'request-write' and args[0] == owned._request.fileno()):
                injected.append(site)
                raise first
            return actual(*args)

        monkeypatch.setattr(reader.os, name, interrupt_once)
    with pytest.raises(TimeoutError) as failure:
        owned.for_account(ACCOUNT).read()
    assert failure.value is first and owned._interruption is first
    assert owned._failed and not owned._busy and not owned.closed
    if site in ('spawn-result', 'spawn-exception'):
        assert owned._spawning is world['executors'][0].futures[0]
        assert owned._spawning.finished and owned._child is None
    # A later cleanup failure cannot replace the parent's already-observed timeout.
    world['shutdown_errors'] = [OSError(CANARY)]
    with pytest.raises(TimeoutError) as cleanup:
        owned.close()
    assert cleanup.value is first and not owned.closed
    assert all(child.returncode == 0 for child in world['children'])
    assert owned._spawning is None and owned._child is None
    assert owned._request is None and owned._response is None
    owned.close()
    assert owned.closed and world['executors'][0].joined


@pytest.mark.parametrize('site', ['poll', 'signal', 'pipe-close', 'sleep', 'shutdown'])
@pytest.mark.parametrize('kind', [TimeoutError, OSError])
def test_parent_timeout_during_cleanup_is_preserved_without_changing_oserror_classification(
        world, monkeypatch, site, kind):
    world['setup'] = lambda child: setattr(child, 'hold_read', True)
    world['ignore_term'] = True
    owned = owner(world, deadline=100.02)
    with pytest.raises(reader.KeychainReadError, match='DEADLINE'):
        owned.for_account(ACCOUNT).read()
    first = kind('synthetic cleanup deadline or ordinary failure')
    if site in ('poll', 'signal'):
        name = 'waitpid' if site == 'poll' else 'killpg'
        actual, injected = getattr(reader.os, name), []

        def interrupt_once(*args):
            if not injected:
                injected.append(site)
                raise first
            return actual(*args)

        monkeypatch.setattr(reader.os, name, interrupt_once)
    elif site == 'pipe-close':
        owned._response = InterruptedPipe(owned._response, first)
    else:
        world['sleep_errors' if site == 'sleep' else 'shutdown_errors'] = [first]
    if kind is TimeoutError:
        with pytest.raises(TimeoutError) as failure:
            owned.close()
        assert failure.value is first
    elif site in ('sleep', 'shutdown'):
        with pytest.raises(reader.KeychainReadError, match='^SDC_KEYCHAIN_READ_CHILD$'):
            owned.close()
        assert not owned.closed
        owned.close()
    else:
        owned.close()
    assert owned.closed and world['executors'][0].joined
    assert owned._spawning is None and owned._child is None
    assert owned._request is None and owned._response is None
    assert world['children'][0].returncode == 0
    assert any(sig == signal.SIGKILL for _, sig in world['signals'])


def test_completed_not_found_allows_fresh_copy_after_first_credential_write(world):
    def setup(child):
        if not world['children']:
            child.error = 'SDC_KEYCHAIN_NOT_FOUND'
    world['setup'] = setup
    owned = owner(world)
    proxy = owned.for_account(ACCOUNT)
    with pytest.raises(keychain.KeychainError, match='SDC_KEYCHAIN_NOT_FOUND'):
        proxy.read()
    assert not owned._failed and world['children'][0].returncode == 0
    assert proxy.read() == credential(2)
    assert len(world['children']) == 2


@pytest.mark.parametrize('code', ['SDC_KEYCHAIN_LOCKED', 'SDC_KEYCHAIN_AUTH_FAILED', 'SDC_KEYCHAIN_UNAVAILABLE'])
def test_completed_native_failure_is_fixed_and_poisons_retries(world, code):
    world['setup'] = lambda child: setattr(child, 'error', code)
    owned = owner(world)
    proxy = owned.for_account(ACCOUNT)
    with pytest.raises(keychain.KeychainError, match=code):
        proxy.read()
    assert owned._failed and world['children'][0].returncode == 0
    with pytest.raises(reader.KeychainReadError, match='FAILED'):
        proxy.read()
    assert len(world['children']) == 1


def test_nonzero_exit_or_lost_reap_cannot_be_closed_success(world):
    world['setup'] = lambda child: setattr(child, 'exit_code', 1)
    owned = owner(world)
    with pytest.raises(reader.KeychainReadError, match='CHILD'):
        owned.for_account(ACCOUNT).read()
    assert not owned.closed
    owned.close()
    assert owned.closed
    world['setup'] = lambda child: None
    world['lost_reap'] = True
    lost = owner(world)
    with pytest.raises(reader.KeychainReadError, match='CHILD'):
        lost.for_account(ACCOUNT).read()
    assert lost._lost_reap and not lost.closed and not lost._retired()


def test_lost_reap_close_returns_without_claiming_reap_or_allowing_new_reads(world):
    world['lost_reap'] = True
    owned = owner(world)
    proxy = owned.for_account(ACCOUNT)
    with pytest.raises(reader.KeychainReadError, match='CHILD'):
        proxy.read()
    child, request, response = owned._child, owned._request, owned._response
    response_fd = response.fileno()
    # A bounded old-code oracle: another sleep must fail, never hang the suite.
    world['sleep_errors'] = [RuntimeError('lost reap must return to the caller')]
    owned.close()
    assert request.closed and response.closed
    with pytest.raises(OSError):
        os.fstat(response_fd)
    assert owned._spawning is None and owned._executor is None
    assert world['executors'][0].joined
    assert owned._child is child and child.returncode is None
    assert owned._lost_reap and owned._failed and not owned._closed and not owned.closed
    assert not owned._retired()
    with pytest.raises(reader.KeychainReadError, match='FAILED'):
        proxy.read()
    with pytest.raises(reader.KeychainReadError, match='FAILED'):
        owned.for_account(ACCOUNT)
    owned.close()
    assert not owned.closed and len(world['spawns']) == 1 and world['signals'] == []


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit])
def test_lost_reap_close_preserves_read_cancellation_after_repeated_cleanup_interrupts(world, kind):
    first = kind('synthetic original cancellation')

    def check():
        if world['children']:
            raise first

    owned = owner(world, check=check)
    with pytest.raises(kind) as read_failure:
        owned.for_account(ACCOUNT).read()
    assert read_failure.value is first
    world['lost_reap'] = True
    world['shutdown_errors'] = [KeyboardInterrupt('second'), SystemExit('third')]
    world['sleep_errors'] = [RuntimeError('lost reap must not wait for a new reap')]
    with pytest.raises(kind) as cleanup:
        owned.close()
    assert cleanup.value is first
    assert owned._lost_reap and owned._failed and not owned.closed
    assert owned._request.closed and owned._response.closed and world['executors'][0].joined
    assert owned._child.returncode is None and world['signals'] == []
    owned.close()
    assert not owned.closed


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit, TimeoutError])
def test_lost_reap_close_keeps_first_cleanup_interruption_over_later_failure(world, kind):
    world['lost_reap'] = True
    owned = owner(world)
    with pytest.raises(reader.KeychainReadError, match='CHILD'):
        owned.for_account(ACCOUNT).read()
    first = kind('synthetic first cleanup interruption')
    world['shutdown_errors'] = [first, OSError('synthetic later cleanup failure')]
    world['sleep_errors'] = [RuntimeError('lost reap must return after settled cleanup')]
    with pytest.raises(kind) as failure:
        owned.close()
    assert failure.value is first
    assert owned._executor is world['executors'][0] and not owned._executor.joined
    assert owned._request.closed and owned._response.closed
    assert owned._lost_reap and owned._failed and not owned.closed
    owned.close()
    assert world['executors'][0].joined and owned._executor is None
    assert owned._child.returncode is None and not owned.closed and world['signals'] == []


@pytest.mark.parametrize('body_kind', [None, KeyboardInterrupt, SystemExit, TimeoutError])
def test_store_lost_reap_close_holds_root_fd_and_preserves_original_body(world, tmp_path, body_kind):
    world['lost_reap'] = True
    owned = owner(world)
    store = storage.Store(tmp_path / 'private-lost-reap', create=True, credential_reader=owned)
    fd, initial = store.fd, os.fstat(store.fd)
    try:
        with pytest.raises(reader.KeychainReadError, match='CHILD'):
            owned.for_account(ACCOUNT).read()
        world['sleep_errors'] = [RuntimeError('Store must regain control without another reap')]
        if body_kind is None:
            with pytest.raises(keychain.KeychainError, match='^SDC_KEYCHAIN_UNAVAILABLE$'):
                store.close()
        else:
            first = body_kind('synthetic original body failure')
            with pytest.raises(body_kind) as failure:
                with store:
                    raise first
            assert failure.value is first
        current = os.fstat(fd)
        assert (current.st_dev, current.st_ino) == (initial.st_dev, initial.st_ino)
        assert not store._closed and not owned.closed
        assert owned._lost_reap and owned._failed and owned._child.returncode is None
        assert owned._request.closed and owned._response.closed and world['executors'][0].joined
        assert world['signals'] == []
        with pytest.raises(keychain.KeychainError, match='^SDC_KEYCHAIN_UNAVAILABLE$'):
            store.close()
        assert not store._closed and os.fstat(fd).st_ino == initial.st_ino
    finally:
        # Repair only the synthetic OS so the test's real directory fd is not leaked.
        world['lost_reap'] = False
        world['sleep_errors'].clear()
        owned._lost_reap = False
        owned._interruption = None
        for child in world['children']:
            child.finish(0)
        store.close()
    with pytest.raises(OSError):
        os.fstat(fd)


def test_account_thread_and_serial_ownership_are_enforced(world, monkeypatch):
    owned = owner(world)
    proxy = owned.for_account(ACCOUNT)
    assert owned.for_account(ACCOUNT) is proxy
    owned._busy = True
    with pytest.raises(reader.KeychainReadError, match='BUSY'):
        proxy.read()
    owned._busy = False
    with pytest.raises(reader.KeychainReadError, match='INVALID'):
        owned.for_account(OTHER)
    another = owner(world)
    with monkeypatch.context() as local:
        local.setattr(reader.threading, 'get_ident', lambda: another._thread + 1)
        with pytest.raises(reader.KeychainReadError, match='OWNER_REQUIRED'):
            another.for_account(ACCOUNT)
    assert not world['spawns']


@pytest.mark.parametrize('deadline', [float('inf'), float('nan'), True, -1, 100])
def test_invalid_initial_budget_never_starts_any_worker(world, deadline):
    with pytest.raises(reader.KeychainReadError):
        owner(world, deadline=deadline)
    assert not world['spawns'] and not world['executors']


def test_failed_spawn_redacts_dependency_error_and_joins_worker(world):
    def fail(*args, **kwargs):
        raise OSError(CANARY)
    world['popen'] = fail
    owned = owner(world)
    with pytest.raises(reader.KeychainReadError, match='SPAWN') as failure:
        owned.for_account(ACCOUNT).read()
    assert CANARY not in str(failure.value) and not owned.closed
    owned.close()
    assert owned.closed and world['executors'][0].joined


class StoreReader:
    """A completed-response oracle, independent of process/native implementation."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.accounts, self.proofs = [], []
        self.closed, self.finish_close = False, True
        self.close_error, self.before_close, self.close_calls = None, lambda: None, 0

    def for_account(self, account):
        assert not self.closed
        assert not self.accounts or account == self.accounts[0]
        self.accounts.append(account)
        return self

    def read(self, *, deadline=None):
        value = self.responses.pop(0)
        if callable(value):
            value = value()
        if isinstance(value, keychain.KeychainError):
            self.proofs.append({'code': value.code, 'reaped': value.code == 'SDC_KEYCHAIN_NOT_FOUND'})
            raise value
        assert type(value) is bytes
        self.proofs.append({'sha256': hashlib.sha256(value).hexdigest(), 'reaped': True})
        return value

    def close(self):
        self.close_calls += 1
        self.before_close()
        self.closed = self.finish_close
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def stores(tmp_path, monkeypatch):
    opened = []

    def forbidden(*args, **kwargs):
        pytest.fail('Store integration permits only the injected fake owner')

    monkeypatch.setattr(reader, 'KeychainOwner', forbidden)
    monkeypatch.setattr(reader.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(keychain, '_MacKeychain', forbidden)
    monkeypatch.setattr(storage, 'KeychainCredentialStore', forbidden)
    monkeypatch.setattr(storage.signal, 'getitimer', lambda *_: (0.0, 0.0))

    def create(owned=None, **options):
        store = storage.Store(tmp_path / f'private-{len(opened)}', create=True,
                              credential_reader=owned, **options)
        opened.append(store)
        store.write('endpoint.json', {'origin': 'https://synthetic.invalid',
                                     'audience': 'sdc:synthetic', 'environment': 'synthetic'})
        store.enable_customer()
        return store

    yield create
    for store in opened:
        owned = store._credential_reader
        if isinstance(owned, StoreReader):
            owned.finish_close, owned.close_error = True, None
            owned.before_close = lambda: None
        store.close()


def test_store_each_read_uses_fresh_owner_bytes_bound_to_one_account(stores):
    owned = StoreReader(credential(1), credential(2))
    store = stores(owned)
    account = store.read('credential-store.json')['account']
    first = store.read('credential')
    first['key'] = 'ff' * 32  # A returned dict is not a credential cache.
    assert store.read('credential') == {'key': '02' * 32}
    assert owned.accounts == [account, account]
    assert owned.proofs == [{'sha256': hashlib.sha256(credential(n)).hexdigest(), 'reaped': True}
                            for n in (1, 2)]
    assert store._keychain_store is None


def test_store_completed_not_found_preserves_proof_and_next_read_is_fresh(stores):
    missing = keychain.KeychainError('SDC_KEYCHAIN_NOT_FOUND')
    owned = StoreReader(missing, credential(2))
    store = stores(owned)
    store._write_file('credential', {'key': 'ff' * 32})  # Must not become a fallback.
    sentinel = object()
    assert store.read('credential', sentinel) is sentinel
    proof = owned.proofs[0]
    assert proof == {'code': 'SDC_KEYCHAIN_NOT_FOUND', 'reaped': True}
    assert store.read('credential') == {'key': '02' * 32}
    assert owned.proofs[0] is proof and owned.accounts[0] == owned.accounts[1]


@pytest.mark.parametrize('error', [reader.KeychainReadError('DEADLINE'), reader.KeychainReadError('PROTOCOL'),
                                  keychain.KeychainError('SDC_KEYCHAIN_LOCKED')])
def test_store_failed_or_unfinished_read_is_never_missing_or_plaintext_fallback(stores, error):
    owned = StoreReader(error)
    store = stores(owned)
    store._write_file('credential', {'key': 'ff' * 32})
    with pytest.raises(keychain.KeychainError) as failure:
        store.read('credential', object())
    assert failure.value is error
    assert owned.proofs == [{'code': error.code, 'reaped': False}]
    assert store._keychain_store is None


@pytest.mark.parametrize('change', ['account', 'endpoint', 'root', 'missing-marker'])
@pytest.mark.parametrize('not_found', [False, True])
def test_store_rejects_value_and_not_found_after_marker_changes_during_read(stores, change, not_found):
    def mutate():
        if change == 'account':
            marker = store.read('credential-store.json')
            marker['account'] = OTHER if marker['account'] != OTHER else ACCOUNT
            store._write_file('credential-store.json', marker)
        elif change == 'endpoint':
            store._write_file('endpoint.json', {'origin': 'https://other.invalid',
                                              'audience': 'sdc:synthetic', 'environment': 'synthetic'})
        elif change == 'root':
            store.root.rename(store.root.with_name('moved-private'))
            store.root.mkdir(mode=0o700)
        else:
            os.unlink('credential-store.json', dir_fd=store.fd)
        return keychain.KeychainError('SDC_KEYCHAIN_NOT_FOUND') if not_found else credential(1)

    owned = StoreReader(mutate)
    store = stores(owned)
    with pytest.raises(ValueError, match='^SDC_CREDENTIAL_STORE_(INVALID|REQUIRED)$'):
        store.read('credential', object())
    assert len(owned.accounts) == 1 and len(owned.proofs) == 1
    assert store._keychain_store is None


@pytest.mark.parametrize('closed', [True, False])
@pytest.mark.parametrize('error_kind', ['none', 'ordinary', 'cancel'])
def test_store_releases_fd_only_after_owner_closed_even_when_cleanup_raises(stores, closed, error_kind):
    owned = StoreReader()
    store = stores(owned)
    fd, initial = store.fd, os.fstat(store.fd)
    seen = []

    def retained_during_cleanup():
        current = os.fstat(fd)
        assert (current.st_dev, current.st_ino) == (initial.st_dev, initial.st_ino)
        seen.append(True)

    error = {'none': None, 'ordinary': RuntimeError('synthetic cleanup'),
             'cancel': KeyboardInterrupt('synthetic cleanup cancellation')}[error_kind]
    owned.before_close, owned.finish_close, owned.close_error = retained_during_cleanup, closed, error
    if error is not None:
        with pytest.raises(type(error)) as failure:
            store.close()
        assert failure.value is error
    elif not closed:
        with pytest.raises(keychain.KeychainError, match='^SDC_KEYCHAIN_UNAVAILABLE$'):
            store.close()
    else:
        store.close()
    assert seen == [True] and store._closed is closed
    if closed:
        with pytest.raises(OSError):
            os.fstat(fd)
        store.close()
        assert owned.close_calls == 1
    else:
        assert os.fstat(fd).st_ino == initial.st_ino
        owned.finish_close, owned.close_error = True, None
        store.close()
        assert owned.closed and store._closed and owned.close_calls == 2
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(('body_kind', 'closed'), [('ordinary', True), ('cancel', False),
                                                 ('timeout', False), ('exit', True)])
@pytest.mark.parametrize('cleanup_kind', ['ordinary', 'cancel'])
def test_store_context_preserves_original_body_instance_and_retains_unknown_cleanup_fd(
        stores, body_kind, closed, cleanup_kind):
    owned = StoreReader()
    store = stores(owned)
    fd = store.fd
    body = {'ordinary': ValueError('synthetic body'), 'cancel': KeyboardInterrupt('synthetic body'),
            'timeout': TimeoutError('synthetic body'), 'exit': SystemExit('synthetic body')}[body_kind]
    cleanup = RuntimeError('synthetic cleanup') if cleanup_kind == 'ordinary' else KeyboardInterrupt('synthetic cleanup')
    owned.finish_close, owned.close_error = closed, cleanup
    owned.before_close = lambda: os.fstat(fd)
    try:
        with pytest.raises(type(body)) as failure:
            with store:
                raise body
        assert failure.value is body and owned.close_calls == 1
        assert store._closed is closed
        if closed:
            with pytest.raises(OSError):
                os.fstat(fd)
        else:
            os.fstat(fd)
    finally:
        owned.finish_close, owned.close_error = True, None
        store.close()
    assert store._closed and owned.closed


@pytest.mark.parametrize(('deadline', 'alarm', 'expected'), [(None, 20, 120), (130, 20, 120),
                                                         (110, 20, 110), (None, 0, None), (140, 0, 140)])
def test_store_lazily_creates_one_reader_with_original_shortened_deadline(
        stores, monkeypatch, deadline, alarm, expected):
    owned = StoreReader(credential(1), credential(2))
    created = []

    def factory(**options):
        created.append(options)
        return owned

    monkeypatch.setattr(storage.time, 'monotonic', lambda: 100)
    monkeypatch.setattr(storage.signal, 'getitimer', lambda *_: (alarm, 0))
    monkeypatch.setattr(reader, 'KeychainOwner', factory)
    store = stores(credential_deadline=deadline)
    assert created == [] and store._credential_reader is None
    monkeypatch.setattr(storage.time, 'monotonic', lambda: 200)
    monkeypatch.setattr(storage.signal, 'getitimer', lambda *_: (999, 0))
    assert store.read('credential') == {'key': '01' * 32}
    assert store.read('credential') == {'key': '02' * 32}
    assert created == [{'deadline': expected}]
    assert store._credential_reader is owned


def test_store_explicit_native_test_provider_is_not_silently_replaced_by_reader(stores):
    class Backend:
        def __init__(self):
            self.calls = []

        def read(self):
            self.calls.append('read')
            return credential(1)

    backend = Backend()
    accounts = []

    def factory(account):
        accounts.append(account)
        return backend

    store = stores(keychain_factory=factory)
    assert store.read('credential') == {'key': '01' * 32}
    assert store.read('credential') == {'key': '01' * 32}
    assert accounts == [store.read('credential-store.json')['account']]
    assert backend.calls == ['read', 'read'] and store._credential_reader is None


def test_store_owner_alone_serves_reads_and_approved_mutations_without_a_separate_provider(stores):
    class Owned(StoreReader):
        """The fixed owner-proxy shape: real pending lease and verified CAS around a fake native call."""
        def __init__(self, *responses):
            super().__init__(*responses)
            self.mutations = []

        def write(self, raw, **lease):
            return self._mutate('write', raw, **lease)

        def delete(self, **lease):
            return self._mutate('delete', None, **lease)

        def _mutate(self, kind, raw, *, intent_id, marker_sha256, root_fd, tick_fd, deadline):
            mutation.pending_marker(root_fd, tick_fd, intent_id=intent_id, marker_sha256=marker_sha256,
                                    account=self.accounts[0])
            self.mutations.append((kind, raw))
            verified = mutation.mark_native_verified(root_fd, tick_fd, intent_id=intent_id,
                                                     marker_sha256=marker_sha256, now_ms=int(time.time() * 1000))
            return {'intent_id': intent_id, 'marker_sha256': mutation.marker_sha256(verified)}

    missing = keychain.KeychainError('SDC_KEYCHAIN_NOT_FOUND')
    owned = Owned(missing, credential(1), credential(1), missing)
    store = stores(owned, keychain_factory=lambda _account: pytest.fail('a separate provider was used'))
    after = {'claim_id': '33333333-3333-4333-8333-333333333333', 'device_id': None, 'key_id': None, 'rotation_id': None}
    # Without the tick lock and an explicit approval nothing reaches any provider.
    with pytest.raises(ValueError, match='SDC_CREDENTIAL_LOCK_REQUIRED'):
        store.write('credential', {'key': '01' * 32}, mutation={'purpose': 'initial-key', 'after': after})
    with store.lock(), pytest.raises(ValueError, match='SDC_CREDENTIAL_APPROVAL_REQUIRED'):
        store.remove_credential(mutation={'purpose': 'delete'})
    assert owned.accounts == [] and owned.mutations == []
    approval = {'confirm_human': True, 'reason': 'synthetic owner mutation', 'now_ms': 0}
    store.authorize_credential_mutation(operation='connect', **approval)
    with store.lock():
        store.write('credential', {'key': '01' * 32}, mutation={'purpose': 'initial-key', 'after': after})
    assert store.read('credential') == {'key': '01' * 32}
    store.authorize_credential_mutation(operation='disconnect', **approval)
    with store.lock():
        store.remove_credential(mutation={'purpose': 'delete'})
    assert store.read('credential') is None
    assert owned.mutations == [('write', credential(1)), ('delete', None)]
    assert owned.responses == [] and store._credential_reader is owned
    assert len(set(owned.accounts)) == 1
