"""MCS (MedicalCare Station) adapter — API-first, CDP token bootstrap.

Verified contract (live session, 2026-09):
  auth    : Authorization: Bearer <36char> from localStorage['ngStorage-lastSessionToken']
            (ngStorage JSON-encodes values -> must JSON.parse)
  unread  : GET /api/v2t/projects/unread?per_page=N&page=P&include_paginate_totals=0
            -> {paginate:{timestamp,...}, projects:[{id,type,karte}]}
  messages: GET /api/v2t/projects/{id}/messages?unread=1&timestamp={ts}&keep_read_status=1
            &include_meta=1&exclude_terminated_ex_application=1&include_paginate_totals=1
  threads : GET /api/v2t/projects/{pid}/messages/{mid}/messages  (full reply bodies)
  latest  : GET /api/v2t/projects/{id}/messages/latest?after={ts} -> {is_self_only,message}
  files   : GET {files[].url}  (anonymous 200 — no auth/cookie needed)
  mark    : POST /api/v2t/projects/{id}/mark_as_read  form: timestamp={ts}
            -> 200 {"project":{"is_unread":false}}   (timestamp OPTIONAL server-side:
            adapter MUST always send it — omitted means "read everything now")
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

import asyncio
import hashlib
import json
from datetime import datetime
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field

BASE = "https://www.medical-care.net"
API = f"{BASE}/api/v2t"
LS_TOKEN_KEY = "ngStorage-lastSessionToken"
_ALLOWED_DOWNLOAD_HOSTS = {"www.medical-care.net"}
# MCS /files/* 302s to a self-authenticating signed URL on the operator's CDN;
# following it is safe only WITHOUT the Bearer header (it must never leave
# the origin host), so it is handled manually in _open_download.
_ALLOWED_REDIRECT_HOSTS = _ALLOWED_DOWNLOAD_HOSTS | {"files.medical-care.net"}
_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


class MCSError(Exception):
    """Structured, log-safe error: kind/status/retryable only — never bodies."""
    def __init__(self, kind: str, detail: str = "", status: int | None = None,
                 retryable: bool = False):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.status = status
        self.retryable = retryable


class SessionExpired(MCSError):
    def __init__(self, detail: str = "", status: int | None = None):
        super().__init__("session_expired", detail, status, retryable=False)


class SchemaError(MCSError):
    def __init__(self, detail: str):
        super().__init__("schema_error", detail)


class BootstrapError(MCSError):
    def __init__(self, detail: str):
        super().__init__("bootstrap_error", detail)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when the target stays on an allowlisted host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlparse(newurl)
        if (u.scheme != "https" or u.hostname not in _ALLOWED_DOWNLOAD_HOSTS
                or u.port not in (None, 443)):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _assert_allowed_url(url: str):
    u = urllib.parse.urlparse(url)
    if (u.scheme != "https" or u.hostname not in _ALLOWED_DOWNLOAD_HOSTS
            or u.port not in (None, 443)):
        raise MCSError("url_not_allowed",
                       f"scheme={u.scheme} host={u.hostname}")


@dataclass
class Attachment:
    file_id: str
    name: str
    url: str
    thumbnail_url: str | None = None


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
    body_state: str            # unknown | snippet | full
    is_unread: bool
    reply_count: int
    replies: list["Message"] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    has_new_replies: bool = False   # set by fetch_history when an old
                                    # parent carries replies past cutoff


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
    return type(v) is int and v > 0


def _has_next(pag: dict, label: str) -> bool:
    value = pag.get("has_next")
    if type(value) is not bool:
        raise SchemaError(f"{label}: has_next invalid")
    return value


def _attachments(files: list | None) -> list[Attachment]:
    if files is not None and not isinstance(files, list):
        raise SchemaError("message: files invalid")
    out = []
    for f in files or []:
        if not isinstance(f, dict):
            raise SchemaError("message: file invalid")
        url = _text(f.get("url"), "message: file url")
        fid = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        thumb = f.get("thumbnail_file") or {}
        if not isinstance(thumb, dict):
            raise SchemaError("message: thumbnail invalid")
        thumb_url = thumb.get("url")
        out.append(Attachment(
            file_id=str(fid), name=_text(f.get("name"), "message: file name"),
            url=url,
            thumbnail_url=(None if thumb_url is None else
                           _text(thumb_url, "message: thumbnail url"))))
    return out


def _norm_message(m: dict, project_id: int, parent_id: int | None = None,
                  is_unread: bool | None = None) -> Message:
    if not isinstance(m, dict):
        raise SchemaError("message: object invalid")
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
    )


def _norm_threads(items, project_id: int, parent_id: int) -> list[Message]:
    if not isinstance(items, list):
        raise SchemaError(f"thread[{parent_id}]: messages invalid")
    if any(not isinstance(m, dict) or not _valid_id(m.get("id"))
           for m in items):
        raise SchemaError(f"thread[{parent_id}]: invalid id")
    return [_norm_message(m, project_id, parent_id=parent_id) for m in items]


class MCSAdapter:
    def __init__(self, cdp_url: str = "http://127.0.0.1:9333",
                 token_cache: str | None = None, timeout: int = 20):
        self.cdp_url = cdp_url
        self.timeout = timeout
        self._token: str | None = None
        self._token_cache = token_cache
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect)
        self._dl_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _SameHostRedirect)

    # ---------- session ----------

    def bootstrap_token(self) -> str:
        cached = self._read_cache()
        try:
            tok = self._token_via_cdp()
            self._token = tok
            self._write_cache(tok)
            return tok
        except Exception as e:
            if cached:
                self._token = cached
                return cached
            raise BootstrapError(f"token bootstrap failed: {type(e).__name__}") from e

    def ensure_session(self) -> bool:
        """Validate current token; on failure try the OTHER source once."""
        if self.check_session():
            return True
        cached = self._read_cache()
        if cached and cached != self._token:
            self._token = cached
            if self.check_session():
                return True
        try:
            tok = self._token_via_cdp()
            self._token = tok
            self._write_cache(tok)
            if self.check_session():
                return True
        except Exception:
            pass
        return False

    def _read_cache(self) -> str | None:
        if not self._token_cache:
            return None
        try:
            with open(self._token_cache, encoding="utf-8") as f:
                raw = json.load(f)
            tok = raw.get("token") if isinstance(raw, dict) else None
            if type(tok) is str and 8 <= len(tok) <= 128:
                return tok
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def _write_cache(self, token: str):
        if not self._token_cache:
            return
        try:
            d = os.path.dirname(self._token_cache)
            os.makedirs(d, exist_ok=True)
            os.chmod(d, 0o700)
            tmp = self._token_cache + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"token": token, "fetched_at": time.time()}, f)
            os.replace(tmp, self._token_cache)
            os.chmod(self._token_cache, 0o600)
        except OSError:
            pass

    def _token_via_cdp(self) -> str:
        try:
            import websockets
        except ImportError as e:
            raise BootstrapError("websockets missing") from e

        with urllib.request.urlopen(f"{self.cdp_url}/json/list", timeout=5) as r:
            targets = json.load(r)
        page = next((t for t in targets if t.get("type") == "page"
                     and urllib.parse.urlparse(t.get("url", "")).hostname
                     == "www.medical-care.net"), None)
        if not page:
            req = urllib.request.Request(f"{self.cdp_url}/json/new?{BASE}/unreads",
                                         method="PUT")
            with urllib.request.urlopen(req, timeout=5) as r:
                page = json.load(r)
            time.sleep(3)

        async def _eval():
            async with asyncio.timeout(15):
                async with websockets.connect(page["webSocketDebuggerUrl"],
                                              max_size=8 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({
                        "id": 1, "method": "Runtime.evaluate",
                        "params": {"expression":
                                   f"localStorage.getItem('{LS_TOKEN_KEY}')",
                                   "returnByValue": True}}))
                    while True:
                        m = json.loads(await ws.recv())
                        if m.get("id") == 1:
                            return m["result"]["result"].get("value")

        raw = asyncio.run(_eval())
        if not raw:
            raise BootstrapError("no session token in localStorage")
        try:
            tok = json.loads(raw)
        except json.JSONDecodeError:
            tok = raw.strip('"')
        if type(tok) is not str or not (8 <= len(tok) <= 128):
            raise BootstrapError("session token failed shape check")
        return tok

    def check_session(self) -> bool:
        try:
            self._get("/users/self/count", {"targets": "unread_groups"},
                      extend_session=False)
            return True
        except SessionExpired:
            return False

    # ---------- auto login ----------

    def _cdp_up(self) -> bool:
        try:
            with urllib.request.urlopen(
                    f"{self.cdp_url}/json/version", timeout=3):
                pass
            return True
        except OSError:
            return False

    def _ensure_chrome(self, profile_dir: str, chrome_bin: str):
        if self._cdp_up():
            return
        import subprocess
        subprocess.Popen([
            chrome_bin,
            f"--remote-debugging-port={urllib.parse.urlparse(self.cdp_url).port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run", "--no-default-browser-check",
            f"{BASE}/authentication/login"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(1)
            if self._cdp_up():
                return
        raise BootstrapError("chrome launch timed out")

    def _cdp_eval(self, ws_url: str, expr: str, timeout: int = 15):
        import websockets

        async def _ev():
            async with asyncio.timeout(timeout):
                async with websockets.connect(ws_url,
                                              max_size=8 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({
                        "id": 1, "method": "Runtime.evaluate",
                        "params": {"expression": expr,
                                   "returnByValue": True}}))
                    while True:
                        m = json.loads(await ws.recv())
                        if m.get("id") == 1:
                            return m["result"]["result"].get("value")
        return asyncio.run(_ev())

    def _login_page(self):
        """Pick the login tab by STRICT origin+path — never fill credentials
        into a lookalike path on another host (Oracle B14)."""
        with urllib.request.urlopen(f"{self.cdp_url}/json/list", timeout=5) as r:
            targets = json.load(r)
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
                time.sleep(4)
                return t
        req = urllib.request.Request(
            f"{self.cdp_url}/json/new?{BASE}/authentication/login", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            page = json.load(r)
        time.sleep(4)
        return page

    def _keychain_password(self, service: str = "mcs-adapter") -> str | None:
        """Fetch MCS password from macOS Keychain. First call may show an ACL
        prompt — 'Always Allow' makes subsequent reads silent. Returns None if
        the entry doesn't exist or access is denied."""
        import subprocess
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        return None

    def _config(self) -> dict:
        try:
            with open(os.path.expanduser("~/.mcs/config.json"),
                      encoding="utf-8") as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def auto_login(self, profile_dir: str = "", chrome_bin: str = "",
                   wait_s: int = 45) -> str:
        """Re-login by filling the form from macOS Keychain and clicking
        submit. Native Chrome autofill can't be driven via DOM, so the
        credential lives in Keychain (service 'mcs-adapter'); the adapter
        reads it at runtime and injects via input events. loginId is
        persisted by the app itself; if empty we fill from config
        'mcs_login_id'. Password is never logged or stored by us.

        Returns 'ok' | 'manual_required' | 'failed'."""
        try:
            self._ensure_chrome(profile_dir, chrome_bin)
        except (BootstrapError, OSError):
            return "failed"
        try:
            page = self._login_page()
            ws = page["webSocketDebuggerUrl"]
            time.sleep(3)  # Angular render
            state = self._cdp_eval(ws, """(() => {
              if (location.origin !== 'https://www.medical-care.net')
                return 'bad_origin';
              const id = document.querySelector('input[name=loginId]');
              const pw = document.querySelector('input[type=password]');
              if (!pw) return 'no_form';
              if (pw.value) return 'ready';
              return (id && id.value) ? 'need_pw' : 'need_both';
            })()""")
            if state in ("no_form", "bad_origin"):
                return "manual_required" if state == "no_form" else "failed"
            if state != "ready":
                pw = self._keychain_password()
                if not pw:
                    return "manual_required"
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
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(2)
            try:
                tok = self._token_via_cdp()
            except Exception:
                continue
            if tok:
                self._token = tok
                if self.check_session():
                    self._write_cache(tok)
                    return "ok"
                # token present but not yet valid, or MFA page — keep waiting
        return "manual_required"

    # ---------- http ----------

    def _request(self, method: str, path: str, params: dict | None = None,
                 data: bytes | None = None, headers: dict | None = None,
                 extend_session: bool = True, retries: int = 2) -> tuple[int, bytes, dict]:
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
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            try:
                with self._opener.open(req, timeout=self.timeout) as res:
                    return res.status, res.read(), dict(res.headers)
            except urllib.error.HTTPError as e:
                e.read()
                if e.code in (401, 403):
                    raise SessionExpired(f"{method} {path}", status=e.code)
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    last = e
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise MCSError("http_error", f"{method} {path}", status=e.code,
                               retryable=e.code >= 500)
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise MCSError("network_error", f"{method} {path}", retryable=True) from last

    def _get(self, path: str, params: dict | None = None,
             extend_session: bool = True) -> dict:
        status, body, _ = self._request("GET", path, params,
                                      extend_session=extend_session)
        try:
            out = json.loads(body)
        except json.JSONDecodeError:
            raise SessionExpired(f"{path} non-json (login redirect?)")
        if not isinstance(out, dict):
            raise SchemaError(f"{path} -> non-object json")
        return out

    # ---------- reads ----------

    def list_unread(self, per_page: int = 10, max_pages: int = 50) -> UnreadSnapshot:
        patients: list[UnreadPatient] = []
        ts = None
        prev_page_ids: set[int] | None = None
        for page in range(1, max_pages + 1):
            r = self._get("/projects/unread", {
                "per_page": per_page, "page": page,
                "include_paginate_totals": 0})
            pag = r.get("paginate")
            if not isinstance(pag, dict):
                raise SchemaError("unread: paginate missing")
            projs = r.get("projects")
            if not isinstance(projs, list):
                raise SchemaError("unread: projects missing")
            page_ts = pag.get("timestamp")
            if ts is not None and page_ts != ts:
                raise SchemaError("unread: timestamp changed during pagination")
            ts = page_ts if ts is None else ts
            ids = set()
            for p in projs:
                if not isinstance(p, dict) or not _valid_id(p.get("id")):
                    raise SchemaError("unread: invalid project id")
                ids.add(p["id"])
                k = p.get("karte") or {}
                if not isinstance(k, dict):
                    raise SchemaError("unread: karte invalid")
                st = k.get("station") or {}
                if not isinstance(st, dict):
                    raise SchemaError("unread: station invalid")
                patients.append(UnreadPatient(
                    project_id=p["id"],
                    project_type=_text(p.get("type"), "unread: project type"),
                    patient_name=(
                        f"{_text(k.get('last_name'), 'unread: last_name')} "
                        f"{_text(k.get('first_name'), 'unread: first_name')}"
                    ).strip(),
                    disease=_text(k.get("disease"), "unread: disease"),
                    station_name=_text(st.get("name"),
                                       "unread: station name"),
                    url=f"{BASE}/projects/medical/{p['id']}"))
            if prev_page_ids is not None and ids and ids <= prev_page_ids:
                raise SchemaError("unread: pagination not advancing")
            prev_page_ids = ids
            if not _has_next(pag, "unread"):
                break
        else:
            raise MCSError("pages_exceeded", "unread list > 50 pages", retryable=True)
        if type(ts) is not int:
            raise SchemaError("unread: paginate.timestamp missing/invalid")
        return UnreadSnapshot(timestamp=ts, patients=patients)

    def fetch_unread_messages(self, project_id: int, timestamp: int,
                              per_page: int = 10,
                              max_pages: int = 20) -> MessageBatch:
        """Partial-page resilience: if a mid-walk page fails, already-fetched
        completed pages and any terminal error are returned together so the
        caller can save them and mark the patient incomplete (Oracle B04)."""
        msgs: list[Message] = []
        pages = 0
        reached = False
        error = None
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
        return MessageBatch(msgs, pages, reached, error)

    def fetch_thread(self, project_id: int, message_id: int,
                     max_pages: int = 10) -> list[Message]:
        """All replies in the thread — the endpoint paginates (10/page)
        and page 1 alone truncated any thread beyond that, leaving
        reply jobs to burn out permanently. A thread still reporting
        has_next after max_pages raises thread_incomplete rather than
        certify a truncated result — callers keep their durable retry."""
        out: list[Message] = []
        seen: set[int] = set()
        for page in range(1, max_pages + 1):
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
            for m in _norm_threads(r.get("messages"), project_id,
                                   message_id):
                if m.message_id not in seen:
                    seen.add(m.message_id)
                    out.append(m)
            if not has_next:
                return out
        raise MCSError("thread_incomplete", retryable=True)

    def fetch_unread_replies(self, msg: Message) -> ReplyBatch:
        """Full bodies for replies flagged is_unread (list gives snippets only).
        Merges in place; returns fetched full replies. Replies absent from
        the thread response are returned in ReplyBatch.missing so the caller
        can queue a durable refetch job instead of falsely passing the patient
        as complete (Oracle B05)."""
        if not any(t.is_unread for t in msg.replies):
            return ReplyBatch([], [])
        full = self.fetch_thread(msg.project_id, msg.message_id)
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

    def check_project_delta(self, project_id: int, after_ts: int) -> dict | None:
        r = self._get(f"/projects/{project_id}/messages/latest",
                      {"after": after_ts, "keep_read_status": 1},
                      extend_session=False)
        return r.get("message")

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
                if not isinstance(p, dict) or not _valid_id(p.get("id")):
                    raise SchemaError("projects: invalid project id")
                k = p.get("karte") or {}
                if not isinstance(k, dict):
                    raise SchemaError("projects: karte invalid")
                st = k.get("station") or {}
                lm = p.get("last_message") or {}
                if not all(isinstance(x, dict) for x in (st, lm)):
                    raise SchemaError("projects: nested object invalid")
                up = UnreadPatient(
                    project_id=p["id"],
                    project_type=_text(p.get("type"),
                                       "projects: project type"),
                    patient_name=(
                        f"{_text(k.get('last_name'), 'projects: last_name')} "
                        f"{_text(k.get('first_name'), 'projects: first_name')}"
                    ).strip(),
                    disease=_text(k.get("disease"), "projects: disease"),
                    station_name=_text(st.get("name"),
                                       "projects: station name"),
                    url=f"{BASE}/projects/medical/{p['id']}")
                ca = _text(lm.get("created_at"),
                           "projects: last_message.created_at")
                try:
                    up.last_activity = int(
                        datetime.fromisoformat(ca).timestamp()) if ca else 0
                except ValueError:
                    up.last_activity = 0
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
                    url=f"{BASE}/projects/medical/{pid}"))
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
        MessageBatch.reached reports cutoff/natural end and pages reports
        pages consumed by this call for durable resume cursors.
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
                stop = False
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
                        if parent_ts <= since_ts < newest:
                            nm.has_new_replies = True
                        if newest <= since_ts:
                            stop = True
                            break
                    page_messages.append(nm)
            except MCSError as e:
                error = e
                break
            out.extend(page_messages)
            pages += 1
            if stop:
                reached = True
                break
            if not has_next:
                reached = True
                break
        return MessageBatch(out, pages, reached, error)

    def _open_download(self, url: str):
        if not self._token:
            raise MCSError("no_token")
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self._token}")
        try:
            return self._dl_opener.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("location")
                if loc:
                    u = urllib.parse.urlparse(loc)
                    if (u.scheme == "https"
                            and u.hostname in _ALLOWED_REDIRECT_HOSTS
                            and u.port in (None, 443)):
                        # signed CDN URL authenticates itself — follow with
                        # a fresh request carrying NO Authorization header
                        return self._dl_opener.open(
                            urllib.request.Request(loc), timeout=60)
            raise

    def download(self, url: str, dest: str) -> dict:
        _assert_allowed_url(url)
        total = 0
        h = hashlib.sha256()
        tmp = dest + ".part"
        try:
            with self._open_download(url) as res, open(tmp, "wb") as f:
                while True:
                    chunk = res.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise MCSError("download_too_large")
                    h.update(chunk)
                    f.write(chunk)
            os.replace(tmp, dest)
            return {"bytes": total, "sha256": h.hexdigest()}
        except Exception as e:
            # every failure path must remove the partial file (Oracle B28)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if isinstance(e, MCSError):
                raise
            raise MCSError("download_failed", retryable=True) from e

    # ---------- write (guarded) ----------

    def mark_patient_read(self, project_id: int, snapshot_ts: int) -> dict:
        """snapshot_ts is MANDATORY here even though the server accepts it empty —
        omitting marks everything read including unfetched messages. It must be
        the exact int returned by list_unread().timestamp (type checked — bool
        is an int subclass and is explicitly rejected).

        The response is CONFIRMED only when the project payload positively
        reports the read state cleared; anything else (200 {}, missing keys,
        non-JSON, odd shapes) is mark_result_unknown — never confirmed
        (Oracle B02)."""
        if type(snapshot_ts) is not int or snapshot_ts <= 0:
            raise MCSError("bad_snapshot_ts")
        body = urllib.parse.urlencode({"timestamp": snapshot_ts}).encode()
        status, raw, _ = self._request(
            "POST", f"/projects/{project_id}/mark_as_read", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            # 200 + non-JSON: mark result UNKNOWN — never record as success
            raise MCSError("mark_result_unknown", status=status)
        if not isinstance(out, dict):
            raise MCSError("mark_result_unknown", status=status)
        proj = out.get("project")
        if proj is None and isinstance(out.get("data"), dict):
            proj = out["data"].get("project")
        if isinstance(proj, dict):
            if proj.get("is_unread") is False:
                return out
            unread_count = proj.get("unread_count")
            if ("is_unread" not in proj and type(unread_count) is int
                    and unread_count == 0):
                return out
        raise MCSError("mark_result_unknown", status=status)
