"""Send one bounded request to a locally approved HTTPS origin."""
import re
from email.utils import parsedate_to_datetime
import ssl
import time
import urllib.error
import urllib.request

from mcs_util import NoRedirect, no_proxy_opener
from .protocol import PATHS, VERSION, endpoint, strict_json


class TransportError(Exception):
    def __init__(self, status=503, code='SDC_NETWORK_UNAVAILABLE', retry_after=0):
        super().__init__(code)
        self.status, self.code, self.retry_after = status, code, retry_after


class Transport:
    def __init__(self, config):
        self.config = endpoint(config)
        self.opener = no_proxy_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def send(self, path, raw, headers=None):
        allowed = {'X-SDC-Device', 'X-SDC-Key-ID', 'X-SDC-Time', 'X-SDC-Nonce', 'X-SDC-Signature'}
        if path not in PATHS or not isinstance(raw, bytes) or len(raw) > 16384 or headers is not None and set(headers) != allowed:
            raise ValueError('SDC_REQUEST_INVALID')
        # Cloudflare's edge rejects the default Python-urllib signature (error 1010) before the Worker runs.
        request = urllib.request.Request(self.config['origin'] + path, data=raw, headers={'Content-Type': 'application/json', 'Content-Encoding': 'identity', 'User-Agent': 'sdc-connector/' + VERSION, **(headers or {})}, method='POST')
        started = time.monotonic()
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, ValueError):
            raise TransportError() from None
        with response:
            if 300 <= response.code < 400:
                raise TransportError(403, 'SDC_REDIRECT_DENIED')
            try:
                raw_response = response.read(16385)
                if len(raw_response) > 16384 or time.monotonic() - started > 10:
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
