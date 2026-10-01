"""Bounded LINE WORKS JWT authentication and Bot REST transport."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

AUTH_URL = "https://auth.worksmobile.com/oauth2/v2.0/token"
API_BASE = "https://www.worksapis.com/v1.0"
UPLOAD_HOSTS = frozenset({"apis-storage.worksmobile.com", "storage.worksmobile.com"})
MAX_RESPONSE_BYTES = 262144
MAX_REQUEST_BYTES = 25 * 1024 * 1024


class ClientError(RuntimeError):
    """Redacted failure; response content and credentials are never retained."""

    def __init__(self, error_code: str, status: int | None = None):
        self.error_code = error_code
        self.status = status
        super().__init__(error_code)


def _text(value, limit=512):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                        for c in value))


def valid_filename(filename):
    return _text(filename, 255) and not any(c in filename for c in '/\\"')


def _validate_content(content):
    if not isinstance(content, dict):
        raise ClientError("validation_invalid")
    kind = content.get("type")
    if kind == "text":
        value = content.get("text")
        valid = isinstance(value, str) and 0 < len(value) <= 2000
    elif kind == "file":
        valid = _text(content.get("fileId"), 1024)
    elif kind == "button_template":
        value, actions = content.get("contentText"), content.get("actions")
        valid = (isinstance(value, str) and 0 < len(value) <= 1000
                 and isinstance(actions, list) and 1 <= len(actions) <= 10)
        if valid:
            for action in actions:
                if not isinstance(action, dict) or not _text(action.get("label"), 20):
                    valid = False
                    break
                if action.get("type") == "message":
                    valid = _text(action.get("postback"), 1000)
                elif action.get("type") == "uri":
                    valid = (_text(action.get("uri"), 1000)
                             and action["uri"].startswith(("https://", "http://")))
                else:
                    valid = False
                if not valid:
                    break
    else:
        valid = False
    if not valid:
        raise ClientError("validation_invalid")


@dataclass(frozen=True, repr=False)
class Credentials:
    """Explicit credentials; secrets are never read from ambient environment."""

    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    service_account: str = field(repr=False)
    private_key_path: str = field(repr=False)
    bot_id: str = field(repr=False)

    def __post_init__(self):
        if (not all(_text(v) for v in (self.client_id, self.client_secret,
                                      self.service_account, self.private_key_path))
                or not isinstance(self.bot_id, str) or not 0 < len(self.bot_id) <= 19
                or not self.bot_id.isascii()
                or not self.bot_id.isdigit() or not 0 < int(self.bot_id) < 2**63):
            raise ClientError("credentials_invalid")


def verify_signature(body: bytes, signature: str, bot_secret: str) -> bool:
    """Verify X-WORKS-Signature over the untouched HTTP request bytes."""
    if (not isinstance(body, bytes) or not isinstance(signature, str)
            or len(signature) != 44 or not _text(bot_secret, 4096)):
        return False
    try:
        supplied = base64.b64decode(signature, validate=True)
        expected = hmac.new(bot_secret.encode("utf-8"), body, hashlib.sha256).digest()
    except (ValueError, UnicodeError):
        return False
    return hmac.compare_digest(expected, supplied)


def _runtime_environment():
    return {k: v for k, v in os.environ.items()
            if k in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}}


def _sign(signing_input: bytes, private_key_path: str, timeout: float) -> bytes:
    try:
        key = Path(private_key_path).expanduser().resolve(strict=True)
        info = key.stat()
        if not key.is_file() or (os.name == "posix" and info.st_mode & 0o077):
            raise ClientError("private_key_permissions")
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key),
             "-sigopt", "rsa_padding_mode:pkcs1"],
            input=signing_input, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False, env=_runtime_environment())
        if result.returncode or not 256 <= len(result.stdout) <= 1024:
            raise ClientError("signature_failed")
        return result.stdout
    except ClientError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ClientError("signature_failed") from None


def _allowed_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None
                or parts.fragment or not _text(url, 8192)):
            return False
        return (url == AUTH_URL
                or (parts.netloc == "www.worksapis.com"
                    and parts.path.startswith("/v1.0/bots/"))
                or (parts.hostname in UPLOAD_HOSTS
                    and parts.path.startswith("/k/emsg/")))
    except (ValueError, TypeError):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_worker():
    """Perform one request; the parent kills/reaps it at the absolute deadline."""
    try:
        data = json.loads(sys.stdin.buffer.read(MAX_REQUEST_BYTES * 2))
        url = data["url"]
        timeout = data["timeout"]
        if (not _allowed_url(url) or data["method"] != "POST"
                or isinstance(timeout, bool) or not isinstance(timeout, int | float)
                or not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise ValueError
        body = base64.b64decode(data["body"], validate=True)
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError
        headers = data["headers"]
        if (not isinstance(headers, dict)
                or set(headers) - {"Authorization", "Content-Type"}
                or not all(_text(value, 8192) for value in headers.values())):
            raise ValueError
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            result = {"status": response.code, "body": base64.b64encode(raw).decode("ascii"),
                      "headers": {"RateLimit-Reset": response.headers.get("RateLimit-Reset", "")}}
        finally:
            response.close()
    except Exception:
        result = {"error": "transport_unknown"}
    sys.stdout.write(json.dumps(result))


def _http_request(method, url, headers, body, timeout):
    if not _allowed_url(url) or method != "POST" or len(body) > MAX_REQUEST_BYTES:
        raise ClientError("validation_invalid")
    envelope = json.dumps({"method": method, "url": url, "headers": headers,
                           "body": base64.b64encode(body).decode("ascii"),
                           "timeout": timeout}).encode("utf-8")
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--http-worker"],
            input=envelope, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False, env=_runtime_environment())
        parsed = json.loads(result.stdout)
        if result.returncode or "error" in parsed:
            raise ClientError("transport_unknown")
        return parsed["status"], parsed.get("headers", {}), base64.b64decode(parsed["body"], validate=True)
    except ClientError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise ClientError("transport_unknown") from None


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _json(raw):
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError
        return parsed
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ClientError("response_invalid") from None


class LineWorksClient:
    """Single-attempt Bot API client with a synchronized in-memory token cache."""

    def __init__(self, credentials: Credentials, *, transport=None, signer=None,
                 clock=time.time, monotonic=time.monotonic, timeout=20,
                 max_upload_bytes=24 * 1024 * 1024):
        if (not isinstance(credentials, Credentials) or isinstance(timeout, bool)
                or not isinstance(timeout, int | float) or not math.isfinite(timeout)
                or not 0 < timeout <= 120 or isinstance(max_upload_bytes, bool)
                or not isinstance(max_upload_bytes, int)
                or not 0 < max_upload_bytes <= 24 * 1024 * 1024):
            raise ClientError("validation_invalid")
        self.credentials = credentials
        self.transport = transport or _http_request
        self.signer = signer or _sign
        self.clock, self.monotonic = clock, monotonic
        self.timeout, self.max_upload_bytes = float(timeout), max_upload_bytes
        self._lock = threading.Lock()
        self._wire_lock = threading.Lock()
        self._token, self._expires = "", 0.0
        self._not_before = 0.0

    def _request(self, url, body, content_type, token=None):
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        try:
            with self._wire_lock:
                if self.monotonic() < self._not_before:
                    raise ClientError("rate_limited", 429)
                status, response_headers, raw = self.transport("POST", url, headers, body, self.timeout)
                if status == 429:
                    try:
                        reset = float(next(value for key, value in response_headers.items()
                                           if key.casefold() == "ratelimit-reset"))
                    except (StopIteration, AttributeError, TypeError, ValueError):
                        reset = 60
                    if not math.isfinite(reset) or not 0 < reset <= 60:
                        reset = 60
                    self._not_before = self.monotonic() + reset
        except ClientError:
            raise
        except Exception:
            raise ClientError("transport_unknown") from None
        if (isinstance(status, bool) or not isinstance(status, int)
                or not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES):
            raise ClientError("response_invalid")
        if not 200 <= status < 300:
            if status == 401 and token is not None:
                with self._lock:
                    if self._token == token:
                        self._token, self._expires = "", 0.0
            raise ClientError("rate_limited" if status == 429 else "http_error", status)
        return status, raw

    def _access_token(self):
        with self._lock:
            if self._token and self.monotonic() < self._expires:
                return self._token
            now = int(self.clock())
            cred = self.credentials
            header = _b64(b'{"alg":"RS256","typ":"JWT"}')
            claims = _b64(json.dumps({"iss": cred.client_id, "sub": cred.service_account,
                                      "iat": now - 30, "exp": now + 3300},
                                     separators=(",", ":")).encode("utf-8"))
            signing_input = (header + "." + claims).encode("ascii")
            try:
                signature = self.signer(signing_input, cred.private_key_path, self.timeout)
                if not isinstance(signature, bytes) or not signature:
                    raise ValueError
            except Exception:
                raise ClientError("signature_failed") from None
            body = urllib.parse.urlencode({
                "assertion": signing_input.decode("ascii") + "." + _b64(signature),
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "client_id": cred.client_id, "client_secret": cred.client_secret,
                "scope": "bot.message"}).encode("ascii")
            started = self.monotonic()
            _, raw = self._request(AUTH_URL, body, "application/x-www-form-urlencoded")
            response = _json(raw)
            token = response.get("access_token")
            lifetime = response.get("expires_in")
            if isinstance(lifetime, str) and len(lifetime) <= 5 \
                    and lifetime.isascii() and lifetime.isdigit():
                lifetime = int(lifetime)
            token_type = response.get("token_type")
            if (not _text(token, 8192) or not isinstance(token_type, str)
                    or token_type.casefold() != "bearer"
                    or isinstance(lifetime, bool) or not isinstance(lifetime, int)
                    or not 60 <= lifetime <= 86400):
                raise ClientError("response_invalid")
            self._token = token
            self._expires = started + lifetime - 30
            return token

    def send_message(self, content: dict, *, channel_id=None, user_id=None):
        """Send content once; HTTP 201 is the official receipt, without message IDs."""
        if ((channel_id is None) == (user_id is None) or not isinstance(content, dict)
                or not _text(content.get("type"))):
            raise ClientError("validation_invalid")
        _validate_content(content)
        target = channel_id if channel_id is not None else user_id
        if not _text(target) or user_id == "me":
            raise ClientError("validation_invalid")
        kind = "channels" if channel_id is not None else "users"
        url = (f"{API_BASE}/bots/{self.credentials.bot_id}/{kind}/"
               f"{urllib.parse.quote(target, safe='')}/messages")
        try:
            body = json.dumps({"content": content}, ensure_ascii=False,
                              allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ClientError("validation_invalid") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise ClientError("validation_invalid")
        status, _ = self._request(url, body, "application/json; charset=UTF-8", self._access_token())
        if status != 201:
            raise ClientError("response_invalid", status)
        return {"status": status}

    def upload_file(self, data: bytes, filename: str):
        """Upload sealed bytes once using the official two-step multipart contract."""
        if (not isinstance(data, bytes) or len(data) > self.max_upload_bytes
                or not valid_filename(filename)):
            raise ClientError("validation_invalid")
        token = self._access_token()
        url = f"{API_BASE}/bots/{self.credentials.bot_id}/attachments"
        _, raw = self._request(url, json.dumps({"fileName": filename}).encode("utf-8"),
                               "application/json; charset=UTF-8", token)
        response = _json(raw)
        upload_url, file_id = response.get("uploadUrl"), response.get("fileId")
        if (not isinstance(upload_url, str) or not _allowed_url(upload_url)
                or urllib.parse.urlsplit(upload_url).hostname not in UPLOAD_HOSTS):
            raise ClientError("upload_url_invalid")
        if not _text(file_id, 1024):
            raise ClientError("response_invalid")
        boundary = "mcs-" + secrets.token_hex(24)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="resourceName"\r\n\r\n'
                f'{filename}\r\n--{boundary}\r\nContent-Disposition: form-data; name="Filedata"; '
                f'filename="{filename}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
        body += data + f"\r\n--{boundary}--\r\n".encode("ascii")
        _, raw = self._request(upload_url, body, "multipart/form-data; boundary=" + boundary, token)
        uploaded = _json(raw)
        uploaded_id = uploaded.get("fileId")
        if not _text(uploaded_id, 1024):
            raise ClientError("response_invalid")
        return uploaded_id


if __name__ == "__main__" and sys.argv[1:] == ["--http-worker"]:
    _http_worker()
