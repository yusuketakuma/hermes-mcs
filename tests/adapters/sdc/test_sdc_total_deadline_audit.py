"""SDC network budgets interrupt blocked I/O and borrow existing POSIX alarms safely."""
import io
import signal
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from adapters.sdc import transport as module

CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}


@pytest.mark.parametrize('phase', ['open', 'read'])
def test_blocked_io_is_interrupted_and_releases_state_lock(tmp_path, phase):
    repo = Path(__file__).resolve().parents[3]
    code = '''
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
repo, root, phase = sys.argv[1:]
sys.path.insert(0, repo)
sys.path.insert(0, str(Path(repo) / 'mcs'))
import _mcs_path
from adapters.sdc import transport as module
from adapters.sdc.storage import Store
module._TIMEOUT_S = 0.05
config = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
closed = []
class Response:
    code = 200
    def __enter__(self): return self
    def __exit__(self, *args): closed.append(True)
    def read(self, limit):
        signal.pause()
        raise AssertionError('deadline did not interrupt the synthetic read')
def open_request(*args, **kwargs):
    if phase == 'open':
        signal.pause()
        raise AssertionError('deadline did not interrupt the synthetic open')
    return Response()
client = module.Transport(config)
client.opener = SimpleNamespace(open=open_request)
old_handler = signal.getsignal(signal.SIGALRM)
try:
    with Store(root, create=True) as store, store.lock():
        client.send('/v2/heartbeats', b'{}')
    raise AssertionError('synthetic request unexpectedly completed')
except module.TransportError as error:
    assert str(error) == 'SDC_NETWORK_UNAVAILABLE'
with Store(root) as store, store.lock():
    pass
assert closed == ([True] if phase == 'read' else [])
assert signal.getsignal(signal.SIGALRM) == old_handler
assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
print('deadline_and_lock_ok')
'''
    result = subprocess.run([sys.executable, '-I', '-c', code, str(repo), str(tmp_path / 'state'), phase],
                            capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'deadline_and_lock_ok\n'


class Response(io.BytesIO):
    code = 200
    headers = {}


@pytest.mark.parametrize('interval', [0, 5])
def test_success_restores_handler_and_remaining_alarm_without_extending_it(monkeypatch, interval):
    clock = [100.0]
    def handler(*_args):
        pass
    handlers, timers = [], []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.signal, 'getsignal', lambda _sig: handler)
    monkeypatch.setattr(module.signal, 'getitimer', lambda _which: (30, interval))
    monkeypatch.setattr(module.signal, 'signal', lambda _sig, value: handlers.append(value))
    monkeypatch.setattr(module.signal, 'setitimer', lambda _which, delay, repeat=0: timers.append((delay, repeat)))
    def open_request(*_args, **_kwargs):
        clock[0] += 3
        return Response(b'{"code":"accepted"}')
    client = module.Transport(CONFIG)
    client.opener = SimpleNamespace(open=open_request)
    assert client.send('/v2/heartbeats', b'{}')['code'] == 'accepted'
    assert handlers[-1] is handler
    assert timers == [(10, 0), (0, 0), (0, 0), (27, interval)]


@pytest.mark.parametrize('interval', [0, 5])
def test_earlier_alarm_is_delegated_once_and_keeps_its_phase(monkeypatch, interval):
    clock = [100.0]
    fired, handlers, timers = [], [], []
    def prior_handler(signum, frame):
        fired.append(signum)
        raise TimeoutError('synthetic enclosing deadline')
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.signal, 'getsignal', lambda _sig: prior_handler)
    monkeypatch.setattr(module.signal, 'getitimer', lambda _which: (2, interval))
    monkeypatch.setattr(module.signal, 'signal', lambda _sig, value: handlers.append(value))
    monkeypatch.setattr(module.signal, 'setitimer', lambda _which, delay, repeat=0: timers.append((delay, repeat)))
    def open_request(*_args, **_kwargs):
        clock[0] = 102.25
        handlers[0](module.signal.SIGALRM, None)
    client = module.Transport(CONFIG)
    client.opener = SimpleNamespace(open=open_request)
    with pytest.raises(TimeoutError, match='synthetic enclosing deadline'):
        client.send('/v2/heartbeats', b'{}')
    assert fired == [module.signal.SIGALRM]
    assert handlers[-1] is prior_handler
    assert timers == [(2, 0), (0, 0), (0, 0)] + ([(4.75, 5)] if interval else [])


def test_enclosing_tick_deadline_escapes_connector_retry_path(monkeypatch):
    from adapters.sdc.connector import Connector
    clock, handlers, saved = [100.0], [], []
    def prior_handler(*_args):
        raise TimeoutError('synthetic tick deadline')
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.signal, 'getsignal', lambda _sig: prior_handler)
    monkeypatch.setattr(module.signal, 'getitimer', lambda _which: (2, 0))
    monkeypatch.setattr(module.signal, 'signal', lambda _sig, value: handlers.append(value))
    monkeypatch.setattr(module.signal, 'setitimer', lambda *_args: None)
    def open_request(*_args, **_kwargs):
        clock[0] = 102.25
        handlers[0](module.signal.SIGALRM, None)
    values = {'endpoint.json': CONFIG,
              'credential': {'key': 'a1' * 32, 'key_id': 'synthetic-key'},
              'state.json': {'enabled': True, 'state': 'active',
                             'device_id': 'synthetic-device', 'key_id': 'synthetic-key'}}
    store = SimpleNamespace(read=lambda name, default=None: values.get(name, default),
                            write=lambda name, value: saved.append((name, dict(value))))
    client = module.Transport(CONFIG)
    client.opener = SimpleNamespace(open=open_request)
    with pytest.raises(TimeoutError, match='synthetic tick deadline'):
        Connector(store, client, clock=lambda: 1_000_000_000).tick({})
    # Pending bytes stay resumable; the enclosing deadline is not recorded as a retry.
    assert saved[-1][1]['sending']
    assert 'last_result' not in saved[-1][1]
    assert 'next_due' not in saved[-1][1]


def test_non_main_thread_refuses_before_any_request():
    import threading
    calls, errors = [], []
    client = module.Transport(CONFIG)
    client.opener = SimpleNamespace(open=lambda *_args, **_kw: calls.append(1))
    def run():
        try:
            client.send('/v2/heartbeats', b'{}')
        except ValueError as error:
            errors.append(str(error))
    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == ['SDC_MAIN_THREAD_REQUIRED']
    assert calls == []


@pytest.mark.parametrize("handler", [signal.SIG_DFL, signal.SIG_IGN])
def test_non_callable_active_alarm_is_left_untouched_before_sending(monkeypatch, handler):
    calls = []
    monkeypatch.setattr(module.signal, "getsignal", lambda _sig: handler)
    monkeypatch.setattr(module.signal, "getitimer", lambda _timer: (2, 0))
    monkeypatch.setattr(module.signal, "signal", lambda *_args: calls.append("handler"))
    monkeypatch.setattr(module.signal, "setitimer", lambda *_args: calls.append("timer"))
    client = module.Transport(CONFIG)
    client.opener = SimpleNamespace(open=lambda *_args, **_kwargs: calls.append("request"))
    with pytest.raises(module.TransportError):
        client.send("/v2/heartbeats", b"{}")
    assert calls == []


def test_alarm_during_handler_installation_restores_and_delegates(monkeypatch):
    clock, installed, fired, timers, requests = [100.0], [], [], [], []
    def previous(signum, frame):
        fired.append(signum)
        raise TimeoutError("synthetic enclosing deadline")
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.signal, "getsignal", lambda _sig: previous)
    monkeypatch.setattr(module.signal, "getitimer", lambda _timer: (2, 0))
    monkeypatch.setattr(module.signal, "setitimer", lambda _which, delay, repeat=0: timers.append((delay, repeat)))
    def install(sig, handler):
        installed.append(handler)
        if handler is not previous:
            clock[0] = 102.25
            handler(sig, None)
    monkeypatch.setattr(module.signal, "signal", install)
    client = module.Transport(CONFIG)
    client.opener = SimpleNamespace(open=lambda *_a, **_kw: requests.append(1))
    with pytest.raises(TimeoutError, match="synthetic enclosing deadline"):
        client.send("/v2/heartbeats", b"{}")
    assert fired == [signal.SIGALRM]
    assert installed[-1] is previous
    assert timers == [(0, 0)]
    assert requests == []


@pytest.mark.parametrize("enclosing", [False, True])
def test_deadline_during_timer_cancellation_still_restores_handler(monkeypatch, enclosing):
    clock, handlers, fired, cancelled = [100.0], [], [], []
    def previous(*_args):
        fired.append(True)
        raise TimeoutError("synthetic enclosing deadline")
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.signal, "getsignal", lambda _sig: previous)
    monkeypatch.setattr(module.signal, "getitimer", lambda _timer: (2 if enclosing else 0, 0))
    monkeypatch.setattr(module.signal, "signal", lambda _sig, value: handlers.append(value))
    def set_timer(_timer, delay, interval=0):
        if delay == 0 and not cancelled:
            cancelled.append(True)
            clock[0] = 102.25 if enclosing else 110.25
            handlers[0](signal.SIGALRM, None)
    monkeypatch.setattr(module.signal, "setitimer", set_timer)
    client = module.Transport(CONFIG)
    monkeypatch.setattr(client, "_send", lambda *_a, **_kw: {"ok": True})
    error = TimeoutError if enclosing else module.TransportError
    with pytest.raises(error):
        client.send("/v2/heartbeats", b"{}")
    assert handlers[-1] is previous
    assert fired == ([True] if enclosing else [])
