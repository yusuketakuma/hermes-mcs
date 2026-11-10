"""v3 release transport: fixed device paths, bounded JSON replies and artifact stream.

spec 03§3: 単一登録済みHTTPS originの固定POST pathのみ。JSON制御は
api-contract.jsonのmax_request/response_bytes、artifactは
application/octet-stream 200のみ・256MiB上限・64KiB chunk・総deadline 300秒・
Content-Encoding identity・redirect禁止・予想size超過で即中止。Range再開なし。
"""
import hashlib
import json
import re
import signal
import ssl
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from mcs_util import NoRedirect, no_proxy_opener
from .protocol import endpoint, _support_headers, VERSION

_CONTRACT = json.loads(
    (Path(__file__).resolve().parent / 'contracts/mcs-auto/v3/contracts/api-contract.json').read_text())
_ROUTES = {r['path']: r for r in _CONTRACT['routes'] if r.get('auth') == 'device'}
ARTIFACT_PATH = '/v3/mcs/releases/artifact'
JSON_PATHS = frozenset(p for p in _ROUTES if p != ARTIFACT_PATH)
ALL_PATHS = frozenset(_ROUTES)
_ARTIFACT_PATHS = frozenset({ARTIFACT_PATH})
ARTIFACT_MAX_BYTES = 268435456
ARTIFACT_CHUNK = 65536
ARTIFACT_DEADLINE_S = 300.0
ARTIFACT_READ_TIMEOUT_S = 30.0
_JSON_DEADLINE_S = 10.0
_ERROR_MAX_BYTES = 1024
_REDIRECT = range(300, 400)


class ReleaseAutoError(Exception):
    """v3 transport refusal; status=0 means the failure was local/transport-side."""

    def __init__(self, status, code, retry_after_ms=0):
        super().__init__(code)
        self.status, self.code, self.retry_after_ms = status, code, retry_after_ms


class _DeadlineExpired(BaseException):
    """Cross error-conversion boundary before re-raising the caller's alarm."""


@contextmanager
def _deadline(seconds):
    """Own a POSIX main-thread monotonic deadline; preserve the caller's alarm."""
    previous_handler = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    if previous_delay and not callable(previous_handler):
        raise ReleaseAutoError(0, 'DEADLINE_UNAVAILABLE')
    previous_deadline = started + previous_delay if previous_delay else None
    previous_fired = False

    def expired(signum, frame):
        nonlocal previous_fired
        if previous_deadline is not None and time.monotonic() >= previous_deadline:
            previous_fired = True
        raise _DeadlineExpired(signum, frame)

    try:
        try:
            signal.signal(signal.SIGALRM, expired)
        except ValueError:
            raise ReleaseAutoError(0, 'MAIN_THREAD_REQUIRED') from None
        deadline = min(started + seconds, previous_deadline) if previous_deadline else started + seconds
        delay = deadline - time.monotonic()
        if delay <= 0:
            expired(signal.SIGALRM, None)
        signal.setitimer(signal.ITIMER_REAL, delay)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except _DeadlineExpired as error:
        if previous_fired and callable(previous_handler):
            previous_handler(*error.args)
        raise ReleaseAutoError(0, 'DEADLINE_EXCEEDED') from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_deadline is not None:
            remaining = previous_deadline - time.monotonic()
            if previous_fired:
                if previous_interval:
                    signal.setitimer(signal.ITIMER_REAL,
                                     max(1e-6, remaining % previous_interval), previous_interval)
            else:
                signal.setitimer(signal.ITIMER_REAL, max(1e-6, remaining), previous_interval)


def _check_request(path, raw, headers, *, paths, max_bytes):
    if (type(path) is not str or path not in paths or type(raw) is not bytes
            or not raw or len(raw) > max_bytes
            or type(headers) is not dict or not _support_headers(headers)):
        raise ValueError('RELEASE_AUTO_REQUEST_INVALID')


def _media_type(value):
    return value.split(';', 1)[0].strip().lower() if type(value) is str else ''


def _decode_error(raw, status):
    """mcs.release-error.v3 closed object ≤1KiB → ReleaseAutoError. Malformed → generic."""
    code, retry = 'REQUEST_DENIED', 0
    if type(raw) is bytes and 0 < len(raw) <= _ERROR_MAX_BYTES:
        try:
            value = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError, RecursionError):
            value = None
        if (type(value) is dict and set(value) == {'schema_version', 'code', 'retry_after_ms'}
                and value['schema_version'] == 'mcs.release-error.v3'
                and type(value['code']) is str
                and re.fullmatch(r'[A-Z_]{1,64}', value['code'])
                and type(value['retry_after_ms']) is int
                and 0 <= value['retry_after_ms'] <= 86400000):
            code, retry = value['code'], value['retry_after_ms']
    return ReleaseAutoError(status, code, retry)


class ReleaseAutoTransport:
    """One bounded v3 request/response exchange on the approved origin."""

    def __init__(self, config):
        self.config = endpoint(dict(config))
        self.opener = no_proxy_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def _open(self, path, raw, headers, timeout):
        request = urllib.request.Request(
            self.config['origin'] + path, data=raw,
            headers={'Content-Type': 'application/json',
                     'Content-Encoding': 'identity',
                     'User-Agent': 'sdc-connector/' + VERSION, **headers},
            method='POST')
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error
        except (OSError, ValueError):
            raise ReleaseAutoError(0, 'NETWORK_UNAVAILABLE') from None

    def send_json(self, path, raw, headers):
        """POST a ≤16KiB signed JSON request; return exact 200 response bytes."""
        route = _ROUTES.get(path)
        _check_request(path, raw, headers, paths=JSON_PATHS,
                       max_bytes=route['max_request_bytes'] if route else 0)
        max_response = route['max_response_bytes']
        with _deadline(_JSON_DEADLINE_S):
            response = self._open(path, raw, headers, _JSON_DEADLINE_S)
            with response:
                status = response.code
                if status in _REDIRECT:
                    raise ReleaseAutoError(status, 'REDIRECT_DENIED')
                try:
                    body = response.read(max_response + 1)
                except (OSError, ValueError):
                    raise ReleaseAutoError(0, 'NETWORK_UNAVAILABLE') from None
                if len(body) > max_response:
                    raise ReleaseAutoError(status, 'RESPONSE_TOO_LARGE')
                if status == 200:
                    return body
                raise _decode_error(body, status)

    def fetch_artifact(self, path, raw, headers, *, expected_sha256, expected_size, sink):
        """Stream the artifact to sink; return {'sha256','size'} only on exact match.

        expected_sha256/expected_size come from the verified manifest. Any
        deviation aborts; the caller owns discarding the partial stage.
        """
        route = _ROUTES[ARTIFACT_PATH]
        _check_request(path, raw, headers, paths=_ARTIFACT_PATHS,
                       max_bytes=route['max_request_bytes'])
        if (type(expected_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
                or type(expected_size) is not int
                or not 0 < expected_size <= ARTIFACT_MAX_BYTES):
            raise ValueError('RELEASE_AUTO_REQUEST_INVALID')
        digest = hashlib.sha256()
        received = 0
        with _deadline(ARTIFACT_DEADLINE_S):
            response = self._open(path, raw, headers, ARTIFACT_READ_TIMEOUT_S)
            with response:
                status = response.code
                if status in _REDIRECT:
                    raise ReleaseAutoError(status, 'REDIRECT_DENIED')
                headers_in = response.headers
                if status != 200:
                    try:
                        body = response.read(_ERROR_MAX_BYTES + 1)
                    except (OSError, ValueError):
                        body = b''
                    if len(body) > _ERROR_MAX_BYTES:
                        body = b''
                    raise _decode_error(body, status)
                if _media_type(headers_in.get('Content-Type')) != 'application/octet-stream':
                    raise ReleaseAutoError(status, 'CONTENT_TYPE_INVALID')
                encoding = headers_in.get('Content-Encoding')
                if encoding is not None and encoding.strip().lower() != 'identity':
                    raise ReleaseAutoError(status, 'CONTENT_ENCODING_INVALID')
                declared = headers_in.get('Content-Length')
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        raise ReleaseAutoError(status, 'CONTENT_LENGTH_INVALID') from None
                    if declared_size != expected_size or declared_size > ARTIFACT_MAX_BYTES:
                        raise ReleaseAutoError(status, 'SIZE_MISMATCH')
                while True:
                    try:
                        chunk = response.read(min(ARTIFACT_CHUNK, expected_size - received + 1))
                    except (OSError, ValueError):
                        raise ReleaseAutoError(0, 'NETWORK_UNAVAILABLE') from None
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > expected_size:
                        raise ReleaseAutoError(status, 'SIZE_EXCEEDED')
                    digest.update(chunk)
                    sink.write(chunk)
                    sink.flush()
                if received != expected_size:
                    raise ReleaseAutoError(status, 'SIZE_MISMATCH')
                result = digest.hexdigest()
                if result != expected_sha256:
                    raise ReleaseAutoError(status, 'DIGEST_MISMATCH')
                return {'sha256': result, 'size': received}
