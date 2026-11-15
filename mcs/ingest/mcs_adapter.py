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
  files   : GET {files[].url} with Bearer; /files/* 302s to a signed URL on
            files.medical-care.net, followed WITHOUT Authorization
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
from mcs_util import HOME as HOME
from mcs_util import env_value as env_value
import hashlib as hashlib
from mcs_util import load_config as load_config
import re as re
import json
from datetime import datetime
import os
import tempfile
import time
import urllib.parse
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from project_metadata_contract import MetadataFetch

from mcs_util import RETRY_LATER_4XX
from safe_io import atomic_write, fsync_dir
from mcs_worker import WorkerError, bounded_call
from mcs_authentication import (
    MCSAuthenticationMixin,
    KeychainLocked as KeychainLocked,
    HUNG_PROBE_S as HUNG_PROBE_S,
    LOGIN_BACKOFF_BASE_S as LOGIN_BACKOFF_BASE_S,
    LOGIN_BACKOFF_MAX_S as LOGIN_BACKOFF_MAX_S,
    _MCS_HOST as _MCS_HOST,
    _is_mcs_page as _is_mcs_page,
    _login_accounts as _login_accounts,
    _other_login_accounts as _other_login_accounts,
)

from mcs_download import disk_error_kind, validate_download_url

from mcs_contract import (API, BASE, BootstrapError, MCSError as MCSError, SessionExpired as SessionExpired, SchemaError as SchemaError, Attachment as Attachment, Message as Message,
                          UnreadPatient as UnreadPatient, UnreadSnapshot as UnreadSnapshot, MessageBatch as MessageBatch, ReplyBatch as ReplyBatch, project_url as project_url, valid_id)
from mcs_payload import (text_field, sender_name, profession, organization, page_has_next, patient_name_info, patient_name_karte, unread_patient,
                         message_metadata, normalize_message, normalize_root, normalize_threads)
from stamp_contract import REACTION_LABELS, reaction_kind_ok
# The MCS unread screen (and its unread=1 route) lists at most this many
# messages per project — owner-confirmed 2026-09-30. Reaching it means
# older unread rows may be invisible to the unread walk.
UNREAD_SCREEN_CAP = 80


# cheap authenticated read used to tell an expired session from a
# route-level 403 (see _request)
SESSION_PROBE_PATH = "/users/self/count"
LS_TOKEN_KEY = "ngStorage-lastSessionToken"
# 連携サマリー is a 150-char field on the MCS side; 600 bounds a misbehaving
# response without ever truncating a legitimate one
KARTE_SUMMARY_MAX_CHARS = 600
# H5: a login form submitted with the stored credential that never
# validated is not retried with the SAME credential until this backoff
# (1h, 2h, 4h ... capped at 24h) passes — a changed password must not be
# re-sent ~430 times a day towards an account lock. A changed credential
# (its Keychain item or .env changed since) is tried at once. Form-free
# session recovery is never gated. No function of the password is stored.
LOGIN_STATE_NAME = "login_state.json"


def _thread_has_next(r) -> bool:
    """Thread page continuation: no ``paginate`` means the last page; a non-object one is a schema error."""
    if "paginate" not in r:
        return False
    if not isinstance(r["paginate"], dict):
        raise SchemaError("thread: paginate invalid")
    return page_has_next(r["paginate"], "thread")


def walk_reaction_actors(get, project_id: int, message_id: int, *,
                         reaction_type: str | None = None,
                         max_pages: int = 10, per_page: int = 50) -> dict:
    """Walk one fixed-snapshot actor list; complete only when it matches the post's counts.

    `get` is an adapter `_get`. Transport and schema failures end the
    walk with `error`/`status` and the partial rows; a partial walk is
    never a proof of absence. Rows keep only actor id, kind and
    profession, name and facility — the icon is dropped here.
    """
    if (not valid_id(project_id) or not valid_id(message_id)
            or type(max_pages) is not int or not 1 <= max_pages <= 10
            or type(per_page) is not int or not 1 <= per_page <= 50
            or (reaction_type is not None
                and not reaction_kind_ok(reaction_type))):
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
                    if name in meta and (not valid_id(meta[name]) or meta[name] != wanted):
                        raise SchemaError("actors: target mismatch")
            if page == 1:
                normalized, errors = message_metadata(meta or {}, project_id)
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
            if not valid_id(server_ts):
                raise SchemaError("actors: timestamp invalid")
            if timestamp is None:
                timestamp = server_ts
                result["timestamp_stable"] = True
            elif server_ts != timestamp:
                result["timestamp_stable"] = False
                raise SchemaError("actors: timestamp changed")
            if "has_next" in paginate:
                has_next = page_has_next(paginate, "actors")
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
                if (not isinstance(user, dict) or not valid_id(user.get("id"))
                        or not reaction_kind_ok(kind)):
                    raise SchemaError("actors: identity invalid")
                identity = (user["id"], kind)
                if identity in actors:
                    raise SchemaError("actors: duplicate actor")
                # optional display fields, never identity (#22-D2 2026-10-03:
                # names are kept and shown; the icon is never taken)
                extra = {}
                for field, read in (("profession", profession),
                                    ("name", sender_name),
                                    ("organization", organization)):
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


def thread_page_budget(reply_count: int) -> dict:
    """fetch_thread kwargs that cover a thread of reply_count replies (10 per page)."""
    # fetch_thread's default 10 pages already covers 100 replies
    return {"max_pages": (reply_count + 9) // 10} if reply_count > 100 else {}


RATE_LIMIT_STOP = 2      # 429 responses per run before API reads stop
RETRY_AFTER_MAX_S = 60   # longest honored Retry-After wait


def _retry_after(headers, default: float) -> float:
    """Bounded Retry-After delay in seconds; ``default`` when absent or not delta-seconds."""
    value = next((v for k, v in (headers or {}).items()
                  if str(k).lower() == "retry-after"), None)
    try:
        return min(RETRY_AFTER_MAX_S, max(0.0, float(str(value).strip())))
    except ValueError:
        return default


class MCSAdapter(MCSAuthenticationMixin):
    def __init__(self, cdp_url: str = "http://127.0.0.1:9333",
                 token_cache: str | None = None, timeout: int = 20, *,
                 worker=None):
        self.cdp_url = cdp_url
        self.timeout = timeout
        self._token: str | None = None
        self._token_cache = token_cache
        self._deadline: float | None = None
        # one adapter per run: after RATE_LIMIT_STOP 429s every later API
        # request of this run is refused locally, so the next tick retries
        self._rate_limited = 0
        self._worker = bounded_call if worker is None else worker
        # per-attempt browser repair facts, journaled by run_check
        self.healed_tabs, self.chrome_restarted = 0, False
        # submitted-but-rejected login outcomes persist across runs next to
        # the token cache (outside the sandbox-mounted data/)
        self._login_state = (os.path.join(os.path.dirname(
            os.path.abspath(token_cache)), LOGIN_STATE_NAME)
            if token_cache else None)

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

    def load_cached_session(self) -> bool:
        """Explicitly load only the configured token cache; never log in or contact CDP."""
        self._token = self._read_cache()
        return bool(self._token)

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
        page = next((t for t in targets if _is_mcs_page(t)), None)
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
        profs = [n for n in (text_field(c.get("name"),
                                   "self_profile: specialist name")
                           for c in cats) if n]
        orgs = [n for n in (text_field(s.get("name"),
                                  "self_profile: station name")
                          for s in sts) if n]
        name = sender_name(u)
        if not (name or profs or orgs):
            raise SchemaError("self_profile: empty profile")
        sid = u.get("id")
        # station ids feed station_staffs(); record_self_profile keeps
        # only the identity fields, so the stored artifact is unchanged
        stations = [{"id": s["id"], "name": text_field(s.get("name"),
                                                  "self_profile: station name")}
                    for s in sts if valid_id(s.get("id"))]
        return {"sender_id": sid if type(sid) in (int, str) else None,
                "name": name,
                "professions": profs, "organizations": orgs,
                "stations": stations}

    def fetch_message_metadata(self, project_id: int, message_id: int, *,
                               parent_id: int | None = None) -> Message:
        """Read one exact post with read/session preservation for shadow refresh."""
        if not valid_id(project_id) or not valid_id(message_id):
            raise SchemaError("metadata: invalid id")
        if parent_id is not None and (not valid_id(parent_id) or parent_id == message_id):
            raise SchemaError("metadata: invalid parent id")
        path = f"/projects/{project_id}/messages"
        if parent_id is not None:
            path += f"/{parent_id}/messages"
        r = self._get(path, {
            "message_id": message_id, "per_page": 1, "keep_read_status": 1},
            extend_session=False)
        items = r.get("messages")
        if (not isinstance(items, list) or len(items) != 1
                or not isinstance(items[0], dict) or not valid_id(items[0].get("id"))
                or not valid_id(items[0].get("project_id", project_id))
                or items[0].get("id") != message_id
                or items[0].get("project_id", project_id) != project_id):
            raise SchemaError("metadata: target mismatch")
        return normalize_message(items[0], project_id, parent_id=parent_id)

    def fetch_reaction_actors(self, project_id: int, message_id: int, *,
                              max_pages: int = 10, per_page: int = 50) -> dict:
        """GET-only walk of every reaction actor on one post (see walk_reaction_actors)."""
        return walk_reaction_actors(self._get, project_id, message_id,
                                    max_pages=max_pages, per_page=per_page)

    def station_staffs(self, stations: list, per_page: int = 100,
                       max_pages: int = 10, *, strict_self: bool = False) -> list:
        """The pharmacy's own member roster: GET /stations/{id}/staffs
        for each own station (from /users/self), paginated. Shared
        facility accounts (is_station_account) are skipped. Returns
        [{staff_id, name, professions, station, is_self}] in MCS order.
        Raises MCSError(kind='station_staffs_unavailable') on failure —
        an expired session stays SessionExpired. strict_self (identity
        proof for a write) raises SchemaError instead of skipping an
        is_self row that is unusable, so a dropped second self cannot
        make the remaining one look unique."""
        out = []
        for st in stations:
            sid, sname = st.get("id"), st.get("name") or ""
            if not valid_id(sid):
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
                            or not valid_id(u.get("id")):
                        if strict_self and u.get("is_self") is True:
                            raise SchemaError("staffs: self row unusable")
                        continue
                    name = sender_name(u)
                    if not name:
                        if strict_self and u.get("is_self") is True:
                            raise SchemaError("staffs: self row unusable")
                        continue
                    out.append({"staff_id": u["id"], "name": name,
                                "professions": [p for p in profession(u)
                                                .split(", ") if p],
                                "station": sname,
                                "is_self": u.get("is_self") is True})
                if not page_has_next(pag, "staffs"):
                    break
            else:
                raise MCSError("pages_exceeded", "station staffs",
                               retryable=True)
        return out

    # ---------- auto login ----------


    # ---------- http ----------

    def _request(self, method: str, path: str, params: dict | None = None,
                 data: bytes | None = None, headers: dict | None = None,
                 extend_session: bool = True, retries: int = 2, *,
                 _cached_session_only: bool = False) -> tuple[int, bytes, dict]:
        self._remaining_timeout(self.timeout)
        if _cached_session_only and (not isinstance(self._token, str)
                or not self._token.strip() or any(c in self._token for c in "\r\n")):
            raise MCSError("no_token")
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
            # a human-approved stamp POST is never refused locally: a refusal
            # after its witness would read as an unknown, never-resent effect
            if self._rate_limited >= RATE_LIMIT_STOP and not _cached_session_only:
                raise MCSError("rate_limited", f"{method} {path}", status=429,
                               retryable=True)
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
            if status == 403 and not _cached_session_only and path != SESSION_PROBE_PATH \
                    and self._probe_session_ok():
                # the session is fine — a route/project-level denial
                # (retired route, revoked project) is a per-request
                # error, never a re-login trigger that aborts the run
                raise MCSError("forbidden", f"{method} {path}",
                               status=status)
            if status in (401, 403):
                raise SessionExpired(f"{method} {path}", status=status)
            if status >= 300:
                if status == 429:
                    self._rate_limited += 1
                if status in (429, 500, 502, 503, 504) and attempt < retries \
                        and self._rate_limited < RATE_LIMIT_STOP:
                    delay = 1.5 * (attempt + 1)
                    self._sleep_bounded(_retry_after(result.get("headers"), delay)
                                        if status == 429 else delay)
                    continue
                raise MCSError("http_error", f"{method} {path}", status=status,
                               retryable=status >= 500 or status in RETRY_LATER_4XX)
            return status, base64.b64decode(result["body"], validate=True), result["headers"]
        raise MCSError("network_error", f"{method} {path}", retryable=True) from last

    def write_reaction(self, message_id: int, reaction_type: str | None = None, *,
                       cancel: bool = False) -> int:
        """Attempt one authenticated own-stamp mutation; the caller owns its durable fence.

        Never log in, probe the session, or retry a possible remote effect.
        A response status is transport evidence only; callers must verify the
        exact post before reporting the requested reaction state as observed.
        """
        if (not valid_id(message_id) or type(cancel) is not bool
                or (cancel and reaction_type is not None)
                or (not cancel and (not isinstance(reaction_type, str)
                                    or reaction_type not in REACTION_LABELS))):
            raise ValueError("invalid reaction arguments")
        path = f"/messages/{message_id}/reactions"
        data, headers = None, None
        if cancel:
            path += "/delete"
        else:
            data = urllib.parse.urlencode({"reaction_type": reaction_type}).encode("utf-8")
            headers = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
        try:
            status, _body, _headers = self._request(
                "POST", path, data=data, headers=headers,
                extend_session=False, retries=0, _cached_session_only=True)
            return status
        except SessionExpired as error:
            raise SessionExpired(status=error.status) from None
        except MCSError as error:
            raise MCSError(error.kind, status=error.status, retryable=False) from None
        except Exception:
            # Neither server bodies nor worker diagnostics belong in receipts.
            raise MCSError("reaction_request_failed", retryable=False) from None

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
        """Read one bounded cached-session cross-list snapshot without login, probe or retries."""
        from cross_lists import (CrossListFetch, normalize_entry, parse_page,
                                 validate_fetch_options, validate_page_rows,
                                 validate_page_total)
        validate_fetch_options(dataset, unread_only, max_pages=max_pages,
                               per_page=per_page, max_rows=max_rows, deadline_s=deadline_s)
        result = CrossListFetch()
        if not self._token:
            result.reason = "cached_session_required"
            return result
        previous_deadline = self._deadline
        end = time.monotonic() + deadline_s
        self.set_deadline(min(end, previous_deadline) if previous_deadline is not None else end)
        entries, seen = [], set()
        total, total_pages = None, None
        try:
            for page in range(1, max_pages + 1):
                params = {"page": page, "per_page": per_page, "include_paginate_totals": 0}
                if dataset == "mentioned":
                    params["unread"] = int(unread_only)
                    if page == 1:
                        params["include_meta"] = 1
                if result.timestamp is not None:
                    params["timestamp"] = result.timestamp
                # Ordinary _request can probe/retry/sleep when classifying 403.
                # Use its existing bounded worker: no login, retry or extra probe.
                params["no_extend_session"] = 1
                response = self._io(
                    "api", self.timeout,
                    url=f"{API}/messages/{dataset}?{urllib.parse.urlencode(params)}",
                    method="GET", data=None,
                    headers={"Authorization": f"Bearer {self._token}",
                             "Accept": "application/json"})
                status = response["status"]
                if status == 401:
                    raise SessionExpired(status=status)
                if status >= 300:
                    raise MCSError("http_error", status=status)
                body = base64.b64decode(response["body"], validate=True)
                self._remaining_timeout(self.timeout)
                try:
                    raw = json.loads(body)
                except (ValueError, RecursionError):
                    raise SchemaError("cross list: json invalid") from None
                result.pages += 1
                parsed = parse_page(raw, page=page, per_page=per_page,
                                    previous_timestamp=result.timestamp, total_pages=total_pages)
                total_pages = parsed.total_pages
                if page == 1:
                    result.timestamp = parsed.timestamp
                reason, total = validate_page_rows(
                    parsed, entry_count=len(entries), max_rows=max_rows, total=total)
                if reason:
                    result.reason = reason
                    break
                rows, has_next = parsed.rows, parsed.has_next
                for row in rows:
                    self._remaining_timeout(self.timeout)
                    entry = normalize_entry(row, seen)
                    identity = (entry.message.project_id, entry.message.message_id)
                    seen.add(identity)
                    entries.append(entry)
                validate_page_total(has_next=has_next, entry_count=len(entries), total=total)
                self._remaining_timeout(self.timeout)
                if not has_next:
                    result.complete = True
                    result.entries = entries
                    break
            else:
                result.reason = "page_limit"
        except MCSError as error:
            result.reason = error.kind
            result.http_status = error.status
        finally:
            self.set_deadline(previous_deadline)
        return result

    def fetch_project_members(self, project_id: int, *, max_pages: int = 5,
                              per_page: int = 50, retain_names: bool = False) -> MetadataFetch:
        """Bounded current membership GET; no photos, contacts or departed members.

        Kept since 1.0.13 as a compatibility entry point for callers outside
        the scheduler; production paths call project_metadata.fetch_metadata.
        """
        from project_metadata import fetch_metadata
        return fetch_metadata(self._get, "care_team", project_id,
                              max_pages=max_pages, per_page=per_page,
                              retain_names=retain_names)

    def fetch_observation_values(self, karte_id: int, lab_test_item_id: int, *,
                                 max_pages: int = 5, per_page: int = 50) -> MetadataFetch:
        """Read one evidenced lab-test item's values without fetching full karte data.

        Kept since 1.0.13 as a compatibility entry point for callers outside
        the scheduler; production paths call project_metadata.fetch_metadata.
        """
        from project_metadata import fetch_metadata
        return fetch_metadata(self._get, "observation_values", karte_id,
                              item_id=lab_test_item_id,
                              max_pages=max_pages, per_page=per_page)

    def fetch_group_consultations(self, project_id: int, *, project_type: str,
                                  max_pages: int = 5, per_page: int = 20) -> MetadataFetch:
        """Read group consultation metadata only; never infer patient associations.

        Kept since 1.0.13 as a compatibility entry point for callers outside
        the scheduler; production paths call project_metadata.fetch_metadata.
        """
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
            "updated_at": text_field(ms.get("updated_at"),
                                "memo_summary: updated_at"),
            "is_editable": ms["is_editable"],
            "user": {
                "profession": text_field(user.get("profession"),
                                    "memo_summary: user profession"),
                "name": text_field(user.get("name"), "memo_summary: user name")},
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
        listed_patients: list[UnreadPatient] = []
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
            if not valid_id(page_ts):
                raise SchemaError("unread: paginate.timestamp invalid")
            ts = page_ts if ts is None else min(ts, page_ts)
            ids = set()
            for p in projs:
                if not isinstance(p, dict) or not valid_id(p.get("id")):
                    raise SchemaError("unread: invalid project id")
                ids.add(p["id"])
                if type(p.get("is_unread")) is not bool:
                    raise SchemaError("unread: is_unread missing/invalid")
                patient = unread_patient(p, "unread")
                listed_patients.append(patient)
                if not p["is_unread"]:
                    continue
                patients.append(patient)
            if prev_page_ids is not None and ids and ids <= prev_page_ids:
                raise SchemaError("unread: pagination not advancing")
            prev_page_ids = ids
            if not page_has_next(pag, "unread"):
                break
        else:
            raise MCSError("pages_exceeded", "unread list > 50 pages",
                           retryable=True)
        return UnreadSnapshot(timestamp=ts, patients=patients,
                              listed_patients=listed_patients)

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
                has_next = page_has_next(pag, f"messages[{project_id}]")
                if type(pag.get("total_entries")) is int:
                    total = pag["total_entries"]
                page_messages = []
                for m in items:
                    page_messages.append(normalize_root(m, project_id, f"messages[{project_id}]"))
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
        if not isinstance(oldest, dict) or not valid_id(oldest.get("id")):
            raise SchemaError(f"project[{project_id}]: oldest_unread invalid")
        return oldest["id"]

    def _thread_page(self, project_id: int, message_id: int,
                     page: int) -> tuple[list[Message], bool]:
        r = self._get(
            f"/projects/{project_id}/messages/{message_id}/messages",
            {"keep_read_status": 1, "page": page})
        has_next = _thread_has_next(r)
        return (normalize_threads(r.get("messages"), project_id,
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
        full = self.fetch_thread(msg.project_id, msg.message_id,
                                 **thread_page_budget(msg.reply_count))
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
                up = unread_patient(p, "projects")
                lm = p.get("last_message") or {}
                if not isinstance(lm, dict):
                    raise SchemaError("projects: nested object invalid")
                ca = text_field(lm.get("created_at"),
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
            if not page_has_next(pag, "projects"):
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
                        or not valid_id(proj.get("id")):
                    raise SchemaError("kartes: medical_project invalid")
                pid = proj["id"]
                if pid in seen:
                    continue
                seen.add(pid)
                station = k.get("station") or {}
                if not isinstance(station, dict):
                    raise SchemaError("kartes: station invalid")
                patient_name, name_presence = patient_name_info(k, "kartes")
                name_karte_id, name_karte_state = patient_name_karte(k)
                out.append(UnreadPatient(
                    # medical_project.id joins the same /projects/medical
                    # namespace — the join itself is the type
                    project_id=pid,
                    project_type="medical",
                    patient_name=patient_name,
                    disease=text_field(k.get("disease"), "kartes: disease"),
                    station_name=text_field(station.get("name"),
                                       "kartes: station name"),
                    url=project_url(pid), patient_name_presence=name_presence,
                    patient_name_karte_id=name_karte_id, patient_name_karte_state=name_karte_state))
            if not page_has_next(pag, "kartes"):
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
        observations = []
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
                has_next = page_has_next(pag, f"history[{project_id}]")
                page_messages = []
                root_ids = []
                for m in items:
                    nm = normalize_root(m, project_id, f"history[{project_id}]")
                    root_ids.append(nm.message_id)
                    if since_ts and nm.posted_at:
                        try:
                            parent_ts = datetime.fromisoformat(
                                nm.posted_at).timestamp()
                        except ValueError as e:
                            raise SchemaError("history: created_at invalid") from e
                        newest = parent_ts
                        for t in nm.replies:
                            if not t.posted_at:
                                # an undated reply may be newer than since —
                                # keep the parent, like an undated parent
                                newest = float("inf")
                                break
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
            import history_walk
            observations.append(history_walk.observation(project_id, since_ts, page, per_page, has_next, root_ids))
            pages += 1
            if not has_next:
                reached = True
                break
        return MessageBatch(out, pages, reached, error, page_observations=observations)

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
        if not isinstance(msg, dict) or not valid_id(msg.get("id")):
            raise SchemaError(f"latest[{project_id}]: message invalid")
        return {"message_id": msg["id"],
                "is_self_only": bool(self_only)}


    def download(self, url: str, dest: str) -> dict:
        validate_download_url(url)
        if not self._token:
            self.bootstrap_token()
        try:
            fd, tmp = tempfile.mkstemp(prefix=".download-", suffix=".part",
                                       dir=os.path.dirname(dest) or ".")
            os.close(fd)
        except OSError as e:
            raise MCSError(disk_error_kind(e), retryable=True) from None
        try:
            result = self._io("download", 60, url=url, token=self._token,
                              partial=tmp)
            self._remaining_timeout(60)
            os.replace(tmp, dest)
            fsync_dir(os.path.dirname(dest) or ".")
            return result
        except BaseException:
            # _io has already killed and reaped any timed-out writer.
            with suppress(OSError):
                os.unlink(tmp)
            raise


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
        try:
            for page in range(1, max_pages + 1):
                r = self._get(
                    f"/projects/{project_id}/messages/{parent_id}/messages",
                    {"page": page})
                seen.update(m.message_id for m in normalize_threads(
                    r.get("messages"), project_id, parent_id))
                if not _thread_has_next(r):
                    return seen
            raise MCSError("thread_incomplete", retryable=True)
        except MCSError as error:
            # Successful pages have already changed server read state.
            # Preserve their IDs without certifying the failed walk, and
            # keep the original exception's type and diagnostic fields.
            error.observed_reply_ids = frozenset(seen)
            raise

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
        if not valid_id(snapshot_ts):
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
