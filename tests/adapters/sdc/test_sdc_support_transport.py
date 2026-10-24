"""Synthetic support-only transport oracles; no runner, live endpoint or credentials."""
import hashlib
import hmac
import io
import json
from pathlib import Path
import ssl
from types import SimpleNamespace

import pytest

from adapters.sdc import protocol, transport as module

CONFIG = {'origin': 'https://support.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
STATE = {'device_id': '11111111-1111-4111-8111-111111111111',
         'key_id': '22222222-2222-4222-8222-222222222222'}
KEY = 'a1' * 32
NOW, NONCE = 1792742400, 'b2' * 16
ROUTES = frozenset(('/v2/support/cases/preview', '/v2/support/cases/accept', '/v2/support/requests/poll',
                    '/v2/support/operations/admit', '/v2/support/operations/grant',
                    '/v2/support/operations/report', '/v2/support/operations/status'))


class Response(io.BytesIO):
    def __init__(self, raw=b'{"state":"unknown"}', code=200, headers=None):
        super().__init__(raw)
        self.code, self.headers = code, headers or {}


def headers(path='/v2/support/requests/poll', raw=b'{}'):
    return protocol.sign_support(CONFIG, path, raw, STATE, KEY, now=NOW, nonce=NONCE)


def client_with_capture():
    client, calls = module.SupportTransport(CONFIG), []
    def opened(request, timeout):
        calls.append((request, timeout))
        return Response()
    client.opener = SimpleNamespace(open=opened)
    return client, calls


def test_seven_routes_match_central_fixed_device_vectors_without_expanding_r1():
    assert protocol.SUPPORT_PATHS == ROUTES and not protocol.PATHS & ROUTES
    contract = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/support/control-vectors.json'
    vectors = json.loads(contract.read_text())
    device = [vector for vector in vectors['positive_requests']
              if vector['schema_ref'].rsplit('/', 1)[-1] in
              {'PreviewRequest', 'AcceptRequest', 'PollRequest', 'AdmitRequest', 'GrantRequest', 'ReportRequest', 'StatusRequest'}]
    assert {vector['path'] for vector in device} == ROUTES
    client, calls = client_with_capture()
    for vector in device:
        assert vector['method'] == 'POST'
        raw = vector['raw_json'].encode('ascii')
        client.send(vector['path'], raw, headers(vector['path'], raw))
        assert calls[-1][0].data == raw


@pytest.mark.parametrize('path', sorted(ROUTES))
def test_fixed_post_hmac_bytes_are_bound_to_separate_origin(path):
    raw = b'{"schema_version":"sdc.support.v1"}'
    signed = headers(path, raw)
    message = '\n'.join(('SDC-HMAC-v1', CONFIG['audience'], 'POST', path, STATE['device_id'],
                         STATE['key_id'], str(NOW), NONCE, hashlib.sha256(raw).hexdigest()))
    assert signed['X-SDC-Signature'] == hmac.new(bytes.fromhex(KEY), message.encode('ascii'), hashlib.sha256).hexdigest()
    client, calls = client_with_capture()
    assert client.send(path, raw, signed) == {'state': 'unknown'}
    request, timeout = calls[0]
    assert request.full_url == CONFIG['origin'] + path
    assert request.get_method() == 'POST' and request.data == raw and timeout == 10
    assert request.get_header('Content-type') == 'application/json'
    assert request.get_header('Content-encoding') == 'identity'
    assert request.get_header('X-sdc-signature') == signed['X-SDC-Signature']


def test_r1_and_support_signing_and_transport_reject_each_others_paths():
    support, calls = client_with_capture()
    legacy = module.Transport({**CONFIG, 'origin': 'https://ingest.invalid'})
    legacy.opener = SimpleNamespace(open=lambda *_a, **_kw: pytest.fail('cross-route wire'))
    for path in protocol.PATHS:
        with pytest.raises(ValueError):
            protocol.sign_support(CONFIG, path, b'{}', STATE, KEY, now=NOW, nonce=NONCE)
        with pytest.raises(ValueError):
            support.send(path, b'{}', headers())
    for path in ROUTES:
        with pytest.raises(ValueError):
            protocol.sign(CONFIG, path, b'{}', STATE, KEY, now=NOW, nonce=NONCE)
        with pytest.raises(ValueError):
            legacy.send(path, b'{}', headers(path))
    assert calls == []
    # The old unsigned enrollment/heartbeat transport contract remains intact.
    legacy.opener = SimpleNamespace(open=lambda *_a, **_kw: Response())
    assert legacy.send('/v2/enrollments/claim', b'{}') == {'state': 'unknown'}


@pytest.mark.parametrize('path', ['https://evil.invalid/v2/support/requests/poll',
    '//evil.invalid', '/v2/support/requests/poll?x=1', '/v2/support/requests/poll#x',
    '/v2/support/requests/poll/', '/v2/support/requests/../requests/poll',
    '/v2/support/requests/%70oll', '/v2/support/commands/run', '/v2/support/requests/poll\n', None])
def test_arbitrary_path_or_url_never_reaches_wire(path):
    client, calls = client_with_capture()
    with pytest.raises(ValueError, match='^SDC_REQUEST_INVALID$'):
        client.send(path, b'{}', headers())
    assert calls == []


@pytest.mark.parametrize('raw', [b'', b'[]', b'null', b'{"x":1,"x":2}', b'{"x":NaN}',
    b'{"x":-0}', b'{"x":1.0}', b'{"x":"\\u00e9"}', b'{ "x":1}', b'not-json', b'x' * 16385])
def test_support_request_is_bounded_closed_canonical_json_object(raw):
    client, calls = client_with_capture()
    with pytest.raises(ValueError, match='^SDC_REQUEST_INVALID$'):
        client.send('/v2/support/requests/poll', raw, headers())
    with pytest.raises(ValueError, match='^SDC_REQUEST_INVALID$'):
        headers(raw=raw)
    assert calls == []


@pytest.mark.parametrize('change', [None, {}, {'Host': 'evil.invalid'}, {'X-SDC-Nonce': 'x\nsecret'},
    {'X-SDC-Device': 'invalid'}, {'X-SDC-Key-ID': True}, {'X-SDC-Time': '01'},
    {'X-SDC-Time': '9007199254740992'}, {'X-SDC-Signature': 'secret'}, {'X-SDC-Time': None}])
def test_complete_typed_hmac_headers_are_required(change):
    client, calls = client_with_capture()
    supplied = change if change is None or change == {} else {**headers(), **change}
    with pytest.raises(ValueError, match='^SDC_REQUEST_INVALID$'):
        client.send('/v2/support/requests/poll', b'{}', supplied)
    assert calls == []


@pytest.mark.parametrize('origin', ['http://support.invalid', 'https://support.invalid:443',
    'https://user@support.invalid', 'https://support.invalid/path', 'https://support.invalid?q=secret',
    'https://support.invalid#fragment', 'https://support..invalid'])
def test_explicit_approved_endpoint_has_no_url_extensions(origin):
    with pytest.raises(ValueError, match='^SDC_ENDPOINT_NOT_APPROVED$'):
        module.SupportTransport({**CONFIG, 'origin': origin})


def test_support_endpoint_is_snapshotted_without_default_or_discovery():
    config = dict(CONFIG)
    client = module.SupportTransport(config)
    config['origin'] = 'https://changed.invalid'
    assert client.config == CONFIG and client.config is not config
    for invalid in (None, {}, {**CONFIG, 'secret': 'SYNTHETIC_SECRET'}, {**CONFIG, 'origin': True}):
        with pytest.raises(ValueError, match='^SDC_ENDPOINT_NOT_APPROVED$'):
            module.SupportTransport(invalid)


def test_tls_no_proxy_no_redirect_and_shared_deadline_implementation():
    client = module.SupportTransport(CONFIG)
    assert module.SupportTransport.send is module.Transport.send
    assert module.SupportTransport._send is module.Transport._send
    proxies = [handler for handler in client.opener.handlers if hasattr(handler, 'proxies')]
    assert all(handler.proxies == {} for handler in proxies)
    assert any(type(handler).__name__ == 'NoRedirect' for handler in client.opener.handlers)
    assert all(handler._context.verify_mode == ssl.CERT_REQUIRED for handler in client.opener.handlers if hasattr(handler, '_context'))
    client.opener = SimpleNamespace(open=lambda *_a, **_kw: Response(b'SYNTHETIC_SECRET', 302, {'Location': 'https://evil.invalid'}))
    with pytest.raises(module.TransportError, match='^SDC_REDIRECT_DENIED$'):
        client.send('/v2/support/requests/poll', b'{}', headers())


@pytest.mark.parametrize('raw', [b'x' * 16385, b'{"x":1,"x":2}', b'not-json', b'[]', b'{"x":NaN}'])
def test_response_bounds_and_errors_do_not_echo_private_body(raw):
    client = module.SupportTransport(CONFIG)
    client.opener = SimpleNamespace(open=lambda *_a, **_kw: Response(raw))
    with pytest.raises(module.TransportError, match='^SDC_NETWORK_UNAVAILABLE$'):
        client.send('/v2/support/requests/poll', b'{}', headers())
    client.opener = SimpleNamespace(open=lambda *_a, **_kw: Response(
        b'{"code":"SYNTHETIC_SECRET_WITH_SPACES ","detail":"SYNTHETIC_PRIVATE"}', 403))
    with pytest.raises(module.TransportError) as caught:
        client.send('/v2/support/requests/poll', b'{}', headers())
    assert str(caught.value) == 'SDC_REQUEST_DENIED'
    assert caught.value.status == 403


def test_valid_request_at_exact_body_bound_is_not_truncated():
    raw = b'{"x":"' + b'x' * (16384 - 8) + b'"}'
    assert len(raw) == 16384
    client, calls = client_with_capture()
    client.send('/v2/support/requests/poll', raw, headers(raw=raw))
    assert calls[0][0].data == raw


def test_parent_deadline_is_borrowed_and_alarm_restored(monkeypatch):
    clock, timers, handlers = [100.0], [], []
    def handler(*_args):
        pass
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.signal, 'getsignal', lambda _sig: handler)
    monkeypatch.setattr(module.signal, 'getitimer', lambda _which: (4, 5))
    monkeypatch.setattr(module.signal, 'signal', lambda _sig, value: handlers.append(value))
    monkeypatch.setattr(module.signal, 'setitimer', lambda _which, delay, repeat=0: timers.append((delay, repeat)))
    client = module.SupportTransport(CONFIG)
    def opened(*_args, **_kwargs):
        clock[0] += 1
        return Response()
    client.opener = SimpleNamespace(open=opened)
    client.send('/v2/support/requests/poll', b'{}', headers())
    assert handlers[-1] is handler
    assert timers == [(4, 0), (0, 0), (0, 0), (3, 5)]


def test_main_thread_requirement_rejects_before_wire(monkeypatch):
    def denied(*_args):
        raise ValueError('synthetic non-main-thread')
    monkeypatch.setattr(module.signal, 'signal', denied)
    client, calls = client_with_capture()
    with pytest.raises(ValueError, match='^SDC_MAIN_THREAD_REQUIRED$'):
        client.send('/v2/support/requests/poll', b'{}', headers())
    assert calls == []


@pytest.mark.parametrize('changes', [{'key': None}, {'now': True}, {'now': -1}, {'nonce': 'bad'},
                                     {'state': {}}, {'state': {**STATE, 'device_id': 'SYNTHETIC_SECRET'}}])
def test_support_signing_invalid_inputs_use_fixed_errors(changes):
    inputs = {'state': STATE, 'key': KEY, 'now': NOW, 'nonce': NONCE, **changes}
    with pytest.raises(ValueError, match='^SDC_SIGNING_INVALID$'):
        protocol.sign_support(CONFIG, '/v2/support/requests/poll', b'{}', **inputs)
