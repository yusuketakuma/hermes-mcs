"""Provision uses fixed HMAC POSTs and the existing isolated transport contract."""
import hashlib
import hmac
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.sdc import protocol, transport
from adapters.sdc.support_contract import canonical

CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
STATE = {'device_id': '11111111-1111-4111-8111-111111111111',
         'key_id': '22222222-2222-4222-8222-222222222222'}
KEY, NONCE = 'a1' * 32, 'b2' * 16
VECTORS = json.loads((Path(__file__).resolve().parents[3] /
                      'adapters/sdc/contracts/provision/provision-vectors.json').read_text())
ROUTES = {'Ack': protocol.PROVISION_ACK, 'Status': protocol.PROVISION_STATUS}


def value(definition):
    return next(v['value'] for v in VECTORS['positive'] if v['definition'] == definition)


def signed(path, raw):
    return protocol.sign_provision(CONFIG, path, raw, STATE, KEY,
                                   now=VECTORS['now'] / 1000, nonce=NONCE)


class Response(io.BytesIO):
    code = 200
    headers = {}


@pytest.mark.parametrize('kind,path', ROUTES.items())
def test_exact_request_hmac_and_closed_response(monkeypatch, kind, path):
    monkeypatch.setattr(transport.time, 'time', lambda: VECTORS['now'] / 1000)
    raw = canonical(value(kind + 'Request')).encode('ascii')
    expected = value(kind + 'Response')
    headers = signed(path, raw)
    wire, calls = transport.ProvisionTransport(CONFIG), []
    def reply(request, timeout):
        calls.append((request, timeout))
        return Response(canonical(expected).encode('ascii'))
    wire.opener = SimpleNamespace(open=reply)
    assert wire.send(path, raw, headers) == expected
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.data == raw and request.full_url == CONFIG['origin'] + path
    assert request.get_method() == 'POST' and timeout == 10
    source = '\n'.join(('SDC-HMAC-v1', CONFIG['audience'], 'POST', path,
                         STATE['device_id'], STATE['key_id'], str(VECTORS['now'] // 1000),
                         NONCE, hashlib.sha256(raw).hexdigest())).encode('ascii')
    assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex(KEY), source, hashlib.sha256).hexdigest()


@pytest.mark.parametrize('client_type', [transport.Transport, transport.SupportTransport, transport.RestartTransport])
@pytest.mark.parametrize('kind,path', ROUTES.items())
def test_existing_routes_do_not_gain_provision_admission(client_type, kind, path):
    raw = canonical(value(kind + 'Request')).encode('ascii')
    wire = client_type(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: pytest.fail('unexpected dispatch'))
    with pytest.raises(ValueError):
        wire.send(path, raw, signed(path, raw))


@pytest.mark.parametrize('path', ['/v2/heartbeats', '/v2/support/operations/grant',
                                  '/v2/support/restarts/operations/grant', '/v2/provisioning/ack?x=1'])
def test_provision_rejects_other_purposes_before_dispatch(path):
    wire = transport.ProvisionTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: pytest.fail('unexpected dispatch'))
    with pytest.raises(ValueError):
        protocol.sign_provision(CONFIG, path, b'{}', STATE, KEY,
                                now=VECTORS['now'] / 1000, nonce=NONCE)
    with pytest.raises(ValueError):
        wire.send(path, b'{}', {})


@pytest.mark.parametrize('kind,path', ROUTES.items())
@pytest.mark.parametrize('body', [b'{}', b'[]', b'\xef\xbb\xbf{}', b'{"x":1,"x":2}', b'x' * 16385])
def test_invalid_success_is_unknown_and_never_accepted(kind, path, body):
    raw = canonical(value(kind + 'Request')).encode('ascii')
    wire = transport.ProvisionTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: Response(body))
    with pytest.raises(transport.TransportError) as caught:
        wire.send(path, raw, signed(path, raw))
    assert caught.value.status == 503
    assert str(caught.value) == 'SDC_NETWORK_UNAVAILABLE'


@pytest.mark.parametrize('status', [400, 401, 403, 404, 409, 413, 429])
@pytest.mark.parametrize('phase', ['body', 'close'])
def test_observed_refusal_survives_response_failure(status, phase):
    raw = canonical(value('AckRequest')).encode('ascii')
    class Refusal(Response):
        code = status
        def read(self, limit):
            if phase == 'body':
                raise OSError('synthetic private canary')
            return b'{"code":"PROVISION_CONFLICT"}'
        def __exit__(self, *args):
            super().__exit__(*args)
            if phase == 'close':
                raise OSError('synthetic private canary')
    wire = transport.ProvisionTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: Refusal(b''))
    with pytest.raises(transport.TransportError) as caught:
        wire.send(protocol.PROVISION_ACK, raw, signed(protocol.PROVISION_ACK, raw))
    assert caught.value.status == status
    assert 'canary' not in str(caught.value)


def test_queued_http_status_is_not_a_provision_receipt(monkeypatch):
    monkeypatch.setattr(transport.time, 'time', lambda: VECTORS['now'] / 1000)
    raw = canonical(value('AckRequest')).encode('ascii')
    response = Response(canonical(value('AckResponse')).encode('ascii'))
    response.code = 202
    wire = transport.ProvisionTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: response)
    with pytest.raises(transport.TransportError) as caught:
        wire.send(protocol.PROVISION_ACK, raw, signed(protocol.PROVISION_ACK, raw))
    assert caught.value.status == 503


@pytest.mark.parametrize('phase', ['body', 'close', 'timer-restore'])
def test_cancellation_preserves_observed_refusal_and_next_request_resets_it(monkeypatch, phase):
    interrupted = KeyboardInterrupt()
    if phase == 'timer-restore':
        prior = transport.signal.SIG_DFL
        restores = []
        monkeypatch.setattr(transport.signal, 'getsignal', lambda _sig: prior)
        monkeypatch.setattr(transport.signal, 'getitimer', lambda _timer: (0, 0))
        monkeypatch.setattr(transport.signal, 'setitimer', lambda *_args: None)
        def handler(_sig, callback):
            if callback == prior and not restores:
                restores.append(True)
                raise interrupted
        monkeypatch.setattr(transport.signal, 'signal', handler)
    class Refusal(Response):
        code = 401
        def read(self, limit):
            if phase == 'body':
                raise interrupted
            return b'{"code":"SCOPE_DENIED"}'
        def __exit__(self, *args):
            super().__exit__(*args)
            if phase == 'close':
                raise interrupted
    raw = canonical(value('AckRequest')).encode('ascii')
    wire = transport.ProvisionTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: Refusal(b''))
    with pytest.raises(KeyboardInterrupt) as caught:
        wire.send(protocol.PROVISION_ACK, raw, signed(protocol.PROVISION_ACK, raw))
    assert caught.value is interrupted
    assert wire.refusal_status == 401
    with pytest.raises(ValueError):
        wire.send('/v2/provisioning/unknown', b'{}', {})
    assert wire.refusal_status is None
