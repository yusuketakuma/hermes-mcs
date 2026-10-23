"""Send one bounded request to a locally approved HTTPS origin."""
import re
from email.utils import parsedate_to_datetime
import signal
import ssl
import time
import urllib.error
import urllib.request

from mcs_util import NoRedirect, no_proxy_opener
from .protocol import PATHS, VERSION, endpoint, strict_json

_TIMEOUT_S = 10


class _DeadlineExpired(BaseException):
    """Cross transport error conversion before dispatching the caller's alarm."""


class TransportError(Exception):
    def __init__(self, status=503, code='SDC_NETWORK_UNAVAILABLE', retry_after=0):
        super().__init__(code)
        self.status, self.code, self.retry_after = status, code, retry_after


class Transport:
    def __init__(self, config):
        self.config = endpoint(config)
        self.opener = no_proxy_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def send(self, path, raw, headers=None):
        """Own a POSIX main-thread deadline while preserving the caller's alarm."""
        previous_handler = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
        if previous_delay and not callable(previous_handler):
            raise TransportError()  # Do not take ownership of another component's default/ignored alarm.
        previous_deadline = started + previous_delay if previous_delay else None
        previous_fired = False

        def expired(signum, frame):
            nonlocal previous_fired
            if previous_deadline is not None and time.monotonic() >= previous_deadline:
                previous_fired = True
            raise _DeadlineExpired(signum, frame)

        installed = True  # A signal may arrive immediately after installing the handler.
        try:
            try:
                signal.signal(signal.SIGALRM, expired)
            except ValueError:
                installed = False
                # Connector CLI and tick run on the main thread; never send unbounded elsewhere.
                raise ValueError('SDC_MAIN_THREAD_REQUIRED') from None
            deadline = min(started + _TIMEOUT_S, previous_deadline) if previous_deadline else started + _TIMEOUT_S
            delay = deadline - time.monotonic()
            if delay <= 0:
                expired(signal.SIGALRM, None)
            signal.setitimer(signal.ITIMER_REAL, delay)
            try:
                return self._send(path, raw, headers)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        except _DeadlineExpired as error:
            # The tick's deadline must escape Connector's TransportError retry path.
            if previous_fired and callable(previous_handler):
                previous_handler(*error.args)
            raise TransportError() from None
        finally:
            if installed:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
                if previous_deadline is not None:
                    remaining = previous_deadline - time.monotonic()
                    if previous_fired:
                        # An elapsed one-shot stays elapsed; periodic alarms keep their phase.
                        if previous_interval:
                            signal.setitimer(signal.ITIMER_REAL,
                                             max(1e-6, remaining % previous_interval), previous_interval)
                    else:
                        signal.setitimer(signal.ITIMER_REAL, max(1e-6, remaining), previous_interval)

    def _send(self, path, raw, headers=None):
        allowed = {'X-SDC-Device', 'X-SDC-Key-ID', 'X-SDC-Time', 'X-SDC-Nonce', 'X-SDC-Signature'}
        if path not in PATHS or not isinstance(raw, bytes) or len(raw) > 16384 or headers is not None and set(headers) != allowed:
            raise ValueError('SDC_REQUEST_INVALID')
        # Cloudflare's edge rejects the default Python-urllib signature (error 1010) before the Worker runs.
        request = urllib.request.Request(self.config['origin'] + path, data=raw, headers={'Content-Type': 'application/json', 'Content-Encoding': 'identity', 'User-Agent': 'sdc-connector/' + VERSION, **(headers or {})}, method='POST')
        started = time.monotonic()
        try:
            response = self.opener.open(request, timeout=_TIMEOUT_S)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, ValueError):
            raise TransportError() from None
        with response:
            if 300 <= response.code < 400:
                raise TransportError(403, 'SDC_REDIRECT_DENIED')
            try:
                raw_response = response.read(16385)
                if len(raw_response) > 16384 or time.monotonic() - started > _TIMEOUT_S:
                    raise ValueError
                data = strict_json(raw_response)
                if not isinstance(data, dict):
                    raise ValueError
            except (OSError, ValueError, RecursionError):
                raise TransportError() from None
            status = response.code
            if status not in (200, 202):
                code = data.get('code', '')
                code = code if isinstance(code, str) and re.fullmatch('[A-Z_]{1,64}', code) else 'SDC_REQUEST_DENIED'
                retry = response.headers.get('Retry-After', '')
                try:
                    delay = int(retry) if retry.isdigit() else int(parsedate_to_datetime(retry).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    delay = 0
                raise TransportError(status, code, max(0, min(delay, 86400)))
            return data
