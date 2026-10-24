"""Verify public synthetic signatures without credentials, network or a remote runner."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

import pytest

import sdc_support_signatures as signatures

CANARY = 'SYNTHETIC_PRIVATE_DO_NOT_ECHO'
VECTOR = json.loads((Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/support/signature-vectors.json').read_text())


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def verify(envelope=None, *, index=0, pins=None, now=None, purpose=None):
    vector = VECTOR['vectors'][index]
    return signatures.verify_envelope(encoded(envelope if envelope is not None else vector['envelope']),
                                     purpose=purpose or vector['purpose'], pins=VECTOR['pinned_keys'] if pins is None else pins,
                                     now_ms=VECTOR['now'] if now is None else now)


@pytest.mark.parametrize('index', [0, 1])
def test_shared_ps256_vector_with_os_crypto(index):
    assert verify(index=index) == VECTOR['vectors'][index]['payload']


@pytest.mark.parametrize('change', ['payload', 'signature', 'purpose'])
def test_signed_content_changes_are_rejected_by_os_crypto(change):
    envelope = copy.deepcopy(VECTOR['vectors'][0]['envelope'])
    purpose = 'operation'
    if change == 'payload':
        payload = copy.deepcopy(VECTOR['vectors'][0]['payload'])
        payload['params']['diagnostic_id'] = 'notification.health.v1'
        envelope['payload'] = b64(encoded(payload))
    elif change == 'signature':
        signature = bytearray(base64.urlsafe_b64decode(envelope['signature']))
        signature[0] ^= 1
        envelope['signature'] = b64(signature)
    else:
        purpose = envelope['purpose'] = 'execution-grant'
    with pytest.raises(signatures.SignatureError, match='^SDC_SUPPORT_SIGNATURE_INVALID$'):
        verify(envelope, purpose=purpose)


@pytest.fixture
def no_crypto(monkeypatch):
    monkeypatch.setattr(signatures, '_verify_pss', lambda *a: pytest.fail('invalid input reached crypto'))


@pytest.mark.parametrize('raw', [b'', b'{}', b'x' * 16385, b'[]', b'{"alg":1,"alg":2}', b'{"' + CANARY.encode() + b'":NaN}', b'\xff', 'secret', bytearray(b'{}'), None])
def test_invalid_outer_wire_is_fixed_and_never_reaches_crypto(no_crypto, raw):
    with pytest.raises(signatures.SignatureError) as caught:
        signatures.verify_envelope(raw, purpose='operation', pins=VECTOR['pinned_keys'], now_ms=VECTOR['now'])
    assert str(caught.value) == 'SDC_SUPPORT_SIGNATURE_INVALID'
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize('field,value', [('alg', 'none'), ('alg', 'RS256'), ('purpose', 'execution-grant'),
    ('kid', 'wrong'), ('kid', 'x\n'), ('kid', 'x' * 65), ('signature', ''), ('signature', 'YQ'),
    ('signature', 'x' * 513), ('payload', 'YR'), ('payload', 'YQ='), ('payload', CANARY + '\n'), ('extra', CANARY)])
def test_closed_envelope_algorithms_and_base64_are_enforced(no_crypto, field, value):
    envelope = {**VECTOR['vectors'][0]['envelope'], field: value}
    with pytest.raises(signatures.SignatureError):
        verify(envelope)


@pytest.mark.parametrize('payload', [b'{"a":1,"a":2}', b'{"a":-0}', b'{"a":1.0}', b'{"a":1e0}',
    b'{"a":9007199254740992}', b'{"a":-9007199254740992}', b'{"a":NaN}', b'{"a":true }',
    b'{"z":1,"a":2}', b'{"a":"\\u0061"}', b'{"a":"\\u00e9"}', b'{"a":"\\/"}', b'[]',
    b'{"a":' + b'[' * 9 + b'0' + b']' * 9 + b'}', b'{"a":"' + b'x' * 8192 + b'"}'])
def test_payload_is_bounded_ascii_safe_integer_strict_cjson(no_crypto, payload):
    envelope = {**VECTOR['vectors'][0]['envelope'], 'payload': b64(payload)}
    with pytest.raises(signatures.SignatureError):
        verify(envelope)


@pytest.mark.parametrize('now', [True, None, -1, 1.0, 2**53])
def test_caller_time_is_strict(no_crypto, now):
    with pytest.raises(signatures.SignatureError):
        signatures.verify_envelope(encoded(VECTOR['vectors'][0]['envelope']), purpose='operation', pins=VECTOR['pinned_keys'], now_ms=now)


@pytest.mark.parametrize('change', ['expired', 'future', 'bool_time', 'reversed', 'purpose', 'duplicate', 'unknown', 'extra', 'empty', 'bad_spki'])
def test_pins_require_trusted_unique_current_key_and_purpose(no_crypto, change):
    pins = copy.deepcopy(VECTOR['pinned_keys'])
    if change == 'expired':
        pins[0]['expires_at'] = VECTOR['now']
    elif change == 'future':
        pins[0]['not_before'] = VECTOR['now'] + 1
    elif change == 'bool_time':
        pins[0]['not_before'] = True
    elif change == 'reversed':
        pins[0]['expires_at'] = pins[0]['not_before']
    elif change == 'purpose':
        pins[0]['purposes'] = ['execution-grant']
    elif change == 'duplicate':
        pins.append(copy.deepcopy(pins[0]))
    elif change == 'unknown':
        pins[0]['kid'] = 'other'
    elif change == 'extra':
        pins[0]['unexpected'] = CANARY
    elif change == 'bad_spki':
        pins[0]['spki'] = CANARY
    else:
        pins = []
    with pytest.raises(signatures.SignatureError):
        verify(pins=pins)


@pytest.mark.parametrize('change', ['truncated', 'trailing', 'oid', 'null', 'unused_bits', 'modulus_bits', 'even_modulus', 'exponent', 'nonminimal_length'])
def test_der_is_one_rsa3072_e65537_spki(no_crypto, change):
    pins = copy.deepcopy(VECTOR['pinned_keys'])
    raw = bytearray(base64.urlsafe_b64decode(pins[0]['spki'] + '=' * (-len(pins[0]['spki']) % 4)))
    if change == 'truncated':
        raw = raw[:-1]
    elif change == 'trailing':
        raw += b'\0'
    elif change == 'oid':
        raw[16] ^= 1
    elif change == 'null':
        raw[17] = 4
    elif change == 'unused_bits':
        raw[23] = 1
    elif change == 'modulus_bits':
        start = raw.index(bytes.fromhex('0282018100')) + 5
        raw[start] &= 127
    elif change == 'even_modulus':
        raw[-6] &= 254
    elif change == 'exponent':
        raw[-1] = 3
    else:
        raw[1] = 0x83
    pins[0]['spki'] = b64(raw)
    with pytest.raises(signatures.SignatureError):
        verify(pins=pins)


@pytest.fixture
def crypto_clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(signatures.time, 'monotonic', lambda: now[0])
    return now


def test_crypto_process_receives_only_fixed_arguments_and_decoded_cjson(monkeypatch):
    paths, messages = [], []
    monkeypatch.setenv('SECRET_CANARY', CANARY)
    class Child:
        returncode = 0
        stdin = io.BytesIO()
        def communicate(self, *, input, timeout):
            messages.append(input)
            assert 0 < timeout <= 0.05
            self.stdin.close()
    def popen(argv, **kwargs):
        assert argv[:3] == ['/usr/bin/openssl', 'dgst', '-sha256']
        assert argv[-6:] == ['-sigopt', 'rsa_padding_mode:pss', '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256']
        assert kwargs == {'stdin': subprocess.PIPE, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
                          'env': {'LC_ALL': 'C', 'OPENSSL_CONF': '/dev/null'}, 'start_new_session': True}
        assert CANARY not in str(argv) + str(kwargs)
        for flag in ('-verify', '-signature'):
            path = Path(argv[argv.index(flag) + 1])
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
            paths.append(path)
        return Child()
    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    assert verify() == VECTOR['vectors'][0]['payload']
    assert messages == [VECTOR['vectors'][0]['signed_ascii'].encode('ascii')]
    assert all(not path.exists() and not path.parent.exists() for path in paths)


@pytest.mark.parametrize('failure', ['timeout', 'cancel', 'spawn', 'unknown', 'nonzero'])
def test_crypto_failure_cleans_temps_reaps_and_redacts(monkeypatch, crypto_clock, failure, capsys):
    paths, stopped = [], []
    class Child:
        returncode = 1 if failure == 'nonzero' else None
        stdin = io.BytesIO()
        def communicate(self, **kwargs):
            if failure == 'timeout':
                crypto_clock[0] += kwargs['timeout']
                raise subprocess.TimeoutExpired(CANARY, 5, output=CANARY, stderr=CANARY)
            if failure == 'cancel':
                raise KeyboardInterrupt(CANARY)
            if failure == 'unknown':
                raise RuntimeError(CANARY)
            self.stdin.close()
    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        if failure == 'spawn':
            raise OSError(CANARY)
        return Child()
    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    def stop(child):
        stopped.append(child)
        child.returncode = -9
        child.stdin.close()
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', stop)
    expected = KeyboardInterrupt if failure == 'cancel' else signatures.SignatureError
    with pytest.raises(expected) as caught:
        verify()
    assert CANARY not in str(caught.value)
    assert bool(stopped) == (failure in ('timeout', 'cancel', 'unknown'))
    assert all(not path.exists() for path in paths)
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err


@pytest.mark.parametrize('failure', ['timeout', 'cancel', 'unknown'])
def test_crypto_failure_retains_owner_and_temps_until_actual_reap(monkeypatch, crypto_clock, failure):
    waiting, release = threading.Event(), threading.Event()
    paths, outcomes = [], []

    class Child:
        returncode = None
        stdin = io.BytesIO()
        waits = 0

        def communicate(self, **kwargs):
            if failure == 'timeout':
                crypto_clock[0] += kwargs['timeout']
                raise subprocess.TimeoutExpired(CANARY, 5)
            if failure == 'cancel':
                raise KeyboardInterrupt(CANARY)
            raise RuntimeError(CANARY)

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise InterruptedError(CANARY)
            waiting.set()
            if not release.wait(timeout):
                raise subprocess.TimeoutExpired(CANARY, timeout)
            self.returncode = -9
            return self.returncode

    child = Child()

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        return child

    def run():
        try:
            verify()
        except BaseException as exc:
            outcomes.append((type(exc), str(exc)))

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', lambda child: None)
    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert waiting.wait(2), 'verification returned before reaping its child'
        assert child.stdin.closed and child.returncode is None
        assert owner.is_alive() and not outcomes
        assert paths and all(path.exists() for path in paths)
    finally:
        release.set()
        owner.join(3)
    assert not owner.is_alive() and child.returncode == -9
    expected = KeyboardInterrupt if failure == 'cancel' else signatures.SignatureError
    assert len(outcomes) == 1 and outcomes[0][0] is expected
    assert CANARY not in outcomes[0][1]
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize('interrupted_stop', [False, True])
def test_crypto_retries_stop_while_its_child_remains_unreaped(monkeypatch, interrupted_stop):
    paths, stops = [], []

    class Child:
        returncode = None
        stdin = io.BytesIO()
        waits = 0

        def communicate(self, **kwargs):
            raise RuntimeError(CANARY)

        def wait(self, timeout):
            self.waits += 1
            if self.waits > 20:
                # Bound the regression itself if the stop retry is missing.
                self.returncode = -9
                return self.returncode
            raise subprocess.TimeoutExpired(CANARY, timeout)

    child = Child()

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        return child

    def stop(owned):
        assert owned is child and child.returncode is None
        assert all(path.exists() for path in paths)
        stops.append(child.waits)
        if len(stops) == 1:
            if interrupted_stop:
                raise OSError(CANARY)
            return
        assert child.stdin.closed
        child.returncode = -9

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', stop)
    monkeypatch.setattr(signatures.time, 'sleep', lambda _: None)
    with pytest.raises(signatures.SignatureError) as caught:
        verify()
    assert CANARY not in str(caught.value)
    assert stops == [0, 20]
    assert child.returncode == -9 and child.stdin.closed
    assert all(not path.exists() for path in paths)


@pytest.fixture
def fake_process_signals(monkeypatch):
    handlers = {signal.SIGINT: signal.default_int_handler, signal.SIGALRM: lambda *_: None}
    changes, forbidden_calls = [], []

    def replace(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        changes.append((signum, handler))
        return previous

    monkeypatch.setattr(signal, 'getsignal', handlers.__getitem__)
    monkeypatch.setattr(signal, 'signal', replace)
    def forbidden(*_args, **_kwargs):
        forbidden_calls.append(True)
        pytest.fail('signature verifier must not own process timers or signal masks')
    for name in ('alarm', 'setitimer', 'getitimer', 'pthread_sigmask'):
        monkeypatch.setattr(signal, name, forbidden)
    yield handlers, changes
    assert not forbidden_calls


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGALRM], ids=['sigint', 'sigalrm'])
@pytest.mark.parametrize('boundary', ['spawn_return', 'reap_condition', 'successful_communicate', 'temp_cleanup'])
def test_crypto_defers_repeated_signals_until_reap_pipe_and_temp_cleanup(monkeypatch, fake_process_signals,
                                                                      crypto_clock, signum, boundary):
    handlers, changes = fake_process_signals
    paths, delivered, replayed = [], [], []
    frame = object()
    phase = 'starting'
    injected = False

    class DeadlineExpired(BaseException):
        pass

    def original(received, _frame):
        replayed.append((received, child._returncode, child.stdin.closed,
                         all(not path.exists() for path in paths)))
        if received == signal.SIGINT:
            raise KeyboardInterrupt(CANARY)
        raise DeadlineExpired()

    handlers.update({signal.SIGINT: original, signal.SIGALRM: original})
    originals = dict(handlers)

    def deliver_twice():
        nonlocal injected
        injected = True
        for _ in range(2):
            handlers[signum](signum, frame)
            delivered.append((child._returncode, child.stdin.closed,
                              all(path.exists() for path in paths), len(replayed)))

    class InputPipe(io.BytesIO):
        close_calls = 0

        def close(self):
            self.close_calls += 1
            if boundary == 'reap_condition' and self.close_calls == 1:
                raise OSError(CANARY)
            super().close()

    class Child:
        _returncode = None
        stdin = InputPipe()
        waits = 0
        communications = 0

        @property
        def returncode(self):
            if not injected and boundary == 'reap_condition' and phase == 'reaping':
                deliver_twice()
            return self._returncode

        def communicate(self, **kwargs):
            self.communications += 1
            if boundary == 'reap_condition':
                crypto_clock[0] += kwargs['timeout']
                raise subprocess.TimeoutExpired(CANARY, 5)
            self.stdin.close()
            self._returncode = 0
            if boundary == 'successful_communicate':
                deliver_twice()

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise InterruptedError(CANARY)
            self._returncode = -9
            return self._returncode

    child = Child()

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        if boundary == 'spawn_return':
            deliver_twice()
        return child

    def stop(_child):
        nonlocal phase
        phase = 'reaping'

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', stop)
    if boundary == 'temp_cleanup':
        cleanup = signatures.tempfile.TemporaryDirectory.cleanup
        def cleanup_with_signals(directory):
            if Path(directory.name) in paths:
                deliver_twice()
            cleanup(directory)
        monkeypatch.setattr(signatures.tempfile.TemporaryDirectory, 'cleanup', cleanup_with_signals)
    expected = KeyboardInterrupt if signum == signal.SIGINT else DeadlineExpired
    with pytest.raises(expected) as caught:
        verify()

    code = -9 if boundary in ('spawn_return', 'reap_condition') else 0
    assert paths and injected and len(delivered) == 2
    assert all(temps_present and replay_count == 0 for _, _, temps_present, replay_count in delivered)
    if boundary in ('spawn_return', 'reap_condition'):
        assert all(returncode is None and not closed for returncode, closed, _, _ in delivered)
    assert replayed == [(signum, code, True, True)]
    assert handlers == originals and changes
    assert child._returncode == code and child.stdin.closed
    assert all(not path.exists() for path in paths)
    assert CANARY not in str(caught.value)
    if boundary == 'reap_condition':
        assert child.waits >= 2 and child.stdin.close_calls >= 2
    if boundary == 'spawn_return':
        assert child.communications == 0


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGALRM], ids=['sigint', 'sigalrm'])
@pytest.mark.parametrize('disposition', [signal.SIG_IGN, signal.SIG_DFL], ids=['ignored', 'default'])
def test_crypto_preserves_noncallable_signals(monkeypatch, fake_process_signals, signum, disposition):
    handlers, changes = fake_process_signals
    handlers[signum] = disposition
    originals = dict(handlers)
    paths, observed = [], []

    class Child:
        returncode = None
        stdin = io.BytesIO()

        def communicate(self, **kwargs):
            observed.append(handlers[signum])
            self.stdin.close()
            self.returncode = 0

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        observed.append(handlers[signum])
        return Child()

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    assert verify() == VECTOR['vectors'][0]['payload']
    assert observed == [disposition, disposition]
    assert all(changed != signum for changed, _ in changes)
    assert handlers == originals
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGALRM], ids=['sigint', 'sigalrm'])
def test_crypto_unknown_native_handler_fails_before_spawn(monkeypatch, fake_process_signals, signum):
    handlers, _changes = fake_process_signals
    handlers[signum] = None
    originals = dict(handlers)
    spawned = []
    monkeypatch.setattr(signatures.subprocess, 'Popen', lambda *_args, **_kwargs: spawned.append(True))
    with pytest.raises(signatures.SignatureError, match='^SDC_SUPPORT_CRYPTO_UNAVAILABLE$'):
        verify()
    assert not spawned and handlers == originals


@pytest.mark.parametrize('failure', [False, True], ids=['success', 'timeout'])
def test_crypto_worker_keeps_process_signal_handlers_unchanged(monkeypatch, fake_process_signals, crypto_clock, failure):
    handlers, changes = fake_process_signals
    originals = dict(handlers)
    paths, outcomes = [], []

    class Child:
        returncode = None
        stdin = io.BytesIO()

        def communicate(self, **kwargs):
            if failure:
                crypto_clock[0] += kwargs['timeout']
                raise subprocess.TimeoutExpired(CANARY, 5)
            self.stdin.close()
            self.returncode = 0

        def wait(self, timeout):
            self.returncode = -9
            return self.returncode

    child = Child()

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        return child

    def run():
        try:
            outcomes.append(verify())
        except BaseException as exc:
            outcomes.append((type(exc), str(exc)))

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', lambda _child: None)
    owner = threading.Thread(target=run)
    owner.start()
    owner.join(3)
    assert not owner.is_alive()
    expected = ((signatures.SignatureError, 'SDC_SUPPORT_CRYPTO_UNAVAILABLE') if failure
                else VECTOR['vectors'][0]['payload'])
    assert outcomes == [expected]
    assert handlers == originals and not changes
    assert child.returncode == (-9 if failure else 0) and child.stdin.closed
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.parametrize('result', ['timeout', 'success_before_deadline', 'success_after_deadline'])
def test_crypto_uses_one_absolute_budget_and_supplies_input_only_once(monkeypatch, fake_process_signals,
                                                                  crypto_clock, result):
    paths, calls = [], []
    started = crypto_clock[0]

    class Child:
        returncode = None
        stdin = io.BytesIO()

        def communicate(self, **kwargs):
            calls.append((crypto_clock[0], dict(kwargs)))
            if len(calls) <= 2:
                crypto_clock[0] = started + (2.0 if len(calls) == 1 else 4.99)
                raise subprocess.TimeoutExpired(CANARY, kwargs['timeout'])
            if len(calls) != 3:
                raise RuntimeError('unexpected verification retry')
            if result == 'timeout':
                crypto_clock[0] = started + 5.0
                raise subprocess.TimeoutExpired(CANARY, kwargs['timeout'])
            crypto_clock[0] = started + (4.995 if result == 'success_before_deadline' else 5.01)
            self.returncode = 0
            self.stdin.close()

    child = Child()

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        return child

    def stop(_child):
        if child.returncode is None:
            child.returncode = -9
        child.stdin.close()

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', stop)
    if result == 'success_before_deadline':
        assert verify() == VECTOR['vectors'][0]['payload']
    else:
        with pytest.raises(signatures.SignatureError, match='^SDC_SUPPORT_CRYPTO_UNAVAILABLE$'):
            verify()
    assert len(calls) == 3
    assert [kwargs['timeout'] for _, kwargs in calls] == pytest.approx([0.05, 0.05, 0.01])
    assert [at - started for at, _ in calls] == pytest.approx([0, 2, 4.99])
    assert calls[0][1]['input'] == VECTOR['vectors'][0]['signed_ascii'].encode('ascii')
    assert all(set(kwargs) == {'timeout'} for _, kwargs in calls[1:])
    assert all(at + kwargs['timeout'] <= started + 5.0 for at, kwargs in calls)
    assert child.returncode == (-9 if result == 'timeout' else 0) and child.stdin.closed
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.parametrize('unreaped', [False, True], ids=['pipe_open', 'unreaped'])
def test_crypto_normal_return_still_settles_actual_child_and_pipe(monkeypatch, fake_process_signals, unreaped):
    paths, closing = [], []

    class InputPipe(io.BytesIO):
        def close(self):
            closing.append(all(path.exists() for path in paths))
            if len(closing) == 1:
                raise OSError(CANARY)
            super().close()

    class Child:
        returncode = None if unreaped else 0
        stdin = InputPipe()

        def communicate(self, **kwargs):
            pass

        def wait(self, timeout):
            self.returncode = 0
            return self.returncode

    child = Child()

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        return child

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', lambda _child: None)
    assert verify() == VECTOR['vectors'][0]['payload']
    assert len(closing) >= 2 and all(closing)
    assert child.returncode == 0 and child.stdin.closed
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.parametrize('parent_raises', [False, True], ids=['cli_expired', 'parent_expired'])
def test_crypto_preserves_parent_deadline_when_alarm_and_interrupt_are_pending(monkeypatch, fake_process_signals,
                                                                            crypto_clock, parent_raises):
    import sdc_support_cli as cli

    handlers, _changes = fake_process_signals
    paths, calls, during_reap, timers = [], [], [], []
    started = crypto_clock[0]

    class ParentExpired(BaseException):
        pass

    def record(kind):
        calls.append((kind, child.returncode, child.stdin.closed, all(not path.exists() for path in paths)))

    def alarm(_signum, _frame):
        record('alarm')
        if parent_raises:
            raise ParentExpired()

    def interrupt(_signum, _frame):
        record('interrupt')
        raise KeyboardInterrupt(CANARY)

    handlers.update({signal.SIGALRM: alarm, signal.SIGINT: interrupt})
    originals = dict(handlers)
    monkeypatch.setattr(signal, 'getitimer', lambda _which: (2.0, 3.0))
    monkeypatch.setattr(signal, 'setitimer', lambda which, delay, interval=0: timers.append((which, delay, interval)))

    class Child:
        returncode = None
        stdin = io.BytesIO()
        communications = 0

        def communicate(self, **kwargs):
            self.communications += 1
            crypto_clock[0] += kwargs['timeout']
            raise subprocess.TimeoutExpired(CANARY, kwargs['timeout'])

        def wait(self, timeout):
            crypto_clock[0] = started + 2.5
            for _ in range(2):
                handlers[signal.SIGALRM](signal.SIGALRM, None)
                handlers[signal.SIGINT](signal.SIGINT, None)
                during_reap.append((self.returncode, bool(calls), all(path.exists() for path in paths)))
            self.returncode = -9
            return self.returncode

    child = Child()

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        handlers[signal.SIGINT](signal.SIGINT, None)
        return child

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', lambda _child: None)
    expected = ParentExpired if parent_raises else cli._Expired
    with pytest.raises(expected):
        with cli._deadline():
            verify()
    assert child.communications == 0
    assert during_reap == [(None, False, True), (None, False, True)]
    assert calls == [('alarm', -9, True, True), ('interrupt', -9, True, True)]
    assert handlers == originals
    assert timers == [(signal.ITIMER_REAL, 2.0, 0), (signal.ITIMER_REAL, 0, 0), (signal.ITIMER_REAL, 2.5, 3.0)]
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.parametrize('boundary', ['before', 'after'])
def test_crypto_retries_finite_restore_interrupts_without_hiding_alarm(monkeypatch, fake_process_signals, boundary):
    handlers, _changes = fake_process_signals
    paths, replayed, restores = [], [], []

    class DeadlineExpired(BaseException):
        pass

    def original_alarm(_signum, _frame):
        replayed.append(('alarm', child.returncode, child.stdin.closed, all(not path.exists() for path in paths)))
        raise DeadlineExpired()

    def original_interrupt(_signum, _frame):
        replayed.append(('interrupt', child.returncode, child.stdin.closed, all(not path.exists() for path in paths)))
        raise KeyboardInterrupt(CANARY)

    handlers.update({signal.SIGALRM: original_alarm, signal.SIGINT: original_interrupt})
    originals = dict(handlers)
    replace = signal.signal

    def restoring(signum, handler):
        if signum == signal.SIGINT and handler is original_interrupt:
            restores.append(True)
            if len(restores) <= 2:
                if boundary == 'after':
                    replace(signum, handler)
                handlers[signal.SIGALRM](signal.SIGALRM, None)
        return replace(signum, handler)

    monkeypatch.setattr(signal, 'signal', restoring)

    class Child:
        returncode = None
        stdin = io.BytesIO()

        def communicate(self, **kwargs):
            raise RuntimeError('pending signal must stop before communication')

        def wait(self, timeout):
            self.returncode = -9
            return self.returncode

    child = Child()

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        handlers[signal.SIGINT](signal.SIGINT, None)
        return child

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    monkeypatch.setattr(signatures.owned_process, 'stop_owned', lambda _child: None)
    with pytest.raises(DeadlineExpired):
        verify()
    assert 1 <= len(restores) <= 3 and handlers == originals
    if boundary == 'before':
        assert len(restores) == 3
    assert replayed == [('interrupt', -9, True, True)] + [('alarm', -9, True, True)] * min(len(restores), 2)
    assert paths and all(not path.exists() for path in paths)


def test_crypto_permanent_restore_failure_is_bounded_and_restores_other_handler(monkeypatch, fake_process_signals):
    handlers, _changes = fake_process_signals
    originals = dict(handlers)
    paths, restores = [], []
    replace = signal.signal

    def restoring(signum, handler):
        if signum == signal.SIGINT and handler is originals[signal.SIGINT]:
            restores.append(True)
            raise OSError(CANARY)
        return replace(signum, handler)

    monkeypatch.setattr(signal, 'signal', restoring)

    class Child:
        returncode = None
        stdin = io.BytesIO()

        def communicate(self, **kwargs):
            self.returncode = 0
            self.stdin.close()

    child = Child()

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        return child

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    with pytest.raises(signatures.SignatureError, match='^SDC_SUPPORT_CRYPTO_UNAVAILABLE$'):
        verify()
    assert len(restores) == 3
    assert handlers[signal.SIGALRM] is originals[signal.SIGALRM]
    assert handlers[signal.SIGINT] is not originals[signal.SIGINT]
    assert child.returncode == 0 and child.stdin.closed
    assert paths and all(not path.exists() for path in paths)


def test_crypto_reaps_one_owned_synthetic_process_before_replaying_interrupt(monkeypatch, fake_process_signals):
    handlers, _changes = fake_process_signals
    originals = dict(handlers)
    paths, children = [], []
    popen_real = subprocess.Popen

    def popen(argv, **kwargs):
        paths.append(Path(argv[argv.index('-verify') + 1]).parent)
        child = popen_real([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], **kwargs)
        children.append(child)
        handlers[signal.SIGINT](signal.SIGINT, None)
        return child

    monkeypatch.setattr(signatures.subprocess, 'Popen', popen)
    try:
        with pytest.raises(KeyboardInterrupt):
            verify()
        assert len(children) == 1
        child = children[0]
        assert child.returncode is not None and child.stdin.closed
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        assert handlers == originals
        assert paths and all(not path.exists() for path in paths)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            child.stdin.close()


def test_public_preflight_pins_validates_without_crypto_or_network(monkeypatch):
    monkeypatch.setattr(signatures, '_verify_pss', lambda *_: pytest.fail('preflight must not spawn crypto'))
    pins = copy.deepcopy(VECTOR['pinned_keys'])
    purpose = pins[0]['purposes'][0]
    assert signatures.validate_pins(pins, purpose=purpose, now_ms=VECTOR['now']) == pins


@pytest.mark.parametrize('change', ['der', 'purpose', 'expired', 'duplicate', 'extra'])
def test_public_pin_preflight_rejects_before_http(change):
    pins = copy.deepcopy(VECTOR['pinned_keys'])
    if change == 'der':
        pins[-1]['spki'] = b64(b'not DER')
    elif change == 'purpose':
        for pin in pins:
            pin['purposes'] = ['operation']
    elif change == 'expired':
        for pin in pins:
            pin['expires_at'] = VECTOR['now']
    elif change == 'duplicate':
        pins.append(copy.deepcopy(pins[0]))
    else:
        pins[0]['extra'] = CANARY
    with pytest.raises(signatures.SignatureError):
        signatures.validate_pins(pins, purpose='consent-preview', now_ms=VECTOR['now'])


def test_preflight_explicit_consent_preview_purpose_is_required_and_supported():
    pins = copy.deepcopy(VECTOR['pinned_keys'])
    for pin in pins:
        pin['purposes'] = ['consent-preview']
    assert signatures.validate_pins(pins, purpose='consent-preview', now_ms=VECTOR['now']) == pins
