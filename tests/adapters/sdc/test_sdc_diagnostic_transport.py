"""Fixed diagnostic routes preserve raw HMAC, strict responses and bounded transport admission."""
import hashlib
import hmac
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.sdc import protocol, transport
from adapters.sdc.support_contract import canonical

CONFIG = {'origin': 'https://support.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
IDENTITY = {'device_id': '11111111-1111-4111-8111-111111111111',
            'key_id': '22222222-2222-4222-8222-222222222222'}
KEY, NONCE = 'a1' * 32, 'b2' * 16
VECTORS = json.loads((Path(__file__).resolve().parents[3] /
                      'adapters/sdc/contracts/diagnostics/diagnostics-vectors.json').read_text())
ROUTES = [(protocol.DIAGNOSTIC_EVENTS_REPORT, 'Batch', 'Receipt'),
          (protocol.DIAGNOSTIC_EVENTS_STATUS, 'StatusRequest', 'Status'),
          (protocol.DIAGNOSTIC_ARTIFACT_SUBMIT, 'ArtifactSubmit', 'ArtifactReceipt')]


def value(definition):
    return next(item['value'] for item in VECTORS['positive'] if item['definition'] == definition)


def request(path, definition):
    raw = canonical(value(definition)).encode('ascii')
    headers = protocol.sign_diagnostic(CONFIG, path, raw, IDENTITY, KEY,
                                       now=VECTORS['now'] / 1000, nonce=NONCE)
    return raw, headers


class Response(io.BytesIO):
    code = 200
    headers = {}


@pytest.mark.parametrize('path,definition,result', ROUTES)
def test_exact_diagnostic_request_and_strict_response(path, definition, result):
    raw, headers = request(path, definition)
    expected = {**value(result), 'request_id': IDENTITY['device_id']}
    calls = []

    def reply(req, timeout):
        calls.append((req, timeout))
        return Response(canonical(expected).encode('ascii'))

    wire = transport.DiagnosticTransport(CONFIG)
    wire.opener = SimpleNamespace(open=reply)
    assert wire.send(path, raw, headers) == expected
    assert len(calls) == 1
    req, timeout = calls[0]
    assert req.data == raw and req.full_url == CONFIG['origin'] + path
    assert req.get_method() == 'POST' and timeout == 10
    signed = '\n'.join(('SDC-HMAC-v1', CONFIG['audience'], 'POST', path,
                        IDENTITY['device_id'], IDENTITY['key_id'], str(VECTORS['now'] // 1000),
                        NONCE, hashlib.sha256(raw).hexdigest())).encode('ascii')
    assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex(KEY), signed, hashlib.sha256).hexdigest()


@pytest.mark.parametrize('path,definition,result', ROUTES)
@pytest.mark.parametrize('client', [transport.Transport, transport.SupportTransport,
                                   transport.RestartTransport, transport.ProvisionTransport])
def test_diagnostic_admission_does_not_expand_existing_transports(path, definition, result, client):
    raw, headers = request(path, definition)
    wire = client(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: pytest.fail('unexpected dispatch'))
    with pytest.raises(ValueError):
        wire.send(path, raw, headers)


@pytest.mark.parametrize('path', ['/v2/heartbeats', '/v2/support/operations/grant',
                                  '/v2/diagnostics/events/report?x=1', '/admin/v1/diagnostics/events/read'])
def test_diagnostic_signer_and_transport_reject_other_purposes(path):
    with pytest.raises(ValueError):
        protocol.sign_diagnostic(CONFIG, path, b'{}', IDENTITY, KEY, now=VECTORS['now'] / 1000, nonce=NONCE)
    wire = transport.DiagnosticTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: pytest.fail('unexpected dispatch'))
    with pytest.raises(ValueError):
        wire.send(path, b'{}', {})


@pytest.mark.parametrize('path,definition,result', ROUTES)
@pytest.mark.parametrize('body', [b'{}', b'[]', b'\xef\xbb\xbf{}', b'{"x":1,"x":2}', b'x' * 16385])
def test_invalid_diagnostic_success_remains_unknown(path, definition, result, body):
    raw, headers = request(path, definition)
    wire = transport.DiagnosticTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: Response(body))
    with pytest.raises(transport.TransportError) as caught:
        wire.send(path, raw, headers)
    assert caught.value.status == 503
    assert str(caught.value) == 'SDC_NETWORK_UNAVAILABLE'


def test_negative_zero_is_rejected_in_original_diagnostic_request_and_response():
    path = protocol.DIAGNOSTIC_EVENTS_REPORT
    raw = canonical({**value('Batch'), 'dropped_events': 0}).encode('ascii')
    headers = protocol.sign_diagnostic(CONFIG, path, raw, IDENTITY, KEY,
                                       now=VECTORS['now'] / 1000, nonce=NONCE)
    negative = raw.replace(b'"dropped_events":0', b'"dropped_events":-0')
    assert negative != raw
    with pytest.raises(ValueError):
        protocol.sign_diagnostic(CONFIG, path, negative, IDENTITY, KEY, now=VECTORS['now'] / 1000, nonce=NONCE)
    response = canonical({**value('Receipt'), 'dropped_events': 0,
                          'request_id': IDENTITY['device_id']}).encode('ascii')
    negative = response.replace(b'"dropped_events":0', b'"dropped_events":-0')
    assert negative != response
    wire = transport.DiagnosticTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: Response(negative))
    with pytest.raises(transport.TransportError):
        wire.send(path, raw, headers)


@pytest.mark.parametrize('path,definition,result', ROUTES)
def test_queued_diagnostic_response_is_not_a_receipt(path, definition, result):
    raw, headers = request(path, definition)
    response = Response(canonical({**value(result), 'request_id': IDENTITY['device_id']}).encode('ascii'))
    response.code = 202
    wire = transport.DiagnosticTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: response)
    with pytest.raises(transport.TransportError) as caught:
        wire.send(path, raw, headers)
    assert caught.value.status == 503


@pytest.mark.parametrize('path,definition,result', ROUTES)
@pytest.mark.parametrize('status', [401, 403, 429])
@pytest.mark.parametrize('phase', ['body', 'close'])
def test_diagnostic_refusal_survives_response_failure(path, definition, result, status, phase):
    raw, headers = request(path, definition)

    class Refusal(Response):
        code = status

        def read(self, limit):
            if phase == 'body':
                raise OSError('synthetic private canary')
            return b'{"code":"SCOPE_DENIED"}'

        def __exit__(self, *args):
            super().__exit__(*args)
            if phase == 'close':
                raise OSError('synthetic private canary')

    wire = transport.DiagnosticTransport(CONFIG)
    wire.opener = SimpleNamespace(open=lambda *_a, **_k: Refusal(b''))
    with pytest.raises(transport.TransportError) as caught:
        wire.send(path, raw, headers)
    assert caught.value.status == wire.refusal_status == status
    assert 'canary' not in str(caught.value)
