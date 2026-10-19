"""Attachment URL policy and child-owned streaming to a private partial."""
from __future__ import annotations

from contextlib import suppress
import errno
import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from mcs_contract import MCSError
from mcs_util import RETRY_LATER_4XX, no_proxy_opener

_ALLOWED_DOWNLOAD_HOSTS = {"www.medical-care.net"}
_ALLOWED_REDIRECT_HOSTS = _ALLOWED_DOWNLOAD_HOSTS | {"files.medical-care.net"}
_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


def _allowed_port(u) -> bool:
    try:
        return u.port in (None, 443)
    except ValueError:
        return False  # malformed port — refuse, never follow


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when the target stays on an allowlisted host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            u = urllib.parse.urlparse(newurl)
            ok = (u.scheme == "https"
                  and u.hostname in _ALLOWED_DOWNLOAD_HOSTS
                  and _allowed_port(u)
                  and u.username is None and u.password is None and not u.fragment)
        except ValueError:
            ok = False  # malformed redirect target — refuse, never follow
        if not ok:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_download_url(url: str):
    try:
        u = urllib.parse.urlparse(url)
        ok_port = _allowed_port(u)
        host = u.hostname
    except ValueError:
        # urlparse/.hostname/.port raise on bad brackets or malformed
        # ports — a stored malformed URL must fail as MCSError, not
        # escape as a bare ValueError
        raise MCSError("url_not_allowed", "unparseable") from None
    if (u.scheme != "https" or host not in _ALLOWED_DOWNLOAD_HOSTS
            or not ok_port or u.username is not None or u.password is not None
            or u.fragment):
        raise MCSError("url_not_allowed", "download origin invalid")


def disk_error_kind(e: BaseException) -> str:
    """'disk_full' for a local out-of-space write, so the caller can
    defer instead of burning a download attempt; else download_failed."""
    full = isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT)
    return "disk_full" if full else "download_failed"


def _remaining_timeout(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MCSError("deadline_exceeded", retryable=True)
    return min(maximum, remaining)


def _open_download(url: str, token, deadline, opener):
    if not token:
        raise MCSError("no_token")
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        return opener.open(req, timeout=_remaining_timeout(deadline, 60))
    except urllib.error.HTTPError as e:
        try:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("location")
                if loc:
                    try:
                        u = urllib.parse.urlparse(loc)
                        ok = (u.scheme == "https"
                              and u.hostname in _ALLOWED_REDIRECT_HOSTS
                              and _allowed_port(u)
                              and u.username is None and u.password is None
                              and not u.fragment)
                    except ValueError:
                        ok = False  # malformed Location — never follow
                    if ok:
                        # signed CDN URL authenticates itself — follow with
                        # a fresh request carrying NO Authorization header
                        return opener.open(
                            urllib.request.Request(loc),
                            timeout=_remaining_timeout(deadline, 60))
                    # a rejected/malformed redirect target is permanent —
                    # fail as url_not_allowed instead of retrying
                    raise MCSError("url_not_allowed",
                                   "redirect rejected") from e
            raise
        finally:
            # A redirect can consume the first error without returning its body.
            with suppress(OSError):
                e.close()


def download_to_part(url: str, tmp: str, *, token: str | None,
                     timeout: float) -> dict:
    """Write one child-owned partial; the supervising parent commits it.

    The token and remaining timeout are values, never parent callbacks. This
    call owns its HTTP response/output handles and never renames the partial.
    """
    opener = no_proxy_opener(_SameHostRedirect)
    deadline = time.monotonic() + timeout
    validate_download_url(url)
    total = 0
    h = hashlib.sha256()
    try:
        with _open_download(url, token, deadline, opener) as res, open(tmp, "wb") as f:
            while True:
                _remaining_timeout(deadline, 60)
                chunk = res.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    raise MCSError("download_too_large")
                h.update(chunk)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        _remaining_timeout(deadline, 60)
        if total == 0:
            raise MCSError("download_empty")
        return {"bytes": total, "sha256": h.hexdigest()}
    except Exception as e:
        # every failure path must remove the partial file (Oracle B28)
        with suppress(OSError):
            os.unlink(tmp)
        if isinstance(e, MCSError):
            raise
        if isinstance(e, urllib.error.HTTPError):
            # Includes a failed second hop; never drain an unused error body.
            with suppress(OSError):
                e.close()
            raise MCSError("http_error", status=e.code,
                           retryable=e.code in RETRY_LATER_4XX
                           or e.code >= 500) from e
        raise MCSError(disk_error_kind(e), retryable=True) from e
