"""Interactive notification cards — delivery ledger and neutral render specs.

This module owns card intents, rendering, actions, and maintenance in the
Discord interactive notification pipeline (plan: .omo/plans/discord-interactive-cards.md):

- notify_outbox intents routed 'interactive' are FROZEN into sealed
  batches, fanned out to durable cards (1 intent -> N cards), and
  rendered as immutable, neutral specs published atomically to
  data/discord_render/<delivery_id>.json.
- notify_transport owns delivery grants, receipts, and operator recovery.
  Discord HTTP and the DB commit are never one transaction: attempts hold
  exclusive per-card send ownership until their factual result is settled
  (delivered/not_sent/unknown).
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

import hashlib
import json
import os
import secrets
import tempfile
import time
import uuid
from contextlib import suppress

from mcs_adapter import project_url
from mcs_queries import current_extract_pred, current_v4_id
from mcs_requests import canonical, payload_hash, positive, valid_hash
from notify_render import (
    _anchor_keys, _card_body_text, _card_content, _content_fp,
    _latest_signals, _mmdd, _patient_name, _signal_evidence, _source_fp,
    current_ackers, my_tasks_view, open_tasks, patient_search_view,
    patient_summary_text, unacked_view)

RENDER_SCHEMA = "mcs-card-render/v1"
SLACK_RENDER_SCHEMA = "mcs-card-render/v2"

# outbox kinds that become interactive cards when notify.interactive is
# on; everything else (ops alerts, semantic notices) stays legacy text.
INTERACTIVE_KINDS = frozenset({"new_messages", "signal"})

# renders whose spec file must be available to a claiming worker
LIVE_RENDER = ("queued", "sending", "unknown", "held")

RESEAT_S = 3600           # re-examine a dispatched pending intent hourly
TOKEN_VIEW_S = 30 * 86400
TOKEN_WRITE_S = 7 * 86400
DEFER_S = 86400           # fixed 'hold' duration for v1
MAX_RESEND = 3            # consecutive not_sent attempts before a card
                          # suspends auto-retry (update_failed)
RESTORE_MARKER = "restore_pending.json"
RESTORE_RECEIPT = "restore_reconcile.json"

# durable part plan (T7): thread bodies split under the 2000-char
# message bound; MAX_PARTS is a DECLARED bound — a pathological body
# still gets a visible marker part, never a silently dropped tail
THREAD_PART_LIMIT = 1900
MAX_PARTS = 256
_TRUNCATED_PART = "（上限を超えたため残りは省略 — 原本を参照）"
_ATTACHMENT_UNAVAILABLE = frozenset(
    {"failed", "deleted", "withdrawn", "pruned"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_cards(
  card_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK(kind IN ('thread','signal','digest')),
  project_id INTEGER,
  root_message_id INTEGER,
  anchor_key TEXT NOT NULL,
  profile TEXT, application_id TEXT, guild_id TEXT, channel_id TEXT,
  transport TEXT NOT NULL DEFAULT 'discord',
  team_id TEXT,
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
  transport TEXT NOT NULL DEFAULT 'discord',
  scope_json TEXT,
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
  transport TEXT NOT NULL DEFAULT 'discord',
  team_id TEXT,
  spec_json TEXT,
  spec_published INTEGER NOT NULL DEFAULT 0,
  first_published_at REAL,
  payload_hash TEXT NOT NULL,
  correlation TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL DEFAULT 'queued'
    CHECK(state IN ('queued','sending','delivered','not_sent',
                    'unknown','held','cancelled')),
  parts_state TEXT NOT NULL DEFAULT 'none',
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
-- Durable per-render delivery plan: card/thread/body_part/
-- attachment_part identities bound to the sealed spec (generation,
-- render_rev, scope, ordered hashes). A delivered card never implies
-- complete delivery — full completion requires every planned part
-- delivered. 'held' marks dependents blocked by a failed thread.
CREATE TABLE IF NOT EXISTS notification_render_parts(
  delivery_id TEXT NOT NULL,
  part_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('card','thread','body_part',
                                    'attachment_part')),
  idx INTEGER NOT NULL,
  payload_sha256 TEXT, bytes INTEGER, name TEXT, attachment_id INTEGER,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','delivered','not_sent','unknown','held')),
  remote_id TEXT, error_code TEXT, attempt_id TEXT,
  updated_at REAL,
  PRIMARY KEY(delivery_id, part_id));
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
  created_at REAL NOT NULL,
  withdrawn_at REAL);
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
-- Post-restore holds: a DB rewind can erase attempt/render rows that
-- the delivery journal proves had external effects. Each row pins one
-- held scope until an operator (ops.card_resolve) or a factual receipt
-- releases it — a held card never re-issues a render.
CREATE TABLE IF NOT EXISTS notification_restore_holds(
  hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER REFERENCES notification_cards(card_id),
  delivery_id TEXT,
  attempt_id TEXT,
  events_json TEXT,
  reason TEXT NOT NULL,
  scope_json TEXT,
  held_at REAL NOT NULL,
  released_at REAL,
  release_command_id TEXT);
CREATE INDEX IF NOT EXISTS idx_nholds_active
  ON notification_restore_holds(card_id, released_at);
-- ⏰ one reminder per task, stage and due date — the durable once-only
-- marker (a changed due date re-arms). event_id NULL = recorded without
-- sending (first-activation baseline), and request_id 0 / 'baseline' marks
-- that the baseline ran.
CREATE TABLE IF NOT EXISTS notification_task_reminders(
  request_id INTEGER NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('due','overdue','baseline')),
  due_date TEXT NOT NULL,
  event_id INTEGER,
  created_at REAL NOT NULL,
  PRIMARY KEY(request_id, stage, due_date));
"""

# card action vocabulary -> (label, discord style, class). ack/assign
# labels are state-dependent (_action_rows); "defer" is retired — never
# minted, kept so tokens on already-posted cards still resolve.
_ACTIONS = {
    "ack":     ("☐ 確認", "secondary", "write"),
    "assign":  ("👤 担当する", "secondary", "write"),
    "defer":   ("⏸ 保留", "secondary", "write"),
    "body":    ("📄 本文表示", "secondary", "view"),
    "tasks":   ("☑ タスク完了", "secondary", "view"),
    "request": ("📝 タスク作成", "secondary", "write"),
    "summary": ("🧾 患者サマリー", "secondary", "view"),
    "report":  ("⚠ 抽出の誤りを報告", "secondary", "write"),
    "dismiss": ("🚫 却下", "danger", "write"),
    "prev":    ("◀ 前へ", "secondary", "view"),
    "next":    ("次へ ▶", "secondary", "view"),
    "mytasks": ("📋 自分のタスク", "secondary", "view"),
    "unacked": ("🗂 未確認一覧", "secondary", "view"),
    "search":  ("🔎 この患者を検索", "secondary", "view"),
    # minted only inside a tasks view — never a card button; the plugin
    # supplies its own labels from the view's transitions
    "task_status": ("", "secondary", "write"),
}
_WRITE_ACTIONS = frozenset(
    a for a, (_, _, cls) in _ACTIONS.items() if cls == "write")
# recomputed on every click — a replayed command_id never returns the
# stored receipt for these
_LIVE_VIEWS = frozenset({"body", "summary", "request", "dismiss", "report",
                         "mytasks", "unacked", "search"})

MAX_COMPONENTS = 40           # the worker's per-card component ceiling
                              # (hermes_plugin spec.MAX_COMPONENTS)
TASK_HINT_MAX = 300           # 📝 prefill — the modal field holds 1000
STAFF_CHOICES = 25            # Discord/Slack select option ceiling
TASK_VIEW_LIMIT = 12          # ephemeral list rows — 12 tasks x <=2
                              # buttons stays under Discord's 25-button
                              # per-message ceiling


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
            ("discord_render", "discord_state", "slack_render", "slack_state", "flags",
             "cmd_int", "cmd_results")}


def ensure_dirs(root: str) -> None:
    for path in notify_dirs(root).values():
        os.makedirs(path, mode=0o700, exist_ok=True)


def notify_cfg(cfg: dict) -> dict:
    n = (cfg or {}).get("notify")
    return n if isinstance(n, dict) else {}


def interactive_enabled(cfg: dict) -> bool:
    return notify_cfg(cfg).get("interactive") in ("discord", "slack")


def signals_notify(cfg: dict) -> bool:
    signals = (cfg or {}).get("signals")
    return isinstance(signals, dict) and signals.get("notify") is True


def active_transport(cfg) -> str:
    return "slack" if notify_cfg(cfg).get("interactive") == "slack" else "discord"


def scope_fields(transport: str) -> tuple[str, ...]:
    return ("profile", "application_id",
            "team_id" if transport == "slack" else "guild_id", "channel_id")


def stored_scope(row) -> dict[str, str | None]:
    transport = row["transport"]
    scope = {k: row[k] for k in scope_fields(transport)}
    if transport == "slack":
        scope["transport"] = transport
    return scope


def route_epoch(cfg: dict) -> int:
    v = notify_cfg(cfg).get("route_epoch", 1)
    return v if type(v) is int and v > 0 else 1


def delivery_scope(cfg: dict) -> dict | None:
    """The Discord destination the runner addresses — all four fields
    required; a partial scope is a config error, never a fallback."""
    transport = active_transport(cfg)
    d = notify_cfg(cfg).get(transport)
    if not isinstance(d, dict):
        return None
    out = {}
    for k in scope_fields(transport):
        v = d.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
        out[k] = v.strip()
    if transport == "slack":
        if "guild_id" in d:
            return None
        out["transport"] = transport
    return out


def restore_pending(root: str) -> dict | None:
    """A restore marker blocks every send grant until reconcile runs.
    A corrupt or unreadable marker still blocks — fail closed."""
    path = os.path.join(root, RESTORE_MARKER)
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except FileNotFoundError:
        return {"unreadable": True} if os.path.lexists(path) else None
    except (OSError, ValueError, RecursionError):
        return {"unreadable": True}
    if isinstance(data, dict) and (
            data.get("phase") in ("restored", "awaiting_consent")
            or ("phase" not in data and "restored_at" in data)):
        return data
    return {"unreadable": True}


def mark_restored(root: str, backup_path=None, by="manual",
                  now=None, phase="restored", report_id=None) -> str:
    """Durable 'a restore happened' witness — written by every restore
    path so the runner can hold sends until reconcile completes.
    phase='awaiting_consent' holds sends while a schema-bump rollback
    waits on its per-restore human approval; nothing was swapped yet,
    so restored_at stays unset and reconcile must NOT clear it."""
    marker = {"v": 1, "phase": phase, "backup_path": backup_path,
              "by": by, "at": now if now is not None else time.time()}
    if phase == "restored":
        marker["restored_at"] = marker["at"]
    if report_id is not None:
        marker["report_id"] = report_id
    return publish_file(root, RESTORE_MARKER, canonical(marker))


def restore_awaiting_consent(root: str) -> dict | None:
    """Hold writers for pending consent or an unreadable restore phase."""
    marker = restore_pending(root)
    if isinstance(marker, dict) \
            and (marker.get("phase") == "awaiting_consent"
                 or marker.get("unreadable")):
        return marker
    return None


def clear_restore_pending(root: str) -> None:
    with suppress(FileNotFoundError):
        os.unlink(os.path.join(root, RESTORE_MARKER))
def _restore_hold_active(db, *, card_id=None, delivery_id=None) -> bool:
    """An unreleased restore hold on this card/delivery freezes it."""
    if card_id is not None and db.execute(
            "SELECT 1 FROM notification_restore_holds "
            "WHERE released_at IS NULL AND card_id=? LIMIT 1",
            (card_id,)).fetchone():
        return True
    return delivery_id is not None and db.execute(
        "SELECT 1 FROM notification_restore_holds "
        "WHERE released_at IS NULL AND delivery_id=? LIMIT 1",
        (delivery_id,)).fetchone() is not None


def release_holds(db, *, delivery_id=None, card_id=None,
                  command_id=None, now=None) -> int:
    """Release active restore holds for a settled scope."""
    now = time.time() if now is None else now
    clauses, vals = [], []
    if delivery_id is not None:
        clauses.append("delivery_id=?")
        vals.append(delivery_id)
    if card_id is not None:
        clauses.append("card_id=?")
        vals.append(card_id)
    if not clauses:
        return 0
    return db.execute(
        "UPDATE notification_restore_holds SET released_at=?,"
        "release_command_id=? WHERE released_at IS NULL AND ("
        + " OR ".join(clauses) + ")",
        [now, command_id, *vals]).rowcount


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
        with suppress(OSError):
            os.unlink(tmp)
        raise
    return os.path.join(directory, name)


def publish_flags(cfg: dict, root: str) -> bool:
    """flags/notify.json — the plugin's DB-free view of the effective
    interactive/kill-switch state. Published only when it changes."""
    n = notify_cfg(cfg)
    flags = {
        "interactive": interactive_enabled(cfg),
        "kill_switch": not interactive_enabled(cfg),
        "transport": active_transport(cfg),
        "route_epoch": route_epoch(cfg),
        "card_thread": n.get("card_thread") is True,
        "restore_pending": restore_pending(root) is not None,
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
            if isinstance(prior, dict):
                prior.pop("at", None)
                cur = dict(flags)
                cur.pop("at", None)
                if prior == cur:
                    return False
        except (ValueError, RecursionError):
            pass
    publish_file(os.path.dirname(path), "notify.json", raw)
    return True


# ---------- cards / renders ----------

def _unsettled_attempt(db, card_id: int):
    return db.execute(
        """SELECT a.attempt_id, a.state FROM notification_delivery_attempts a
           JOIN notification_renders r ON r.delivery_id=a.delivery_id
           WHERE r.card_id=? AND a.state IN ('granted','unknown')
           ORDER BY a.created_at DESC LIMIT 1""", (card_id,)).fetchone()


def _find_signal_card(db, pid, keys, scope):
    """A member key of an existing signal card resolves to that card —
    a fresh card_key must never fork a second card for the same unit."""
    keyset = set(keys)
    for r in db.execute(
            "SELECT * FROM notification_cards "
            "WHERE kind='signal' AND project_id=? AND delivery_state!='revoked'",
            (pid,)).fetchall():
        if r["transport"] != scope.get("transport", "discord"):
            continue
        if r["transport"] == "slack" and not _scope_match(r, scope):
            continue
        if keyset & set(_anchor_keys(r)):
            return r["card_id"]
    return None


def _card_for(db, target, scope, now) -> int:
    if target["kind"] == "signal":
        found = _find_signal_card(db, target["project_id"],
                                  target["anchor"]["signal_keys"], scope)
        if found is not None:
            # widen the anchor if this intent adds member keys
            row = db.execute("SELECT kind,anchor_key FROM notification_cards "
                             "WHERE card_id=?", (found,)).fetchone()
            prior_keys = _anchor_keys(row)
            merged = list(dict.fromkeys(
                prior_keys + target["anchor"]["signal_keys"]))
            if merged != prior_keys:
                db.execute("UPDATE notification_cards SET anchor_key=?,"
                           "updated_at=? WHERE card_id=?",
                           (json.dumps({"signal_keys": merged},
                                       ensure_ascii=False), now, found))
            return found
    key = target["card_key"]
    if scope.get("transport") == "slack":
        key = f"v2|slack|{payload_hash(scope)}|{key.removeprefix('v1|')}"
    row = db.execute("SELECT card_id FROM notification_cards "
                     "WHERE card_key=?", (key,)).fetchone()
    if row is not None:
        return row["card_id"]
    cur = db.execute(
        """INSERT INTO notification_cards(
             card_key,kind,project_id,root_message_id,anchor_key,
             profile,application_id,guild_id,channel_id,
             transport,team_id,
             ui_state,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, target["kind"], target.get("project_id"),
         target.get("root_message_id"),
         json.dumps(target["anchor"], ensure_ascii=False,
                    sort_keys=True),
         scope.get("profile"), scope.get("application_id"),
         scope.get("guild_id"), scope.get("channel_id"),
         scope.get("transport", "discord"), scope.get("team_id"),
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


def _action_rows(db, card, content, now, context=None,
                 in_thread_body=False):
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
    # toggles show state: the label/style is what a glance must tell
    acked = bool(current_ackers(db, card["card_id"],
                                card["source_generation"], content["shown"]))
    if kind == "digest":
        ack_label = "✅ このページ確認済み" if acked else "☐ このページを確認"
    else:
        ack_label = "✅ 確認済み" if acked else _ACTIONS["ack"][0]
    tri = db.execute("SELECT state,owner FROM notification_triage "
                     "WHERE card_id=?", (card["card_id"],)).fetchone()
    assigned = bool(tri and tri["state"] == "assigned" and tri["owner"])

    def btn(action, params=None, label=None, style=None):
        tok = _mint_token(db, card["card_id"], action, params, need, now)
        b = {"id": action, "ui": "button",
             "style": style or _ACTIONS[action][1],
             "label": label or _ACTIONS[action][0], "token": tok}
        row.append(b)

    btn("ack", {"shown_kind": content["shown_kind"]}, label=ack_label,
        style="success" if acked else None)
    btn("assign", label="👤 担当中" if assigned else None,
        style="primary" if assigned else None)
    def flush():
        if row:
            rows.append(list(row))
            row.clear()

    # row 1 — state toggles (+ 📄 where no thread carries the body)
    if not in_thread_body:
        # cards with a companion thread show the body inside it on
        # delivery — the 📄 button only remains where no thread can
        # carry it (card_thread off, failed/deleted thread)
        btn("body")
    flush()
    # row 2 — actions
    pid = card["project_id"]
    if positive(pid) and positive(context.get("source_message_id")) \
            and context.get("source_hash"):
        # a digest spans projects — a card-level request cannot pin a
        # single source, so the button is only emitted where it can work
        btn("request", {"project_id": pid})
    if open_tasks(db, card):
        # only while the thread has open tasks — the list view is the
        # thread-anchored _thread_tasks (signal/digest cards span
        # messages/projects the requests table does not key on)
        btn("tasks")
    if positive(pid):
        btn("summary")
    if context.get("extract_ref"):
        btn("report")
    if kind == "signal" and len(keys) == 1 \
            and keys[0] in (context.get("signals") or {}):
        btn("dismiss", {"signal_key": keys[0]})
    flush()
    # row 3 — MCS link + paging
    if positive(pid):
        # a plain link: no token, no runner round-trip
        row.append({"id": "link", "ui": "link", "label": "🔗 MCSで開く",
                    "url": project_url(pid)})
    if content["pages"] > 1:
        # only mint buttons that can actually move — a dead nav button
        # always comes back bad_page
        if content["page"] > 0:
            btn("prev", {"page": content["page"] - 1})
        if content["page"] + 1 < content["pages"]:
            btn("next", {"page": content["page"] + 1})
    flush()
    # row 4 — clicker-scoped lists (ephemeral answers). Optional: only
    # as many as the worker's component ceiling still allows — a spec
    # over budget is rejected whole and the card would not render at all
    used = (sum(c["type"] != "meta" for c in content["containers"])
            + sum(f["type"] == "text" for f in content["footer"])
            + sum(len(r) + 1 for r in rows))
    extras = ["mytasks", "unacked"] + (["search"] if positive(pid) else [])
    for action in extras[:max(0, MAX_COMPONENTS - used - 1)]:
        btn(action)
    flush()
    return rows


# ---------- durable part plan (T7) --------------------------------------

def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_body_chunks(text: str, limit: int = THREAD_PART_LIMIT) -> list:
    """Lossless chunks <= limit for the durable thread plan —
    ``"".join(chunks) == text`` always holds: lines keep their trailing
    newline and overlong lines hard-wrap, so planned parts reassemble
    byte-exact into the sealed body (the plugin sends these verbatim)."""
    chunks, cur = [], ""
    for seg in text.splitlines(keepends=True):
        while len(seg) > limit:
            take = limit - len(cur)
            if take <= 0:
                chunks.append(cur)
                cur = ""
                take = limit
            cur += seg[:take]
            seg = seg[take:]
        if cur and len(cur) + len(seg) > limit:
            chunks.append(cur)
            cur = ""
        cur += seg
    if cur:
        chunks.append(cur)
    return chunks


def _plan_attachments(db, shown) -> list:
    """Attachment parts from the render's sealed shown-set — never the
    mutable outbox. Downloaded files are deliverable parts; terminally
    unavailable files are disclosed as unavailable items (visible
    incompleteness, not silent omission)."""
    ids = [m for m in shown if type(m) is int]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = db.execute(
        f"""SELECT a.attachment_id,a.name,a.local_path,a.bytes,a.sha256,
                   a.state,m.body_state
            FROM attachments a
            JOIN messages m ON m.message_id=a.message_id
            WHERE a.message_id IN ({ph})
            ORDER BY a.attachment_id""", ids).fetchall()
    out = []
    for a in rows:
        name = a["name"] or f"file-{a['attachment_id']}"
        if len(name) > 200:
            name = name[:199] + "…"
        entry = {"attachment_id": a["attachment_id"],
                 "name": name}
        if a["state"] == "pending":
            continue      # in-flight download — followup path owns it
        if a["state"] == "downloaded" and a["local_path"] \
                and a["sha256"] and a["body_state"] != "deleted":
            out.append({**entry, "path": a["local_path"],
                        "sha256": a["sha256"],
                        "bytes": a["bytes"] or 0})
        else:
            # failed/withdrawn/deleted-source — disclosed, not sent
            out.append({**entry, "unavailable": True})
    return out


def _thread_plan_ids(db, card, shown) -> list:
    """Messages whose bodies a thread-bound render carries: the face's
    shown page plus every message a still-pending intent announces on
    this card. The face opens on the last page, so an intent spanning
    several pages would otherwise never post its earlier bodies (the
    in-thread card has no 📄 button to reach them)."""
    if card["kind"] != "thread":
        return list(shown)
    covered = set()
    for r in db.execute(
            "SELECT coverage FROM notification_intent_cards "
            "WHERE card_id=? AND state='pending'", (card["card_id"],)):
        try:
            cov = json.loads(r["coverage"] or "[]")
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(cov, list):
            covered.update(m for m in cov if positive(m))
    if not covered - set(shown):
        return list(shown)
    wanted = covered | {m for m in shown if positive(m)}
    # thread order, same query shape as the card face; ids outside
    # this card's thread/project never enter the plan
    return [r["message_id"] for r in db.execute(
        "SELECT message_id FROM messages WHERE (message_id=? OR "
        "parent_id=?) AND project_id=? ORDER BY posted_at_ts",
        (card["root_message_id"], card["root_message_id"],
         card["project_id"])) if r["message_id"] in wanted]


def _prior_remote_ids(db, card) -> dict:
    """part_id -> (remote id, payload sha256) of the newest delivered
    body chunk or attachment among this card's earlier renders. A
    chunk's text changes after the first post (the extraction result
    arrives, a reply joins the thread) and every update render re-plans
    the thread's attachments; the plan names the message that already
    carries each part so the worker rewrites a chunk in place and reuses
    an identical file instead of posting a second copy."""
    out: dict = {}
    for r in db.execute(
            """SELECT p.part_id, p.remote_id, p.payload_sha256
               FROM notification_render_parts p
               JOIN notification_renders r ON r.delivery_id=p.delivery_id
               WHERE r.card_id=?
                 AND p.kind IN ('body_part','attachment_part')
                 AND p.state='delivered' AND p.remote_id IS NOT NULL
               ORDER BY r.render_rev, p.idx""", (card["card_id"],)):
        out[r["part_id"]] = (str(r["remote_id"]), r["payload_sha256"])
    return out


def _build_part_manifest(db, card, spec, content, in_thread_body) -> None:
    """Seal the ordered delivery plan into the spec: the card is always
    part 0; a thread-bound render adds the thread, every body chunk and
    every covered attachment as individually journaled parts. An update
    names, per body chunk and unchanged attachment, the message that
    already carries it (``prior_remote_id``) so each stays one post."""
    parts = spec["parts"]
    card_payload = canonical({"containers": parts["containers"],
                              "footer": parts["footer"],
                              "action_rows": parts["action_rows"]})
    manifest = [{"part_id": "card", "kind": "card", "index": 0,
                 "sha256": hashlib.sha256(card_payload).hexdigest(),
                 "bytes": len(card_payload)}]
    if not in_thread_body or spec["op"] == "revoke":
        parts["manifest"] = manifest
        return
    idx = 1
    tname = parts.get("thread_name") or card["card_key"]
    manifest.append({"part_id": "thread", "kind": "thread",
                     "index": idx, "name": tname,
                     "sha256": _sha_text(tname)})
    idx += 1
    planned = _thread_plan_ids(db, card, content["shown"])
    man = {"shown": json.dumps(planned, ensure_ascii=False)}
    body = _card_body_text(db, card, man, max_chars=None)[1]
    chunks = _split_body_chunks(body)      # lossless — no chunk dropped
    attachments = _plan_attachments(db, planned)
    if len(chunks) + len(attachments) > MAX_PARTS - 2:
        # One shared budget includes card, thread and an explicit omission
        # marker. Unplanned downloaded attachments retain the existing
        # attachment-followup path after the card is delivered.
        chunks = chunks[:MAX_PARTS - 3]
        attachments = attachments[:MAX_PARTS - 3 - len(chunks)]
        chunks.append(_TRUNCATED_PART)
    parts["thread_body_parts"] = chunks
    prior = _prior_remote_ids(db, card) if spec["op"] == "update" else {}
    for i, chunk in enumerate(chunks):
        entry = {"part_id": f"body:{i + 1:04d}",
                 "kind": "body_part", "index": idx,
                 "sha256": _sha_text(chunk),
                 "bytes": len(chunk.encode("utf-8"))}
        if entry["part_id"] in prior:
            entry["prior_remote_id"] = prior[entry["part_id"]][0]
        manifest.append(entry)
        idx += 1
    for a in attachments:
        entry = {"part_id": f"attach:{a['attachment_id']:04d}",
                 "kind": "attachment_part", "index": idx, **a}
        rid, sha = prior.get(entry["part_id"], (None, None))
        if rid and a.get("sha256") and sha == a["sha256"]:
            # the same bytes are already in the thread — reuse, never
            # upload a second copy on every update render
            entry["prior_remote_id"] = rid
        manifest.append(entry)
        idx += 1
    parts["manifest"] = manifest


def _seed_parts(db, spec, now) -> None:
    """Persist the sealed plan rows in the same transaction as the
    render insert — restart-stable identities from the first write."""
    for p in spec["parts"]["manifest"]:
        unavailable = p.get("unavailable") is True
        db.execute(
            """INSERT INTO notification_render_parts(
                 delivery_id,part_id,kind,idx,payload_sha256,bytes,name,
                 attachment_id,state,error_code,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (spec["delivery_id"], p["part_id"], p["kind"], p["index"],
             p.get("sha256"), p.get("bytes"), p.get("name"),
             p.get("attachment_id"),
             "not_sent" if unavailable else "pending",
             "attachment_unavailable" if unavailable else None, now))


def _rollup_parts_state(db, delivery_id) -> str:
    """none / pending / complete / incomplete — complete iff the planned
    part-id set equals the delivered part-id set."""
    rows = db.execute(
        "SELECT state FROM notification_render_parts WHERE delivery_id=?",
        (delivery_id,)).fetchall()
    if not rows:
        return "none"
    states = {r["state"] for r in rows}
    if states <= {"delivered"}:
        return "complete"
    if states <= {"pending", "delivered"}:
        return "pending"      # still could complete — nothing failed
    return "incomplete"


def _update_parts_state(db, delivery_id, now) -> str:
    state = _rollup_parts_state(db, delivery_id)
    db.execute(
        "UPDATE notification_renders SET parts_state=?,updated_at=? "
        "WHERE delivery_id=?", (state, now, delivery_id))
    return state


def _render_gates(db, card_id, cfg):
    """Issuance preconditions: the card exists, belongs to the active
    transport (and for Slack, still matches the configured scope), has
    no unsettled attempt owning send rights, and is not frozen after a
    DB restore."""
    card = _card_row(db, card_id)
    if card is None:
        return None
    if card["transport"] != active_transport(cfg):
        return None
    if card["transport"] == "slack" \
            and not _scope_match(card, delivery_scope(cfg) or {}):
        return None
    if _unsettled_attempt(db, card_id):
        return None
    if _restore_hold_active(db, card_id=card_id):
        return None                        # post-restore freeze
    return card


def _parts_in_flight(db, latest) -> bool:
    """The card message itself is delivered but thread body chunks or
    attachments of that render are still pending. An update names the posts that
    already carry each part (``prior_remote_id``) — issued before those
    receipts land it would know none and post every chunk and file a
    second time — so it waits for the parts to settle and a later sweep
    issues it. A render that never reached the wire (queued) is not
    waited on, and neither are create/revoke renders."""
    if latest is None or latest["state"] != "delivered":
        return False
    return db.execute(
        "SELECT 1 FROM notification_render_parts WHERE delivery_id=? "
        "AND kind IN ('body_part','attachment_part') AND state='pending' "
        "LIMIT 1", (latest["delivery_id"],)).fetchone() is not None


def _generation_drift(card, content) -> dict:
    """Source/presentation fingerprint diff — the first observation
    seeds each baseline without a generation bump."""
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
    return gens


def _render_needed(db, card, latest, gens, cfg, now, force) -> bool:
    """Whether the display model owes a new render — cancelling a
    queued/held render whose content already went stale, honoring the
    update_failed suspension until a route_epoch bump or fresh drift
    re-opens it, and the revoke special case: a delivered card that is
    revoked owes Discord a delete (without this, archive-revoke left
    the message up forever), while an already-delivered revoke render
    must not re-issue."""
    epoch_changed = latest is not None and latest["route_epoch"] != route_epoch(cfg)
    live = latest is not None and latest["state"] in LIVE_RENDER
    if live and latest["state"] in ("queued", "held") and (gens or epoch_changed):
        # a not-yet-sent render whose content is already stale is
        # cancelled — a sending/unknown render keeps its attempt's
        # exclusivity instead (guarded above)
        _cancel_render(db, latest["delivery_id"], now)
        live = False
    if live:
        return False                       # in-flight render is current
    # update_failed suspends the not_sent/cancelled auto-retry — but a
    # route_epoch bump (config changed) or fresh drift re-opens it
    suspended = (card["delivery_state"] == "update_failed"
                 and latest is not None
                 and latest["route_epoch"] == route_epoch(cfg))
    unbound_intent = db.execute(
        "SELECT 1 FROM notification_intent_cards WHERE card_id=? "
        "AND state='pending' AND delivery_id IS NULL LIMIT 1",
        (card["card_id"],)).fetchone() is not None
    needed = force or bool(gens) or latest is None \
        or (not suspended and (unbound_intent or epoch_changed
                               or latest["state"] in ("not_sent", "cancelled")))
    if card["delivery_state"] == "revoked" and card["message_id"]:
        needed = not (latest is not None
                      and latest["op"] == "revoke"
                      and (latest["state"] == "delivered"
                           or (latest["state"] == "not_sent"
                               and not epoch_changed and not force
                               and _resend_exhausted(db, card["card_id"]))))
    return needed


def _render_op(card) -> str | None:
    """revoke for a revoked card that still has a Discord-side message;
    create when unbound; otherwise update the bound message."""
    if card["delivery_state"] == "revoked":
        if card["message_id"] is None:
            return None      # never delivered — nothing exists on
                             # Discord to delete
        return "revoke"
    if card["message_id"] is None:
        return "create"      # never bound, or unbound after a proven
    return "update"          # delete — re-post instead of patching a
                             # ghost


def _cancel_render(db, delivery_id, now):
    """Cancel one render and unbind the intents it was carrying."""
    db.execute("UPDATE notification_renders SET state='cancelled',"
               "updated_at=? WHERE delivery_id=?", (now, delivery_id))
    db.execute("UPDATE notification_intent_cards SET delivery_id=NULL,"
               "required_render_rev=0 WHERE delivery_id=?", (delivery_id,))


def _cancel_open_renders(db, card_id, now):
    """Cancel any leftover open renders of this card (defensive; the
    guards above mean at most stale queued/held rows can exist)."""
    for r in db.execute(
            "SELECT delivery_id FROM notification_renders WHERE card_id=?"
            " AND state IN ('queued','held')", (card_id,)).fetchall():
        _cancel_render(db, r["delivery_id"], now)


def _build_spec(db, card, content, gens, op, rev, cfg, now) -> dict:
    """Self-contained render spec: the pinned manifest row, bound
    delivery scope, minted action tokens and the journaled parts
    manifest — a worker never needs a registry/snapshot lookup to aim."""
    card.update(gens)
    cur = db.execute(
        """INSERT INTO notification_view_manifests(
             card_id,render_rev,source_generation,presentation_generation,
             digest,shown,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (card["card_id"], rev, card["source_generation"],
         card["presentation_generation"], 1 if card["kind"] == "digest" else 0,
         json.dumps(content["shown"], ensure_ascii=False), now))
    content["manifest_id"] = cur.lastrowid
    scope = (stored_scope(card)
             if card["transport"] == "slack"
             or (card["message_id"] and card["channel_id"])
             else (delivery_scope(cfg) or {}))
    event_ids = sorted(
        r["event_id"] for r in db.execute(
            "SELECT event_id FROM notification_intent_cards "
            "WHERE card_id=? AND state='pending'", (card["card_id"],)))
    correlation = secrets.token_hex(16)
    spec = {
        "schema": SLACK_RENDER_SCHEMA if card["transport"] == "slack" else RENDER_SCHEMA,
        "delivery_id": str(uuid.uuid4()),
        "logical_intent_id": card["card_key"],
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
    # failed/deleted threads can never carry the body — those cards
    # keep the 📄 button so the full text stays reachable. Slack honors
    # the same card_thread switch: its posted ts is the thread root, so
    # the body lands as channel-visible replies (T9), not an ephemeral
    # answer only the clicker can see.
    thread_on = notify_cfg(cfg).get("card_thread") is True
    in_thread_body = (thread_on
                      and card["thread_state"] not in ("failed", "deleted"))
    spec["parts"] = {
        "containers": content["containers"],
        "action_rows": _action_rows(db, card, content, now, context,
                                    in_thread_body=in_thread_body),
        "footer": content["footer"]
                  + [{"type": "meta", "correlation": correlation}],
        "manifest_id": content["manifest_id"],
        "page": content["page"], "pages": content["pages"],
        "context": context,
    }
    if any("<@" in (f.get("text") or "") for f in content["footer"]):
        # the footer names members as <@id> mentions — a worker must
        # render them without pinging; one that predates this key
        # rejects the spec (held) instead of sending live mentions
        spec["parts"]["mentions"] = "silent"
    if thread_on:
        spec["parts"]["thread_name"] = (_digest_thread_name()
                                        if card["kind"] == "digest"
                                        else _thread_name(db, card))
    # the body travels inside the spec as individually journaled
    # durable parts — the card stays a summary surface while the
    # thread carries the untruncated shown-set text (T7)
    _build_part_manifest(db, card, spec, content, in_thread_body)
    return spec


def _persist_render(db, card, spec, op, rev, now):
    """Durable writes for an issued render: the renders row, seeded
    parts journal, pending-intent binding and the card's generation /
    desired_rev bookkeeping."""
    db.execute(
        """INSERT INTO notification_renders(
             delivery_id,card_id,op,render_rev,manifest_id,route_epoch,
             profile,application_id,guild_id,channel_id,
             transport,team_id,
             spec_json,payload_hash,correlation,state,parts_state,
             created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued','pending',?,?)""",
        (spec["delivery_id"], card["card_id"], op, rev,
         spec["parts"]["manifest_id"],
         spec["delivery"]["route_epoch"],
         spec["delivery"].get("profile"),
         spec["delivery"].get("application_id"),
         spec["delivery"].get("guild_id"),
         spec["delivery"].get("channel_id"),
         card["transport"], spec["delivery"].get("team_id"),
         canonical(spec).decode(),
         payload_hash(spec), spec["delivery"]["correlation"], now, now))
    _seed_parts(db, spec, now)
    db.execute(
        """UPDATE notification_intent_cards
           SET delivery_id=?, required_render_rev=?
           WHERE card_id=? AND state='pending'""",
        (spec["delivery_id"], rev, card["card_id"]))
    db.execute(
        """UPDATE notification_cards SET source_generation=?,
             source_fp=?, presentation_generation=?, content_fp=?,
             desired_render_rev=?, updated_at=? WHERE card_id=?""",
        (card["source_generation"], card["source_fp"],
         card["presentation_generation"], card["content_fp"],
         rev, now, card["card_id"]))


def _issue_render(db, card_id, cfg, now, specs, force=False):
    """Issue the next render for a card when the display model demands
    one. Precondition per §4: no unsettled attempt may own the card —
    a granted/unknown attempt keeps exclusive send rights until its
    factual result lands, so issuance here never races an HTTP call."""
    card = _render_gates(db, card_id, cfg)
    if card is None:
        return None
    if card["kind"] in ("signal", "digest") and not signals_notify(cfg):
        # begin denies these anyway (signal_notify_off, not final), so a
        # queued render would sit live forever and block re-issue once
        # notify is back on — cancel it so re-enabling issues a fresh
        # delivery_id instead
        _cancel_open_renders(db, card_id, now)
        return None
    content = _card_content(db, card)
    gens = _generation_drift(card, content)
    latest = db.execute(
        "SELECT * FROM notification_renders WHERE card_id=? "
        "ORDER BY render_rev DESC LIMIT 1", (card_id,)).fetchone()
    if not _render_needed(db, card, latest, gens, cfg, now, force):
        return None                        # delivered/terminal & no drift
    op = _render_op(card)
    if op is None:
        return None
    if op == "update" and _parts_in_flight(db, latest):
        return None                        # a later sweep issues it
    rev = (latest["render_rev"] + 1) if latest is not None else 1
    _cancel_open_renders(db, card_id, now)
    spec = _build_spec(db, card, content, gens, op, rev, cfg, now)
    _persist_render(db, card, spec, op, rev, now)
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
    title = f"💬 {name} — {d}"
    return title if len(title) <= 100 else title[:99] + "…"


def _digest_thread_name() -> str:
    return f"💬 レビュー候補 — {time.strftime('%m-%d')}"


def _source_hash(db, mid, project_id) -> str | None:
    """Pinned content hash of a displayed source message — only a
    full-body message with a valid hash can anchor a render."""
    r = db.execute(
        "SELECT content_hash,body_state FROM messages "
        "WHERE message_id=? AND project_id=?", (mid, project_id)).fetchone()
    if r and r["body_state"] == "full" and valid_hash(r["content_hash"]):
        return r["content_hash"]
    return None


def _render_context(db, card) -> dict:
    """Modal-flow context pinned at render time — the plugin builds
    request.create/ops.signal_dismiss from THIS, so a confirmed command
    always refers to what the card actually showed (snapshot lag safe).

    - ``source_message_id``/``source_hash``: thread cards pin the root
      message; signal cards pin the representative signal's displayed
      evidence message (the same 最新言及 the user sees).
    - ``signals``: rendered signal_key -> artifact_id — dismiss's
      ``expected_signal_artifact_id`` must equal what was rendered.
    - ``extract_ref``: the extraction a ⚠ report would pin (see
      ``_extract_ref``).
    Digests span projects, so no card-level source pin is emitted."""
    ctx: dict = {}
    if card["kind"] == "thread":
        mid = card["root_message_id"]
        if positive(mid):
            ctx["project_id"] = card["project_id"]
            ctx["source_message_id"] = mid
            pin = _source_hash(db, mid, card["project_id"])
            if pin is not None:
                ctx["source_hash"] = pin
            ref = _extract_ref(db, card["project_id"], root=mid)
            if ref:
                ctx["extract_ref"] = ref
        return ctx
    keys = _anchor_keys(card)
    sigs = _latest_signals(db, keys, card["project_id"])
    if not sigs:
        return ctx
    ctx["signals"] = {k: {"artifact_id": s["artifact_id"],
                          "project_id": s["content"].get("project_id")}
                      for k, s in sigs.items()}
    if not positive(card["project_id"]):
        return ctx                       # digest — no card-level pin
    rep = sigs.get(keys[0]) or next(iter(sigs.values()))
    mid, message = _signal_evidence(db, rep["content"])
    if message is not None:
        ctx["project_id"] = card["project_id"]
        ctx["source_message_id"] = mid
        pin = _source_hash(db, mid, card["project_id"])
        if pin is not None:
            ctx["source_hash"] = pin
        ref = _extract_ref(db, card["project_id"], mid=mid)
        if ref:
            ctx["extract_ref"] = ref
    return ctx


def _extract_ref(db, project_id, *, root=None, mid=None) -> dict | None:
    """The extraction a ⚠ report pins: the newest message of the thread
    (or the signal's evidence message) whose current structured result
    is an extract_llm artifact — a message served by the v4 read model
    has no extract_llm result to re-run, so it offers no report."""
    where = ("(m.message_id=? OR m.parent_id=?)" if root is not None
             else "m.message_id=?")
    args = (root, root) if root is not None else (mid,)
    r = db.execute(
        f"""SELECT a.artifact_id, m.message_id, m.content_hash
            FROM messages m JOIN artifacts a
              ON a.message_id=m.message_id AND a.kind='extract_llm'
            WHERE {where} AND m.project_id=? AND m.body_state='full'
              {current_extract_pred('a', 'm')}
              AND {current_v4_id('m')} IS NULL
            ORDER BY m.posted_at_ts DESC, a.artifact_id DESC LIMIT 1""",
        (*args, project_id)).fetchone()
    if r is None or not valid_hash(r["content_hash"]):
        return None
    return {"message_id": r["message_id"], "artifact_id": r["artifact_id"],
            "content_hash": r["content_hash"]}


def task_form(db, card) -> dict:
    """Live data for the 📝 modal — the task text prefill (the first
    request the source message's extraction found) and the assignee
    choices. Returned with the modal-open result, never persisted."""
    form: dict = {"staff": assignee_choices(db)}
    ctx = _render_context(db, card)
    mid = ctx.get("source_message_id")
    m = db.execute(
        "SELECT message_id,project_id,body_text,body_state,content_hash "
        "FROM messages WHERE message_id=? AND project_id=?",
        (mid, card["project_id"])).fetchone() if positive(mid) else None
    if m is not None:
        from mcs_requests import candidates
        for c in candidates(db, m):
            hint = " ".join(str(c.get("suggestion") or "").split())
            if hint:
                form["hint"] = hint[:TASK_HINT_MAX]
                break
    return form


def assignee_choices(db, limit=STAFF_CHOICES) -> list:
    """Assignee options for a task — ``name（station）`` strings, the
    shape resolve_staff stores. The MCS station staff roster
    (station_staff_v1, fetched from MCS) is the source; without it the
    senders observed posting under our own stations (self profile) stand
    in. Empty -> the plugins fall back to free text. This is the single
    swap point for another roster source."""
    from mcs_queries import staff_directory
    from mcs_signals import _latest_self_profile, latest_station_staff
    out, seen = [], set()

    def add(name, station):
        name = " ".join(str(name or "").split())
        label = f"{name}（{station}）" if station else name
        if name and name not in seen and len(label) <= 75 \
                and len(out) < limit:
            seen.add(name)
            out.append(label)

    for s in latest_station_staff(db):
        add(s.get("name"), s.get("station"))
    if out:
        return out
    prof = _latest_self_profile(db)
    own = [" ".join(o.split()) for o in prof.get("organizations") or []
           if isinstance(o, str) and o.strip()]
    if not own:
        return []
    add(prof.get("name"), own[0])
    for r in staff_directory(db):
        stations = {" ".join(s.split())
                    for s in (r["organization"] or "").split(",")}
        match = next((o for o in own if o in stations), None)
        if match:
            add(r["sender_name"], match)
    return out


def _publish_specs(db, dirs, specs, now) -> list:
    """Atomic file publication AFTER the render commit. A crash between
    leaves spec_published=0; recovery republishes identical bytes."""
    published = []
    for spec in specs:
        transport = spec["delivery"].get("transport", "discord")
        path = publish_file(dirs[transport + "_render"],
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
    """MAX_RESEND consecutive real not_sents on a card suspends auto-retry —
    denied begins (error_code 'denied_*') are not send failures and never
    count. Drift, a route_epoch bump, or an operator resolve re-opens."""
    return db.execute(
        """SELECT COUNT(*) c FROM notification_delivery_attempts a
           JOIN notification_renders r ON r.delivery_id=a.delivery_id
           WHERE r.card_id=? AND a.state='not_sent'
             AND r.render_rev > COALESCE((
               SELECT MAX(done.render_rev) FROM notification_renders done
               JOIN notification_delivery_attempts sent
                 ON sent.delivery_id=done.delivery_id
               WHERE done.card_id=r.card_id AND sent.state='delivered'), 0)
             AND (a.error_code IS NULL
                  OR a.error_code NOT LIKE 'denied_%')""",
        (card_id,)).fetchone()["c"] >= MAX_RESEND


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
        # Cards carry text, while attachments retain the existing file
        # transport. Include files downloaded before card acceptance too.
        from ledger import enqueue_ready_attachment_followups_tx
        enqueue_ready_attachment_followups_tx(db, event_id, now)
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
                      if positive(m)})
        roots = {}
        for mid in ids:
            row = db.execute("SELECT project_id FROM messages WHERE message_id=?",
                             (mid,)).fetchone()
            if row is None or not positive(row["project_id"]) \
                    or (ev["project_id"] is not None
                        and row["project_id"] != ev["project_id"]):
                continue
            pid = row["project_id"]
            roots.setdefault((pid, _thread_root(db, mid, pid)), []).append(mid)
        out = []
        for (pid, root_mid), mids in roots.items():
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
        if ev["project_id"] is not None and ev["project_id"] != pid:
            return []
        return [{"card_key": f"v1|signal|{pid}|{keys[0]}",
                 "kind": "signal", "project_id": pid,
                 "coverage": keys, "anchor": {"signal_keys": keys}}]
    return []


def _thread_root(db, mid, project_id):
    cur, seen = mid, set()
    for _ in range(32):
        if cur is None or cur in seen:
            return mid
        seen.add(cur)
        r = db.execute("SELECT parent_id,project_id FROM messages "
                       "WHERE message_id=?", (cur,)).fetchone()
        if r is not None and r["project_id"] != project_id:
            return mid
        if r is None or r["parent_id"] is None:
            return cur
        cur = r["parent_id"]
    return cur or mid


def _reseat(db, event_id, now) -> None:
    """Push a still-pending card-owned intent's next re-examination out
    by RESEAT_S so it does not stay due (and hog flush slots) forever."""
    db.execute(
        "UPDATE notify_outbox SET next_try=?,updated_at=? "
        "WHERE event_id=?", (now + RESEAT_S, now, event_id))


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
            "SELECT * FROM notification_intent_batches "
            "WHERE event_id=?", (event_id,)).fetchone()
        if row["kind"] == "signal" and not signals_notify(cfg):
            if batch is None:
                db.execute(
                    "UPDATE notify_outbox SET state='suppressed',"
                    "next_try=NULL,updated_at=? WHERE event_id=?",
                    (now, event_id))
                mark_snapshot_dirty(db)
                return {"suppressed": True}
            # sealed with signals off: re-examine hourly, never every
            # flush — a due-forever intent would starve outbox_due slots
            _reseat(db, event_id, now)
            return {"skipped": True}
        if batch is not None:
            if interactive_enabled(cfg) and batch["transport"] != active_transport(cfg):
                return {"error": "transport_mismatch"}
            if batch["transport"] == "slack" \
                    and batch["scope_json"] != canonical(scope).decode():
                return {"error": "scope_mismatch"}
            # sealed already — a flush re-entry only repairs + completes
            if _complete_intent(db, event_id, now) is None:
                _reseat(db, event_id, now)
            specs.extend(
                json.loads(r["spec_json"])
                for r in db.execute(
                    """SELECT r.spec_json, r.delivery_id
                       FROM notification_renders r
                       JOIN notification_intent_cards ic
                         ON ic.delivery_id = r.delivery_id
                       WHERE ic.event_id=? AND r.state='queued'
                         AND (r.spec_published=0 OR r.spec_json IS NULL)""",
                    (event_id,)).fetchall()
                if r["spec_json"])
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
                "INSERT INTO notification_intent_batches("
                "event_id,frozen_payload,payload_hash,route_epoch,sealed_at,"
                "transport,scope_json) VALUES(?,?,?,?,?,?,?)",
                (event_id, canonical(frozen).decode(),
                 payload_hash(frozen), epoch, now, active_transport(cfg),
                 canonical(scope).decode() if active_transport(cfg) == "slack" else None))
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
                _reseat(db, event_id, now)
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
        "AND channel_id=? AND application_id=? AND transport=? "
        "AND (transport='discord' OR (profile=? AND team_id=?))",
        (mid, ch, app, origin.get("transport", "discord"),
         origin.get("profile"), origin.get("team_id"))).fetchone()
    return dict(row) if row else None


def _scope_match(card, origin) -> bool:
    origin = origin or {}
    transport = card["transport"]
    if transport != origin.get("transport", "discord"):
        return False
    if transport == "slack" and "guild_id" in origin:
        return False
    if transport == "slack":
        return all(card[k] and card[k] == origin.get(k)
                   for k in scope_fields(transport))
    return all(not card[k] or card[k] == origin.get(k)
               for k in scope_fields(transport))


def apply_notification(ledger, req, cfg, now=None) -> dict:
    """op='notification': a card action (page/ack/assign/defer). The
    token binds action+params+generations; the plugin's verified actor
    and the card's bound native scope are both re-checked here."""
    db = _db(ledger)
    now = time.time() if now is None else now
    command_id = req["command_id"]
    digest = payload_hash({k: v for k, v in req.items() if k != "request_id"})
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload_hash,receipt_json FROM command_receipts "
            "WHERE command_id=?", (command_id,)).fetchone()
        stored = None
        if old:
            stored = json.loads(old["receipt_json"])
            if old["payload_hash"] != digest:
                return {"outcome": "rejected", "error": "command_id_conflict"}
        # Idempotent writes retain their original result, but authorization
        # and view responses are checked against live state on every click.
        receipt = _apply_notification_tx(db, req, cfg, now, specs, replay=stored)
        if old is None:
            # 📄 is view-only and every click recomputes the text live
            # (replay skips "body"), so the durable audit row keeps the
            # outcome but never a copy of the source text — a copy would
            # outlive the source's deletion. The caller's result still
            # carries the body for this one delivery.
            # the 📝 form's prefill/staff list is the same kind of live
            # view data — recomputed per click, never persisted
            kept = {k: v for k, v in receipt.items()
                    if k not in ("body", "form", "list")}
            db.execute(
                "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                (command_id, digest, receipt.get("project_id"),
                 None, receipt["outcome"], canonical(kept).decode(), now))
        mark_snapshot_dirty(db)
    if specs:
        root = data_root(ledger)
        ensure_dirs(root)
        _publish_specs(db, notify_dirs(root), specs, now)
    return receipt


def _apply_notification_tx(db, req, cfg, now, specs, replay=None) -> dict:
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
    action = tok["action"]
    try:
        tok_params = json.loads(tok["params"] or "{}")
    except (json.JSONDecodeError, TypeError):
        tok_params = {}
    if card["message_id"] \
            and card["message_id"] != (origin or {}).get("message_id") \
            and not tok_params.get("ephemeral"):
        # a delivered card's buttons live on its bound message — a token
        # arriving with another message's origin is being replayed in a
        # context that never rendered this card. Tokens minted for an
        # ephemeral surface (the 📋 task list's transition buttons)
        # declare "ephemeral": their binding is the token itself plus
        # the channel/project scope checked above, not the followup
        # message that carried the click.
        return {**base, "outcome": "rejected", "error": "origin_mismatch"}
    if action in _WRITE_ACTIONS and (
            not interactive_enabled(cfg) or card["transport"] != active_transport(cfg)):
        return {**base, "outcome": "rejected", "error": "interactive_off"}
    if card["delivery_state"] == "revoked":
        return {**base, "outcome": "rejected", "error": "card_revoked"}
    if action in _WRITE_ACTIONS and (
            (tok["need_source_gen"] is not None
             and tok["need_source_gen"] != card["source_generation"])
            or (card["source_fp"] is not None
                and _source_fp(db, card) != card["source_fp"])):
        return {**base, "outcome": "rejected", "error": "stale_source",
                "hint": "refresh"}
    if replay is not None and action not in _LIVE_VIEWS:
        return replay
    if action in ("prev", "next"):
        return _act_page(db, base, card, tok, tok_params, cfg, now, specs)
    if action == "body":
        return _act_body(db, base, card, tok)
    if action == "summary":
        # view-only and live like 📄 — the text is never persisted
        title, text = patient_summary_text(db, card["project_id"])
        return {**base, "outcome": "applied", "action": "summary",
                "title": title, "body": text}
    if action == "mytasks":
        view = my_tasks_view(db, (req.get("input") or {}).get("name"), now)
        return {**base, "outcome": "applied", "action": "list", "list": view}
    if action == "unacked":
        view = unacked_view(db, card["transport"], now)
        return {**base, "outcome": "applied", "action": "list", "list": view}
    if action == "search":
        query = (req.get("input") or {}).get("query")
        if not query:
            # the click opens the keyword modal; its submit carries input
            return {**base, "outcome": "applied", "action": action,
                    "modal": True, "params": {}}
        view = patient_search_view(db, card["project_id"], query)
        return {**base, "outcome": "applied", "action": "list", "list": view}
    if action == "tasks":
        # live view — requests anchored to the thread's messages, plus a
        # fresh transition token per reachable status minted in the same
        # tx so the plugin can answer with an actionable ephemeral list
        items, token_ctx = _thread_tasks(db, card, tok, now)
        return {**base, "outcome": "applied", "action": "tasks",
                "tasks": items, "token_ctx": token_ctx}
    if action == "task_status":
        return _act_task_status(db, base, card, tok, tok_params, cfg,
                                now, specs)
    if action == "ack":
        return _act_ack(db, base, req, card, tok, cfg, now, specs)
    if action == "assign":
        return _act_assign(db, base, card, tok, actor, cfg, now, specs)
    if action == "defer":
        # 保留 was retired — a button on an already-posted card answers
        # and refreshes that card to the current button set
        _issue_render(db, card["card_id"], cfg, now, specs, force=True)
        return {**base, "outcome": "rejected", "error": "action_retired"}
    # request/dismiss tokens authorize the plugin-side modal — nothing
    # is applied here; the human command itself arrives separately as
    # request.create/ops.signal_dismiss with the full envelope. The
    # stored params go back to the caller so the plugin builds the modal
    # against what was rendered — user input never picks the target.
    if action in ("dismiss", "report"):
        return {**base, "outcome": "applied", "action": action,
                "modal": True, "params": tok_params}
    if action == "request":
        return {**base, "outcome": "applied", "action": action,
                "modal": True, "params": tok_params,
                "form": task_form(db, card)}
    return {**base, "outcome": "rejected",
            "error": "action_not_applicable"}


def _act_page(db, base, card, tok, tok_params, cfg, now, specs) -> dict:
    if tok["need_ui_rev"] is not None \
            and tok["need_ui_rev"] != card["ui_revision"]:
        return {**base, "outcome": "rejected", "error": "stale_ui",
                "hint": "refresh"}
    page = tok_params.get("page")
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


def _act_body(db, base, card, tok) -> dict:
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


def _act_task_status(db, base, card, tok, tok_params, cfg, now,
                     specs) -> dict:
    rid, to = tok_params.get("request_id"), tok_params.get("status")
    row = (db.execute(
        "SELECT * FROM requests WHERE request_id=? AND project_id=?",
        (rid, card["project_id"])).fetchone()
        if positive(rid) else None)
    if row is None or to not in ("in_progress", "done"):
        return {**base, "outcome": "rejected",
                "error": "request_not_found"}
    if tok["need_request_rev"] is not None \
            and tok["need_request_rev"] != row["revision"]:
        return {**base, "outcome": "rejected",
                "error": "stale_task", "hint": "tasks"}
    if row["status"] == to:
        return {**base, "outcome": "applied", "action": "task_status",
                "request_id": rid, "status": to, "absorbed": True,
                "title": row["title"]}
    if row["status"] not in ("open", "in_progress"):
        return {**base, "outcome": "rejected",
                "error": "request_not_open"}
    cur = db.execute(
        "UPDATE requests SET status=?,revision=revision+1,"
        "updated_at=? WHERE request_id=? AND revision=?",
        (to, now, rid, row["revision"]))
    if cur.rowcount != 1:
        # raced transition — answer against what actually landed
        again = db.execute(
            "SELECT status FROM requests WHERE request_id=?",
            (rid,)).fetchone()
        if again and again["status"] == to:
            return {**base, "outcome": "applied",
                    "action": "task_status", "request_id": rid,
                    "status": to, "absorbed": True,
                    "title": row["title"]}
        return {**base, "outcome": "rejected",
                "error": "stale_task", "hint": "tasks"}
    # the card footer lists open tasks — show the change now
    new_render = _issue_render(db, card["card_id"], cfg, now, specs)
    return {**base, "outcome": "applied", "action": "task_status",
            "request_id": rid, "status": to, "title": row["title"],
            "revision": row["revision"] + 1, "delivery_id": new_render}


def _act_ack(db, base, req, card, tok, cfg, now, specs) -> dict:
    mid = tok["need_manifest_id"]
    man = db.execute(
        "SELECT * FROM notification_view_manifests "
        "WHERE manifest_id=? AND card_id=?",
        (mid, card["card_id"])).fetchone()
    if man is None or man["invalidated"]:
        return {**base, "outcome": "rejected",
                "error": "manifest_invalid"}
    # toggle: an actor whose ack already covers this content (same
    # source generation + shown set) withdraws it — unless the ack is
    # newer than the button clicked, i.e. a double tap on the stale face
    mine = db.execute(
        """SELECT a.ack_id, a.created_at FROM notification_acknowledgements a
           JOIN notification_view_manifests m
             ON m.manifest_id=a.manifest_id
           WHERE a.card_id=? AND a.actor=? AND a.withdrawn_at IS NULL
             AND m.source_generation=? AND m.shown=?""",
        (card["card_id"], base["actor"], man["source_generation"],
         man["shown"])).fetchall()
    if any(r["created_at"] > tok["created_at"] for r in mine):
        return {**base, "outcome": "applied", "action": "ack",
                "absorbed": True, "manifest_id": mid}
    if mine:
        db.execute(
            "UPDATE notification_acknowledgements SET withdrawn_at=? "
            "WHERE ack_id IN (" + ",".join("?" * len(mine)) + ")",
            [now, *(r["ack_id"] for r in mine)])
        new_render = _issue_render(db, card["card_id"], cfg, now, specs)
        return {**base, "outcome": "applied", "action": "ack",
                "withdrawn": True, "manifest_id": mid,
                "delivery_id": new_render}
    db.execute(
        "INSERT INTO notification_acknowledgements("
        "card_id,manifest_id,actor,command_id,receipt_ref,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (card["card_id"], mid, base["actor"], req["command_id"],
         req["command_id"], now))
    shown = json.loads(man["shown"] or "[]")
    # the footer now shows the ack — re-render immediately like a
    # nav click, not at the next sweep (§2: applied state must show
    # in the card itself within the interaction budget)
    new_render = _issue_render(db, card["card_id"], cfg, now, specs)
    return {**base, "outcome": "applied", "action": "ack",
            "manifest_id": mid, "shown": shown,
            "delivery_id": new_render}


def _act_assign(db, base, card, tok, actor, cfg, now, specs) -> dict:
    tri = db.execute("SELECT state,owner,updated_at FROM notification_triage"
                     " WHERE card_id=?", (card["card_id"],)).fetchone()
    if tri and tri["state"] == "assigned" and tri["owner"] == actor:
        if (tri["updated_at"] or 0) > tok["created_at"]:
            # double tap on the face that predates this assignment
            return {**base, "outcome": "applied", "action": "assign",
                    "owner": actor, "absorbed": True}
        # the owner taps 担当中 — release back to open
        db.execute(
            "UPDATE notification_triage SET owner=NULL,state='open',"
            "revision=revision+1,last_actor=?,updated_at=? "
            "WHERE card_id=?", (actor, now, card["card_id"]))
        new_render = _issue_render(db, card["card_id"], cfg, now, specs)
        return {**base, "outcome": "applied", "action": "assign",
                "owner": None, "released": True, "delivery_id": new_render}
    # unassigned, or another member takes over
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


def _thread_tasks(db, card, tok, now):
    """The thread card's live task list — requests anchored to the
    thread's own messages — with one transition token per reachable
    status. Each token pins the request's revision so a stale view
    cannot overwrite a concurrent update."""
    if card["kind"] != "thread" or not positive(card["root_message_id"]) \
            or not positive(card["project_id"]):
        return [], {}
    rows = db.execute(
        """SELECT request_id,title,status,assignee,due_date,revision
           FROM requests WHERE project_id=? AND status!='cancelled'
           AND source_message_id IN (
             SELECT message_id FROM messages
             WHERE message_id=? OR parent_id=?)
           ORDER BY CASE status WHEN 'open' THEN 0
                    WHEN 'in_progress' THEN 1 ELSE 2 END,
                    due_date IS NULL, due_date, request_id
           LIMIT ?""",
        (card["project_id"], card["root_message_id"],
         card["root_message_id"], TASK_VIEW_LIMIT)).fetchall()
    need = {"source_gen": tok["need_source_gen"],
            "manifest_id": tok["need_manifest_id"],
            "ui_rev": tok["need_ui_rev"]}
    items, token_ctx = [], {}
    for r in rows:
        transitions = {}
        for to, label in (("in_progress", "⏳ 対応中"), ("done", "✅ 完了")):
            if r["status"] == "done" \
                    or (to == "in_progress" and r["status"] == "in_progress"):
                continue
            t = _mint_token(
                db, card["card_id"], "task_status",
                {"request_id": r["request_id"], "status": to,
                 "ephemeral": True},
                {**need, "request_rev": r["revision"]}, now)
            transitions[to] = {"token": t, "label": label}
            token_ctx[t] = {"action": "task_status",
                            "card_key": card["card_key"],
                            "kind": card["kind"],
                            "project_id": card["project_id"]}
        items.append({**dict(r), "transitions": transitions})
    return items, token_ctx


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
        elif not _scope_match(card, req.get("origin") or {}):
            receipt = {"kind": "refresh", "command_id": command_id,
                       "actor": req["actor"], "origin": req.get("origin"),
                       "outcome": "rejected", "error": "scope_mismatch",
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


def rerender_message_cards(ledger, cfg, project_id, message_id,
                           now=None) -> list:
    """Issue the next render of the live thread card holding a message
    (root or reply) — its footer shows a task/report that just landed.
    The drain's bounded sweep alone may not reach it among many cards."""
    if not positive(project_id) or not positive(message_id):
        return []
    db = _db(ledger)
    now = time.time() if now is None else now
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        root = _thread_root(db, message_id, project_id)
        for r in db.execute(
                "SELECT card_id FROM notification_cards WHERE kind='thread' "
                "AND project_id=? AND root_message_id=? "
                "AND delivery_state!='revoked'",
                (project_id, root)).fetchall():
            _issue_render(db, r["card_id"], cfg, now, specs)
        if specs:
            mark_snapshot_dirty(db)
    if specs:
        root_dir = data_root(ledger)
        ensure_dirs(root_dir)
        _publish_specs(db, notify_dirs(root_dir), specs, now)
    return [s["delivery_id"] for s in specs]


# reminders post inside this JST hour window only — a due-day reminder
# at 3 a.m. helps nobody; the next tick in the window sends it
REMINDER_HOURS = (8, 21)
# a few per tick — reminders share notify_flush's per-tick budget with
# new-message notices and must never delay them
REMINDER_LIMIT = 3


def task_reminders(ledger, cfg, now=None, limit=REMINDER_LIMIT) -> int:
    """⏰ due-day and ⚠ overdue reminders for open tasks linked to a live
    thread card: one notify_outbox text notice per (task, stage,
    due_date) — the notification_task_reminders row is the once-only
    marker, set in the same transaction, so changing a due date re-arms.
    A task already past due when first seen gets only the overdue
    reminder. The first run with the interactive switch on records every
    already-overdue task without sending (baseline) — only tasks that
    fall due afterwards are announced. Off with the interactive switch
    and outside REMINDER_HOURS; at most ``limit`` per call."""
    if not interactive_enabled(cfg):
        return 0
    from datetime import datetime
    from mcs_queries import JST
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, JST)
    today = local.date().isoformat()
    db = _db(ledger)
    pending = """FROM requests r
               JOIN messages m ON m.message_id=r.source_message_id
                AND m.project_id=r.project_id
               WHERE r.status IN ('open','in_progress')
                 AND r.due_date IS NOT NULL AND r.due_date<=?
                 AND EXISTS (SELECT 1 FROM notification_cards c
                     WHERE c.kind='thread' AND c.project_id=r.project_id
                       AND c.transport=? AND c.delivery_state!='revoked'
                       AND c.root_message_id IN (m.message_id, m.parent_id))
                 AND NOT EXISTS (SELECT 1
                     FROM notification_task_reminders t
                     WHERE t.request_id=r.request_id
                       AND t.due_date=r.due_date
                       AND t.stage=CASE WHEN r.due_date=? THEN 'due'
                                        ELSE 'overdue' END)"""
    args = (today, active_transport(cfg), today)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM notification_task_reminders"
                      " WHERE stage='baseline'").fetchone() is None:
            db.execute(
                "INSERT INTO notification_task_reminders(request_id,"
                "stage,due_date,event_id,created_at) SELECT r.request_id,"
                f"'overdue',r.due_date,NULL,? {pending}"
                " AND r.due_date<?", (now, *args, today))
            db.execute(
                "INSERT INTO notification_task_reminders VALUES"
                "(0,'baseline',?,NULL,?)", (today, now))
    if not REMINDER_HOURS[0] <= local.hour < REMINDER_HOURS[1]:
        return 0
    sent = 0
    with db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            f"""SELECT r.request_id, r.project_id, r.title, r.assignee,
                      r.due_date {pending}
               ORDER BY r.due_date, r.request_id LIMIT ?""",
            (*args, limit)).fetchall()
        for r in rows:
            stage = "due" if r["due_date"] == today else "overdue"
            head = (f"⏰ 期限リマインド（本日 {r['due_date']}）" if stage == "due"
                    else f"⚠ 期限切れ（期限 {r['due_date']}）")
            name = _patient_name(db, r["project_id"]) \
                or f"project {r['project_id']}"
            text = (f"{head} — {_plain(name)}: {_plain(r['title'])}"
                    f" — 担当 {_plain(r['assignee'] or '未設定')}")
            eid = ledger.outbox_add_tx(
                "task_reminder", r["project_id"],
                {"text": text, "request_id": r["request_id"],
                 "stage": stage})
            db.execute(
                "INSERT INTO notification_task_reminders(request_id,"
                "stage,due_date,event_id,created_at) VALUES(?,?,?,?,?)",
                (r["request_id"], stage, r["due_date"], eid, now))
            sent += 1
    return sent


def _plain(text) -> str:
    """Frozen notice text — one line, no mention/broadcast syntax."""
    t = " ".join(str(text or "").split())[:200]
    return (t.replace("<", "＜").replace(">", "＞")
            .replace("@", "＠"))


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
    _cancel_open_renders(db, card_id, now)
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
                                        'message_deleted','revoked')
               ORDER BY updated_at,card_id LIMIT ?""", (limit,)).fetchall()
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
            # Rotate unchanged and in-flight cards too. Otherwise the first
            # limit stable cards permanently starve every newer card.
            db.execute("UPDATE notification_cards SET updated_at=? WHERE card_id=?",
                       (now, card["card_id"]))
        # watchdog: live renders whose spec never published (or whose
        # file vanished) get their stored bytes republished
        specs.extend(
            json.loads(r["spec_json"])
            for r in db.execute(
                "SELECT delivery_id,spec_json FROM notification_renders "
                "WHERE state='queued' AND spec_published=0").fetchall()
            if r["spec_json"])
        if specs:
            mark_snapshot_dirty(db)
    _publish_specs(db, dirs, specs, now)
    return {"scanned": scanned, "updated": updated,
            "republished": len(specs)}


def gc(ledger, cfg=None, now=None, limit=500) -> dict:
    """Delete expired action tokens and strip PHI-bearing spec_json
    (containers and thread_body_parts) once nothing can replay it:
    superseded renders (cancelled/not_sent with a delivered successor)
    and delivered renders whose durable parts are all settled and no
    active restore hold covers the render or its card — in both cases
    only with no unsettled attempt on the card and no pending intent
    binding. Every DB reader of spec_json (dispatch re-entry, sweep
    watchdog, recover) republishes queued/held renders only, and a
    delivered render never returns to either; the worker and reconcile
    read the spec file, which a pending part plan keeps. Rows, hashes,
    correlations and part metadata stay for audit. Bounded per call —
    the rest waits for the next tick."""
    db = _db(ledger)
    now = time.time() if now is None else now
    with db:
        tokens = db.execute(
            "DELETE FROM notification_action_tokens WHERE expires_at<?",
            (now,)).rowcount
        cleared = 0
        for r in db.execute(
                """SELECT r.delivery_id FROM notification_renders r
                   WHERE r.spec_json IS NOT NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM notification_delivery_attempts a
                       JOIN notification_renders active
                         ON active.delivery_id=a.delivery_id
                       WHERE active.card_id=r.card_id
                         AND a.state IN ('granted','unknown'))
                     AND NOT EXISTS (
                       SELECT 1 FROM notification_intent_cards ic
                       WHERE ic.delivery_id=r.delivery_id AND ic.state='pending')
                     AND ((r.state IN ('cancelled','not_sent')
                           AND EXISTS (
                             SELECT 1 FROM notification_renders successor
                             WHERE successor.card_id=r.card_id
                               AND successor.render_rev>r.render_rev
                               AND successor.state='delivered'))
                          OR (r.state='delivered'
                              AND NOT EXISTS (
                                SELECT 1 FROM notification_render_parts p
                                WHERE p.delivery_id=r.delivery_id
                                  AND p.state='pending')
                              AND NOT EXISTS (
                                SELECT 1 FROM notification_restore_holds h
                                WHERE h.released_at IS NULL
                                  AND (h.delivery_id=r.delivery_id
                                       OR h.card_id=r.card_id))))
                   ORDER BY r.updated_at LIMIT ?""", (limit,)).fetchall():
            db.execute(
                "UPDATE notification_renders SET spec_json=NULL,"
                "updated_at=? WHERE delivery_id=?",
                (now, r["delivery_id"]))
            cleared += 1
        # spec files for terminal renders can go — a begin against them
        # would be denied anyway; unknown stays for investigation. A
        # render whose durable parts are still pending keeps its spec:
        # the worker never reads the DB, so the file is the only copy
        # its restart-resume can replay — removing it would strand
        # parts the journal could still finish.
        removed = 0
        root = data_root(ledger)
        dirs = notify_dirs(root)
        for r in db.execute(
                "SELECT delivery_id,transport FROM notification_renders "
                "WHERE state IN ('delivered','not_sent','cancelled') "
                "AND spec_published=1 AND parts_state!='pending' "
                "ORDER BY updated_at LIMIT ?", (limit,)).fetchall():
            path = os.path.join(dirs[r["transport"] + "_render"],
                                r["delivery_id"] + ".json")
            try:
                os.unlink(path)
                removed += 1
            except FileNotFoundError:
                pass
            except OSError:
                continue
            # Terminal renders are never republished. Remember successful
            # cleanup so a bounded GC can reach older files on later ticks.
            db.execute("UPDATE notification_renders SET spec_published=0 "
                       "WHERE delivery_id=?", (r["delivery_id"],))
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
            with suppress(OSError):
                if now - os.stat(path).st_mtime > TOKEN_WRITE_S:
                    os.unlink(path)
                    results += 1
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
                "SELECT delivery_id,spec_json,transport FROM notification_renders "
                "WHERE state IN ('queued','held')").fetchall():
            path = os.path.join(dirs[r["transport"] + "_render"],
                                r["delivery_id"] + ".json")
            if os.path.isfile(path):
                db.execute(
                    "UPDATE notification_renders SET spec_published=1 "
                    "WHERE delivery_id=?", (r["delivery_id"],))
            elif r["spec_json"]:
                publish_file(dirs[r["transport"] + "_render"],
                             r["delivery_id"] + ".json",
                             r["spec_json"].encode("utf-8"))
                db.execute(
                    "UPDATE notification_renders SET spec_published=1,"
                    "first_published_at=COALESCE(first_published_at,?),"
                    "updated_at=? WHERE delivery_id=?",
                    (now, now, r["delivery_id"]))
                fixed["republished"] += 1
        try:
            claimed = [os.path.join(dirs[t + "_render"], n)
                       for t in ("discord", "slack")
                       for n in os.listdir(dirs[t + "_render"])
                       if n.endswith(".json.claimed")]
        except OSError:
            claimed = []
        stale = 0
        for n in claimed:
            with suppress(OSError):
                if now - os.stat(n).st_mtime > 60:
                    stale += 1
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
