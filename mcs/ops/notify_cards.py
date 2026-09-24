"""Interactive notification cards — delivery ledger and neutral render specs.

This module owns the MCS side of the Discord interactive notification
pipeline (plan: .omo/plans/discord-interactive-cards.md):

- notify_outbox intents routed 'interactive' are FROZEN into sealed
  batches, fanned out to durable cards (1 intent -> N cards), and
  rendered as immutable, neutral specs published atomically to
  data/discord_render/<delivery_id>.json.
- A plugin claims a spec file, asks permission with transport_begin,
  receives a durable grant/denial, sends to Discord, then reports via
  transport_receipt. Discord HTTP and the DB commit are never one
  transaction: attempts hold exclusive per-card send ownership until
  their factual result is settled (delivered/not_sent/unknown).
- 'unknown' is a first-class state: time alone never moves it back to
  undelivered; only a real receipt or an operator ops.card_resolve does.

The SQLite ledger stays the source of truth; this process (the runner)
is the only writer. The plugin reads published snapshots/spec files and
writes the protected cmd_int inbox.

Config (config.json):
  "notify": {
    "interactive": "discord" | "off",       # kill switch
    "route_epoch": 1,                        # bump on delivery change
    "operator": "<discord user id>",
    "card_thread": true,
    "card_thread_archive_min": 10080,
    "discord": {"profile": "...", "application_id": "...",
                "guild_id": "...", "channel_id": "..."}
  }
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
import uuid

from mcs_requests import canonical, payload_hash, positive, valid_hash, \
    valid_uuid

RENDER_SCHEMA = "mcs-card-render/v1"

# outbox kinds that become interactive cards when notify.interactive is
# on; everything else (ops alerts, semantic notices) stays legacy text.
INTERACTIVE_KINDS = frozenset({"new_messages", "signal"})

CARD_KINDS = ("thread", "signal", "digest")
CARD_STATES = ("pending", "delivered", "update_failed",
               "delivery_unknown", "message_deleted", "revoked")
RENDER_STATES = ("queued", "sending", "delivered", "not_sent",
                 "unknown", "held", "cancelled")
ATTEMPT_STATES = ("granted", "delivered", "not_sent", "unknown")
# an attempt that still owns the card's send exclusivity
UNSETTLED_ATTEMPT = ("granted", "unknown")
# renders whose spec file must be available to a claiming worker
LIVE_RENDER = ("queued", "sending", "unknown", "held")

PAGE_DIGEST = 5           # digest candidates per page
PAGE_THREAD = 8           # messages per page on a thread card
RESEAT_S = 3600           # re-examine a dispatched pending intent hourly
TOKEN_VIEW_S = 30 * 86400
TOKEN_WRITE_S = 7 * 86400
DEFER_S = 86400           # fixed 'hold' duration for v1
MAX_SNIPPET = 160
MAX_RESEND = 3            # consecutive not_sent attempts before a card
                          # suspends auto-retry (update_failed)
# receipt error_codes proving the bound Discord message no longer
# exists — an update/revoke against it must never retry as-is
MESSAGE_GONE = frozenset(
    {"http_404", "http_410", "unknown_message", "message_deleted",
     "gone"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_cards(
  card_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK(kind IN ('thread','signal','digest')),
  project_id INTEGER,
  root_message_id INTEGER,
  anchor_key TEXT NOT NULL,
  profile TEXT, application_id TEXT, guild_id TEXT, channel_id TEXT,
  message_id TEXT, thread_id TEXT,
  thread_state TEXT NOT NULL DEFAULT 'none'
    CHECK(thread_state IN ('none','created','failed','deleted')),
  source_generation INTEGER NOT NULL DEFAULT 1,
  source_fp TEXT,
  presentation_generation INTEGER NOT NULL DEFAULT 1,
  content_fp TEXT,
  ui_revision INTEGER NOT NULL DEFAULT 1,
  desired_render_rev INTEGER NOT NULL DEFAULT 0,
  applied_render_rev INTEGER NOT NULL DEFAULT 0,
  ui_state TEXT,
  delivery_state TEXT NOT NULL DEFAULT 'pending'
    CHECK(delivery_state IN ('pending','delivered','update_failed',
                             'delivery_unknown','message_deleted','revoked')),
  last_delivery_error TEXT,
  revoked_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ncards_state
  ON notification_cards(delivery_state);
CREATE TABLE IF NOT EXISTS notification_intent_batches(
  event_id INTEGER PRIMARY KEY REFERENCES notify_outbox(event_id),
  frozen_payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  route_epoch INTEGER NOT NULL,
  sealed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notification_intent_cards(
  event_id INTEGER NOT NULL REFERENCES notify_outbox(event_id),
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  coverage TEXT NOT NULL,
  required_render_rev INTEGER NOT NULL DEFAULT 0,
  delivery_id TEXT REFERENCES notification_renders(delivery_id),
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','delivered','suppressed')),
  PRIMARY KEY(event_id, card_id));
CREATE INDEX IF NOT EXISTS idx_nic_card
  ON notification_intent_cards(card_id, state);
CREATE TABLE IF NOT EXISTS notification_renders(
  delivery_id TEXT PRIMARY KEY,
  card_id INTEGER REFERENCES notification_cards(card_id),
  op TEXT NOT NULL CHECK(op IN ('create','update','revoke','notice')),
  render_rev INTEGER NOT NULL,
  manifest_id INTEGER REFERENCES notification_view_manifests(manifest_id),
  route_epoch INTEGER NOT NULL,
  profile TEXT, application_id TEXT, guild_id TEXT, channel_id TEXT,
  spec_json TEXT,
  spec_published INTEGER NOT NULL DEFAULT 0,
  first_published_at REAL,
  payload_hash TEXT NOT NULL,
  correlation TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL DEFAULT 'queued'
    CHECK(state IN ('queued','sending','delivered','not_sent',
                    'unknown','held','cancelled')),
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE(card_id, render_rev));
CREATE INDEX IF NOT EXISTS idx_nrenders_card
  ON notification_renders(card_id);
CREATE TABLE IF NOT EXISTS notification_delivery_attempts(
  attempt_id TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL REFERENCES notification_renders(delivery_id),
  begin_command_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL
    CHECK(state IN ('granted','delivered','not_sent','unknown')),
  message_id TEXT, error_code TEXT,
  worker_id TEXT,
  created_at REAL NOT NULL, finished_at REAL);
CREATE INDEX IF NOT EXISTS idx_nattempts_delivery
  ON notification_delivery_attempts(delivery_id);
CREATE TABLE IF NOT EXISTS notification_view_manifests(
  manifest_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  render_rev INTEGER NOT NULL,
  source_generation INTEGER NOT NULL,
  presentation_generation INTEGER NOT NULL,
  digest INTEGER NOT NULL DEFAULT 0,
  shown TEXT NOT NULL,
  invalidated INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_nmanifests_card
  ON notification_view_manifests(card_id);
CREATE TABLE IF NOT EXISTS notification_acknowledgements(
  ack_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  manifest_id INTEGER NOT NULL REFERENCES notification_view_manifests(manifest_id),
  actor TEXT NOT NULL,
  command_id TEXT NOT NULL UNIQUE,
  receipt_ref TEXT,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notification_triage(
  card_id INTEGER PRIMARY KEY REFERENCES notification_cards(card_id),
  owner TEXT, defer_until REAL,
  state TEXT NOT NULL DEFAULT 'open'
    CHECK(state IN ('open','assigned','deferred')),
  revision INTEGER NOT NULL DEFAULT 0,
  last_actor TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS notification_action_tokens(
  token TEXT PRIMARY KEY,
  card_id INTEGER NOT NULL REFERENCES notification_cards(card_id),
  action TEXT NOT NULL,
  params TEXT,
  need_source_gen INTEGER, need_manifest_id INTEGER,
  need_ui_rev INTEGER, need_request_rev INTEGER,
  expires_at REAL NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ntokens_card
  ON notification_action_tokens(card_id);
CREATE TABLE IF NOT EXISTS notification_meta(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  notify_dirty INTEGER NOT NULL DEFAULT 0);
"""

# card action vocabulary -> (label, discord style, class)
_ACTIONS = {
    "ack":     ("✅ 確認", "success", "write"),
    "assign":  ("👤 担当", "primary", "write"),
    "defer":   ("⏸ 保留", "secondary", "write"),
    "body":    ("📄 本文表示", "secondary", "view"),
    "request": ("📝 依頼作成", "secondary", "write"),
    "dismiss": ("🚫 却下", "danger", "write"),
    "prev":    ("◀ 前へ", "secondary", "view"),
    "next":    ("次へ ▶", "secondary", "view"),
}
_WRITE_ACTIONS = frozenset(
    a for a, (_, _, cls) in _ACTIONS.items() if cls == "write")


# ---------- config / dirs ----------

def _db(ledger):
    return getattr(ledger, "db", ledger)


def data_root(ledger) -> str:
    """The ledger's own data dir (…/.mcs/data) — derived, never assumed."""
    main = _db(ledger).execute(
        "PRAGMA database_list").fetchone()["file"]
    return os.path.dirname(os.path.abspath(main))


def notify_dirs(root: str) -> dict:
    return {name: os.path.join(root, name) for name in
            ("discord_render", "discord_state", "flags",
             "cmd_int", "cmd_results")}


def ensure_dirs(root: str) -> None:
    for path in notify_dirs(root).values():
        os.makedirs(path, mode=0o700, exist_ok=True)


def notify_cfg(cfg: dict) -> dict:
    n = (cfg or {}).get("notify")
    return n if isinstance(n, dict) else {}


def interactive_enabled(cfg: dict) -> bool:
    return notify_cfg(cfg).get("interactive") == "discord"


def route_epoch(cfg: dict) -> int:
    v = notify_cfg(cfg).get("route_epoch", 1)
    return v if type(v) is int and v > 0 else 1


def delivery_scope(cfg: dict) -> dict | None:
    """The Discord destination the runner addresses — all four fields
    required; a partial scope is a config error, never a fallback."""
    d = notify_cfg(cfg).get("discord")
    if not isinstance(d, dict):
        return None
    out = {}
    for k in ("profile", "application_id", "guild_id", "channel_id"):
        v = d.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
        out[k] = v.strip()
    return out


def mark_snapshot_dirty(db) -> None:
    """The published snapshot is now behind for notification tables;
    persists across a crash so the next run republishes (RC21). Owns
    its own table — snapshot_meta's 3-column insert contract is shared
    with publish_snapshot and must not change."""
    db.execute(
        "INSERT INTO notification_meta(singleton,notify_dirty) "
        "VALUES(1,1) ON CONFLICT(singleton) DO UPDATE SET notify_dirty=1")


def snapshot_dirty(ledger) -> bool:
    row = _db(ledger).execute(
        "SELECT notify_dirty FROM notification_meta WHERE singleton=1"
    ).fetchone()
    return bool(row and row["notify_dirty"])


def clear_snapshot_dirty(ledger) -> None:
    _db(ledger).execute(
        "UPDATE notification_meta SET notify_dirty=0 WHERE singleton=1")
    _db(ledger).commit()


def publish_file(directory: str, name: str, raw: bytes) -> str:
    """mkstemp -> fsync -> os.replace -> dir fsync. Never a partial file."""
    fd, tmp = tempfile.mkstemp(prefix=".pub-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, os.path.join(directory, name))
        dirfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return os.path.join(directory, name)


def publish_flags(cfg: dict, root: str) -> bool:
    """flags/notify.json — the plugin's DB-free view of the effective
    interactive/kill-switch state. Published only when it changes."""
    n = notify_cfg(cfg)
    flags = {
        "interactive": interactive_enabled(cfg),
        "kill_switch": not interactive_enabled(cfg),
        "route_epoch": route_epoch(cfg),
        "card_thread": n.get("card_thread") is True,
        "at": time.time(),
    }
    raw = canonical(flags)
    path = os.path.join(notify_dirs(root)["flags"], "notify.json")
    try:
        with open(path, "rb") as f:
            old = f.read()
    except OSError:
        old = None
    if old is not None:
        try:
            prior = json.loads(old)
            prior.pop("at", None)
            cur = dict(flags)
            cur.pop("at", None)
            if prior == cur:
                return False
        except (json.JSONDecodeError, TypeError):
            pass
    publish_file(os.path.dirname(path), "notify.json", raw)
    return True


# ---------- fingerprints ----------

def _fp(value) -> str:
    return payload_hash(value)


def _latest_signals(db, keys: list) -> dict:
    """key -> {'artifact_id','content'} of the newest signal_v1 row."""
    out = {}
    for k in keys:
        row = db.execute(
            """SELECT artifact_id, content FROM artifacts
               WHERE kind='signal_v1' AND json_valid(meta)
                 AND json_valid(content)
                 AND json_extract(meta,'$.key')=?
               ORDER BY artifact_id DESC LIMIT 1""", (k,)).fetchone()
        if row is None:
            continue
        try:
            content = json.loads(row["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(content, dict):
            out[k] = {"artifact_id": row["artifact_id"],
                      "content": content}
    return out


def _patient_name(db, pid) -> str:
    if not positive(pid):
        return ""
    r = db.execute("SELECT patient_name, is_archived FROM patients "
                   "WHERE project_id=?", (pid,)).fetchone()
    return (r["patient_name"] or "").strip() if r else ""


def _mmdd(posted_at) -> str:
    # posted_at is ISO local text; keep its MM-DD without re-parsing zones
    if isinstance(posted_at, str) and len(posted_at) >= 10:
        return posted_at[5:10]
    return "??-??"


def _hhmm(posted_at) -> str:
    if isinstance(posted_at, str) and len(posted_at) >= 16:
        return posted_at[11:16]
    return "??:??"


def _snippet(text, n=MAX_SNIPPET) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _source_fp(db, card) -> str:
    """Fingerprint of the source material a card renders — a change here
    bumps source_generation."""
    kind = card["kind"]
    if kind == "thread":
        msgs = db.execute(
            "SELECT message_id,content_hash,body_state FROM messages "
            "WHERE (message_id=? OR parent_id=?) ORDER BY posted_at_ts",
            (card["root_message_id"], card["root_message_id"])).fetchall()
        return _fp({"k": "t", "msgs": [
            (m["message_id"], m["content_hash"], m["body_state"])
            for m in msgs],
            "name": _patient_name(db, card["project_id"])})
    keys = _anchor_keys(card)
    sigs = _latest_signals(db, keys)
    names = sorted({_patient_name(db, s["content"].get("project_id"))
                    for s in sigs.values()})
    return _fp({"k": kind, "m": [
        (k, sigs[k]["artifact_id"], sigs[k]["content"].get("state"))
        for k in keys if k in sigs], "names": names})


def _anchor_keys(card) -> list:
    try:
        anchor = json.loads(card["anchor_key"] or "{}")
    except (json.JSONDecodeError, TypeError):
        anchor = {}
    if card["kind"] == "thread":
        return [card["root_message_id"]]
    keys = anchor.get("signal_keys")
    return [k for k in keys or [] if type(k) is str]


def _page(ui_state, pages: int, default: int = 0) -> int:
    try:
        p = (json.loads(ui_state or "{}") or {}).get("page", default)
    except (json.JSONDecodeError, TypeError):
        p = default
    if type(p) is not int:
        p = default
    return max(0, min(p, max(0, pages - 1)))


# ---------- card content ----------

def _signal_display(db, sig: dict) -> list:
    """Neutral display blocks for one signal row — shared by the signal
    card and the digest's per-candidate rendering."""
    import mcs_signals
    pid = sig.get("project_id")
    blocks = [{"type": "text",
               "text": mcs_signals.signal_notice_text(sig)}]
    name = _patient_name(db, pid)
    if name:
        blocks.append({"type": "field", "name": "患者", "value": name})
    ev = sig.get("evidence") or {}
    mids = ev.get("message_ids") or []
    mid = (mids[-1] if mids and type(mids[-1]) is int else None) \
        or ev.get("discharge_message_id") or ev.get("message_id")
    if type(mid) is int:
        m = db.execute(
            "SELECT sender_name,posted_at,body_text,body_state "
            "FROM messages WHERE message_id=?", (mid,)).fetchone()
        if m and m["body_state"] != "deleted" and m["body_text"]:
            blocks.append({"type": "quote", "text":
                           f"最新言及 {m['posted_at'] or '?'} "
                           f"{m['sender_name'] or '?'}: "
                           f"{_snippet(m['body_text'], 120)}"})
    state = sig.get("state")
    if state and state != "open":
        blocks.append({"type": "field", "name": "状態",
                       "value": state})
    return blocks


BODY_MAX_CHARS = 6000


def _signal_body(db, sig: dict) -> str:
    """Full-text view of one signal for the 'body' action — the same
    fields _signal_display shows on the card, but the evidence quote is
    the untruncated message body."""
    import mcs_signals
    lines = [mcs_signals.signal_notice_text(sig)]
    name = _patient_name(db, sig.get("project_id"))
    if name:
        lines.append(f"患者: {name}")
    ev = sig.get("evidence") or {}
    mids = ev.get("message_ids") or []
    mid = (mids[-1] if mids and type(mids[-1]) is int else None) \
        or ev.get("discharge_message_id") or ev.get("message_id")
    if type(mid) is int:
        m = db.execute(
            "SELECT sender_name,posted_at,body_text,body_state "
            "FROM messages WHERE message_id=?", (mid,)).fetchone()
        if m and m["body_text"]:
            body = ("（削除済み）" if m["body_state"] == "deleted"
                    else m["body_text"])
            lines.append(f"最新言及 {m['posted_at'] or '?'} "
                         f"{m['sender_name'] or '?'}: {body}")
    state = sig.get("state")
    if state and state != "open":
        lines.append(f"状態: {state}")
    return "\n".join(lines)


def _card_body_text(db, card, man) -> tuple:
    """Full text of the shown set frozen into the click's manifest —
    'body' answers what the button rendered, never the card's *current*
    page, so a concurrent nav cannot swap the view under the click."""
    try:
        shown = json.loads(man["shown"] or "[]")
    except (json.JSONDecodeError, TypeError):
        shown = []
    if card["kind"] == "thread":
        lines = []
        for mid in shown:
            if type(mid) is not int:
                continue
            m = db.execute(
                "SELECT sender_name,posted_at,body_text,body_state "
                "FROM messages WHERE message_id=?", (mid,)).fetchone()
            if m is None:
                continue
            body = ("（削除済み）" if m["body_state"] == "deleted"
                    else (m["body_text"] or ""))
            lines.append(f"{_hhmm(m['posted_at'])} "
                         f"{m['sender_name'] or '?'}: {body}")
        name = _patient_name(db, card["project_id"]) \
            or "project " + str(card["project_id"])
        title = f"💬 {name} — 本文"
        text = "\n\n".join(lines)
    else:
        sigs = _latest_signals(db, shown)
        text = "\n\n— — —\n\n".join(
            _signal_body(db, sigs[k]["content"]) for k in shown
            if k in sigs)
        title = ("レビュー候補 — 本文" if card["kind"] == "digest"
                 else "シグナル — 本文")
    if len(text) > BODY_MAX_CHARS:
        text = text[:BODY_MAX_CHARS - 1] + "…\n（省略 — 原本を参照）"
    return title, text or "（表示できる本文がありません）"


def _card_content(db, card) -> dict:
    """The deterministic display model for a card at its current page:
    containers + footer + shown-set + pages. Tokens/buttons are added
    per-render and are NOT part of the content fingerprint."""
    kind = card["kind"]
    ui = card["ui_state"]
    if kind == "thread":
        msgs = [dict(m) for m in db.execute(
            """SELECT message_id,sender_name,posted_at,body_text,body_state,
                      reply_count FROM messages
               WHERE (message_id=? OR parent_id=?) AND project_id=?
               ORDER BY posted_at_ts""",
            (card["root_message_id"], card["root_message_id"],
             card["project_id"]))]
        pages = max(1, (len(msgs) + PAGE_THREAD - 1) // PAGE_THREAD)
        page = _page(ui, pages, default=pages - 1)
        name = _patient_name(db, card["project_id"])
        first = msgs[0] if msgs else {}
        containers = [{"type": "heading", "text":
                       f"💬 {name or 'project ' + str(card['project_id'])}"
                       f" — {_mmdd(first.get('posted_at'))}"}]
        shown = []
        for m in msgs[page * PAGE_THREAD:(page + 1) * PAGE_THREAD]:
            shown.append(m["message_id"])
            body = ("（削除済み）" if m["body_state"] == "deleted"
                    else _snippet(m["body_text"]))
            containers.append({"type": "text", "text":
                               f"{_hhmm(m['posted_at'])} "
                               f"{m['sender_name'] or '?'}: {body}"})
        shown_kind = "message_ids"
    else:
        keys = _anchor_keys(card)
        sigs = _latest_signals(db, keys)
        ordered = [k for k in keys if k in sigs]
        if kind == "digest":
            pages = max(1, (len(ordered) + PAGE_DIGEST - 1) // PAGE_DIGEST)
            page = _page(ui, pages)
            name = ""
            containers = [{"type": "heading",
                           "text": f"💬 レビュー候補（{len(ordered)}件）"}]
            slice_keys = ordered[page * PAGE_DIGEST:
                                 (page + 1) * PAGE_DIGEST]
        else:
            pages, page, slice_keys = 1, 0, ordered
            containers = [{"type": "heading", "text": "レビュー候補"}]
        for k in slice_keys:
            containers.extend(_signal_display(db, sigs[k]["content"]))
        shown = slice_keys
        shown_kind = "signal_keys"
    footer = _footer(db, card)
    return {"containers": containers, "footer": footer,
            "shown": shown, "shown_kind": shown_kind,
            "page": page, "pages": pages,
            "source_fp": _source_fp(db, card)}


def _footer(db, card) -> list:
    out = []
    tri = db.execute(
        "SELECT owner,state,defer_until,last_actor FROM notification_triage"
        " WHERE card_id=?", (card["card_id"],)).fetchone()
    if tri and tri["state"] == "assigned" and tri["owner"]:
        out.append({"type": "text", "text": f"👤 担当: {tri['owner']}"})
    elif tri and tri["state"] == "deferred" and tri["defer_until"]:
        until = time.strftime("%m-%d %H:%M",
                              time.localtime(tri["defer_until"]))
        out.append({"type": "text", "text": f"⏸ 保留中（〜{until}）"})
    acks = db.execute(
        """SELECT DISTINCT a.actor FROM notification_acknowledgements a
           WHERE a.card_id=? ORDER BY a.ack_id LIMIT 8""",
        (card["card_id"],)).fetchall()
    if acks:
        out.append({"type": "text",
                    "text": "✅ 確認: " + "・".join(a["actor"]
                                                  for a in acks)})
    if card["delivery_state"] == "revoked":
        out.append({"type": "text", "text": "（取り下げ済み）"})
    return out


def _content_fp(content: dict) -> str:
    return _fp({"c": content["containers"], "f": content["footer"],
                "s": content["shown"], "p": content["page"]})


# ---------- cards / renders ----------

def _unsettled_attempt(db, card_id: int):
    return db.execute(
        """SELECT a.attempt_id, a.state FROM notification_delivery_attempts a
           JOIN notification_renders r ON r.delivery_id=a.delivery_id
           WHERE r.card_id=? AND a.state IN ('granted','unknown')
           ORDER BY a.created_at DESC LIMIT 1""", (card_id,)).fetchone()


def _find_signal_card(db, pid, keys):
    """A member key of an existing signal card resolves to that card —
    a fresh card_key must never fork a second card for the same unit."""
    keyset = set(keys)
    for r in db.execute(
            "SELECT card_id,anchor_key FROM notification_cards "
            "WHERE kind='signal' AND project_id=? AND delivery_state!='revoked'",
            (pid,)).fetchall():
        try:
            anchor = json.loads(r["anchor_key"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if keyset & set(anchor.get("signal_keys") or []):
            return r["card_id"]
    return None


def _card_for(db, target, scope, now) -> int:
    if target["kind"] == "signal":
        found = _find_signal_card(db, target["project_id"],
                                  target["anchor"]["signal_keys"])
        if found is not None:
            # widen the anchor if this intent adds member keys
            row = db.execute("SELECT anchor_key FROM notification_cards "
                             "WHERE card_id=?", (found,)).fetchone()
            try:
                anchor = json.loads(row["anchor_key"] or "{}")
            except (json.JSONDecodeError, TypeError):
                anchor = {}
            merged = list(dict.fromkeys(
                (anchor.get("signal_keys") or [])
                + target["anchor"]["signal_keys"]))
            if merged != anchor.get("signal_keys"):
                db.execute("UPDATE notification_cards SET anchor_key=?,"
                           "updated_at=? WHERE card_id=?",
                           (json.dumps({"signal_keys": merged},
                                       ensure_ascii=False), now, found))
            return found
    row = db.execute("SELECT card_id FROM notification_cards "
                     "WHERE card_key=?", (target["card_key"],)).fetchone()
    if row is not None:
        return row["card_id"]
    cur = db.execute(
        """INSERT INTO notification_cards(
             card_key,kind,project_id,root_message_id,anchor_key,
             profile,application_id,guild_id,channel_id,
             ui_state,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (target["card_key"], target["kind"], target.get("project_id"),
         target.get("root_message_id"),
         json.dumps(target["anchor"], ensure_ascii=False,
                    sort_keys=True),
         scope.get("profile"), scope.get("application_id"),
         scope.get("guild_id"), scope.get("channel_id"),
         "{}", now, now))
    return cur.lastrowid


def _card_row(db, card_id: int):
    r = db.execute("SELECT * FROM notification_cards WHERE card_id=?",
                   (card_id,)).fetchone()
    return dict(r) if r is not None else None


def _mint_token(db, card_id, action, params, need, now) -> str:
    token = secrets.token_hex(16)
    ttl = TOKEN_VIEW_S if _ACTIONS[action][2] == "view" else TOKEN_WRITE_S
    db.execute(
        """INSERT INTO notification_action_tokens(
             token,card_id,action,params,need_source_gen,
             need_manifest_id,need_ui_rev,need_request_rev,
             expires_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (token, card_id, action,
         json.dumps(params, ensure_ascii=False, sort_keys=True)
         if params else None,
         need.get("source_gen"), need.get("manifest_id"),
         need.get("ui_rev"), need.get("request_rev"),
         now + ttl, now))
    return token


def _action_rows(db, card, content, now, context=None):
    """Button rows for a render; every button carries a fresh token.
    Buttons whose modal flow cannot pin a source (no context) are not
    emitted — a button that can never succeed is worse than none."""
    kind = card["kind"]
    keys = _anchor_keys(card)
    context = context or {}
    rows, row = [], []
    need = {"source_gen": card["source_generation"],
            "manifest_id": content["manifest_id"],
            "ui_rev": card["ui_revision"]}
    ack_label = ("✅ このページを確認" if kind == "digest"
                 else _ACTIONS["ack"][0])

    def btn(action, params=None, label=None, style=None):
        tok = _mint_token(db, card["card_id"], action, params, need, now)
        b = {"id": action, "ui": "button",
             "style": style or _ACTIONS[action][1],
             "label": label or _ACTIONS[action][0], "token": tok}
        row.append(b)

    btn("ack", {"shown_kind": content["shown_kind"]}, label=ack_label)
    btn("assign")
    btn("defer")
    btn("body")
    rows.append(list(row))
    row.clear()
    if positive(card["project_id"]) \
            and positive(context.get("source_message_id")) \
            and context.get("source_hash"):
        # a digest spans projects — a card-level request cannot pin a
        # single source, so the button is only emitted where it can work
        btn("request", {"project_id": card["project_id"]})
    if kind == "signal" and len(keys) == 1 \
            and keys[0] in (context.get("signals") or {}):
        btn("dismiss", {"signal_key": keys[0]})
    if row:
        rows.append(list(row))
        row.clear()
    if content["pages"] > 1:
        # only mint buttons that can actually move — a dead nav button
        # always comes back bad_page
        if content["page"] > 0:
            btn("prev", {"page": content["page"] - 1})
        if content["page"] + 1 < content["pages"]:
            btn("next", {"page": content["page"] + 1})
        if row:
            rows.append(list(row))
    return rows


def _issue_render(db, card_id, cfg, now, specs, force=False):
    """Issue the next render for a card when the display model demands
    one. Precondition per §4: no unsettled attempt may own the card —
    a granted/unknown attempt keeps exclusive send rights until its
    factual result lands, so issuance here never races an HTTP call."""
    card = _card_row(db, card_id)
    if card is None:
        return None
    if _unsettled_attempt(db, card_id):
        return None
    content = _card_content(db, card)
    pres_fp = _content_fp(content)
    gens = {}
    if card["source_fp"] is None:
        gens["source_fp"] = content["source_fp"]   # baseline, not a bump
    elif content["source_fp"] != card["source_fp"]:
        gens["source_generation"] = card["source_generation"] + 1
        gens["source_fp"] = content["source_fp"]
    if card["content_fp"] is None:
        gens["content_fp"] = pres_fp
    elif pres_fp != card["content_fp"]:
        gens["presentation_generation"] = \
            card["presentation_generation"] + 1
        gens["content_fp"] = pres_fp
    latest = db.execute(
        "SELECT * FROM notification_renders WHERE card_id=? "
        "ORDER BY render_rev DESC LIMIT 1", (card_id,)).fetchone()
    live = latest is not None and latest["state"] in LIVE_RENDER
    if live and latest["state"] in ("queued", "held") and gens:
        # a not-yet-sent render whose content is already stale is
        # cancelled — a sending/unknown render keeps its attempt's
        # exclusivity instead (guarded above)
        db.execute("UPDATE notification_renders SET state='cancelled',"
                   "updated_at=? WHERE delivery_id=?",
                   (now, latest["delivery_id"]))
        db.execute("UPDATE notification_intent_cards SET delivery_id=NULL,"
                   "required_render_rev=0 WHERE delivery_id=?",
                   (latest["delivery_id"],))
        live = False
    if live:
        return None                        # in-flight render is current
    # update_failed suspends the not_sent/cancelled auto-retry — but a
    # route_epoch bump (config changed) or fresh drift re-opens it
    suspended = (card["delivery_state"] == "update_failed"
                 and latest is not None
                 and latest["route_epoch"] == route_epoch(cfg))
    needed = force or bool(gens) or latest is None \
        or (latest["state"] in ("not_sent", "cancelled") and not suspended)
    if card["delivery_state"] == "revoked" and card["message_id"]:
        # a delivered card that is revoked owes Discord a delete —
        # without this, archive-revoke left the message up forever.
        # An already-delivered revoke render must not re-issue.
        needed = not (latest is not None
                      and latest["op"] == "revoke"
                      and latest["state"] == "delivered")
    if not needed:
        return None                        # delivered/terminal & no drift

    if card["delivery_state"] == "revoked":
        if card["message_id"] is None:
            return None      # never delivered — nothing exists on
                             # Discord to delete
        op = "revoke"
    elif card["message_id"] is None:
        op = "create"        # never bound, or unbound after a proven
    else:                    # delete — re-post instead of patching a
        op = "update"        # ghost
    rev = (latest["render_rev"] + 1) if latest is not None else 1
    # cancel any leftover open renders of this card (defensive; the
    # guards above mean at most stale queued/held rows can exist)
    for r in db.execute(
            "SELECT delivery_id FROM notification_renders WHERE card_id=?"
            " AND state IN ('queued','held')", (card_id,)).fetchall():
        db.execute("UPDATE notification_renders SET state='cancelled',"
                   "updated_at=? WHERE delivery_id=?",
                   (now, r["delivery_id"]))
        db.execute("UPDATE notification_intent_cards SET delivery_id=NULL,"
                   "required_render_rev=0 WHERE delivery_id=?",
                   (r["delivery_id"],))

    card.update(gens)
    cur = db.execute(
        """INSERT INTO notification_view_manifests(
             card_id,render_rev,source_generation,presentation_generation,
             digest,shown,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (card_id, rev, card["source_generation"],
         card["presentation_generation"], 1 if card["kind"] == "digest" else 0,
         json.dumps(content["shown"], ensure_ascii=False), now))
    content["manifest_id"] = cur.lastrowid
    scope = ({k: card[k] for k in
              ("profile", "application_id", "guild_id", "channel_id")}
             if card["message_id"] and card["channel_id"]
             else (delivery_scope(cfg) or {}))
    event_ids = sorted(
        r["event_id"] for r in db.execute(
            "SELECT event_id FROM notification_intent_cards "
            "WHERE card_id=? AND state='pending'", (card_id,)))
    correlation = secrets.token_hex(16)
    spec = {
        "schema": RENDER_SCHEMA,
        "delivery_id": str(uuid.uuid4()),
        "card_key": card["card_key"], "kind": card["kind"], "op": op,
        "render_rev": rev,
        "source_generation": card["source_generation"],
        "presentation_generation": card["presentation_generation"],
        "ui_revision": card["ui_revision"],
        "delivery": {**scope, "intent_event_ids": event_ids,
                     "route_epoch": route_epoch(cfg),
                     "correlation": correlation},
    }
    # update/revoke target the bound Discord message — self-contained so
    # a worker never needs a registry/snapshot lookup to aim
    for k in ("message_id", "thread_id"):
        if card[k]:
            spec["delivery"][k] = card[k]
    context = _render_context(db, card)
    spec["parts"] = {
        "containers": content["containers"],
        "action_rows": _action_rows(db, card, content, now, context),
        "footer": content["footer"]
                  + [{"type": "meta", "correlation": correlation}],
        "manifest_id": content["manifest_id"],
        "page": content["page"], "pages": content["pages"],
        "context": context,
    }
    if notify_cfg(cfg).get("card_thread") is True and card["kind"] != "digest":
        spec["parts"]["thread_name"] = _thread_name(db, card)
    elif notify_cfg(cfg).get("card_thread") is True:
        spec["parts"]["thread_name"] = _digest_thread_name(content)
    db.execute(
        """INSERT INTO notification_renders(
             delivery_id,card_id,op,render_rev,manifest_id,route_epoch,
             profile,application_id,guild_id,channel_id,
             spec_json,payload_hash,correlation,state,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
        (spec["delivery_id"], card_id, op, rev, content["manifest_id"],
         spec["delivery"]["route_epoch"], scope.get("profile"),
         scope.get("application_id"), scope.get("guild_id"),
         scope.get("channel_id"), canonical(spec).decode(),
         payload_hash(spec), correlation, now, now))
    db.execute(
        """UPDATE notification_intent_cards
           SET delivery_id=?, required_render_rev=?
           WHERE card_id=? AND state='pending'""",
        (spec["delivery_id"], rev, card_id))
    db.execute(
        """UPDATE notification_cards SET source_generation=?,
             source_fp=?, presentation_generation=?, content_fp=?,
             desired_render_rev=?, updated_at=? WHERE card_id=?""",
        (card["source_generation"], card["source_fp"],
         card["presentation_generation"], card["content_fp"],
         rev, now, card_id))
    specs.append(spec)
    return spec["delivery_id"]


def _thread_name(db, card) -> str:
    name = _patient_name(db, card["project_id"]) \
        or f"project {card['project_id']}"
    d = "??-??"
    if card["root_message_id"]:
        r = db.execute("SELECT posted_at FROM messages WHERE message_id=?",
                       (card["root_message_id"],)).fetchone()
        if r:
            d = _mmdd(r["posted_at"])
    return f"💬 {name} — {d}"


def _digest_thread_name(content) -> str:
    return f"💬 レビュー候補 — {time.strftime('%m-%d')}"


def _render_context(db, card) -> dict:
    """Modal-flow context pinned at render time — the plugin builds
    request.create/ops.signal_dismiss from THIS, so a confirmed command
    always refers to what the card actually showed (snapshot lag safe).

    - ``source_message_id``/``source_hash``: thread cards pin the root
      message; signal cards pin the representative signal's displayed
      evidence message (the same 最新言及 the user sees).
    - ``signals``: rendered signal_key -> artifact_id — dismiss's
      ``expected_signal_artifact_id`` must equal what was rendered.
    Digests span projects, so no card-level source pin is emitted."""
    ctx: dict = {}
    if card["kind"] == "thread":
        mid = card["root_message_id"]
        if positive(mid):
            ctx["project_id"] = card["project_id"]
            ctx["source_message_id"] = mid
            r = db.execute(
                "SELECT content_hash,body_state FROM messages "
                "WHERE message_id=?", (mid,)).fetchone()
            if r and r["body_state"] == "full" \
                    and valid_hash(r["content_hash"]):
                ctx["source_hash"] = r["content_hash"]
        return ctx
    keys = _anchor_keys(card)
    sigs = _latest_signals(db, keys)
    if not sigs:
        return ctx
    ctx["signals"] = {k: {"artifact_id": s["artifact_id"],
                          "project_id": s["content"].get("project_id")}
                      for k, s in sigs.items()}
    if not positive(card["project_id"]):
        return ctx                       # digest — no card-level pin
    rep = sigs.get(keys[0]) or next(iter(sigs.values()))
    ev = rep["content"].get("evidence") or {}
    mids = ev.get("message_ids") or []
    mid = (mids[-1] if mids and type(mids[-1]) is int else None) \
        or ev.get("discharge_message_id") or ev.get("message_id")
    if type(mid) is int:
        ctx["project_id"] = card["project_id"]
        ctx["source_message_id"] = mid
        r = db.execute(
            "SELECT content_hash,body_state FROM messages "
            "WHERE message_id=?", (mid,)).fetchone()
        if r and r["body_state"] == "full" \
                and valid_hash(r["content_hash"]):
            ctx["source_hash"] = r["content_hash"]
    return ctx


def _publish_specs(db, dirs, specs, now) -> list:
    """Atomic file publication AFTER the render commit. A crash between
    leaves spec_published=0; recovery republishes identical bytes."""
    published = []
    for spec in specs:
        path = publish_file(dirs["discord_render"],
                            spec["delivery_id"] + ".json",
                            canonical(spec))
        db.execute(
            "UPDATE notification_renders SET spec_published=1,"
            "first_published_at=COALESCE(first_published_at,?),"
            "updated_at=? WHERE delivery_id=?",
            (now, now, spec["delivery_id"]))
        published.append(path)
    if published:
        db.commit()
    return published


def _resend_exhausted(db, card_id: int) -> bool:
    """MAX_RESEND lifetime real not_sents on a card suspends auto-retry —
    denied begins (error_code 'denied_*') are not send failures and never
    count. Drift, a route_epoch bump, or an operator resolve re-opens."""
    return db.execute(
        """SELECT COUNT(*) c FROM notification_delivery_attempts a
           JOIN notification_renders r ON r.delivery_id=a.delivery_id
           WHERE r.card_id=? AND a.state='not_sent'
             AND (a.error_code IS NULL
                  OR a.error_code NOT LIKE 'denied_%')""",
        (card_id,)).fetchone()["c"] >= MAX_RESEND


def _not_sent_state(db, card, render, error_code):
    """(delivery_state, unbind_message_id) after a factual not_sent.

    - update/revoke reporting the bound message provably gone unbinds it:
      revoke reaches its goal (stays revoked), update becomes
      message_deleted so the next render re-creates rather than PATCHing
      a ghost forever.
    - a revoked card keeps its intent until the resend budget is spent.
    - a failed create stays pending (nothing was ever posted); a failed
      update on a live card keeps 'delivered' (old content still stands).
    - MAX_RESEND real failures suspend auto-retry as update_failed."""
    cur = card["delivery_state"]
    gone = render["op"] in ("update", "revoke") \
        and isinstance(error_code, str) and error_code in MESSAGE_GONE
    if cur == "revoked":
        state = "revoked"   # goal achieved iff gone, else retry pending
        if gone:
            return state, True
        return ("update_failed" if _resend_exhausted(db, card["card_id"])
                else state), False
    if gone:
        return "message_deleted", True
    if _resend_exhausted(db, card["card_id"]):
        return "update_failed", False
    if render["op"] == "create":
        return "pending", False
    return cur, False


def _complete_intent(db, event_id, now) -> str | None:
    """An intent is accepted only once every required card is delivered
    or legitimately suppressed — never on a partial success (RC03)."""
    rows = db.execute(
        "SELECT state FROM notification_intent_cards WHERE event_id=?",
        (event_id,)).fetchall()
    if not rows:
        return None
    states = {r["state"] for r in rows}
    if states - {"delivered", "suppressed"}:
        return None
    if "delivered" in states:
        db.execute(
            "UPDATE notify_outbox SET state='accepted',next_try=NULL,"
            "accepted_ref=?,updated_at=? WHERE event_id=?",
            (f"cards:{len(rows)}", now, event_id))
        return "accepted"
    db.execute(
        "UPDATE notify_outbox SET state='suppressed',next_try=NULL,"
        "updated_at=? WHERE event_id=?", (now, event_id))
    return "suppressed"


# ---------- dispatch (intent -> cards -> renders) ----------

def _resolve_targets(db, ev, payload) -> list:
    """Frozen intent -> [(card_key, coverage, anchor)] fan-out."""
    kind = ev["kind"]
    if kind == "new_messages":
        ids = sorted({m for m in (payload.get("message_ids") or [])
                      if type(m) is int})
        roots = {}
        for mid in ids:
            roots.setdefault(_thread_root(db, mid), []).append(mid)
        out = []
        for root_mid, mids in roots.items():
            r = db.execute("SELECT project_id FROM messages "
                           "WHERE message_id=?", (root_mid,)).fetchone()
            pid = r["project_id"] if r else ev["project_id"]
            if not positive(pid):
                continue
            out.append({"card_key": f"v1|thread|{pid}|{root_mid}",
                        "kind": "thread", "project_id": pid,
                        "root_message_id": root_mid,
                        "coverage": sorted(mids),
                        "anchor": {"root": root_mid}})
        return out
    if kind == "signal":
        if payload.get("digest") is True:
            keys = [k for k in (payload.get("signal_keys") or [])
                    if type(k) is str and k]
            if not keys:
                return []
            return [{"card_key": f"v1|digest|{ev['event_id']}",
                     "kind": "digest", "project_id": None,
                     "coverage": keys,
                     "anchor": {"signal_keys": keys}}]
        keys = [k for k in (payload.get("signal_keys") or [])
                if type(k) is str and k]
        skey = payload.get("signal_key")
        if not keys and type(skey) is str and skey:
            keys = [skey]
        if not keys or not positive(payload.get("project_id")):
            return []
        pid = payload["project_id"]
        return [{"card_key": f"v1|signal|{pid}|{keys[0]}",
                 "kind": "signal", "project_id": pid,
                 "coverage": keys, "anchor": {"signal_keys": keys}}]
    return []


def _thread_root(db, mid):
    cur, seen = mid, set()
    for _ in range(32):
        if cur is None or cur in seen:
            return mid
        seen.add(cur)
        r = db.execute("SELECT parent_id FROM messages "
                       "WHERE message_id=?", (cur,)).fetchone()
        if r is None or r["parent_id"] is None:
            return cur
        cur = r["parent_id"]
    return cur or mid


def dispatch_intent(ledger, ev, cfg, now=None) -> dict:
    """Freeze an interactive intent: sealed batch -> card fan-out ->
    immutable render -> atomic spec publication. Idempotent re-entry on
    a sealed intent repairs missing spec files and re-checks completion."""
    db = _db(ledger)
    now = time.time() if now is None else now
    root = data_root(ledger)
    dirs = notify_dirs(root)
    ensure_dirs(root)
    scope = delivery_scope(cfg)
    epoch = route_epoch(cfg)
    specs = []
    outcome = {"dispatched": False, "cards": 0}
    event_id = ev["event_id"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT event_id,kind,project_id,payload,state,route "
            "FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is None or row["route"] != "interactive" \
                or row["state"] not in ("pending", "failed"):
            return {"skipped": True}
        batch = db.execute(
            "SELECT event_id FROM notification_intent_batches "
            "WHERE event_id=?", (event_id,)).fetchone()
        if batch is not None:
            # sealed already — a flush re-entry only repairs + completes
            _complete_intent(db, event_id, now)
            for r in db.execute(
                    """SELECT r.spec_json, r.delivery_id
                       FROM notification_renders r
                       JOIN notification_intent_cards ic
                         ON ic.delivery_id = r.delivery_id
                       WHERE ic.event_id=? AND r.state='queued'
                         AND (r.spec_published=0 OR r.spec_json IS NULL)""",
                    (event_id,)).fetchall():
                if r["spec_json"]:
                    specs.append(json.loads(r["spec_json"]))
            outcome["resealed"] = True
        else:
            if scope is None:
                return {"error": "delivery_scope_missing"}
            try:
                frozen = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                frozen = None
            if not isinstance(frozen, dict):
                db.execute(
                    "UPDATE notify_outbox SET state='failed',"
                    "next_try=NULL,updated_at=? WHERE event_id=?",
                    (now, event_id))
                return {"error": "payload_invalid"}
            db.execute(
                "INSERT INTO notification_intent_batches VALUES(?,?,?,?,?)",
                (event_id, canonical(frozen).decode(),
                 payload_hash(frozen), epoch, now))
            card_ids = []
            for target in _resolve_targets(db, row, frozen):
                cid = _card_for(db, target, scope, now)
                db.execute(
                    "INSERT OR IGNORE INTO notification_intent_cards("
                    "event_id,card_id,coverage) VALUES(?,?,?)",
                    (event_id, cid,
                     json.dumps(target["coverage"],
                                ensure_ascii=False)))
                card_ids.append(cid)
            if card_ids:
                for cid in card_ids:
                    _issue_render(db, cid, cfg, now, specs)
                db.execute(
                    "UPDATE notify_outbox SET next_try=?,updated_at=? "
                    "WHERE event_id=?", (now + RESEAT_S, now, event_id))
                outcome.update(dispatched=True, cards=len(card_ids))
            else:
                # nothing deliverable — suppress like a stale text send
                db.execute(
                    "UPDATE notify_outbox SET state='suppressed',"
                    "next_try=NULL,updated_at=? WHERE event_id=?",
                    (now, event_id))
                outcome["suppressed"] = True
            _complete_intent(db, event_id, now)
        mark_snapshot_dirty(db)
    _publish_specs(db, dirs, specs, now)
    return outcome


def revert_to_text(ledger, event_id, now=None) -> bool:
    """Kill-switch path: an UNSEALED interactive intent is provably
    unsent (no batch row, no card, no attempt), so converting its route
    to text can never double-deliver. A sealed intent is untouched."""
    now = time.time() if now is None else now
    db = _db(ledger)
    with db:
        cur = db.execute(
            """UPDATE notify_outbox SET route='text',updated_at=?
               WHERE event_id=? AND route='interactive'
                 AND NOT EXISTS(SELECT 1 FROM notification_intent_batches b
                                WHERE b.event_id=notify_outbox.event_id)""",
            (now, event_id))
        return cur.rowcount == 1


# ---------- settle (receipt / resolve shared finalization) ----------

def _settle_attempt(db, attempt, render, result, now,
                    message_id=None, error_code=None) -> dict:
    """One attempt's factual result commits atomically with every
    dependent: render state, card message binding, coverage rows, and
    each owning intent's completion. A cancelled render's late success
    records the fact (message binding, applied rev) without reviving the
    cancelled render or rolling back a newer one."""
    aid = attempt["attempt_id"]
    card = (_card_row(db, render["card_id"])
            if render["card_id"] is not None else None)
    db.execute(
        "UPDATE notification_delivery_attempts SET state=?,"
        "message_id=?,error_code=?,finished_at=? WHERE attempt_id=?",
        (result, message_id, error_code, now, aid))
    if render["state"] != "cancelled":
        db.execute(
            "UPDATE notification_renders SET state=?,updated_at=? "
            "WHERE delivery_id=?",
            (result, now, render["delivery_id"]))
    if result == "delivered" and card is not None:
        applied = card["applied_render_rev"]
        conflict = False
        if render["op"] == "create":
            if card["message_id"] is None:
                db.execute(
                    """UPDATE notification_cards SET message_id=?,
                         profile=?,application_id=?,guild_id=?,
                         channel_id=?,updated_at=? WHERE card_id=?""",
                    (str(message_id), render["profile"],
                     render["application_id"], render["guild_id"],
                     render["channel_id"], now, card["card_id"]))
                card["message_id"] = str(message_id)
            elif card["message_id"] != str(message_id):
                conflict = True
        elif card["message_id"] is not None \
                and str(message_id) != card["message_id"]:
            conflict = True
        if conflict:
            db.execute(
                "UPDATE notification_cards SET delivery_state="
                "'delivery_unknown',last_delivery_error=?,updated_at=? "
                "WHERE card_id=?",
                ("message_id_conflict", now, card["card_id"]))
        else:
            new_applied = max(applied, render["render_rev"])
            state = card["delivery_state"]
            if state not in ("revoked",):
                state = "delivered"
            db.execute(
                """UPDATE notification_cards SET applied_render_rev=?,
                     delivery_state=?,last_delivery_error=NULL,updated_at=?
                   WHERE card_id=?""",
                (new_applied, state, now, card["card_id"]))
        cov = "delivered"
    elif result == "not_sent":
        if card is not None:
            state, unbind_mid = _not_sent_state(db, card, render,
                                              error_code)
            db.execute(
                """UPDATE notification_cards SET delivery_state=?,
                     message_id=?, thread_id=?, thread_state=?,
                     last_delivery_error=?, updated_at=?
                   WHERE card_id=?""",
                (state,
                 None if unbind_mid else card["message_id"],
                 # the thread lived under the bound message — once the
                 # message is proven gone its thread binding is stale
                 # and must not carry into the re-created card
                 None if unbind_mid else card["thread_id"],
                 "none" if unbind_mid else card["thread_state"],
                 error_code, now, card["card_id"]))
        cov = "unbind"
    else:  # unknown — never auto-resend; waits for reconcile/resolve
        if card is not None and card["delivery_state"] not in (
                "revoked", "delivered"):
            db.execute(
                "UPDATE notification_cards SET delivery_state="
                "'delivery_unknown',last_delivery_error=?,updated_at=? "
                "WHERE card_id=?",
                (error_code or "unknown", now, card["card_id"]))
        cov = "keep"
    if cov == "delivered":
        db.execute(
            "UPDATE notification_intent_cards SET state='delivered' "
            "WHERE delivery_id=?", (render["delivery_id"],))
    elif cov == "unbind":
        db.execute(
            "UPDATE notification_intent_cards SET delivery_id=NULL,"
            "required_render_rev=0 WHERE delivery_id=? AND state='pending'",
            (render["delivery_id"],))
    for r in db.execute(
            "SELECT DISTINCT event_id FROM notification_intent_cards "
            "WHERE card_id=?", (render["card_id"],)).fetchall() \
            if render["card_id"] is not None else []:
        _complete_intent(db, r["event_id"], now)
    mark_snapshot_dirty(db)
    return {"settled": result}


# ---------- command-side apply ----------

def _origin_card(db, origin):
    """A card is identified by its bound Discord location — never by an
    actor-supplied card_key alone."""
    if not isinstance(origin, dict):
        return None
    mid, ch, app = (origin.get("message_id"), origin.get("channel_id"),
                    origin.get("application_id"))
    if not all(isinstance(v, str) and v for v in (mid, ch, app)):
        return None
    row = db.execute(
        "SELECT * FROM notification_cards WHERE message_id=? "
        "AND channel_id=? AND application_id=?",
        (mid, ch, app)).fetchone()
    return dict(row) if row else None


def _scope_match(card, origin) -> bool:
    return all(not card[k] or card[k] == origin.get(k)
               for k in ("profile", "application_id", "guild_id",
                         "channel_id"))


def apply_notification(ledger, req, cfg, now=None) -> dict:
    """op='notification': a card action (page/ack/assign/defer). The
    token binds action+params+generations; the plugin's verified actor
    and the card's bound native scope are both re-checked here."""
    db = _db(ledger)
    now = time.time() if now is None else now
    command_id = req["command_id"]
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload_hash,receipt_json FROM command_receipts "
            "WHERE command_id=?", (command_id,)).fetchone()
        if old:
            stored = json.loads(old["receipt_json"])
            if old["payload_hash"] != payload_hash(req):
                return {"outcome": "rejected", "error": "command_id_conflict"}
            return stored
        receipt = _apply_notification_tx(db, req, cfg, now, specs)
        db.execute(
            "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
            (command_id, payload_hash(req), receipt.get("project_id"),
             None, receipt["outcome"], canonical(receipt).decode(), now))
        mark_snapshot_dirty(db)
    if specs:
        root = data_root(ledger)
        ensure_dirs(root)
        _publish_specs(db, notify_dirs(root), specs, now)
    return receipt


def _apply_notification_tx(db, req, cfg, now, specs) -> dict:
    actor, token, origin = req["actor"], req["token"], req.get("origin")
    base = {"kind": "notification", "command_id": req["command_id"],
            "actor": actor, "origin": origin, "processed_at": now}
    tok = db.execute(
        "SELECT * FROM notification_action_tokens WHERE token=?",
        (token,)).fetchone()
    if tok is None:
        return {**base, "outcome": "rejected", "error": "unknown_token"}
    if tok["expires_at"] <= now:
        return {**base, "outcome": "rejected", "error": "token_expired"}
    card = _card_row(db, tok["card_id"])
    if card is None:
        return {**base, "outcome": "rejected", "error": "card_missing"}
    base.update(card_id=card["card_id"], card_key=card["card_key"],
                project_id=card["project_id"],
                projects=[card["project_id"]] if card["project_id"]
                else _digest_projects(db, card))
    if not _scope_match(card, origin):
        return {**base, "outcome": "rejected", "error": "scope_mismatch"}
    if card["message_id"] \
            and card["message_id"] != (origin or {}).get("message_id"):
        # a delivered card's buttons live on its bound message — a token
        # arriving with another message's origin is being replayed in a
        # context that never rendered this card
        return {**base, "outcome": "rejected", "error": "origin_mismatch"}
    action = tok["action"]
    if action in _WRITE_ACTIONS and not interactive_enabled(cfg):
        return {**base, "outcome": "rejected", "error": "interactive_off"}
    if card["delivery_state"] == "revoked":
        return {**base, "outcome": "rejected", "error": "card_revoked"}
    if action in _WRITE_ACTIONS \
            and tok["need_source_gen"] is not None \
            and tok["need_source_gen"] != card["source_generation"]:
        return {**base, "outcome": "rejected", "error": "stale_source",
                "hint": "refresh"}
    if action in ("prev", "next"):
        if tok["need_ui_rev"] is not None \
                and tok["need_ui_rev"] != card["ui_revision"]:
            return {**base, "outcome": "rejected", "error": "stale_ui",
                    "hint": "refresh"}
        try:
            params = json.loads(tok["params"] or "{}")
        except (json.JSONDecodeError, TypeError):
            params = {}
        page = params.get("page")
        content = _card_content(db, card)
        if type(page) is not int or not 0 <= page < content["pages"]:
            return {**base, "outcome": "rejected", "error": "bad_page"}
        db.execute(
            "UPDATE notification_cards SET ui_state=?,ui_revision=?,"
            "updated_at=? WHERE card_id=?",
            (json.dumps({"page": page}), card["ui_revision"] + 1,
             now, card["card_id"]))
        # a nav consumes the old tokens — issue the next render so the
        # card actually changes page and carries fresh ui_rev tokens
        # (§2: view操作は適用後に新revision/token)
        new_render = _issue_render(db, card["card_id"], cfg, now, specs)
        return {**base, "outcome": "applied", "action": "page",
                "page": page, "delivery_id": new_render}
    if action == "body":
        # view-only: answer with the untruncated text of the shown set
        # the click's manifest froze — no state change, no re-render
        man = db.execute(
            "SELECT * FROM notification_view_manifests "
            "WHERE manifest_id=? AND card_id=?",
            (tok["need_manifest_id"], card["card_id"])).fetchone()
        if man is None or man["invalidated"]:
            return {**base, "outcome": "rejected",
                    "error": "manifest_invalid"}
        title, body_text = _card_body_text(db, card, man)
        return {**base, "outcome": "applied", "action": "body",
                "title": title, "body": body_text}
    if action == "ack":
        mid = tok["need_manifest_id"]
        man = db.execute(
            "SELECT * FROM notification_view_manifests "
            "WHERE manifest_id=? AND card_id=?",
            (mid, card["card_id"])).fetchone()
        if man is None or man["invalidated"]:
            return {**base, "outcome": "rejected",
                    "error": "manifest_invalid"}
        dupe = db.execute(
            "SELECT 1 FROM notification_acknowledgements "
            "WHERE card_id=? AND manifest_id=? AND actor=?",
            (card["card_id"], mid, actor)).fetchone()
        if dupe:
            return {**base, "outcome": "applied", "action": "ack",
                    "absorbed": True, "manifest_id": mid}
        db.execute(
            "INSERT INTO notification_acknowledgements("
            "card_id,manifest_id,actor,command_id,receipt_ref,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (card["card_id"], mid, actor, req["command_id"],
             req["command_id"], now))
        shown = json.loads(man["shown"] or "[]")
        # the footer now shows the ack — re-render immediately like a
        # nav click, not at the next sweep (§2: applied state must show
        # in the card itself within the interaction budget)
        new_render = _issue_render(db, card["card_id"], cfg, now, specs)
        return {**base, "outcome": "applied", "action": "ack",
                "manifest_id": mid, "shown": shown,
                "delivery_id": new_render}
    if action == "assign":
        db.execute(
            """INSERT INTO notification_triage(
                 card_id,owner,defer_until,state,revision,last_actor,
                 updated_at) VALUES(?,?,NULL,'assigned',1,?,?)
               ON CONFLICT(card_id) DO UPDATE SET
                 owner=excluded.owner,defer_until=NULL,state='assigned',
                 revision=revision+1,last_actor=excluded.last_actor,
                 updated_at=excluded.updated_at""",
            (card["card_id"], actor, actor, now))
        new_render = _issue_render(db, card["card_id"], cfg, now, specs)
        return {**base, "outcome": "applied", "action": "assign",
                "owner": actor, "delivery_id": new_render}
    if action == "defer":
        until = now + DEFER_S
        db.execute(
            """INSERT INTO notification_triage(
                 card_id,owner,defer_until,state,revision,last_actor,
                 updated_at) VALUES(?,NULL,?,'deferred',1,?,?)
               ON CONFLICT(card_id) DO UPDATE SET
                 defer_until=excluded.defer_until,state='deferred',
                 revision=revision+1,last_actor=excluded.last_actor,
                 updated_at=excluded.updated_at""",
            (card["card_id"], until, actor, now))
        new_render = _issue_render(db, card["card_id"], cfg, now, specs)
        return {**base, "outcome": "applied", "action": "defer",
                "defer_until": until, "delivery_id": new_render}
    # request/dismiss tokens authorize the plugin-side modal — nothing
    # is applied here; the human command itself arrives separately as
    # request.create/ops.signal_dismiss with the full envelope. The
    # stored params go back to the caller so the plugin builds the modal
    # against what was rendered — user input never picks the target.
    if action in ("request", "dismiss"):
        try:
            params = json.loads(tok["params"] or "{}")
        except (json.JSONDecodeError, TypeError):
            params = {}
        return {**base, "outcome": "applied", "action": action,
                "modal": True, "params": params}
    return {**base, "outcome": "rejected",
            "error": "action_not_applicable"}


def _digest_projects(db, card) -> list:
    keys = _anchor_keys(card)
    sigs = _latest_signals(db, keys)
    return sorted({s["content"].get("project_id") for s in sigs.values()
                   if positive(s["content"].get("project_id"))})


def apply_refresh(ledger, req, cfg, now=None) -> dict:
    """op='refresh': self-heal re-render driven by a verified native
    origin (message/channel/application), never a bare card_key."""
    db = _db(ledger)
    now = time.time() if now is None else now
    command_id = req["command_id"]
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload_hash,receipt_json FROM command_receipts "
            "WHERE command_id=?", (command_id,)).fetchone()
        if old:
            stored = json.loads(old["receipt_json"])
            if old["payload_hash"] != payload_hash(req):
                return {"outcome": "rejected", "error": "command_id_conflict"}
            return stored
        card = _origin_card(db, req.get("origin"))
        if card is None:
            receipt = {"kind": "refresh", "command_id": command_id,
                       "actor": req["actor"], "origin": req.get("origin"),
                       "outcome": "rejected", "error": "card_not_found",
                       "processed_at": now}
        elif card["delivery_state"] == "revoked":
            # a refresh on a revoked card must not mint another revoke
            # render — the revocation stands until a new intent arrives
            receipt = {"kind": "refresh", "command_id": command_id,
                       "actor": req["actor"], "origin": req.get("origin"),
                       "card_id": card["card_id"],
                       "project_id": card["project_id"],
                       "outcome": "rejected", "error": "card_revoked",
                       "processed_at": now}
        else:
            _issue_render(db, card["card_id"], cfg, now, specs,
                          force=True)
            receipt = {"kind": "refresh", "command_id": command_id,
                       "actor": req["actor"], "origin": req.get("origin"),
                       "card_id": card["card_id"],
                       "project_id": card["project_id"],
                       "outcome": "applied", "processed_at": now,
                       "projects": ([card["project_id"]]
                                    if card["project_id"]
                                    else _digest_projects(db, card))}
        db.execute(
            "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
            (command_id, payload_hash(req), receipt.get("project_id"),
             None, receipt["outcome"], canonical(receipt).decode(), now))
        mark_snapshot_dirty(db)
    root = data_root(ledger)
    ensure_dirs(root)
    _publish_specs(db, notify_dirs(root), specs, now)
    return receipt


def apply_transport_begin(ledger, req, cfg, now=None) -> dict:
    """transport_begin: the runner re-verifies send eligibility and
    commits the attempt BEFORE any grant. A file claim alone is never
    send authorization; a denial is itself a durable not_sent attempt."""
    db = _db(ledger)
    now = time.time() if now is None else now
    cid = req["command_id"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT * FROM notification_delivery_attempts "
            "WHERE begin_command_id=?", (cid,)).fetchone()
        if old is not None:
            return _begin_result(db, old)
        clash = db.execute(
            "SELECT 1 FROM notification_delivery_attempts "
            "WHERE attempt_id=?", (req["attempt_id"],)).fetchone()
        if clash is not None:
            return {"granted": False, "error": "attempt_id_conflict",
                    "command_id": cid}
        reason = _begin_check(db, req, cfg)
        if reason == "unknown_delivery":
            # nothing to reference — a denied attempt row needs the
            # render FK; the result file carries the audit instead.
            # The spec file is definitively dead — remove it so a
            # worker stops re-claiming bytes that can never grant.
            _unlink_spec(ledger, req["delivery_id"])
            return {"granted": False, "error": "denied_unknown_delivery",
                    "command_id": cid, "attempt_id": req["attempt_id"],
                    "delivery_id": req["delivery_id"]}
        state = "granted" if reason is None else "not_sent"
        db.execute(
            """INSERT INTO notification_delivery_attempts(
                 attempt_id,delivery_id,begin_command_id,state,
                 error_code,worker_id,created_at,finished_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (req["attempt_id"], req["delivery_id"], cid, state,
             None if reason is None else f"denied_{reason}",
             req["worker_id"], now, None if reason is None else now))
        if reason is None:
            db.execute(
                "UPDATE notification_renders SET state='sending',"
                "updated_at=? WHERE delivery_id=? AND state='queued'",
                (now, req["delivery_id"]))
        mark_snapshot_dirty(db)
        result = _begin_result(db, db.execute(
            "SELECT * FROM notification_delivery_attempts "
            "WHERE begin_command_id=?", (cid,)).fetchone())
    if reason is not None and _denial_is_final(
            db, req["delivery_id"], reason):
        _unlink_spec(ledger, req["delivery_id"])
    return result


_FINAL_DENIALS = frozenset({
    "hash_mismatch", "rev_mismatch", "epoch_mismatch",
    "render_cancelled", "card_revoked"})


def _denial_is_final(db, delivery_id, reason) -> bool:
    """Whether a denied begin means the spec bytes can never grant —
    stale/forged content, a cancelled render, a revoked card, or a
    render that already reached a terminal state. Transient answers
    (in_flight, interactive_off, sending) and scope_mismatch — the spec
    may simply be addressed to another worker — keep the file."""
    if reason in _FINAL_DENIALS:
        return True
    if reason == "not_queued":
        r = db.execute(
            "SELECT state FROM notification_renders WHERE delivery_id=?",
            (delivery_id,)).fetchone()
        return r is not None and r["state"] in (
            "delivered", "not_sent", "cancelled")
    return False


def _unlink_spec(ledger, delivery_id) -> None:
    """Remove a definitively dead spec file. The runner owns
    discord_render; a missing file just means 'nothing to claim' —
    the watchdog republishes a live render's spec_json if needed."""
    try:
        os.unlink(os.path.join(
            notify_dirs(data_root(ledger))["discord_render"],
            str(delivery_id) + ".json"))
    except OSError:
        pass


def _begin_check(db, req, cfg) -> str | None:
    if not interactive_enabled(cfg):
        return "interactive_off"
    render = db.execute(
        "SELECT * FROM notification_renders WHERE delivery_id=?",
        (req["delivery_id"],)).fetchone()
    if render is None:
        return "unknown_delivery"
    if render["payload_hash"] != req["payload_hash"]:
        return "hash_mismatch"
    if render["render_rev"] != req["render_rev"]:
        return "rev_mismatch"
    if render["route_epoch"] != req["route_epoch"]:
        return "epoch_mismatch"
    for k in ("profile", "application_id", "guild_id", "channel_id"):
        if render[k] and render[k] != req.get(k):
            return "scope_mismatch"
    if render["state"] == "cancelled":
        return "render_cancelled"
    if render["state"] != "queued":
        return "not_queued"
    card = _card_row(db, render["card_id"]) \
        if render["card_id"] is not None else None
    if card is not None:
        if card["delivery_state"] == "revoked" \
                and render["op"] != "revoke":
            # a revoke render against a revoked card is exactly the
            # delete Discord is owed — every other op is refused
            return "card_revoked"
        if _unsettled_attempt(db, card["card_id"]):
            return "in_flight"
    return None


def _begin_result(db, attempt) -> dict:
    """The durable grant/denial record a worker consumes once — it
    echoes the render identity fields so the worker can verify the
    grant still matches the spec file it claimed."""
    render = db.execute(
        "SELECT render_rev,payload_hash,route_epoch,correlation,op "
        "FROM notification_renders WHERE delivery_id=?",
        (attempt["delivery_id"],)).fetchone()
    granted = attempt["state"] == "granted"
    out = {"granted": granted, "command_id": attempt["begin_command_id"],
           "attempt_id": attempt["attempt_id"],
           "delivery_id": attempt["delivery_id"],
           "attempt_state": attempt["state"],
           "worker_id": attempt["worker_id"]}
    if render is not None:
        out.update(render_rev=render["render_rev"],
                   payload_hash=render["payload_hash"],
                   route_epoch=render["route_epoch"],
                   correlation=render["correlation"], op=render["op"])
    if not granted:
        out["error"] = attempt["error_code"] or attempt["state"]
    return out


def apply_transport_receipt(ledger, req, cfg, now=None) -> dict:
    """transport_receipt: settle an attempt from the worker's factual
    report. Echo fields must match the stored grant/render — an
    arbitrary event_id or payload never marks anything accepted."""
    db = _db(ledger)
    now = time.time() if now is None else now
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        attempt = db.execute(
            "SELECT * FROM notification_delivery_attempts "
            "WHERE attempt_id=?", (req["attempt_id"],)).fetchone()
        render = None
        if attempt is not None:
            render = db.execute(
                "SELECT * FROM notification_renders WHERE delivery_id=?",
                (attempt["delivery_id"],)).fetchone()
        error = _receipt_check(attempt, render, req)
        if error:
            receipt = {"applied": False, "error": error,
                       "attempt_id": req["attempt_id"],
                       "delivery_id": req["delivery_id"]}
        else:
            prior = attempt["state"]
            result = req["result"]
            if prior in ("delivered", "not_sent"):
                # terminal already: same fact is idempotent, a different
                # fact is a conflict — never silently overwrite (§4)
                same = prior == result and (
                    result != "delivered"
                    or str(attempt["message_id"]) == str(req["message_id"]))
                receipt = {"applied": same,
                           "error": None if same else "attempt_conflict",
                           "conflict": not same,
                           "attempt_id": attempt["attempt_id"],
                           "delivery_id": attempt["delivery_id"],
                           "attempt_state": prior}
            else:
                _settle_attempt(db, attempt, render, result, now,
                                message_id=req.get("message_id"),
                                error_code=req.get("error_code"))
                receipt = {"applied": True, "attempt_id": attempt["attempt_id"],
                           "delivery_id": attempt["delivery_id"],
                           "attempt_state": result}
                if render["card_id"] is not None:
                    _issue_render(db, render["card_id"], cfg, now, specs)
        mark_snapshot_dirty(db)
    if specs:
        root = data_root(ledger)
        ensure_dirs(root)
        _publish_specs(db, notify_dirs(root), specs, now)
    return receipt


def _receipt_check(attempt, render, req) -> str | None:
    if attempt is None or render is None:
        return "unknown_attempt"
    if attempt["delivery_id"] != req["delivery_id"]:
        return "delivery_mismatch"
    for k, want in (("render_rev", render["render_rev"]),
                    ("payload_hash", render["payload_hash"]),
                    ("route_epoch", render["route_epoch"]),
                    ("correlation", render["correlation"])):
        if req.get(k) != want:
            return f"{k}_mismatch"
    for k in ("profile", "application_id", "guild_id", "channel_id"):
        if render[k] and render[k] != req.get(k):
            return "scope_mismatch"
    if req["result"] == "delivered" \
            and not isinstance(req.get("message_id"), str):
        return "message_id_required"
    if req["result"] == "not_sent" \
            and not req.get("error_code"):
        return "error_code_required"
    return None


def apply_thread_receipt(ledger, req, cfg, now=None) -> dict:
    """thread_receipt: thread creation is independent of the primary
    card delivery — failure here never resends the card."""
    db = _db(ledger)
    now = time.time() if now is None else now
    with db:
        db.execute("BEGIN IMMEDIATE")
        render = db.execute(
            "SELECT * FROM notification_renders WHERE delivery_id=?",
            (req["delivery_id"],)).fetchone()
        if render is None or render["card_id"] is None:
            return {"applied": False, "error": "unknown_delivery"}
        card = _card_row(db, render["card_id"])
        if card["message_id"] is not None \
                and str(req["message_id"]) != card["message_id"]:
            return {"applied": False, "error": "message_id_mismatch"}
        tid, err = req.get("thread_id"), req.get("error_code")
        if isinstance(tid, str) and tid:
            if card["thread_state"] == "deleted":
                state, thread_id = "deleted", card["thread_id"]
            else:
                state = "created"
                thread_id = card["thread_id"] or tid   # bind once
        elif err == "thread_deleted":
            state, thread_id = "deleted", card["thread_id"]
        else:
            state, thread_id = "failed", card["thread_id"]
        db.execute(
            "UPDATE notification_cards SET thread_id=?,thread_state=?,"
            "updated_at=? WHERE card_id=?",
            (thread_id, state, now, card["card_id"]))
        mark_snapshot_dirty(db)
        return {"applied": True, "thread_state": state,
                "thread_id": thread_id}


# ---------- ops.card_resolve (operator recovery, data/cmd only) ----------

_RESOLVE_RESULTS = ("mark_not_sent", "mark_delivered")


def validate_card_resolve(req) -> str | None:
    """Dedicated validator — deliberately NOT routed through
    mcs_requests.validate (its positive project_id rule does not apply:
    the project set is derived from the stored render/coverage)."""
    if not isinstance(req, dict):
        return "bad_command"
    allowed = {"version", "cmd", "command_id", "actor", "human_confirmed",
               "reason", "delivery_id", "attempt_id", "result",
               "profile", "application_id", "guild_id", "channel_id",
               "message_id", "evidence"}
    if req.keys() - allowed:
        return "unknown_field"
    if req.get("cmd") != "ops.card_resolve":
        return "unknown_cmd"
    if type(req.get("version")) is not int or req["version"] != 1:
        return "bad_version"
    if not valid_uuid(req.get("command_id")):
        return "bad_command_id"
    if req.get("human_confirmed") is not True:
        return "human_confirmation_required"
    from mcs_requests import _text
    if not _text(req.get("actor"), 120):
        return "bad_actor"
    if not _text(req.get("reason"), 2000):
        return "bad_reason"
    if not valid_uuid(req.get("delivery_id")):
        return "bad_delivery_id"
    if not _text(req.get("attempt_id"), 120):
        return "bad_attempt_id"
    if req.get("result") not in _RESOLVE_RESULTS:
        return "bad_result"
    for k in ("profile", "application_id", "guild_id", "channel_id"):
        if not _text(req.get(k), 200):
            return f"bad_{k}"
    if req["result"] == "mark_delivered" \
            and not _text(req.get("message_id"), 64):
        return "bad_message_id"
    ev = req.get("evidence")
    if not isinstance(ev, dict) or not ev:
        return "bad_evidence"
    if not _text(ev.get("method"), 200):
        return "bad_evidence_method"
    if not _text(ev.get("ref"), 500):
        return "bad_evidence_ref"
    if req["result"] == "mark_not_sent":
        if ev.get("worker_stopped") is not True:
            return "worker_not_proven_stopped"
        if ev.get("proof") not in ("no_journal_started", "api_rejected"):
            return "bad_proof"
    return None


def apply_card_resolve(ledger, req, cfg=None, now=None) -> dict:
    """Operator-only recovery for an unsettled attempt. Verifies the
    stored grant/render/card scope and evidence, then runs the SAME
    shared settlement as a normal receipt — resolve never sends."""
    db = _db(ledger)
    now = time.time() if now is None else now
    if cfg is None:
        try:
            from mcs_util import load_config
            cfg = load_config(os.path.join(os.path.expanduser("~/.mcs"),
                                           "config.json"))
        except Exception:
            cfg = {}
    digest = payload_hash(req)
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload_hash,receipt_json FROM command_receipts "
            "WHERE command_id=?", (req["command_id"],)).fetchone()
        if old:
            if old["payload_hash"] != digest:
                return {"outcome": "rejected", "error": "command_id_conflict"}
            return json.loads(old["receipt_json"])
        error = validate_card_resolve(req)
        receipt = {"kind": "ops.card_resolve", "command_id": req["command_id"],
                   "actor": req.get("actor"), "reason": req.get("reason"),
                   "delivery_id": req.get("delivery_id"),
                   "attempt_id": req.get("attempt_id"),
                   "processed_at": now}
        attempt = render = None
        if error is None:
            attempt = db.execute(
                "SELECT * FROM notification_delivery_attempts "
                "WHERE attempt_id=?", (req["attempt_id"],)).fetchone()
            render = db.execute(
                "SELECT * FROM notification_renders WHERE delivery_id=?",
                (req["delivery_id"],)).fetchone() if attempt else None
            card = (_card_row(db, render["card_id"])
                    if render is not None
                    and render["card_id"] is not None else None)
            error = _resolve_check(attempt, render, card, req)
        if error is None:
            receipt["scope"] = {k: render[k] for k in
                                ("profile", "application_id",
                                 "guild_id", "channel_id")}
            receipt["projects"] = _card_projects(db, render, card)
            if attempt["state"] in ("delivered", "not_sent"):
                same = (attempt["state"]
                        == ("delivered" if req["result"] == "mark_delivered"
                            else "not_sent"))
                receipt["outcome"] = "applied" if same else "rejected"
                if not same:
                    receipt["error"] = "attempt_conflict"
                receipt["attempt_state"] = attempt["state"]
            else:
                result = ("delivered" if req["result"] == "mark_delivered"
                          else "not_sent")
                _settle_attempt(db, attempt, render, result, now,
                                message_id=req.get("message_id"),
                                error_code=f"resolve:{req['result']}")
                receipt["outcome"] = "applied"
                receipt["attempt_state"] = result
                if card is not None:
                    _issue_render(db, card["card_id"], cfg, now, specs)
        else:
            receipt["outcome"] = "rejected"
            receipt["error"] = error
        db.execute(
            "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
            (req["command_id"], digest, None, None,
             receipt["outcome"], canonical(receipt).decode(), now))
        mark_snapshot_dirty(db)
    if specs:
        root = data_root(ledger)
        ensure_dirs(root)
        _publish_specs(db, notify_dirs(root), specs, now)
    return receipt


def _resolve_check(attempt, render, card, req) -> str | None:
    if attempt is None:
        return "unknown_attempt"
    if render is None or attempt["delivery_id"] != req["delivery_id"]:
        return "delivery_mismatch"
    for k in ("profile", "application_id", "guild_id", "channel_id"):
        if render[k] != req.get(k):
            return "scope_mismatch"
    if req["result"] == "mark_delivered":
        # create binds a discovered message once; update/revoke must
        # match the message the card already knows
        if card is not None and render["op"] != "create" \
                and card.get("message_id") is not None \
                and str(req["message_id"]) != card["message_id"]:
            return "message_id_mismatch"
    return None


def _card_projects(db, render, card) -> list:
    """Project scope for the audit receipt — derived from the stored
    render/coverage, never from operator input."""
    projects = set()
    if card is not None and positive(card["project_id"]):
        projects.add(card["project_id"])
    if render["card_id"] is not None:
        for r in db.execute(
                "SELECT coverage FROM notification_intent_cards "
                "WHERE card_id=?", (render["card_id"],)).fetchall():
            try:
                cov = json.loads(r["coverage"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if card is not None and card["kind"] in ("signal", "digest"):
                sigs = _latest_signals(db, [k for k in cov
                                          if type(k) is str])
                projects.update(
                    s["content"].get("project_id") for s in sigs.values())
    return sorted(p for p in projects if positive(p))


# ---------- sweep / GC / watchdog / health ----------

def revoke_card(db, card_id, now) -> None:
    """Revocation decision: flag the card once, then suppress any still-
    pending coverage — it can never legitimately deliver now, and an
    intent must not stay pending forever on a revoked card."""
    cur = db.execute(
        "UPDATE notification_cards SET delivery_state='revoked',"
        "revoked_at=?,updated_at=? WHERE card_id=? "
        "AND delivery_state!='revoked' AND revoked_at IS NULL",
        (now, now, card_id))
    if not cur.rowcount:
        return
    # a queued-but-unsent render must not linger claimable — a worker
    # picking it up would post a card for an archived/dead unit
    for r in db.execute(
            "SELECT delivery_id FROM notification_renders "
            "WHERE card_id=? AND state IN ('queued','held')",
            (card_id,)).fetchall():
        db.execute("UPDATE notification_renders SET state='cancelled',"
                   "updated_at=? WHERE delivery_id=?",
                   (now, r["delivery_id"]))
        db.execute("UPDATE notification_intent_cards SET delivery_id=NULL,"
                   "required_render_rev=0 WHERE delivery_id=?",
                   (r["delivery_id"],))
    db.execute(
        "UPDATE notification_intent_cards SET state='suppressed' "
        "WHERE card_id=? AND state='pending'", (card_id,))
    for r in db.execute(
            "SELECT DISTINCT event_id FROM notification_intent_cards "
            "WHERE card_id=?", (card_id,)).fetchall():
        _complete_intent(db, r["event_id"], now)


def sweep(ledger, cfg, limit=100, now=None) -> dict:
    """Tick-side change detection over live cards: edited/deleted
    thread messages, signal state transitions, archive revokes,
    defer_until arrival, and renders stuck unpublished. Bounded per
    call; cards with an unsettled attempt are left to the settle path."""
    db = _db(ledger)
    now = time.time() if now is None else now
    root = data_root(ledger)
    dirs = notify_dirs(root)
    ensure_dirs(root)
    specs = []
    scanned = updated = 0
    with db:
        db.execute("BEGIN IMMEDIATE")
        cards = db.execute(
            """SELECT card_id,kind,project_id,delivery_state FROM
                 notification_cards
               WHERE delivery_state IN ('pending','delivered',
                                        'update_failed','delivery_unknown',
                                        'message_deleted')
               ORDER BY updated_at LIMIT ?""", (limit,)).fetchall()
        scanned = len(cards)
        for row in cards:
            card = _card_row(db, row["card_id"])
            if card is None:
                continue
            if positive(card["project_id"]) and db.execute(
                    "SELECT 1 FROM patients WHERE project_id=? "
                    "AND is_archived=1", (card["project_id"],)).fetchone():
                revoke_card(db, card["card_id"], now)
            tri = db.execute(
                "SELECT state,defer_until FROM notification_triage "
                "WHERE card_id=?", (card["card_id"],)).fetchone()
            if tri and tri["state"] == "deferred" \
                    and tri["defer_until"] is not None \
                    and tri["defer_until"] <= now:
                db.execute(
                    "UPDATE notification_triage SET state='open',"
                    "revision=revision+1,updated_at=? WHERE card_id=?",
                    (now, card["card_id"]))
            before = (card["source_generation"],
                      card["presentation_generation"],
                      card["desired_render_rev"])
            _issue_render(db, card["card_id"], cfg, now, specs)
            after = _card_row(db, card["card_id"])
            if (after["source_generation"], after["presentation_generation"],
                    after["desired_render_rev"]) != before:
                updated += 1
        # watchdog: live renders whose spec never published (or whose
        # file vanished) get their stored bytes republished
        for r in db.execute(
                "SELECT delivery_id,spec_json FROM notification_renders "
                "WHERE state='queued' AND spec_published=0").fetchall():
            if r["spec_json"]:
                specs.append(json.loads(r["spec_json"]))
        if specs:
            mark_snapshot_dirty(db)
    _publish_specs(db, dirs, specs, now)
    return {"scanned": scanned, "updated": updated,
            "republished": len(specs)}


def gc(ledger, cfg=None, now=None, token_keep_s=0, limit=500) -> dict:
    """Delete expired action tokens and strip PHI-bearing spec_json from
    renders that are fully settled (cancelled/not_sent, a delivered
    successor, no unsettled attempt, no references). Rows, hashes and
    correlations stay for audit. Bounded per call — the rest waits for
    the next tick."""
    db = _db(ledger)
    now = time.time() if now is None else now
    with db:
        tokens = db.execute(
            "DELETE FROM notification_action_tokens WHERE expires_at<?",
            (now,)).rowcount
        cleared = 0
        for r in db.execute(
                """SELECT delivery_id,card_id FROM notification_renders
                   WHERE spec_json IS NOT NULL
                     AND state IN ('cancelled','not_sent')
                   LIMIT ?""", (limit,)).fetchall():
            if _unsettled_attempt(db, r["card_id"]):
                continue
            referenced = db.execute(
                "SELECT 1 FROM notification_intent_cards "
                "WHERE delivery_id=? AND state='pending'",
                (r["delivery_id"],)).fetchone()
            if referenced:
                continue
            successor = db.execute(
                "SELECT 1 FROM notification_renders WHERE card_id=? "
                "AND state='delivered'", (r["card_id"],)).fetchone()
            if successor is None:
                continue
            db.execute(
                "UPDATE notification_renders SET spec_json=NULL,"
                "updated_at=? WHERE delivery_id=?",
                (now, r["delivery_id"]))
            cleared += 1
        # spec files for terminal renders can go — a begin against them
        # would be denied anyway; unknown stays for investigation
        removed = 0
        root = data_root(ledger)
        dirs = notify_dirs(root)
        for r in db.execute(
                "SELECT delivery_id FROM notification_renders "
                "WHERE state IN ('delivered','not_sent','cancelled') "
                "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall():
            path = os.path.join(dirs["discord_render"],
                                r["delivery_id"] + ".json")
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
        # cmd_results files are a transport artifact — the durable audit
        # lives in command_receipts. The plugin polls a result for
        # minutes at most; anything a week old is dead weight.
        results = 0
        try:
            names = sorted(os.listdir(dirs["cmd_results"]))
        except OSError:
            names = []
        for n in names[:limit]:
            if not n.endswith(".json"):
                continue
            path = os.path.join(dirs["cmd_results"], n)
            try:
                if now - os.stat(path).st_mtime > TOKEN_WRITE_S:
                    os.unlink(path)
                    results += 1
            except OSError:
                pass
        return {"tokens": tokens, "spec_json_cleared": cleared,
                "spec_files": removed, "result_files": results}


def recover(ledger, cfg, result) -> dict:
    """Startup/periodic recovery for the file-published plane: republish
    every queued render whose spec file is missing, repair the flags
    file, and surface stale claims for the health layer. Never consumes
    .claimed work — a claim is not proof the worker is dead."""
    db = _db(ledger)
    root = data_root(ledger)
    dirs = notify_dirs(root)
    ensure_dirs(root)
    now = time.time()
    fixed = {"republished": 0, "missing_claimed": 0}
    with db:
        db.execute("BEGIN IMMEDIATE")
        for r in db.execute(
                "SELECT delivery_id,spec_json FROM notification_renders "
                "WHERE state IN ('queued','held')").fetchall():
            path = os.path.join(dirs["discord_render"],
                                r["delivery_id"] + ".json")
            if os.path.isfile(path):
                db.execute(
                    "UPDATE notification_renders SET spec_published=1 "
                    "WHERE delivery_id=?", (r["delivery_id"],))
            elif r["spec_json"]:
                publish_file(dirs["discord_render"],
                             r["delivery_id"] + ".json",
                             r["spec_json"].encode("utf-8"))
                db.execute(
                    "UPDATE notification_renders SET spec_published=1,"
                    "first_published_at=COALESCE(first_published_at,?),"
                    "updated_at=? WHERE delivery_id=?",
                    (now, now, r["delivery_id"]))
                fixed["republished"] += 1
        try:
            claimed = [n for n in os.listdir(dirs["discord_render"])
                       if n.endswith(".json.claimed")]
        except OSError:
            claimed = []
        stale = 0
        for n in claimed:
            try:
                if now - os.stat(os.path.join(
                        dirs["discord_render"], n)).st_mtime > 60:
                    stale += 1
            except OSError:
                pass
        fixed["missing_claimed"] = stale
        if stale:
            result.setdefault("errors", []).append(
                f"claimed_specs_stale:{stale}")
    publish_flags(cfg, root)
    return fixed


def health_cards(ledger) -> dict:
    """The health.json 'cards' section — counts and ages only, no
    patient data."""
    db = _db(ledger)
    now = time.time()
    states = {r["delivery_state"]: r["c"] for r in db.execute(
        "SELECT delivery_state,COUNT(*) c FROM notification_cards "
        "GROUP BY delivery_state")}
    oldest = db.execute(
        "SELECT MIN(created_at) FROM notification_cards "
        "WHERE delivery_state='pending'").fetchone()[0]
    queued = db.execute(
        "SELECT COUNT(*) FROM notification_renders WHERE state='queued'"
    ).fetchone()[0]
    unsettled = db.execute(
        "SELECT COUNT(*) c,MIN(created_at) o FROM "
        "notification_delivery_attempts WHERE state IN ('granted','unknown')"
    ).fetchone()
    last = db.execute(
        "SELECT MAX(updated_at) FROM notification_renders "
        "WHERE state='delivered'").fetchone()[0]
    unknown = db.execute(
        "SELECT COUNT(*) FROM notification_renders WHERE state='unknown'"
    ).fetchone()[0]
    return {
        "pending": states.get("pending", 0),
        "delivered": states.get("delivered", 0),
        "update_failed": states.get("update_failed", 0),
        "delivery_unknown": states.get("delivery_unknown", 0),
        "message_deleted": states.get("message_deleted", 0),
        "revoked": states.get("revoked", 0),
        "oldest_pending_age_s": round(now - oldest, 1) if oldest else 0,
        "renders_queued": queued,
        "renders_unknown": unknown,
        "attempts_unsettled": unsettled["c"],
        "oldest_unsettled_age_s": (round(now - unsettled["o"], 1)
                                 if unsettled["o"] else 0),
        "last_delivered_at": last or 0,
    }
