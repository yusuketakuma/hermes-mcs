"""Synthetic restart transport boundaries; no live sockets or service operations."""
import hashlib
import hmac
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from adapters.sdc import protocol, restart_control, transport
from adapters.sdc.support_contract import canonical

CONFIG = {'origin': 'https://support.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
STATE = {'device_id': '11111111-1111-4111-8111-111111111111',
         'key_id': '22222222-2222-4222-8222-222222222222'}
KEY, NONCE, NOW = 'a1' * 32, 'b2' * 16, 1792742400
ROUTES = {
    'Inspection': '/v2/support/restarts/inspections/report',
    'Preview': '/v2/support/restarts/cases/preview',
    'Accept': '/v2/support/restarts/cases/accept',
    'Poll': '/v2/support/restarts/requests/poll',
    'Admit': '/v2/support/restarts/operations/admit',
    'Grant': '/v2/support/restarts/operations/grant',
    'Report': '/v2/support/restarts/operations/report',
    'Status': '/v2/support/restarts/operations/status',
    'Read': '/v2/support/restarts/services/read',
}
POLL = ROUTES['Poll']


class Response(io.BytesIO):
    code = 200
    headers = {}


def sign(path=POLL, raw=b'{}', **changes):
    args = dict(now=NOW, nonce=NONCE)
    args.update(changes)
    return protocol.sign_restart(CONFIG, path, raw, STATE, KEY, **args)


def client():
    instance, calls = transport.RestartTransport(CONFIG), []
    def open_response(request, timeout):
        calls.append((request, timeout))
        return Response(b'{"state":"unknown"}')
    instance.opener = SimpleNamespace(open=open_response)
    return instance, calls


@pytest.mark.parametrize('kind,path', ROUTES.items())
def test_fixed_request_bytes_and_independent_hmac_oracle(kind, path):
    filename = 'restart-registration-vectors.json' if kind == 'Read' else 'restart-api-vectors.json'
    vectors = json.loads((Path(__file__).resolve().parents[3] /
                         'adapters/sdc/contracts/support' / filename).read_text())
    value = next(v['value'] for v in vectors['positive'] if v['definition'] == kind + 'Request')
    raw = canonical(value).encode('ascii')
    headers = sign(path, raw)
    signed_bytes = '\n'.join(('SDC-HMAC-v1', CONFIG['audience'], 'POST', path,
                              STATE['device_id'], STATE['key_id'], str(NOW), NONCE,
                              hashlib.sha256(raw).hexdigest())).encode('ascii')
    assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex(KEY), signed_bytes, hashlib.sha256).hexdigest()
    wire, calls = client()
    assert wire.send(path, raw, headers) == {'state': 'unknown'}
    request, timeout = calls[0]
    assert request.data == raw and request.full_url == CONFIG['origin'] + path
    assert request.get_method() == 'POST' and timeout == 10
    assert request.get_header('X-sdc-signature') == headers['X-SDC-Signature']


def test_registration_read_exact_request_and_current_response_cross_existing_transport_once():
    vectors = json.loads((Path(__file__).resolve().parents[3] /
                         'adapters/sdc/contracts/support/restart-registration-vectors.json').read_text())
    request = next(v['value'] for v in vectors['positive'] if v['definition'] == 'ReadRequest')
    expected = next(v['value'] for v in vectors['positive'] if v['definition'] == 'ReadResponse')
    path = ROUTES['Read']
    checked = restart_control.validate_request(path, request, now_ms=vectors['now'])
    raw = canonical(checked).encode('ascii')
    wire, calls = client()
    def reply(sent, timeout):
        calls.append((sent, timeout))
        return Response(canonical(expected).encode('ascii'))
    wire.opener = SimpleNamespace(open=reply)
    result = wire.send(path, raw, sign(path, raw))
    assert restart_control.validate_response(path, result, request=checked, now_ms=vectors['now']) == expected
    assert len(calls) == 1 and calls[0][0].data == raw and calls[0][1] == 10


@pytest.mark.parametrize('status,code', [(403, 'SCOPE_DENIED'), (409, 'STATE_CONFLICT'), (503, 'SDC_UNAVAILABLE')])
def test_registration_read_error_is_one_dispatch_without_automatic_retry(status, code):
    wire, calls = client()
    def reply(request, timeout):
        calls.append((request, timeout))
        response = Response(json.dumps({'code': code}).encode('ascii'))
        response.code = status
        return response
    wire.opener = SimpleNamespace(open=reply)
    with pytest.raises(transport.TransportError) as caught:
        wire.send(ROUTES['Read'], b'{}', sign(ROUTES['Read']))
    assert caught.value.status == status and caught.value.code == code and len(calls) == 1


@pytest.mark.parametrize('status', [400, 401, 403, 404, 405, 408, 409, 413, 415, 422, 429, 499])
@pytest.mark.parametrize('body', [b'<html>synthetic private error</html>', b'x' * 16385,
                                  b'[]', b'{"code":"A","code":"B"}', OSError('private error')])
def test_registration_refusal_survives_unusable_body(status, body):
    wire, calls = client()
    class Refusal(Response):
        code = status
        def read(self, limit):
            assert limit == 16385
            if isinstance(body, Exception):
                raise body
            return body
    response = Refusal(b'')
    def reply(request, timeout):
        calls.append((request, timeout))
        return response
    wire.opener = SimpleNamespace(open=reply)
    with pytest.raises(transport.TransportError) as caught:
        wire.send(ROUTES['Read'], b'{}', sign(ROUTES['Read']))
    assert (caught.value.status, caught.value.code, caught.value.retry_after) == (status, 'SDC_REQUEST_DENIED', 0)
    assert str(caught.value) == 'SDC_REQUEST_DENIED'
    assert len(calls) == 1 and response.closed


@pytest.mark.parametrize('status,path', [(200, ROUTES['Read']), (503, ROUTES['Read']),
                                        (403, POLL)])
def test_unusable_body_keeps_other_routes_and_unknown_results_unchanged(status, path):
    wire, _ = client()
    response = Response(b'<html>synthetic private error</html>')
    response.code = status
    wire.opener = SimpleNamespace(open=lambda *_a, **_kw: response)
    with pytest.raises(transport.TransportError) as caught:
        wire.send(path, b'{}', sign(path))
    assert (caught.value.status, caught.value.code) == (503, 'SDC_NETWORK_UNAVAILABLE')
    assert response.closed


@pytest.mark.parametrize('parent_deadline', [False, True])
@pytest.mark.parametrize('phase', ['read', 'close', 'timer'])
@pytest.mark.parametrize('status', [403, 404, 499])
def test_registration_refusal_deadline_preserves_parent_alarm(monkeypatch, parent_deadline, phase, status):
    clock, handlers, timers, fired = [100.0], [], [], []
    expired = []
    def expire():
        expired.append(True)
        clock[0] = 102.25 if parent_deadline else 110.25
        handlers[0](transport.signal.SIGALRM, None)
    def parent(signum, frame):
        fired.append(signum)
        raise TimeoutError('synthetic parent deadline')
    monkeypatch.setattr(transport.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(transport.signal, 'getsignal', lambda _sig: parent)
    monkeypatch.setattr(transport.signal, 'getitimer', lambda _timer: (2 if parent_deadline else 0, 0))
    monkeypatch.setattr(transport.signal, 'signal', lambda _sig, fn: handlers.append(fn))
    def timer(_timer, delay, interval=0):
        timers.append((delay, interval))
        if phase == 'timer' and delay == 0 and not expired:
            expire()
    monkeypatch.setattr(transport.signal, 'setitimer', timer)
    class Refusal(Response):
        code = status
        def read(self, _limit):
            if phase == 'read':
                expire()
            return super().read(_limit)
        def __exit__(self, *args):
            super().__exit__(*args)
            if phase == 'close':
                expire()
    response = Refusal(b'{"code":"REQUEST_DENIED"}')
    wire, _ = client()
    wire.opener = SimpleNamespace(open=lambda *_a, **_kw: response)
    with pytest.raises(TimeoutError if parent_deadline else transport.TransportError) as caught:
        wire.send(ROUTES['Read'], b'{}', sign(ROUTES['Read']))
    if parent_deadline:
        assert str(caught.value) == 'synthetic parent deadline'
        assert fired == [transport.signal.SIGALRM]
    else:
        assert (caught.value.status, caught.value.code) == (status, 'SDC_REQUEST_DENIED')
        assert fired == []
    assert response.closed and handlers[-1] is parent and timers[-1] == (0, 0)


def test_restart_monitoring_diagnostic_and_admin_routes_are_disjoint():
    restart, calls = client()
    others = [(transport.Transport(CONFIG), protocol.PATHS),
              (transport.SupportTransport(CONFIG), protocol.SUPPORT_PATHS)]
    for other, paths in others:
        other.opener = SimpleNamespace(open=lambda *_a, **_kw: pytest.fail('cross-route network'))
        for path in paths:
            with pytest.raises(ValueError, match='^SDC_REQUEST_INVALID$'):
                sign(path)
            with pytest.raises(ValueError, match='^SDC_REQUEST_INVALID$'):
                restart.send(path, b'{}', sign())
        for path in ROUTES.values():
            with pytest.raises(ValueError):
                other.send(path, b'{}', sign(path))
            with pytest.raises(ValueError):
                protocol.sign_support(CONFIG, path, b'{}', STATE, KEY, now=NOW, nonce=NONCE)
            with pytest.raises(ValueError):
                protocol.sign(CONFIG, path, b'{}', STATE, KEY, now=NOW, nonce=NONCE)
    for path in ('/v2/support/self/restarts/register', '/v2/support/customer/restarts/approve',
                 POLL + '?x=1', POLL + '/', 'https://evil.invalid' + POLL, None):
        with pytest.raises(ValueError):
            restart.send(path, b'{}', sign())
    assert calls == []


@pytest.mark.parametrize('raw', [b'[]', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":-0}',
                                b'{"x":1.0}', b'{"x":"\\u00e9"}', b'{ "x":1}', b'x' * 16385])
def test_invalid_bytes_are_rejected_before_network_or_signing(raw):
    wire, calls = client()
    with pytest.raises(ValueError, match='^SDC_REQUEST_INVALID$'):
        wire.send(POLL, raw, sign())
    with pytest.raises(ValueError, match='^SDC_REQUEST_INVALID$'):
        sign(raw=raw)
    assert calls == []


@pytest.mark.parametrize('headers', [None, {}, {'Host': 'evil.invalid'},
                                   {'X-SDC-Device': 'arbitrary'}, {'X-SDC-Nonce': 'line\nbreak'}])
def test_missing_or_invalid_authentication_cannot_send(headers):
    wire, calls = client()
    supplied = headers if not headers else {**sign(), **headers}
    with pytest.raises(ValueError, match='^SDC_REQUEST_INVALID$'):
        wire.send(POLL, b'{}', supplied)
    assert calls == []


@pytest.mark.parametrize('changes', [{'now': True}, {'now': float('nan')}, {'now': -1},
                                    {'nonce': 'invalid'}, {'nonce': None}])
def test_restart_signing_retains_strict_header_input_contract(changes):
    with pytest.raises(ValueError, match='^SDC_SIGNING_INVALID$'):
        sign(**changes)


def test_shared_deadline_and_endpoint_guard_without_background_fallback():
    assert transport.RestartTransport.send is transport.Transport.send
    assert transport.RestartTransport._send is transport.Transport._send
    config = dict(CONFIG)
    wire = transport.RestartTransport(config)
    config['origin'] = 'https://changed.invalid'
    assert wire.config == CONFIG
    for invalid in (None, {}, {**CONFIG, 'origin': 'http://support.invalid'},
                    {**CONFIG, 'origin': CONFIG['origin'] + '/path'}, {**CONFIG, 'secret': 'test'}):
        with pytest.raises(ValueError, match='^SDC_ENDPOINT_NOT_APPROVED$'):
            transport.RestartTransport(invalid)
    wire, calls = client()
    errors = []
    def send_in_thread():
        try:
            wire.send(POLL, b'{}', sign())
        except ValueError as error:
            errors.append(str(error))
    thread = threading.Thread(target=send_in_thread)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and errors == ['SDC_MAIN_THREAD_REQUIRED'] and calls == []


@pytest.mark.parametrize('raw,code', [(b'{"state":"unknown"}', 302), (b'x' * 16385, 200),
                                    (b'{"x":1,"x":2}', 200),
                                    (b'{"code":"SYNTHETIC_PRIVATE detail"}', 403)])
def test_shared_response_guard_hides_raw_content(raw, code):
    wire, _calls = client()
    response = Response(raw)
    response.code = code
    wire.opener = SimpleNamespace(open=lambda *_a, **_kw: response)
    with pytest.raises(transport.TransportError) as caught:
        wire.send(POLL, b'{}', sign())
    assert str(caught.value) in ('SDC_REDIRECT_DENIED', 'SDC_NETWORK_UNAVAILABLE', 'SDC_REQUEST_DENIED')
    assert response.closed
