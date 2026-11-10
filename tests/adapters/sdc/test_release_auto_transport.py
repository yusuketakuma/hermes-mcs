"""v3 release transport: AT042-045 — bounded JSON/artifact, no redirect, fail-closed."""
import hashlib
import io
import json

import pytest

from adapters.sdc import protocol, release_auto_transport as t

ORIGIN = 'https://sdc.example.test'
CONFIG = {'origin': ORIGIN, 'audience': 'mcs-sdc', 'environment': 'test'}
STATE = {'device_id': '12345678-1234-4234-8234-123456789abc',
         'key_id': '12345678-1234-4234-8234-123456789abd'}
KEY = 'a' * 64


def headers_for(path, raw):
    return protocol.sign_release_auto(CONFIG, path, raw, STATE, KEY, now=1700000000, nonce='b' * 32)


def request(path='/v3/mcs/releases/status', body=None):
    raw = json.dumps(body or {'schema_version': 'x'}).encode()
    return raw, headers_for(path, raw)


class FakeHeaders:
    def __init__(self, values):
        self._values = {k.lower(): v for k, v in values.items()}

    def get(self, name, default=None):
        return self._values.get(name.lower(), default)


class FakeResponse:
    def __init__(self, status=200, body=b'', headers=None, chunked=False):
        self.code = status
        self._body = body
        self.headers = FakeHeaders(headers or {})
        self._pos = 0
        self._chunked = chunked

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._body) - self._pos
        if self._chunked:
            n = min(n, 7)  # force many small reads
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


def transport_with(response=None, error=None):
    transport = t.ReleaseAutoTransport(CONFIG)
    transport.opener = FakeOpener(response, error)
    return transport


def api_error(code='RATE_LIMITED', retry=1500, status=429):
    body = json.dumps({'schema_version': 'mcs.release-error.v3', 'code': code,
                       'retry_after_ms': retry}).encode()
    return FakeResponse(status, body, {'Content-Type': 'application/json'})


# --- request validation -------------------------------------------------

def test_json_paths_come_from_api_contract():
    assert t.JSON_PATHS == frozenset(
        r['path'] for r in t._ROUTES.values() if r['response_definition'] != 'binary-stream')
    assert t.ARTIFACT_PATH in t.ALL_PATHS and t.ARTIFACT_PATH not in t.JSON_PATHS
    assert '/v3/mcs/admin/rollouts/create' not in t.ALL_PATHS  # admin面はdevice transport外


@pytest.mark.parametrize('path', sorted(t.JSON_PATHS))
def test_every_json_path_roundtrip(path):
    raw, headers = request(path)
    transport = transport_with(FakeResponse(200, b'{"ok":true}'))
    assert transport.send_json(path, raw, headers) == b'{"ok":true}'
    sent = transport.opener.requests[0][0]
    assert sent.full_url == ORIGIN + path and sent.get_method() == 'POST'


@pytest.mark.parametrize('bad', ['/v3/mcs/releases/unknown', '/v2/heartbeats',
                                 'https://evil.example/v3/mcs/releases/status',
                                 '/v3/mcs/admin/v3/mcs/rollouts/create'])
def test_unlisted_path_rejected(bad):
    raw, headers = request('/v3/mcs/releases/status')
    transport = transport_with(FakeResponse(200, b'{}'))
    with pytest.raises(ValueError, match='RELEASE_AUTO_REQUEST_INVALID'):
        transport.send_json(bad, raw, headers)
    assert transport.opener.requests == []


def test_oversized_request_rejected():
    raw, headers = request()
    transport = transport_with(FakeResponse(200, b'{}'))
    with pytest.raises(ValueError):
        transport.send_json('/v3/mcs/releases/status', raw + b'x' * 20000, headers)


def test_missing_or_forged_headers_rejected():
    raw, _ = request()
    transport = transport_with(FakeResponse(200, b'{}'))
    for bad in ({}, {'X-SDC-Device': 'x'}, dict(headers_for('/v3/mcs/releases/status', raw),
                                              **{'X-SDC-Signature': 'z' * 64})):
        with pytest.raises(ValueError):
            transport.send_json('/v3/mcs/releases/status', raw, bad)


# --- JSON responses -----------------------------------------------------

def test_202_is_not_success():
    raw, headers = request()
    transport = transport_with(FakeResponse(202, b'{}'))
    with pytest.raises(t.ReleaseAutoError) as error:
        transport.send_json('/v3/mcs/releases/status', raw, headers)
    assert error.value.status == 202


def test_redirect_rejected():
    raw, headers = request()
    transport = transport_with(FakeResponse(302, b'', {'Location': 'https://evil.example/'}))
    with pytest.raises(t.ReleaseAutoError) as error:
        transport.send_json('/v3/mcs/releases/status', raw, headers)
    assert error.value.code == 'REDIRECT_DENIED'


def test_api_error_decoded_with_retry():
    raw, headers = request()
    transport = transport_with(api_error('RATE_LIMITED', 42000, 429))
    with pytest.raises(t.ReleaseAutoError) as error:
        transport.send_json('/v3/mcs/releases/status', raw, headers)
    assert (error.value.status, error.value.code, error.value.retry_after_ms) == (429, 'RATE_LIMITED', 42000)


def test_malformed_error_body_is_generic_denial():
    raw, headers = request()
    transport = transport_with(FakeResponse(503, b'<html>bad gateway</html>'))
    with pytest.raises(t.ReleaseAutoError) as error:
        transport.send_json('/v3/mcs/releases/status', raw, headers)
    assert error.value.code == 'REQUEST_DENIED'


def test_oversized_json_response_rejected():
    raw, headers = request()
    transport = transport_with(FakeResponse(200, b'x' * 20000))
    with pytest.raises(t.ReleaseAutoError) as error:
        transport.send_json('/v3/mcs/releases/status', raw, headers)
    assert error.value.code == 'RESPONSE_TOO_LARGE'


def test_network_failure_is_bounded_error():
    raw, headers = request()
    transport = transport_with(error=OSError('down'))
    with pytest.raises(t.ReleaseAutoError) as error:
        transport.send_json('/v3/mcs/releases/status', raw, headers)
    assert error.value.code == 'NETWORK_UNAVAILABLE'


# --- artifact stream ----------------------------------------------------

def artifact_body(size=200000):
    return bytes((i * 31 + 7) % 256 for i in range(size))


def artifact_request():
    raw = json.dumps({'schema_version': 'mcs.read-artifact.v3',
                      'artifact_sha256': 'c' * 64}).encode()
    return raw, headers_for(t.ARTIFACT_PATH, raw)


def fetch(transport, body, *, sha=None, size=None, sink=None):
    raw, headers = artifact_request()
    body = artifact_body() if body is None else body
    sha = hashlib.sha256(body).hexdigest() if sha is None else sha
    size = len(body) if size is None else size
    out = sink if sink is not None else io.BytesIO()
    result = transport.fetch_artifact(t.ARTIFACT_PATH, raw, headers,
                                      expected_sha256=sha, expected_size=size, sink=out)
    return result, out


def octet(body, **headers):
    return FakeResponse(200, body, {'Content-Type': 'application/octet-stream', **headers},
                        chunked=True)


def test_artifact_happy_path_streams_and_verifies():
    transport = transport_with(octet(artifact_body()))
    result, out = fetch(transport, artifact_body())
    assert result['size'] == len(artifact_body())
    assert result['sha256'] == hashlib.sha256(artifact_body()).hexdigest()
    assert out.getvalue() == artifact_body()


def test_artifact_content_length_mismatch_aborts():
    body = artifact_body()
    transport = transport_with(octet(body, **{'Content-Length': str(len(body) + 1)}))
    with pytest.raises(t.ReleaseAutoError) as error:
        fetch(transport, body)
    assert error.value.code == 'SIZE_MISMATCH'


def test_artifact_stream_over_expected_aborts():
    body = artifact_body()
    transport = transport_with(octet(body))
    with pytest.raises(t.ReleaseAutoError) as error:
        fetch(transport, body, size=len(body) - 1)  # declared smaller than stream
    assert error.value.code in ('SIZE_EXCEEDED', 'SIZE_MISMATCH')


def test_artifact_truncated_stream_aborts():
    body = artifact_body()[:-100]
    transport = transport_with(octet(body))
    with pytest.raises(t.ReleaseAutoError) as error:
        fetch(transport, artifact_body())
    assert error.value.code == 'SIZE_MISMATCH'


def test_artifact_digest_mismatch_aborts():
    body = artifact_body()
    transport = transport_with(octet(body))
    with pytest.raises(t.ReleaseAutoError) as error:
        fetch(transport, body, sha='0' * 64)
    assert error.value.code == 'DIGEST_MISMATCH'


def test_artifact_wrong_content_type_rejected():
    transport = transport_with(FakeResponse(200, artifact_body(),
                                            {'Content-Type': 'text/html'}))
    with pytest.raises(t.ReleaseAutoError) as error:
        fetch(transport, artifact_body())
    assert error.value.code == 'CONTENT_TYPE_INVALID'


def test_artifact_content_encoding_rejected():
    transport = transport_with(octet(artifact_body(), **{'Content-Encoding': 'gzip'}))
    with pytest.raises(t.ReleaseAutoError) as error:
        fetch(transport, artifact_body())
    assert error.value.code == 'CONTENT_ENCODING_INVALID'


def test_artifact_identity_encoding_accepted():
    transport = transport_with(octet(artifact_body(), **{'Content-Encoding': 'identity'}))
    result, _ = fetch(transport, artifact_body())
    assert result['size'] == len(artifact_body())


def test_artifact_error_response_decoded():
    transport = transport_with(api_error('OBJECT_INCOMPLETE', 0, 409))
    with pytest.raises(t.ReleaseAutoError) as error:
        fetch(transport, artifact_body())
    assert (error.value.status, error.value.code) == (409, 'OBJECT_INCOMPLETE')


def test_artifact_non_artifact_path_rejected():
    raw, headers = request('/v3/mcs/releases/status')
    transport = transport_with(octet(artifact_body()))
    with pytest.raises(ValueError):
        transport.fetch_artifact('/v3/mcs/releases/status', raw, headers,
                                 expected_sha256='c' * 64, expected_size=10, sink=io.BytesIO())


def test_artifact_expected_size_over_cap_rejected():
    raw, headers = artifact_request()
    transport = transport_with(octet(artifact_body()))
    with pytest.raises(ValueError):
        transport.fetch_artifact(t.ARTIFACT_PATH, raw, headers,
                                 expected_sha256='c' * 64,
                                 expected_size=t.ARTIFACT_MAX_BYTES + 1, sink=io.BytesIO())


# --- signing ------------------------------------------------------------

def test_sign_release_auto_binds_path():
    raw = json.dumps({'a': 1}).encode()
    headers = headers_for('/v3/mcs/releases/select', raw)
    assert set(headers) == {'X-SDC-Device', 'X-SDC-Key-ID', 'X-SDC-Time',
                            'X-SDC-Nonce', 'X-SDC-Signature'}


@pytest.mark.parametrize('bad', ['/v2/heartbeats', '/v3/mcs/admin/x',
                                 '/v3/mcs/releases/status/extra'])
def test_sign_rejects_non_v3_paths(bad):
    raw = b'{}'
    with pytest.raises(ValueError):
        protocol.sign_release_auto(CONFIG, bad, raw, STATE, KEY, now=1, nonce='b' * 32)
