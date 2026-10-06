"""MCS (MedicalCare Station) adapter — API-first, CDP token bootstrap.

Verified contract (live session, 2026-09):
  auth    : Authorization: Bearer <36char> from localStorage['ngStorage-lastSessionToken']
            (ngStorage JSON-encodes values -> must JSON.parse)
  unread  : GET /api/v2t/projects?include_meta=1&per_page=N&page=P&include_paginate_totals=0
            -> {paginate:{timestamp,...}, projects:[{id,type,karte,is_unread,last_message}]}
            (the dedicated /projects/unread route went 403 server-side while
            every sibling stayed 200 — the web app enumerates unread this way)
  messages: GET /api/v2t/projects/{id}/messages?unread=1&timestamp={ts}&keep_read_status=1
            &include_meta=1&exclude_terminated_ex_application=1&include_paginate_totals=1
  threads : GET /api/v2t/projects/{pid}/messages/{mid}/messages  (full reply bodies)
  latest  : GET /api/v2t/projects/{id}/messages/latest -> {is_self_only,message{id}}
  files   : GET {files[].url}  (anonymous 200 — no auth/cookie needed)
  mark    : GET /api/v2t/projects/{id}/messages?unread=1&timestamp={ts}
            without keep_read_status, followed by project-detail confirmation.
            The adapter requires the collection snapshot timestamp.
  session : ~30min sliding expiry observed via file_access_token cookie rolling;
            whether Bearer itself slides the same way is UNPROVEN (see C03).

Safety rules enforced here (post-review):
  - no redirects on API calls (Bearer must never leak off-origin)
  - no proxy auto-detection, scheme+host pinned to www.medical-care.net
  - errors carry structured info only — response bodies never enter exceptions/logs
  - fetch completeness is explicit (complete / incomplete / schema_error)
  - message ids must be positive ints before they reach the ledger
  - downloads are allowlist-checked, size-capped, streamed, atomic-renamed
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
from datetime import datetime
import os
import re
import socket
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from project_metadata import MetadataFetch

from mcs_util import atomic_write, env_value, load_config, no_proxy_opener
from mcs_worker import WorkerError, bounded_call

BASE = "https://www.medical-care.net"
# The MCS unread screen (and its unread=1 route) lists at most this many
# messages per project — owner-confirmed 2026-09-30. Reaching it means
# older unread rows may be invisible to the unread walk.
UNREAD_SCREEN_CAP = 80
API = f"{BASE}/api/v2t"


def project_url(project_id: int) -> str:
    """The MCS web page of a patient project (medical room)."""
    return f"{BASE}/projects/medical/{project_id}"
# cheap authenticated read used to tell an expired session from a
# route-level 403 (see _request)
SESSION_PROBE_PATH = "/users/self/count"
LS_TOKEN_KEY = "ngStorage-lastSessionToken"
_ALLOWED_DOWNLOAD_HOSTS = {"www.medical-care.net"}
# MCS /files/* 302s to a self-authenticating signed URL on the operator's CDN;
# following it is safe only WITHOUT the Bearer header (it must never leave
# the origin host), so it is handled manually in _open_download.
_ALLOWED_REDIRECT_HOSTS = _ALLOWED_DOWNLOAD_HOSTS | {"files.medical-care.net"}
_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
# 連携サマリー is a 150-char field on the MCS side; 600 bounds a misbehaving
# response without ever truncating a legitimate one
KARTE_SUMMARY_MAX_CHARS = 600


class MCSError(Exception):
    """Structured, log-safe error: kind/status/retryable only — never bodies."""
    def __init__(self, kind: str, detail: str = "", status: int | None = None,
                 retryable: bool = False):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.status = status
        self.retryable = retryable


class SessionExpired(MCSError):
    def __init__(self, detail: str = "", status: int | None = None):
        super().__init__("session_expired", detail, status, retryable=False)


class KeychainLocked(Exception):
    """The Keychain entry exists but its secret cannot be read — the
    login keychain is locked (or this context may not interact).
    Distinct from 'missing': recovery is an unlock, not
    re-provisioning."""


class SchemaError(MCSError):
    def __init__(self, detail: str):
        super().__init__("schema_error", detail)


class BootstrapError(MCSError):
    def __init__(self, detail: str):
        super().__init__("bootstrap_error", detail)


def _allowed_port(u) -> bool:
    try:
        return u.port in (None, 443)
    except ValueError:
        return False  # malformed port — refuse, never follow


class _WSConn:
    """Minimal RFC 6455 client — Chrome DevTools ws:// on loopback.

    The declared dependency set is stdlib-only (AGENTS.md); the old
    `import websockets` worked only because the production interpreter
    happened to be another project's venv. Covers exactly what CDP eval
    needs: upgrade handshake (with Accept verification), masked client
    text frames, server fragmentation reassembly, ping->pong, close.
    """
    _MAX_MSG = 8 * 1024 * 1024

    def __init__(self, url: str, timeout: float):
        try:
            u = urllib.parse.urlparse(url)
            host, port = u.hostname, u.port or 80
        except ValueError:
            raise BootstrapError("cdp_ws_url_invalid") from None
        if u.scheme != "ws" or not host:
            raise BootstrapError("cdp_ws_url_invalid")
        self._deadline = time.monotonic() + timeout
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = bytearray()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        try:
            self._handshake(host, port, path)
        except (BootstrapError, OSError):
            self._sock.close()
            raise

    def _handshake(self, host: str, port: int, path: str):
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\n"
             "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n", 65536)
        lines = head.split(b"\r\n")
        accept = next((ln.split(b":", 1)[1].strip() for ln in lines[1:]
                       if ln.lower().startswith(b"sec-websocket-accept:")),
                      None)
        want = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
            .encode()).digest())
        if not lines[0].startswith(b"HTTP/") or b" 101" not in lines[0] \
                or accept != want:
            raise BootstrapError("cdp_ws_handshake")

    def _fill(self):
        if time.monotonic() > self._deadline:
            raise BootstrapError("cdp_ws_timeout")
        chunk = self._sock.recv(65536)
        if not chunk:
            raise BootstrapError("cdp_ws_closed")
        self._buf += chunk

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _read_until(self, marker: bytes, cap: int) -> bytes:
        while marker not in self._buf:
            if len(self._buf) > cap:
                raise BootstrapError("cdp_ws_too_large")
            self._fill()
        idx = self._buf.index(marker) + len(marker)
        if idx > cap:
            raise BootstrapError("cdp_ws_too_large")
        out = bytes(self._buf[:idx])
        del self._buf[:idx]
        return out

    def _send_frame(self, opcode: int, payload: bytes):
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(bytes(head) + masked)

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self) -> bytes:
        """Reassemble one complete data message; answer pings inline."""
        message = bytearray()
        started = False
        while True:
            b0, b1 = self._read_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            masked, ln = b1 & 0x80, b1 & 0x7F
            if masked:
                raise BootstrapError("cdp_ws_protocol")
            if ln == 126:
                ln = int.from_bytes(self._read_exact(2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._read_exact(8), "big")
            # Validate before reading the advertised payload: a corrupt CDP
            # frame must not allocate beyond the message budget. Control
            # frames have their own RFC 6455 bound and cannot be fragmented.
            if opcode in (0x8, 0x9, 0xA):
                if not fin or ln > 125:
                    raise BootstrapError("cdp_ws_protocol")
            elif len(message) + ln > self._MAX_MSG:
                raise BootstrapError("cdp_ws_too_large")
            payload = self._read_exact(ln) if ln else b""
            if opcode == 0x9:
                self._send_frame(0xA, payload)   # ping -> pong
                continue
            if opcode == 0xA:
                continue                         # pong — ignore
            if opcode == 0x8:
                raise BootstrapError("cdp_ws_closed")
            if opcode in (0x1, 0x2):
                if started:
                    raise BootstrapError("cdp_ws_protocol")
                started = True
            elif opcode != 0x0 or not started:
                raise BootstrapError("cdp_ws_protocol")
            message.extend(payload)
            if fin:
                return bytes(message)

    def close(self):
        with suppress(OSError):
            self._sock.close()


def _ws_eval(ws_url: str, expression: str, timeout: float = 15):
    """Runtime.evaluate over the minimal ws client; returns the
    result.value, or raises BootstrapError/MCSError — never a bare
    KeyError on a malformed CDP reply."""
    conn = _WSConn(ws_url, timeout)
    try:
        conn.send_text(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression,
                       "returnByValue": True}}))
        while True:
            m = json.loads(conn.recv_message())
            if isinstance(m, dict) and m.get("id") == 1:
                result = m.get("result")
                if not isinstance(result, dict):
                    raise BootstrapError("cdp_eval_malformed")
                inner = result.get("result")
                return inner.get("value") if isinstance(inner, dict) \
                    else None
    finally:
        conn.close()


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


def _assert_allowed_url(url: str):
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


@dataclass
class Attachment:
    file_id: str
    name: str
    url: str


@dataclass
class Message:
    message_id: int
    project_id: int
    parent_id: int | None
    sender_id: int | None
    sender_name: str
    sender_type: str
    profession: str
    organization: str
    posted_at: str
    body_html: str
    body_state: str            # unknown | snippet | full | deleted
    is_unread: bool
    reply_count: int
    replies: list[Message] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    # whether the response enumerated `files` at all — only a complete
    # list (possibly empty) may reconcile the stored attachment set;
    # an absent key means "not returned this call", never "no files"
    files_present: bool = False
    metadata: dict = field(default_factory=dict)
    metadata_errors: list[str] = field(default_factory=list)


@dataclass
class UnreadPatient:
    project_id: int
    project_type: str
    patient_name: str
    disease: str
    station_name: str
    url: str
    messages: list[Message] = field(default_factory=list)
    fetch_state: str = "pending"   # pending | complete | incomplete
    fetch_reason: str = ""
    # F-5: the unread route itself failed (server-side cap) but the
    # history walk certified every unread row — acknowledge through the
    # plain list read instead of the unread-filtered one
    ack_fallback: bool = False
    # MCS karte id (patient record) — the 連携サマリー route is keyed by it
    karte_id: int | None = None


@dataclass
class UnreadSnapshot:
    timestamp: int
    patients: list[UnreadPatient]


@dataclass
class MessageBatch:
    messages: list[Message]
    pages: int = 0
    reached: bool = False
    error: MCSError | None = None
    # the unread route showed its screen cap (UNREAD_SCREEN_CAP) or fewer
    # rows than paginate.total_entries — older unread rows exist that this
    # walk could not see, so the patient must not be acknowledged yet
    capped: bool = False


@dataclass
class ReplyBatch:
    messages: list[Message]
    missing: list[int]


def _text(value, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SchemaError(f"{label} invalid")
    return value


def _sender_name(u: dict | None) -> str:
    if not u:
        return ""
    if not isinstance(u, dict):
        raise SchemaError("message: user invalid")
    return f"{_text(u.get('last_name'), 'message: last_name')} " \
           f"{_text(u.get('first_name'), 'message: first_name')}".strip()


def _profession(u: dict | None) -> str:
    cats = (u or {}).get("specialist_categories") or []
    if not isinstance(cats, list) or any(not isinstance(c, dict) for c in cats):
        raise SchemaError("message: specialist_categories invalid")
    names = [_text(c.get("name"), "message: specialist name") for c in cats]
    return ", ".join(name for name in names if name)


def _organization(u: dict | None) -> str:
    sts = (u or {}).get("stations") or []
    if not isinstance(sts, list) or any(not isinstance(s, dict) for s in sts):
        raise SchemaError("message: stations invalid")
    names = [_text(s.get("name"), "message: station name") for s in sts]
    return ", ".join(name for name in names if name)


def _valid_id(v) -> bool:
    return type(v) is int and 0 < v < 2**63


def _has_next(pag: dict, label: str) -> bool:
    value = pag.get("has_next")
    if type(value) is not bool:
        raise SchemaError(f"{label}: has_next invalid")
    return value


def _unread_patient(p, src: str) -> UnreadPatient:
    """Project row -> UnreadPatient — shape shared by the /projects
    readers (unread list and full inventory)."""
    if not isinstance(p, dict) or not _valid_id(p.get("id")):
        raise SchemaError(f"{src}: invalid project id")
    k = p.get("karte") or {}
    if not isinstance(k, dict):
        raise SchemaError(f"{src}: karte invalid")
    # a row without a usable karte.id (null/absent/legacy shape) must not
    # take the whole /projects snapshot down — it just gets no 連携サマリー
    karte_id = k.get("id") if _valid_id(k.get("id")) else None
    st = k.get("station") or {}
    if not isinstance(st, dict):
        raise SchemaError(f"{src}: station invalid")
    return UnreadPatient(
        project_id=p["id"],
        project_type=_text(p.get("type"), f"{src}: project type"),
        patient_name=(
            f"{_text(k.get('last_name'), f'{src}: last_name')} "
            f"{_text(k.get('first_name'), f'{src}: first_name')}"
        ).strip(),
        disease=_text(k.get("disease"), f"{src}: disease"),
        station_name=_text(st.get("name"), f"{src}: station name"),
        url=project_url(p['id']), karte_id=karte_id)


def _attachments(files: list | None) -> list[Attachment]:
    if files is not None and not isinstance(files, list):
        raise SchemaError("message: files invalid")
    out = []
    for f in files or []:
        if not isinstance(f, dict):
            raise SchemaError("message: file invalid")
        url = _text(f.get("url"), "message: file url")
        fid = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        out.append(Attachment(
            file_id=str(fid), name=_text(f.get("name"), "message: file name"),
            url=url))
    return out


def _message_metadata(m: dict, project_id: int) -> tuple[dict, list[str]]:
    """Normalize optional metadata independently of the required chat body."""
    out, errors = {}, []
    for key in ("reactions", "mentions", "is_bookmarked", "is_pinned"):
        if key not in m:
            continue
        value = m[key]
        try:
            if key in ("is_bookmarked", "is_pinned"):
                if type(value) is not bool:
                    raise ValueError
            elif not isinstance(value, list):
                raise ValueError
            elif key == "reactions":
                normalized, seen = [], set()
                for r in value:
                    if (not isinstance(r, dict)
                            or not isinstance(r.get("type"), str)
                            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", r["type"])
                            or r["type"] == "all" or r["type"] in seen
                            or type(r.get("count")) is not int
                            or not 0 <= r["count"] <= 2**63 - 1
                            or type(r.get("self_reacted")) is not bool
                            or (r["self_reacted"] and r["count"] == 0)):
                        raise ValueError
                    seen.add(r["type"])
                    normalized.append({k: r[k] for k in
                                       ("type", "count", "self_reacted")})
                value = normalized
            else:
                normalized = []
                for item in value:
                    if not isinstance(item, dict):
                        raise ValueError
                    kind = item.get("type")
                    target = item.get(kind) if kind in ("user", "station", "project") else None
                    if kind == "project" and target is None:
                        target = {"id": project_id}
                    if (not isinstance(target, dict) or not _valid_id(target.get("id"))
                            or (kind == "project" and target["id"] != project_id)):
                        raise ValueError
                    normalized.append({"type": kind, "id": target["id"]})
                value = normalized
            out[key] = value
        except ValueError:
            errors.append(key + "_invalid")
    return out, errors


_REACTION_KIND = re.compile(r"[a-zA-Z0-9_-]{1,64}")


def walk_reaction_actors(get, project_id: int, message_id: int, *,
                         reaction_type: str | None = None,
                         max_pages: int = 10, per_page: int = 50) -> dict:
    """Walk one fixed-snapshot actor list; complete only when it matches the post's counts.

    `get` is an adapter `_get`. Transport and schema failures end the
    walk with `error`/`status` and the partial rows; a partial walk is
    never a proof of absence. Rows keep only actor id, kind and
    profession, name and facility — the icon is dropped here.
    """
    if (not _valid_id(project_id) or not _valid_id(message_id)
            or type(max_pages) is not int or not 1 <= max_pages <= 10
            or type(per_page) is not int or not 1 <= per_page <= 50
            or (reaction_type is not None and (
                not isinstance(reaction_type, str)
                or not _REACTION_KIND.fullmatch(reaction_type)
                or reaction_type == "all"))):
        raise ValueError("invalid actor arguments")
    suffix, key = (("user_reactions", "reactions") if reaction_type is None
                   else ("reactions", "users"))
    path = f"/messages/{message_id}/{suffix}"
    actors, expected, timestamp = {}, None, None
    result = {"complete": False, "actors": [], "pages": 0,
              "counts_match_message": None, "timestamp_stable": None}
    try:
        for page in range(1, max_pages + 1):
            # timestamp: omitted first, then the server's own value; no
            # keep_read_status — the official client never sends it here
            params = {"page": page, "per_page": per_page,
                      "include_meta": int(page == 1), "include_paginate_totals": 0}
            if reaction_type is not None:
                params["reaction_type"] = reaction_type
            if timestamp is not None:
                params["timestamp"] = timestamp
            raw = get(path, params, extend_session=False)
            result["pages"] += 1
            meta = raw.get("message")
            if meta is not None:
                if not isinstance(meta, dict):
                    raise SchemaError("actors: message invalid")
                for name, wanted in (("id", message_id), ("project_id", project_id)):
                    if name in meta and (not _valid_id(meta[name]) or meta[name] != wanted):
                        raise SchemaError("actors: target mismatch")
            if page == 1:
                normalized, errors = _message_metadata(meta or {}, project_id)
                if "reactions_invalid" in errors:
                    raise SchemaError("actors: summary invalid")
                if "reactions" in normalized:
                    expected = {r["type"]: r["count"] for r in normalized["reactions"]}
            paginate, rows = raw.get("paginate"), raw.get(key)
            if (not isinstance(paginate, dict) or not isinstance(rows, list)
                    or len(rows) > per_page):
                raise SchemaError("actors: collection invalid")
            if "current_page" in paginate and (
                    type(paginate["current_page"]) is not int
                    or paginate["current_page"] != page):
                raise SchemaError("actors: page invalid")
            if "per_page" in paginate and (
                    type(paginate["per_page"]) is not int or paginate["per_page"] != per_page):
                raise SchemaError("actors: page size invalid")
            server_ts = paginate.get("timestamp")
            if not _valid_id(server_ts):
                raise SchemaError("actors: timestamp invalid")
            if timestamp is None:
                timestamp = server_ts
                result["timestamp_stable"] = True
            elif server_ts != timestamp:
                result["timestamp_stable"] = False
                raise SchemaError("actors: timestamp changed")
            if "has_next" in paginate:
                has_next = _has_next(paginate, "actors")
            else:
                total_pages = paginate.get("total_pages")
                if (type(total_pages) is not int or total_pages < page
                        or paginate.get("current_page") != page):
                    raise SchemaError("actors: terminal state invalid")
                has_next = page < total_pages
            if has_next and not rows:
                raise SchemaError("actors: no progress")
            for row in rows:
                if not isinstance(row, dict):
                    raise SchemaError("actors: row invalid")
                user = row.get("user") if reaction_type is None else row
                kind = row.get("reaction_type") if reaction_type is None else reaction_type
                if (not isinstance(user, dict) or not _valid_id(user.get("id"))
                        or not isinstance(kind, str)
                        or not _REACTION_KIND.fullmatch(kind) or kind == "all"):
                    raise SchemaError("actors: identity invalid")
                identity = (user["id"], kind)
                if identity in actors:
                    raise SchemaError("actors: duplicate actor")
                # optional display fields, never identity (#22-D2 2026-10-03:
                # names are kept and shown; the icon is never taken)
                extra = {}
                for field, read in (("profession", _profession),
                                    ("name", _sender_name),
                                    ("organization", _organization)):
                    try:
                        extra[field] = read(user) or None
                    except SchemaError:
                        extra[field] = None
                actors[identity] = {"actor_id": user["id"],
                                    "reaction_type": kind, **extra}
            if not has_next:
                counts = {}
                for _, kind in actors:
                    counts[kind] = counts.get(kind, 0) + 1
                if expected is not None:
                    if reaction_type is None:
                        result["counts_match_message"] = (
                            counts == {k: v for k, v in expected.items() if v})
                    elif reaction_type in expected or not expected:
                        result["counts_match_message"] = (
                            counts.get(reaction_type, 0) == expected.get(reaction_type, 0))
                if "total_entries" in paginate and (
                        type(paginate["total_entries"]) is not int
                        or paginate["total_entries"] != len(actors)):
                    raise SchemaError("actors: total mismatch")
                result["complete"] = result["counts_match_message"] is True
                if not result["complete"]:
                    result["error"] = ("count_mismatch" if result["counts_match_message"] is False
                                       else "summary_missing")
                break
        else:
            result["error"] = "page_limit"
    except MCSError as error:
        result["error"] = error.kind
        if error.status:
            result["status"] = error.status
    result["actors"] = list(actors.values())
    return result


def _norm_message(m: dict, project_id: int, parent_id: int | None = None,
                  is_unread: bool | None = None) -> Message:
    if not isinstance(m, dict):
        raise SchemaError("message: object invalid")
    if "project_id" in m and (
            not _valid_id(m["project_id"]) or m["project_id"] != project_id):
        raise SchemaError("message: project mismatch")
    u = m.get("user") or {}
    if not isinstance(u, dict):
        raise SchemaError("message: user invalid")
    comment = m.get("comment")
    snippet = m.get("comment_snippet")
    created = _text(m.get("created_at"), "message: created_at")
    count = m.get("count") or {}
    if ((comment is not None and not isinstance(comment, str))
            or (snippet is not None and not isinstance(snippet, str))
            or not isinstance(created, str) or not isinstance(count, dict)):
        raise SchemaError("message: fields invalid")
    reply_count = count.get("thread_messages", 0)
    if type(reply_count) is not int or reply_count < 0:
        raise SchemaError("message: reply count invalid")
    if "delete_user" in m:
        # tombstone: the reply was deleted on the MCS side — no body will
        # ever arrive, so this is a terminal state, not a retryable gap.
        body, state = "", "deleted"
    elif comment is not None:
        # comment key present (even "") means the full body was returned —
        # file-only posts carry an empty string, which is still complete.
        body, state = comment, "full"
    elif snippet:
        body, state = snippet, "snippet"
    else:
        body, state = "", "unknown"
    metadata, metadata_errors = _message_metadata(m, project_id)
    return Message(
        message_id=m.get("id"),
        project_id=project_id,
        parent_id=parent_id,
        sender_id=u.get("id"),
        sender_name=_sender_name(u),
        sender_type=_text(u.get("type"), "message: user type"),
        profession=_profession(u),
        organization=_organization(u),
        posted_at=created,
        body_html=body,
        body_state=state,
        is_unread=bool(m.get("is_unread")) if is_unread is None else is_unread,
        reply_count=reply_count,
        attachments=_attachments(m.get("files")),
        files_present="files" in m,
        metadata=metadata,
        metadata_errors=metadata_errors,
    )


def _norm_threads(items, project_id: int, parent_id: int) -> list[Message]:
    if not isinstance(items, list):
        raise SchemaError(f"thread[{parent_id}]: messages invalid")
    if any(not isinstance(m, dict) or not _valid_id(m.get("id"))
           for m in items):
        raise SchemaError(f"thread[{parent_id}]: invalid id")
    return [_norm_message(m, project_id, parent_id=parent_id) for m in items]


def _disk_kind(e: BaseException) -> str:
    """'disk_full' for a local out-of-space write, so the caller can
    defer instead of burning a download attempt; else download_failed."""
    full = isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT)
    return "disk_full" if full else "download_failed"


class MCSAdapter:
    def __init__(self, cdp_url: str = "http://127.0.0.1:9333",
                 token_cache: str | None = None, timeout: int = 20, *,
                 worker=None):
        self.cdp_url = cdp_url
        self.timeout = timeout
        self._token: str | None = None
        self._token_cache = token_cache
        self._dl_opener = no_proxy_opener(_SameHostRedirect)
        self._deadline: float | None = None
        self._worker = bounded_call if worker is None else worker

    def set_deadline(self, monotonic_deadline: float | None):
        """Absolute run budget enforced by reaped I/O workers (F13)."""
        self._deadline = monotonic_deadline

    def _remaining_timeout(self, maximum: float) -> float:
        if self._deadline is None:
            return maximum
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise MCSError("deadline_exceeded", retryable=True)
        return min(maximum, remaining)

    def _io(self, operation: str, maximum: float, **payload) -> dict:
        timeout = self._remaining_timeout(maximum)
        try:
            return self._worker(dict(payload, operation=operation),
                                timeout=timeout, deadline=self._deadline)
        except TimeoutError:
            kind = "deadline_exceeded" if (self._deadline is not None
                       and time.monotonic() >= self._deadline) else "network_error"
            raise MCSError(kind, retryable=True) from None
        except WorkerError as error:
            raise MCSError(error.kind, status=error.status,
                           retryable=error.retryable) from None

    # ---------- session ----------

    def bootstrap_token(self) -> str:
        self._remaining_timeout(5)
        cached = self._read_cache()
        try:
            tok = self._token_via_cdp()
            self._token = tok
            self._write_cache(tok)
            return tok
        except Exception as e:
            self._remaining_timeout(5)
            if cached:
                self._token = cached
                return cached
            raise BootstrapError(f"token bootstrap failed: {type(e).__name__}") from e

    def _read_cache(self) -> str | None:
        if not self._token_cache:
            return None
        try:
            with open(self._token_cache, encoding="utf-8") as f:
                raw = json.load(f)
            tok = raw.get("token") if isinstance(raw, dict) else None
            if type(tok) is str and 8 <= len(tok) <= 128:
                return tok
        except (OSError, ValueError, RecursionError):
            pass
        return None

    def _write_cache(self, token: str):
        if not self._token_cache:
            return
        with suppress(OSError):
            d = os.path.dirname(self._token_cache)
            if d:
                os.makedirs(d, mode=0o700, exist_ok=True)
                os.chmod(d, 0o700)
            atomic_write(self._token_cache, lambda f: json.dump(
                {"token": token, "fetched_at": time.time()}, f), mode=0o600)

    def _token_via_cdp(self) -> str:
        targets = self._cdp_json("/json/list")
        page = next((t for t in targets if t.get("type") == "page"
                     and urllib.parse.urlparse(t.get("url", "")).hostname
                     == "www.medical-care.net"), None)
        if not page:
            page = self._cdp_json(f"/json/new?{BASE}/unreads", method="PUT")
            self._sleep_bounded(3)

        raw = self._cdp_eval(page["webSocketDebuggerUrl"],
                            f"localStorage.getItem('{LS_TOKEN_KEY}')", 15)
        if not raw:
            raise BootstrapError("no session token in localStorage")
        try:
            tok = json.loads(raw)
        except json.JSONDecodeError:
            tok = raw.strip('"')
        if type(tok) is not str or not (8 <= len(tok) <= 128):
            raise BootstrapError("session token failed shape check")
        return tok

    def _probe_session_ok(self) -> bool:
        """check_session for the 403 classifier — a probe that itself
        fails (network, 5xx, deadline) keeps the pre-existing expiry
        classification instead of replacing the 403 with its own error."""
        try:
            return self.check_session()
        except MCSError:
            return False

    def check_session(self) -> bool:
        try:
            self._get(SESSION_PROBE_PATH, {"targets": "unread_groups"},
                      extend_session=False)
            return True
        except SessionExpired:
            return False

    def self_profile(self) -> dict:
        """The logged-in user's own profile — sender id, display name,
        specialist professions, and station names — the signal engine's
        default self identity (config signals.self_* overrides it).
        Accepts a bare user object or a {"user": {...}} envelope.
        Raises MCSError(kind='self_profile_unavailable') when the
        endpoint does not serve a usable profile."""
        try:
            d = self._get("/users/self", extend_session=False)
        except SessionExpired:
            raise          # an expired session is its own signal —
                           # never relabel it as a missing endpoint
        except MCSError as e:
            raise MCSError("self_profile_unavailable",
                           f"{e.kind}: {e.detail}",
                           status=e.status) from e
        u = d.get("user") if isinstance(d.get("user"), dict) else d
        if not isinstance(u, dict):
            raise SchemaError("self_profile: user invalid")
        cats = u.get("specialist_categories") or []
        sts = u.get("stations") or []
        if (not isinstance(cats, list) or not isinstance(sts, list)
                or any(not isinstance(c, dict) for c in cats)
                or any(not isinstance(s, dict) for s in sts)):
            raise SchemaError("self_profile: profile fields invalid")
        profs = [n for n in (_text(c.get("name"),
                                   "self_profile: specialist name")
                           for c in cats) if n]
        orgs = [n for n in (_text(s.get("name"),
                                  "self_profile: station name")
                          for s in sts) if n]
        name = _sender_name(u)
        if not (name or profs or orgs):
            raise SchemaError("self_profile: empty profile")
        sid = u.get("id")
        # station ids feed station_staffs(); record_self_profile keeps
        # only the identity fields, so the stored artifact is unchanged
        stations = [{"id": s["id"], "name": _text(s.get("name"),
                                                  "self_profile: station name")}
                    for s in sts if _valid_id(s.get("id"))]
        return {"sender_id": sid if type(sid) in (int, str) else None,
                "name": name,
                "professions": profs, "organizations": orgs,
                "stations": stations}

    def fetch_message_metadata(self, project_id: int, message_id: int, *,
                               parent_id: int | None = None) -> Message:
        """Read one exact post with read/session preservation for shadow refresh."""
        if not _valid_id(project_id) or not _valid_id(message_id):
            raise SchemaError("metadata: invalid id")
        if parent_id is not None and (not _valid_id(parent_id) or parent_id == message_id):
            raise SchemaError("metadata: invalid parent id")
        path = f"/projects/{project_id}/messages"
        if parent_id is not None:
            path += f"/{parent_id}/messages"
        r = self._get(path, {
            "message_id": message_id, "per_page": 1, "keep_read_status": 1},
            extend_session=False)
        items = r.get("messages")
        if (not isinstance(items, list) or len(items) != 1
                or not isinstance(items[0], dict) or not _valid_id(items[0].get("id"))
                or not _valid_id(items[0].get("project_id", project_id))
                or items[0].get("id") != message_id
                or items[0].get("project_id", project_id) != project_id):
            raise SchemaError("metadata: target mismatch")
        return _norm_message(items[0], project_id, parent_id=parent_id)

    def fetch_reaction_actors(self, project_id: int, message_id: int, *,
                              max_pages: int = 10, per_page: int = 50) -> dict:
        """GET-only walk of every reaction actor on one post (see walk_reaction_actors)."""
        return walk_reaction_actors(self._get, project_id, message_id,
                                    max_pages=max_pages, per_page=per_page)

    def station_staffs(self, stations: list, per_page: int = 100,
                       max_pages: int = 10) -> list:
        """The pharmacy's own member roster: GET /stations/{id}/staffs
        for each own station (from /users/self), paginated. Shared
        facility accounts (is_station_account) are skipped. Returns
        [{staff_id, name, professions, station, is_self}] in MCS order.
        Raises MCSError(kind='station_staffs_unavailable') on failure —
        an expired session stays SessionExpired."""
        out = []
        for st in stations:
            sid, sname = st.get("id"), st.get("name") or ""
            if not _valid_id(sid):
                continue
            for page in range(1, max_pages + 1):
                try:
                    r = self._get(f"/stations/{sid}/staffs",
                                  {"per_page": per_page, "page": page},
                                  extend_session=False)
                except SessionExpired:
                    raise
                except MCSError as e:
                    raise MCSError("station_staffs_unavailable",
                                   f"{e.kind}: {e.detail}",
                                   status=e.status) from e
                users, pag = r.get("users"), r.get("paginate")
                if not isinstance(users, list) or not isinstance(pag, dict) \
                        or any(not isinstance(u, dict) for u in users):
                    raise SchemaError("staffs: page invalid")
                for u in users:
                    if u.get("is_station_account") is True \
                            or not _valid_id(u.get("id")):
                        continue
                    name = _sender_name(u)
                    if not name:
                        continue
                    out.append({"staff_id": u["id"], "name": name,
                                "professions": [p for p in _profession(u)
                                                .split(", ") if p],
                                "station": sname,
                                "is_self": u.get("is_self") is True})
                if not _has_next(pag, "staffs"):
                    break
            else:
                raise MCSError("pages_exceeded", "station staffs",
                               retryable=True)
        return out

    # ---------- auto login ----------

    def _cdp_up(self) -> bool:
        try:
            self._cdp_json("/json/version", timeout=3)
            return True
        except (OSError, MCSError):
            return False

    def _ensure_chrome(self, profile_dir: str, chrome_bin: str):
        if self._cdp_up():
            return
        import subprocess
        self._remaining_timeout(1)
        subprocess.Popen([
            chrome_bin,
            f"--remote-debugging-port={urllib.parse.urlparse(self.cdp_url).port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run", "--no-default-browser-check",
            # the profile must not pull the ~4 GB on-device AI model
            "--disable-features=OptimizationGuideModelDownloading,"
            "OptimizationHintsFetching,OptimizationGuideOnDeviceModel",
            f"{BASE}/authentication/login"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            self._sleep_bounded(1)
            self._remaining_timeout(3)
            if self._cdp_up():
                return
        raise BootstrapError("chrome launch timed out")

    def _cdp_eval(self, ws_url: str, expr: str, timeout: int = 15):
        return self._io("cdp_eval", timeout, url=ws_url, expression=expr)["value"]

    def _cdp_json(self, path: str, method: str = "GET", timeout: float = 5):
        return self._io("cdp_json", timeout, url=self.cdp_url + path,
                        method=method)["value"]

    def _login_page(self):
        """Pick the login tab by STRICT origin+path — never fill credentials
        into a lookalike path on another host (Oracle B14)."""
        targets = self._cdp_json("/json/list")
        for t in targets:
            if t.get("type") != "page":
                continue
            u = urllib.parse.urlparse(t.get("url", ""))
            if (u.scheme == "https"
                    and u.hostname == "www.medical-care.net"
                    and "authentication/login" in u.path):
                return t
        # reuse an existing mcs tab or open one
        for t in targets:
            if t.get("type") == "page" and urllib.parse.urlparse(
                    t.get("url", "")).hostname == "www.medical-care.net":
                self._cdp_eval(t["webSocketDebuggerUrl"],
                               f"location.href='{BASE}/authentication/login'")
                self._sleep_bounded(4)
                return t
        page = self._cdp_json(f"/json/new?{BASE}/authentication/login", method="PUT")
        self._sleep_bounded(4)
        return page

    def _keychain_password(self, service: str = "mcs-adapter") -> str | None:
        """Fetch MCS password from macOS Keychain. First call may show an ACL
        prompt — 'Always Allow' makes subsequent reads silent. Returns None if
        the entry doesn't exist or access is denied. Raises KeychainLocked
        when the item is unreadable because the keychain is locked (or UI
        interaction is unavailable) — a recoverable operational state that
        must not be conflated with a missing credential."""
        import subprocess
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=self._remaining_timeout(15))
        except subprocess.TimeoutExpired:
            self._remaining_timeout(1)
            # A stalled ACL prompt is temporarily unreadable. Preserve the
            # existing .env fallback only while the run still has time.
            raise KeychainLocked(service) from None
        if r.returncode == 0:
            return r.stdout.strip() or None
        err = (r.stderr or "").lower()
        if (r.returncode == 36 or "interaction is not allowed" in err
                or "keychain is locked" in err):
            raise KeychainLocked(service)
        return None

    def _login_password(self) -> tuple[str | None, bool]:
        """MCS password for form fill — (password, keychain_was_locked).
        Keychain is primary; when it is locked or lacks the entry,
        MCS_PASSWORD in ~/.mcs/.env is the fallback so a rebooted Mac
        (login keychain locked until first unlock) can still re-login.
        Plaintext-at-rest is the accepted tradeoff — FileVault or
        physical security is assumed; the keychain remains preferred
        whenever it is readable."""
        try:
            pw = self._keychain_password()
            locked = False
        except KeychainLocked:
            pw, locked = None, True
        if not pw:
            pw = env_value("MCS_PASSWORD")
        return pw, locked

    def _config(self) -> dict:
        return load_config()

    def _recover_session(self) -> bool:
        """A still-valid browser session recovers a run with zero form
        interaction — API token expiry does not imply logout; cookies
        can keep the app session alive (or a manual/concurrent login
        may have landed). A fresh localStorage token that passes
        check_session is enough."""
        try:
            tok = self._token_via_cdp()
        except Exception:
            return False
        self._token = tok
        with suppress(Exception):
            if self.check_session():
                self._write_cache(tok)
                return True
        return False

    def auto_login(self, profile_dir: str = "", chrome_bin: str = "",
                   wait_s: int = 45) -> str:
        """Re-login by filling the form from macOS Keychain and clicking
        submit. Native Chrome autofill can't be driven via DOM, so the
        credential lives in Keychain (service 'mcs-adapter'); the adapter
        reads it at runtime and injects via input events. loginId is
        persisted by the app itself; if empty we fill from config
        'mcs_login_id'. Password is never logged or stored by us.

        Returns 'ok' | 'manual_required' | 'keychain_locked' | 'failed'.
        'manual_required:no_form' distinguishes a missing login form
        (page never rendered / app redirected) from a submitted login
        that never validated."""
        try:
            self._ensure_chrome(profile_dir, chrome_bin)
        except (MCSError, OSError):  # incl. deadline_exceeded
            return "failed"
        # cheapest recovery first — before touching the login form at
        # all, a live session (fresh token + valid API check) ends it
        if self._recover_session():
            return "ok"
        try:
            page = self._login_page()
            ws = page["webSocketDebuggerUrl"]
            self._sleep_bounded(3)  # Angular render
            state = self._cdp_eval(ws, """(() => {
              if (location.origin !== 'https://www.medical-care.net')
                return 'bad_origin';
              const id = document.querySelector('input[name=loginId]');
              const pw = document.querySelector('input[type=password]');
              if (!pw) return 'no_form';
              if (pw.value) return 'ready';
              return (id && id.value) ? 'need_pw' : 'need_both';
            })()""")
            if state == "bad_origin":
                return "failed"
            if state == "no_form":
                # a logged-in app redirects /authentication/login back
                # home — no form then MEANS a session, verify before
                # escalating to a manual alert
                if self._recover_session():
                    return "ok"
                return "manual_required:no_form"
            if state != "ready":
                pw, locked = self._login_password()
                if not pw:
                    return "keychain_locked" if locked else \
                        "manual_required"
                login_id = self._config().get("mcs_login_id", "")
                fill = self._cdp_eval(ws, f"""(() => {{
                  if (location.origin !== 'https://www.medical-care.net')
                    return 'bad_origin';
                  const idEl = document.querySelector('input[name=loginId]');
                  const pwEl = document.querySelector('input[type=password]');
                  if (idEl && !idEl.value) {{
                    idEl.value = {json.dumps(login_id)};
                    idEl.dispatchEvent(new Event('input', {{bubbles:true}}));
                  }}
                  pwEl.value = {json.dumps(pw)};
                  pwEl.dispatchEvent(new Event('input', {{bubbles:true}}));
                  return 'filled';
                }})()""")
                if fill != "filled":
                    return "failed"
            clicked = self._cdp_eval(ws, """(() => {
              if (location.origin !== 'https://www.medical-care.net')
                return 'bad_origin';
              const btn = document.querySelector(
                'button[type=submit],input[type=submit],' +
                'form button:not([type=button])');
              if (!btn) return 'no_button';
              btn.click();
              return 'clicked';
            })()""")
            if clicked != "clicked":
                return "failed"
        except Exception:
            return "failed"
        # poll for a fresh session token
        deadline = time.monotonic() + wait_s
        if self._deadline is not None:
            deadline = min(deadline, self._deadline)
        while time.monotonic() < deadline:
            self._sleep_bounded(min(2, max(0, deadline - time.monotonic())))
            try:
                tok = self._token_via_cdp()
            except Exception:
                continue
            if tok:
                self._token = tok
                try:
                    ok = self.check_session()
                except MCSError:
                    return "failed"
                if ok:
                    self._write_cache(tok)
                    return "ok"
                # token present but not yet valid, or MFA page — keep waiting
        return "manual_required"

    # ---------- http ----------

    def _request(self, method: str, path: str, params: dict | None = None,
                 data: bytes | None = None, headers: dict | None = None,
                 extend_session: bool = True, retries: int = 2) -> tuple[int, bytes, dict]:
        self._remaining_timeout(self.timeout)
        if not self._token:
            self.bootstrap_token()
        q = dict(params or {})
        if not extend_session:
            q["no_extend_session"] = "1"
        url = API + path + ("?" + urllib.parse.urlencode(q) if q else "")
        h = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        h.update(headers or {})
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                result = self._io(
                    "api", self.timeout, url=url, method=method, headers=h,
                    data=None if data is None else base64.b64encode(data).decode("ascii"))
            except MCSError as e:
                if e.kind != "network_error":
                    raise
                last = e
                if attempt < retries:
                    self._sleep_bounded(1.5 * (attempt + 1))
                continue
            status = result["status"]
            if status == 403 and path != SESSION_PROBE_PATH \
                    and self._probe_session_ok():
                # the session is fine — a route/project-level denial
                # (retired route, revoked project) is a per-request
                # error, never a re-login trigger that aborts the run
                raise MCSError("forbidden", f"{method} {path}",
                               status=status)
            if status in (401, 403):
                raise SessionExpired(f"{method} {path}", status=status)
            if status >= 300:
                if status in (429, 500, 502, 503, 504) and attempt < retries:
                    self._sleep_bounded(1.5 * (attempt + 1))
                    continue
                raise MCSError("http_error", f"{method} {path}", status=status,
                               retryable=status >= 500)
            return status, base64.b64decode(result["body"], validate=True), result["headers"]
        raise MCSError("network_error", f"{method} {path}", retryable=True) from last

    def _sleep_bounded(self, seconds: float):
        """Retry backoff never sleeps past the propagated deadline — the
        next loop iteration then raises deadline_exceeded (F13)."""
        if self._deadline is not None:
            seconds = min(seconds,
                          max(0.0, self._deadline - time.monotonic()))
        time.sleep(seconds)

    def _get(self, path: str, params: dict | None = None,
             extend_session: bool = True) -> dict:
        status, body, _ = self._request("GET", path, params,
                                      extend_session=extend_session)
        try:
            out = json.loads(body)
        except json.JSONDecodeError as e:
            raise SessionExpired(
                f"{path} non-json (login redirect?)") from e
        except (ValueError, RecursionError):
            raise SchemaError("invalid json payload") from None
        if not isinstance(out, dict):
            raise SchemaError(f"{path} -> non-object json")
        return out

    # ---------- reads ----------

    def fetch_cross_list(self, dataset: str, *, unread_only: bool = False,
                         max_pages: int = 5, per_page: int = 20,
                         max_rows: int = 100, deadline_s: float = 25):
        """Explicit bounded mentioned/bookmarked GET; no login or read-mark writes."""
        from cross_lists import fetch_cross_list
        return fetch_cross_list(self, dataset, unread_only=unread_only,
                                max_pages=max_pages, per_page=per_page,
                                max_rows=max_rows, deadline_s=deadline_s)

    def fetch_project_members(self, project_id: int, *, max_pages: int = 5,
                              per_page: int = 50, retain_names: bool = False) -> MetadataFetch:
        """Bounded current membership GET; no photos, contacts or departed members."""
        from project_metadata import fetch_metadata
        return fetch_metadata(self._get, "care_team", project_id,
                              max_pages=max_pages, per_page=per_page,
                              retain_names=retain_names)

    def fetch_observation_values(self, karte_id: int, lab_test_item_id: int, *,
                                 max_pages: int = 5, per_page: int = 50) -> MetadataFetch:
        """Read one evidenced lab-test item's values without fetching full karte data."""
        from project_metadata import fetch_metadata
        return fetch_metadata(self._get, "observation_values", karte_id,
                              item_id=lab_test_item_id,
                              max_pages=max_pages, per_page=per_page)

    def fetch_group_consultations(self, project_id: int, *, project_type: str,
                                  max_pages: int = 5, per_page: int = 20) -> MetadataFetch:
        """Read group consultation metadata only; never infer patient associations."""
        from project_metadata import fetch_metadata
        if project_type != "group":
            raise ValueError("group project evidence required")
        return fetch_metadata(self._get, "consultations", project_id,
                              max_pages=max_pages, per_page=per_page, project_type=project_type)

    def fetch_memo_summary(self, karte_id: int) -> dict | None:
        """Read the patient's 連携サマリー (memo_summary); None when none is registered.

        GET only — the POST routes (update, read_status) are never called,
        so other viewers' 更新 badges stay untouched. An unregistered
        summary carries no comment/user/updated_at at all. Unknown keys
        are dropped; comment is capped at KARTE_SUMMARY_MAX_CHARS."""
        r = self._get(f"/kartes/{karte_id}/memo_summary")
        ms = r.get("memo_summary")
        if not isinstance(ms, dict):
            raise SchemaError("memo_summary: object missing")
        for key in ("is_editable", "is_read"):
            if type(ms.get(key)) is not bool:
                raise SchemaError(f"memo_summary: {key} invalid")
        if not isinstance(ms.get("read_style"), str):
            raise SchemaError("memo_summary: read_style invalid")
        comment = ms.get("comment")
        if comment is None:
            return None
        if not isinstance(comment, str):
            raise SchemaError("memo_summary: comment invalid")
        user = ms.get("user") or {}
        if not isinstance(user, dict):
            raise SchemaError("memo_summary: user invalid")
        return {
            "comment": comment[:KARTE_SUMMARY_MAX_CHARS],
            "updated_at": _text(ms.get("updated_at"),
                                "memo_summary: updated_at"),
            "is_editable": ms["is_editable"],
            "user": {
                "profession": _text(user.get("profession"),
                                    "memo_summary: user profession"),
                "name": _text(user.get("name"), "memo_summary: user name")},
        }

    def list_unread(self, per_page: int = 100,
                    max_pages: int = 50) -> UnreadSnapshot:
        """Unread projects enumerated via /projects?include_meta=1 filtered
        by the per-project is_unread flag — the dedicated /projects/unread
        route went 403 server-side (every sibling stayed 200) and the web
        app itself lists unread through include_meta.

        paginate.timestamp is a per-request server clock on this route, not
        a shared snapshot cursor — consecutive pages legitimately differ,
        so the old page-drift restart no longer applies. snapshot_ts takes
        the MINIMUM observed timestamp (the walk's start): mark_as_read(ts)
        then can never cover a message posted after the walk began — such a
        message stays unread for the next tick."""
        patients: list[UnreadPatient] = []
        ts = None
        prev_page_ids: set[int] | None = None
        for page in range(1, max_pages + 1):
            r = self._get("/projects", {
                "per_page": per_page, "page": page,
                "include_meta": 1, "include_paginate_totals": 0})
            pag = r.get("paginate")
            if not isinstance(pag, dict):
                raise SchemaError("unread: paginate missing")
            projs = r.get("projects")
            if not isinstance(projs, list):
                raise SchemaError("unread: projects missing")
            page_ts = pag.get("timestamp")
            if type(page_ts) is not int or page_ts <= 0:
                raise SchemaError("unread: paginate.timestamp invalid")
            ts = page_ts if ts is None else min(ts, page_ts)
            ids = set()
            for p in projs:
                if not isinstance(p, dict) or not _valid_id(p.get("id")):
                    raise SchemaError("unread: invalid project id")
                ids.add(p["id"])
                if type(p.get("is_unread")) is not bool:
                    raise SchemaError("unread: is_unread missing/invalid")
                if not p["is_unread"]:
                    continue
                patients.append(_unread_patient(p, "unread"))
            if prev_page_ids is not None and ids and ids <= prev_page_ids:
                raise SchemaError("unread: pagination not advancing")
            prev_page_ids = ids
            if not _has_next(pag, "unread"):
                break
        else:
            raise MCSError("pages_exceeded", "unread list > 50 pages",
                           retryable=True)
        return UnreadSnapshot(timestamp=ts, patients=patients)

    def fetch_unread_messages(self, project_id: int, timestamp: int,
                              per_page: int = 10,
                              max_pages: int = 20) -> MessageBatch:
        """Partial-page resilience: if a mid-walk page fails, already-fetched
        completed pages and any terminal error are returned together so the
        caller can save them and mark the patient incomplete (Oracle B04).

        The unread screen lists at most UNREAD_SCREEN_CAP messages (owner
        confirmed 2026-09-30); the route reports has_next=false at that
        point even when older unread rows exist. A walk that ends on the
        cap, or returns fewer rows than paginate.total_entries, is flagged
        ``capped`` — the caller keeps the patient incomplete until the
        history walk has stored everything down to the oldest unread."""
        msgs: list[Message] = []
        pages = 0
        reached = False
        error = None
        total = None
        for page in range(1, max_pages + 1):
            try:
                r = self._get(f"/projects/{project_id}/messages", {
                    "unread": 1, "timestamp": timestamp,
                    "keep_read_status": 1, "include_meta": 1,
                    "exclude_terminated_ex_application": 1,
                    "include_paginate_totals": 1,
                    "per_page": per_page, "page": page})
                items = r.get("messages")
                pag = r.get("paginate")
                if not isinstance(items, list) or not isinstance(pag, dict):
                    raise SchemaError(
                        f"messages[{project_id}]: page schema invalid")
                has_next = _has_next(pag, f"messages[{project_id}]")
                if type(pag.get("total_entries")) is int:
                    total = pag["total_entries"]
                page_messages = []
                for m in items:
                    if not isinstance(m, dict) or not _valid_id(m.get("id")):
                        raise SchemaError(
                            f"messages[{project_id}]: invalid id")
                    nm = _norm_message(m, project_id)
                    nm.replies = _norm_threads(
                        m.get("thread_messages") or [], project_id,
                        nm.message_id)
                    page_messages.append(nm)
            except MCSError as e:
                error = e
                break
            msgs.extend(page_messages)
            pages += 1
            if not has_next:
                reached = True
                break
        else:
            error = MCSError(
                "pages_exceeded", f"messages[{project_id}]", retryable=True)
        capped = error is None and reached and (
            len(msgs) >= UNREAD_SCREEN_CAP
            or (total is not None and total > len(msgs)))
        return MessageBatch(msgs, pages, reached, error, capped)

    def _project_detail(self, project_id: int) -> tuple[dict, dict | None]:
        """(raw envelope, project object or None) of the project detail.
        The object must carry a bool is_archived — a permanent detail
        field — so an empty/malformed object never reads as a valid
        detail; each caller decides what an invalid one means."""
        r = self._get(f"/projects/{project_id}", {})
        proj = r.get("project")
        if proj is None and isinstance(r.get("data"), dict):
            proj = r["data"].get("project")
        if not isinstance(proj, dict) \
                or type(proj.get("is_archived")) is not bool:
            proj = None
        return r, proj

    def oldest_unread_id(self, project_id: int) -> int | None:
        """``oldest_unread_message.id`` from the project detail — the
        anchor an unread-capped patient must be walked down to before it
        may be acknowledged. None when the server reports no unread; a
        malformed detail is a SchemaError (never silently 'none')."""
        _, proj = self._project_detail(project_id)
        if proj is None:
            raise SchemaError(f"project[{project_id}]: detail invalid")
        oldest = proj.get("oldest_unread_message")
        if oldest is None:
            return None
        if not isinstance(oldest, dict) or not _valid_id(oldest.get("id")):
            raise SchemaError(f"project[{project_id}]: oldest_unread invalid")
        return oldest["id"]

    def _thread_page(self, project_id: int, message_id: int,
                     page: int) -> tuple[list[Message], bool]:
        r = self._get(
            f"/projects/{project_id}/messages/{message_id}/messages",
            {"keep_read_status": 1, "page": page})
        if "paginate" in r:
            pag = r["paginate"]
            if not isinstance(pag, dict):
                raise SchemaError("thread: paginate invalid")
            has_next = _has_next(pag, "thread")
        else:
            has_next = False
        return (_norm_threads(r.get("messages"), project_id,
                              message_id), has_next)

    def fetch_thread(self, project_id: int, message_id: int,
                     max_pages: int = 10) -> list[Message]:
        """All replies in the thread — the endpoint paginates (10/page)
        and page 1 alone truncated any thread beyond that, leaving
        reply jobs to burn out permanently. A thread still reporting
        has_next after max_pages raises thread_incomplete rather than
        certify a truncated result — callers keep their durable retry."""
        batch = self.fetch_thread_window(project_id, message_id, max_pages=max_pages)
        if batch.error is not None:
            raise batch.error
        if not batch.reached:
            raise MCSError("thread_incomplete", retryable=True)
        return batch.messages

    def fetch_thread_window(self, project_id: int, message_id: int,
                            start_page: int = 1,
                            max_pages: int = 10
                            ) -> MessageBatch:
        """Return completed thread pages with any later-page error so
        callers can save progress before retrying or recovering auth."""
        out: list[Message] = []
        seen: set[int] = set()
        pages = 0
        for page in range(start_page, start_page + max_pages):
            try:
                msgs, has_next = self._thread_page(project_id, message_id, page)
            except MCSError as error:
                return MessageBatch(out, pages=pages, reached=False, error=error)
            for m in msgs:
                if m.message_id not in seen:
                    seen.add(m.message_id)
                    out.append(m)
            pages += 1
            if not has_next:
                return MessageBatch(out, pages=pages, reached=True)
        return MessageBatch(out, pages=pages, reached=False)

    def fetch_unread_replies(self, msg: Message) -> ReplyBatch:
        """Full bodies for replies flagged is_unread (list gives snippets only).
        Merges in place; returns fetched full replies. Replies absent from
        the thread response are returned in ReplyBatch.missing so the caller
        can queue a durable refetch job instead of falsely passing the patient
        as complete (Oracle B05)."""
        if not any(t.is_unread for t in msg.replies):
            return ReplyBatch([], [])
        kwargs = ({"max_pages": (msg.reply_count + 9) // 10}
                  if msg.reply_count > 100 else {})
        full = self.fetch_thread(msg.project_id, msg.message_id, **kwargs)
        got_ids = {m.message_id for m in full
                   if m.body_state in ("full", "deleted")}
        unread_ids = {t.message_id for t in msg.replies if t.is_unread}
        merged = {t.message_id: t for t in msg.replies}
        for m in full:
            if m.message_id in unread_ids:
                m.is_unread = True
                merged[m.message_id] = m
        msg.replies = list(merged.values())
        missing = sorted(unread_ids - got_ids)
        return ReplyBatch([merged[i] for i in unread_ids if i in merged],
                          missing)

    def list_projects(self, per_page: int = 50,
                      max_pages: int = 20) -> list[UnreadPatient]:
        """All projects, ordered by last_message recency (verified newest-first).
        Returns UnreadPatient with last_activity (epoch) attached."""
        out: list[UnreadPatient] = []
        for page in range(1, max_pages + 1):
            r = self._get("/projects", {
                "per_page": per_page, "page": page,
                "include_paginate_totals": 0})
            projs = r.get("projects")
            pag = r.get("paginate")
            if not isinstance(projs, list) or not isinstance(pag, dict):
                raise SchemaError("projects: page invalid")
            for p in projs:
                up = _unread_patient(p, "projects")
                lm = p.get("last_message") or {}
                if not isinstance(lm, dict):
                    raise SchemaError("projects: nested object invalid")
                ca = _text(lm.get("created_at"),
                           "projects: last_message.created_at")
                try:
                    up.last_activity = int(
                        datetime.fromisoformat(ca).timestamp()) if ca else 0
                except (ValueError, OverflowError) as e:
                    # a malformed timestamp must fail loud like every other
                    # field here — coercing to 0 silently drops a live
                    # project from init_data's active filter (FIX-ID2)
                    raise SchemaError(
                        "projects: last_message.created_at invalid") from e
                out.append(up)
            if not _has_next(pag, "projects"):
                break
        else:
            raise MCSError("pages_exceeded", "projects inventory",
                           retryable=True)
        return out

    def list_archived_kartes(self, per_page: int = 50,
                             max_pages: int = 20) -> list[UnreadPatient]:
        """Archived (保管・削除) kartes -> their linked medical_project ids.

        Verified endpoint: GET /kartes?is_archived=1 — the ONLY params sent
        are the ones exercised live (per_page/page/is_archived/
        include_paginate_totals). Kartes without a medical_project are
        skipped (nothing is fetchable); a present-but-malformed one raises
        SchemaError rather than silently dropping a record (Oracle F10).
        Project ids are deduplicated across pages."""
        out: list[UnreadPatient] = []
        seen: set[int] = set()
        for page in range(1, max_pages + 1):
            r = self._get("/kartes", {
                "per_page": per_page, "page": page,
                "is_archived": 1, "include_paginate_totals": 0})
            kartes = r.get("kartes")
            pag = r.get("paginate")
            if not isinstance(kartes, list) or not isinstance(pag, dict):
                raise SchemaError("kartes: page invalid")
            for k in kartes:
                if not isinstance(k, dict):
                    raise SchemaError("kartes: karte invalid")
                proj = k.get("medical_project")
                if proj is None:
                    continue
                if not isinstance(proj, dict) \
                        or not _valid_id(proj.get("id")):
                    raise SchemaError("kartes: medical_project invalid")
                pid = proj["id"]
                if pid in seen:
                    continue
                seen.add(pid)
                station = k.get("station") or {}
                if not isinstance(station, dict):
                    raise SchemaError("kartes: station invalid")
                out.append(UnreadPatient(
                    # medical_project.id joins the same /projects/medical
                    # namespace — the join itself is the type
                    project_id=pid,
                    project_type="medical",
                    patient_name=(
                        f"{_text(k.get('last_name'), 'kartes: last_name')} "
                        f"{_text(k.get('first_name'), 'kartes: first_name')}"
                    ).strip(),
                    disease=_text(k.get("disease"), "kartes: disease"),
                    station_name=_text(station.get("name"),
                                       "kartes: station name"),
                    url=project_url(pid)))
            if not _has_next(pag, "kartes"):
                break
        else:
            raise MCSError("pages_exceeded", "archived kartes inventory",
                           retryable=True)
        return out

    def fetch_history(self, project_id: int, since_ts: int,
                      max_pages: int = 10, per_page: int = 10,
                      start_page: int = 1) -> MessageBatch:
        """Watermark backfill: walk timeline pages until created_at <= since_ts.
        Catches posts the unread API misses (e.g. read by another human).
        MessageBatch.reached reports the natural end only — under
        sort=pinned a below-cutoff item never certifies the tail — and
        pages reports pages consumed by this call for durable resume
        cursors.
        start_page lets deep imports continue from a stored cursor."""
        out: list[Message] = []
        reached = False
        pages = 0
        error = None
        for page in range(start_page, start_page + max_pages):
            try:
                r = self._get(f"/projects/{project_id}/messages", {
                    "exclude_terminated_ex_application": 1,
                    "keep_read_status": 1,
                    "sort": "pinned", "per_page": per_page, "page": page})
                items = r.get("messages")
                pag = r.get("paginate")
                if not isinstance(items, list) or not isinstance(pag, dict):
                    raise SchemaError(f"history[{project_id}]: page invalid")
                has_next = _has_next(pag, f"history[{project_id}]")
                page_messages = []
                for m in items:
                    if not isinstance(m, dict) or not _valid_id(m.get("id")):
                        raise SchemaError(f"history[{project_id}]: invalid id")
                    nm = _norm_message(m, project_id)
                    nm.replies = _norm_threads(
                        m.get("thread_messages") or [], project_id,
                        nm.message_id)
                    if since_ts and nm.posted_at:
                        try:
                            parent_ts = datetime.fromisoformat(
                                nm.posted_at).timestamp()
                        except ValueError as e:
                            raise SchemaError("history: created_at invalid") from e
                        newest = parent_ts
                        for t in nm.replies:
                            try:
                                newest = max(newest, datetime.fromisoformat(
                                    t.posted_at).timestamp())
                            except (ValueError, TypeError) as e:
                                raise SchemaError(
                                    "history: reply created_at invalid") from e
                        if newest <= since_ts:
                            # sort=pinned interleaves old pinned messages
                            # among newer ones: a below-cutoff item is
                            # skipped but NEVER certifies the tail — not
                            # even at page end, since the next page may
                            # resume above the cutoff.  Termination is
                            # certified only by has_next (FIX-AD1).
                            continue
                    page_messages.append(nm)
            except MCSError as e:
                error = e
                break
            out.extend(page_messages)
            pages += 1
            if not has_next:
                reached = True
                break
        return MessageBatch(out, pages, reached, error)

    def fetch_latest(self, project_id: int) -> dict:
        """Latest-activity probe for one project — sees posts the unread
        set structurally cannot: the operator's own posts (never unread
        for their author) and posts another human already read. The probe
        only decides WHETHER a history fetch is warranted; message content
        is always re-fetched through fetch_history, so a latest-shaped
        object is never persisted directly.
        The endpoint always returns the newest message identity — it does
        not honor an `after` filter — so freshness is decided by comparing
        the returned id against the ledger, not server-side.
        Returns {"message_id": int|None, "is_self_only": bool} —
        message_id is the server's newest message id (None when the
        project has none)."""
        r = self._get(f"/projects/{project_id}/messages/latest")
        self_only = r.get("is_self_only")
        if self_only is not None and type(self_only) is not bool:
            raise SchemaError(f"latest[{project_id}]: is_self_only invalid")
        msg = r.get("message")
        if msg is None or msg == {}:
            # null/empty message object = no messages on the project
            return {"message_id": None, "is_self_only": bool(self_only)}
        if not isinstance(msg, dict) or not _valid_id(msg.get("id")):
            raise SchemaError(f"latest[{project_id}]: message invalid")
        return {"message_id": msg["id"],
                "is_self_only": bool(self_only)}

    def _open_download(self, url: str):
        if not self._token:
            raise MCSError("no_token")
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self._token}")
        try:
            return self._dl_opener.open(req, timeout=self._remaining_timeout(60))
        except urllib.error.HTTPError as e:
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
                        return self._dl_opener.open(
                            urllib.request.Request(loc),
                            timeout=self._remaining_timeout(60))
                    # a rejected/malformed redirect target is permanent —
                    # fail as url_not_allowed instead of retrying
                    raise MCSError("url_not_allowed",
                                   "redirect rejected") from e
            raise

    def download(self, url: str, dest: str) -> dict:
        _assert_allowed_url(url)
        if not self._token:
            self.bootstrap_token()
        try:
            fd, tmp = tempfile.mkstemp(prefix=".download-", suffix=".part",
                                       dir=os.path.dirname(dest) or ".")
            os.close(fd)
        except OSError as e:
            raise MCSError(_disk_kind(e), retryable=True) from None
        try:
            result = self._io("download", 60, url=url, token=self._token,
                              partial=tmp)
            self._remaining_timeout(60)
            os.replace(tmp, dest)
            return result
        except BaseException:
            # _io has already killed and reaped any timed-out writer.
            with suppress(OSError):
                os.unlink(tmp)
            raise

    def _download_to_part(self, url: str, tmp: str) -> dict:
        """Worker-side stream; only the supervising parent may commit it."""
        _assert_allowed_url(url)
        total = 0
        h = hashlib.sha256()
        try:
            with self._open_download(url) as res, open(tmp, "wb") as f:
                while True:
                    self._remaining_timeout(60)
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
            self._remaining_timeout(60)
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
                raise MCSError("http_error", status=e.code,
                               retryable=e.code in (408, 429) or e.code >= 500) from e
            raise MCSError(_disk_kind(e), retryable=True) from e

    # ---------- write (guarded) ----------

    def thread_unread(self, project_id: int, parent_id: int) -> bool:
        """Whether the server still holds an unread reply in this
        thread. Reply read state is tracked per thread, not by the
        project flag or the reply rows (verified live 2026-10-06: the
        thread route carries no per-reply flag and the project detail
        stays clear while the root reports ``oldest_unread_thread_
        message``). A root missing from the answer is a SchemaError,
        never 'read'."""
        r = self._get("/messages", {
            "message_ids": parent_id,
            "include_oldest_unread_thread_message_id": 1})
        items = r.get("messages")
        if not isinstance(items, list):
            raise SchemaError("messages: list invalid")
        root = next((m for m in items if isinstance(m, dict)
                     and m.get("id") == parent_id), None)
        if root is None:
            raise SchemaError(f"messages[{project_id}]: root missing")
        oldest = root.get("oldest_unread_thread_message")
        if oldest is not None and not isinstance(oldest, dict):
            raise SchemaError("messages: oldest_unread_thread_message invalid")
        return oldest is not None

    def read_thread(self, project_id: int, parent_id: int,
                    max_pages: int = 10) -> set[int]:
        """Read thread pages to acknowledge replies and return observed reply ids."""
        # Owner authorized thread acknowledgement without a snapshot gate
        # on 2026-10-06. New replies can also be acknowledged; callers queue
        # any newly observed ids for durable acquisition.
        seen: set[int] = set()
        for page in range(1, max_pages + 1):
            r = self._get(
                f"/projects/{project_id}/messages/{parent_id}/messages",
                {"page": page})
            seen.update(m.message_id for m in _norm_threads(
                r.get("messages"), project_id, parent_id))
            if "paginate" not in r:
                return seen
            pag = r["paginate"]
            if not isinstance(pag, dict):
                raise SchemaError("thread: paginate invalid")
            if not _has_next(pag, "thread"):
                return seen
        raise MCSError("thread_incomplete", retryable=True)

    def mark_patient_read(self, project_id: int, snapshot_ts: int,
                          fallback_plain: bool = False) -> dict:
        """snapshot_ts is still mandatory and still sent — but the POST
        /projects/{id}/mark_as_read route was retired server-side (403
        while every GET stayed 200). Read state now clears as a SIDE
        EFFECT of reading the message list: a GET without
        keep_read_status marks the project's unread messages read
        (verified live — per_page=1 clears the whole flag). The
        timestamp-gated mark is gone server-side; sending the same
        unread=1&timestamp= filters is the closest equivalent — the
        residual window is a post landing between the collection fetch
        and this read (sub-second inside the tick).

        CONFIRMED only when the project detail positively reports no
        oldest_unread_message; anything else (missing project, the key
        still present, odd shapes) is mark_result_unknown — never
        confirmed (Oracle B02)."""
        if type(snapshot_ts) is not int or snapshot_ts <= 0:
            raise MCSError("bad_snapshot_ts")
        try:
            self._get(f"/projects/{project_id}/messages", {
                "unread": 1, "timestamp": snapshot_ts,
                "per_page": 1, "page": 1, "include_paginate_totals": 0})
        except MCSError as e:
            # F-5 fallback: the unread-filtered route can fail once a
            # project holds more unread rows than the screen cap. Only a
            # caller that verified ledger.unread_cap_cleared may clear
            # through the plain list read (same side effect, verified
            # live) — everything else keeps the strict route. The
            # snapshot timestamp still rides along so the read stays
            # gated to the collection snapshot wherever the server
            # honors it.
            if not (fallback_plain and e.kind == "http_error"):
                raise
            self._get(f"/projects/{project_id}/messages", {
                "timestamp": snapshot_ts,
                "per_page": 1, "page": 1, "include_paginate_totals": 0})
        r, proj = self._project_detail(project_id)
        if proj is not None and proj.get("oldest_unread_message") is None:
            return r
        raise MCSError("mark_result_unknown")
