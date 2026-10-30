"""Interactive notification cards — delivery ledger and neutral render specs.

This module owns card intents, transactional render issuance
and maintenance in the interactive notification pipeline (plan: .omo/plans/discord-interactive-cards.md):

- notify_outbox intents routed 'interactive' are FROZEN into sealed
  batches, fanned out to durable cards (1 intent -> N cards), and
  rendered as immutable, neutral specs published atomically to
  data/discord_render/<delivery_id>.json.
- notify_actions owns commands/checkpoints, notify_action_tokens owns tokens,
  and notify_action_context owns read-only source pins and task prefill.
- notify_parts owns sealed part planning/state on the borrowed transaction;
  notify_scope owns config/scope and health reads; notify_render owns display.
- notify_spool owns file publication and restore gates after committed issuance.
- notify_transport owns delivery grants and factual receipt settlement;
  notify_recovery owns human-confirmed operator recovery.
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
import notify_scope
import notify_spool
import notify_history

import hashlib
import json
import os
import secrets
import time
import uuid
from contextlib import suppress

from mcs_contract import project_url
from mcs_queries import HOLD_PROGRESS_SET
from mcs_requests import canonical, payload_hash, positive
from mcs_util import loads_dict, valid_hash
from notify_render import (
    anchor_keys, card_content, content_fingerprint,
    mmdd, patient_name, preview_header, preview_line, signal_evidence,
    fit_parts,
    own_post, plain_notice, presenter, reply_source)
import notify_parts
import notify_action_context as action_context
import notify_action_tokens as action_tokens
from notify_views import drug_search_available


# outbox kinds that become interactive cards when notify.interactive is
# on; everything else (ops alerts, semantic notices) stays legacy text.
# kinds delivered as one card-less ``op=notice`` render (no card row,
# manifest, tokens or thread) — see _dispatch_notice
NOTICE_KINDS = frozenset({"daily_digest"})

# renders whose spec file must be available to a claiming worker
LIVE_RENDER = ("queued", "sending", "unknown", "held")

RESEAT_S = 3600           # re-examine a dispatched pending intent hourly
MAX_RESEND = 3            # consecutive not_sent attempts before a card
                          # suspends auto-retry (update_failed)

LINEWORKS_SUMMARY_WAIT_S = 120

# mirrored from adapters/common/spec (core never imports adapters);
# tests/notify/test_card_extras.py pins them equal
MAX_COMPONENTS = 40           # the worker's per-card component ceiling
MAX_BUTTONS = 5              # spec.MAX_BUTTONS
MAX_ROWS = 5                 # spec.MAX_ROWS
MAX_POST_ACTIONS = 5          # spec.MAX_POST_ACTIONS
# Card face layout stamped at creation: existing cards keep layout 1/2 (no
# fingerprint change, no mass re-render / LINE WORKS re-post); cards
# created from now on use the current one (owner decision 2026-10-08).
# Layout 4 introduced reply cards (notify.reply_cards): the root card shows
# the root post only and each later reply gets its own card in the root's
# thread. Layout 5 retains that format and adds initial-history summaries.
# CARD_LAYOUT is the newest layout this code can write; rollback checks the
# stored layout on the DB selected by the updater's schema/backup contract.
CARD_LAYOUT = 5
FACE_LAYOUT = 3               # every card that is not a reply-card thread


# ---------- borrowed card connection ----------

def connection(ledger):
    """Borrow the caller-owned SQLite connection without opening or closing it."""
    return getattr(ledger, "db", ledger)


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
    with publish_snapshot and must not change. Each mark bumps a
    generation, so a mark landing during a publish survives its clear."""
    db.execute(
        "INSERT INTO notification_meta(singleton,notify_dirty) "
        "VALUES(1,1) ON CONFLICT(singleton) DO UPDATE SET notify_dirty=notify_dirty+1")


def snapshot_dirty(ledger) -> int:
    """The dirty generation (0 = clean); pass it to clear_snapshot_dirty."""
    row = connection(ledger).execute(
        "SELECT notify_dirty FROM notification_meta WHERE singleton=1"
    ).fetchone()
    return int(row["notify_dirty"]) if row and row["notify_dirty"] else 0


def clear_snapshot_dirty(ledger, generation: int) -> None:
    """Clear only the generation read before the publish began."""
    connection(ledger).execute(
        "UPDATE notification_meta SET notify_dirty=0 "
        "WHERE singleton=1 AND notify_dirty=?", (generation,))
    connection(ledger).commit()


# ---------- cards / renders ----------

def unsettled_attempt(db, card_id: int):
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
        if r["transport"] in ("slack", "lineworks") and not notify_scope.scope_matches(r, scope):
            continue
        if keyset & set(anchor_keys(r)):
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
            prior_keys = anchor_keys(row)
            merged = list(dict.fromkeys(
                prior_keys + target["anchor"]["signal_keys"]))
            if merged != prior_keys:
                db.execute("UPDATE notification_cards SET anchor_key=?,"
                           "updated_at=? WHERE card_id=?",
                           (json.dumps({"signal_keys": merged},
                                       ensure_ascii=False), now, found))
            return found
    key = _scoped_card_key(target["card_key"], scope)
    row = db.execute("SELECT card_id,project_id,delivery_state "
                     "FROM notification_cards WHERE card_key=?",
                     (key,)).fetchone()
    if row is not None and row["delivery_state"] == "revoked" and not (
            positive(row["project_id"]) and db.execute(
                "SELECT 1 FROM patients WHERE project_id=? "
                "AND is_archived=1", (row["project_id"],)).fetchone()):
        # a revocation stands only until a new intent arrives for a
        # reactivated unit: retire the revoked card's key and mint a
        # fresh card, which is never revoked and renders a new post
        db.execute("UPDATE notification_cards SET card_key=card_key"
                   "||'|revoked:'||card_id WHERE card_id=?",
                   (row["card_id"],))
        row = None
    if row is not None:
        return row["card_id"]
    cur = db.execute(
        """INSERT INTO notification_cards(
             card_key,kind,project_id,root_message_id,anchor_key,
             profile,application_id,guild_id,channel_id,
             transport,team_id,
             ui_state,created_at,updated_at,layout)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, target["kind"], target.get("project_id"),
         target.get("root_message_id"),
         json.dumps(target["anchor"], ensure_ascii=False,
                    sort_keys=True),
         scope.get("profile"), scope.get("application_id"),
         scope.get("guild_id"), scope.get("channel_id"),
         scope.get("transport", "discord"), scope.get("team_id"),
         "{}", now, now,
         CARD_LAYOUT if target.get("reply_layout") or target["kind"] == "patient_history" else min(CARD_LAYOUT, FACE_LAYOUT)))
    return cur.lastrowid


def _scoped_card_key(key, scope):
    if scope.get("transport") in ("slack", "lineworks"):
        transport = scope["transport"]
        version = notify_scope.TRANSPORT_VERSIONS[transport]
        key = f"v{version}|{transport}|{payload_hash(scope)}|{key.removeprefix('v1|')}"
    return key


# LINE WORKS has no native thread to hold a reply card; it keeps collecting
REPLY_CARD_TRANSPORTS = ("slack", "discord")
# on unless notify.reply_cards is false (owner decision 2026-10-23)
REPLY_CARDS_DEFAULT = True


def reply_cards_on(cfg) -> bool:
    """Reply cards (layout 4) for Slack/Discord with card threads, unless switched off."""
    # reply cards live in the root card's thread: without card_thread no
    # thread is ever opened and every reply would wait, then hit the channel
    return (notify_scope.active_transport(cfg) in REPLY_CARD_TRANSPORTS
            and notify_scope.notify_cfg(cfg).get("reply_cards", REPLY_CARDS_DEFAULT) is True
            and notify_scope.notify_cfg(cfg).get("card_thread") is True)


def _split_reply_targets(db, targets, scope, cfg):
    """Give each later reply of a layout-4 root its own card in the root's thread.

    A root whose card already exists below layout 4 keeps collecting its
    replies (no re-post of history, no double listing); the decision is the
    stored layout, never a date. The root target keeps the whole coverage so
    intent completion and the root-thread attachment route stay unchanged."""
    if notify_scope.active_transport(cfg) not in REPLY_CARD_TRANSPORTS:
        return targets
    on = reply_cards_on(cfg)
    out = []
    for target in targets:
        if target.get("kind") != "thread" or "|reply:" in target["card_key"]:
            out.append(target)
            continue
        row = db.execute("SELECT layout FROM notification_cards WHERE card_key=?",
                         (_scoped_card_key(target["card_key"], scope),)).fetchone()
        # the stored layout decides an existing root (a layout-4 root shows no
        # replies itself, even after the switch is turned off); the switch
        # only decides whether a new root is created as layout 4
        if (row["layout"] or 1) < 4 if row is not None else not on:
            out.append(target)
            continue
        root = target["root_message_id"]
        out.append({**target, "reply_layout": True})
        for mid in target["coverage"]:
            if mid != root:
                out.append({"card_key": f"{target['card_key']}|reply:{mid}",
                            "kind": "thread", "project_id": target["project_id"],
                            "root_message_id": root, "coverage": [mid],
                            "anchor": {"root": root, "source": mid},
                            "reply_layout": True})
    return out


def _stamp_enabled(cfg) -> bool:
    """Shared-card gate only; the clicking operator is re-checked on click and confirm."""
    try:
        import stamp_write
        return stamp_write.enabled(cfg) is True
    except Exception:                      # absent or broken gate: no write button
        return False


def _stamp_target(db, card, content):
    """(latest shown post, the actor's own stamp as last observed or None) for
    MCS's own stamp row, or None when the card shows no post to stamp."""
    import stamp_write
    from message_observations import get_message_metadata
    shown = [m for m in content.get("shown") or [] if positive(m)]
    if not shown:
        return None
    latest = db.execute(
        "SELECT message_id FROM messages WHERE project_id=? AND body_state='full' "
        "AND (message_id=? OR parent_id=?) AND message_id IN (%s) "
        "ORDER BY posted_at DESC, message_id DESC LIMIT 1" % ",".join("?" * len(shown)),
        (card["project_id"], card["root_message_id"], card["root_message_id"], *shown)).fetchone()
    if latest is None:
        return None
    try:
        own = stamp_write._own(get_message_metadata(db, latest[0]))
        mine = own["type"] if own["state"] == "observed" else None
    except Exception:              # unknown is shown as unpressed; the press re-checks MCS state
        mine = None
    return latest[0], mine


def _action_rows(db, card, content, now, context=None,
                 in_thread_body=False, reserve=0, cfg=None, mint=True):
    """Button rows for a render; every button carries a fresh token.
    Buttons whose modal flow cannot pin a source (no context) are not
    emitted — a button that can never succeed is worse than none."""
    if card["kind"] == "patient_history":
        return notify_history.actions(card) if card["delivery_state"] != "revoked" else []
    if card["delivery_state"] == "revoked":
        return []                          # ⛔ 取り下げ済み: nothing to press
    kind = card["kind"]
    keys = anchor_keys(card)
    context = context or {}
    rows, row = [], []
    need = {"source_gen": card["source_generation"],
            "manifest_id": content["manifest_id"],
            "ui_rev": card["ui_revision"]}
    # labels name the action and never change; the footer states who
    # confirmed / owns the card — style is only a secondary cue
    acked = content["toggles"]["acked"]
    assigned = content["toggles"]["assigned"]
    ack_label = "このページを確認する" if kind == "digest" else None

    def btn(action, params=None, label=None, style=None):
        if len(row) == MAX_BUTTONS:
            flush()
        # mint=False measures the same plan without writing tokens
        tok = (action_tokens.mint(db, card["card_id"], action, params, need, now)
               if mint else None)
        b = {"id": action, "ui": "button",
             "style": style or action_tokens.ACTIONS[action][1],
             "label": label or action_tokens.ACTIONS[action][0], "token": tok}
        row.append(b)

    def flush():
        if row:
            rows.append(list(row))
            row.clear()

    # row 1 — primary: 確認する · 担当する · タスク作成 · MCSで開く
    btn("ack", {"shown_kind": content["shown_kind"]}, label=ack_label,
        style="success" if acked else None)
    btn("assign", style="primary" if assigned else None)
    pid = card["project_id"]
    if positive(pid) and positive(context.get("source_message_id")) \
            and context.get("source_hash"):
        # a digest spans projects — a card-level request cannot pin a
        # single source, so the button is only emitted where it can work
        btn("request", {"project_id": pid})
    if positive(pid):
        # a plain link: no token, no runner round-trip
        row.append({"id": "link", "ui": "link", "label": "MCSで開く",
                    "url": project_url(pid)})
    if presenter(card["transport"]).MORE_BUTTON:
        btn("more")
    flush()
    # row 2 — secondary (a select / the LINE WORKS 1:1 menu)
    if not in_thread_body:
        # cards with a companion thread show the body inside it on
        # delivery — 本文表示 only remains where no thread can carry it
        # (card_thread off, failed/deleted thread)
        btn("body")
    if content["toggles"]["has_tasks"]:
        # only while the thread has open tasks — the list view is the
        # thread-anchored _thread_tasks (signal/digest cards span
        # messages/projects the requests table does not key on)
        btn("tasks")
    if positive(pid):
        btn("summary")
        btn("search")
    if context.get("extract_ref"):
        btn("report")
    if kind == "signal" and len(keys) == 1 \
            and keys[0] in (context.get("signals") or {}):
        btn("dismiss", {"signal_key": keys[0]})
    if kind == "thread" and positive(pid) and _stamp_enabled(cfg) \
            and not (own_post(card) and len(content["shown"]) == 1):
        btn("stamp")
    flush()
    # row 2b — 💊 medication check / dictionary search (read-only views)
    if positive(pid) and (in_thread_body
                          or not presenter(card["transport"]).DRUG_ROW_NEEDS_THREAD):
        meds_mid = action_context.meds_message(db, card, context)
        if meds_mid is not None:
            btn("meds", {"message_id": meds_mid})
        if drug_search_available(db):
            btn("drugsearch")
    flush()
    # row 3 — paging
    if content["pages"] > 1:
        # only mint buttons that can actually move — a dead nav button
        # always comes back bad_page
        if content["page"] > 0:
            btn("prev", {"page": content["page"] - 1})
        if content["page"] + 1 < content["pages"]:
            btn("next", {"page": content["page"] + 1})
    flush()
    # row 1b — MCS's own stamp row for the latest post: one press sends.
    # Optional: placed under the primary row only when every other row is
    # already planned and both the row and component ceilings still hold.
    target = (_stamp_target(db, card, content)
              if kind == "thread" and positive(pid) and _stamp_enabled(cfg) else None)
    if target is not None and rows and len(rows) < MAX_ROWS:
        from stamp_contract import REACTION_LABELS, STAMP_EMOJI
        used = (sum(c["type"] != "meta" for c in content["containers"])
                + sum(f["type"] == "text" for f in content["footer"])
                + sum(len(r) + 1 for r in rows))
        if used + len(REACTION_LABELS) + 1 <= MAX_COMPONENTS - reserve:
            mid, mine = target
            for kind_, name in REACTION_LABELS.items():
                # the render binds add/cancel and the own stamp it showed;
                # a press after the own stamp changed is refused, never flipped
                btn("stamp_press", {"message_id": mid, "kind": kind_,
                                    "op": "cancel" if kind_ == mine else "add", "own": mine},
                    label=f"{STAMP_EMOJI[kind_]}{name}" + ("✓" if kind_ == mine else ""),
                    style="primary" if kind_ == mine else None)
            rows.insert(1, list(row))
            row.clear()
    # digest-only lists (ephemeral answers). Optional: only
    # as many as the worker's component ceiling still allows — a spec
    # over budget is rejected whole and the card would not render at all
    if kind == "digest":
        used = (sum(c["type"] != "meta" for c in content["containers"])
                + sum(f["type"] == "text" for f in content["footer"])
                + sum(len(r) + 1 for r in rows))
        for action in ["digest", "mytasks", "unacked"][:max(0, MAX_COMPONENTS - used - 1 - reserve)]:
            btn(action)
        flush()
    return rows


# ---------- transactional render issuance -----------------------------

def _render_gates(db, card_id, cfg):
    """Issuance preconditions: the card exists, belongs to the active
    transport (and for Slack, still matches the configured scope), has
    no unsettled attempt owning send rights, and is not frozen after a
    DB restore."""
    card = notify_scope.card_row(db, card_id)
    if card is None:
        return None
    if card["transport"] != notify_scope.active_transport(cfg):
        return None
    if card["transport"] in ("slack", "lineworks") \
            and not notify_scope.scope_matches(card, notify_scope.delivery_scope(cfg) or {}):
        return None
    if unsettled_attempt(db, card_id):
        return None
    if _restore_hold_active(db, card_id=card_id):
        return None                        # post-restore freeze
    return card


def _parts_in_flight(db, latest) -> bool:
    """The card is delivered but its thread identity is unknown, or body
    chunks/attachments are pending or unknown. An update names the posts that
    already carry each part (``prior_remote_id``) — issued before those
    receipts land it would know none and post every chunk and file a
    second time — so it waits for the parts to settle and a later sweep
    issues it. A render that never reached the wire (queued) is not
    waited on, and neither are create/revoke renders."""
    if latest is None or latest["state"] != "delivered":
        return False
    return db.execute(
        "SELECT 1 FROM notification_render_parts WHERE delivery_id=? "
        "AND ((kind IN ('body_part','attachment_part') "
        "AND state IN ('pending','unknown')) OR (kind='thread' AND state='unknown')) "
        "LIMIT 1", (latest["delivery_id"],)).fetchone() is not None


def _presentation_fp(card, content, cfg=None) -> str:
    """The stamp button is presentation: only an enabled gate changes the
    fingerprint, so default-off cards keep the stored value (no mass re-render)."""
    fp = content_fingerprint(content)
    if _stamp_enabled(cfg) and card["kind"] == "thread" and positive(card["project_id"]):
        fp = hashlib.sha256(f"{fp}:stamp".encode()).hexdigest()
    return fp


def _generation_drift(card, content, cfg=None) -> dict:
    """Source/presentation fingerprint diff — the first observation
    seeds each baseline without a generation bump."""
    pres_fp = _presentation_fp(card, content, cfg)
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


def _ready_intent_ids(db, card, *, unbound=False, waiting=frozenset()):
    """Only bind an intent when every covered post is outside its summary wait."""
    rows = db.execute("SELECT event_id,coverage FROM notification_intent_cards "
                      "WHERE card_id=? AND state='pending'" +
                      (" AND delivery_id IS NULL" if unbound else ""), (card["card_id"],))
    ready = []
    for row in rows:
        try:
            coverage = json.loads(row["coverage"])
        except (ValueError, TypeError, RecursionError):
            continue  # damaged coverage is never proof that an intent was delivered
        if not isinstance(coverage, list) or not coverage:
            continue
        if card["kind"] == "thread":
            if any(not positive(mid) for mid in coverage):
                continue
        elif any(not isinstance(key, str) or not key for key in coverage):
            continue
        if not waiting.intersection(coverage):
            ready.append(row["event_id"])
    return sorted(ready)


def _render_needed(db, card, latest, gens, cfg, now, force, waiting=frozenset()) -> bool:
    """Whether the display model owes a new render — cancelling a
    queued/held render whose content already went stale, honoring the
    update_failed suspension until a route_epoch bump or fresh drift
    re-opens it, and the revoke special case: a delivered card that is
    revoked owes Discord a delete (without this, archive-revoke left
    the message up forever), while an already-delivered revoke render
    must not re-issue."""
    epoch_changed = latest is not None and latest["route_epoch"] != notify_scope.route_epoch(cfg)
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
                 and latest["route_epoch"] == notify_scope.route_epoch(cfg))
    unbound_intent = bool(_ready_intent_ids(db, card, unbound=True, waiting=waiting))
    if (card["kind"] == "patient_history" and latest is not None
            and now - latest["created_at"] < notify_history.UPDATE_SECONDS):
        unbound_intent = False  # New pages wait; adopted phase/source drift still renders immediately.
    needed = force or bool(gens) or latest is None \
        or (not suspended and (unbound_intent or epoch_changed
                               or latest["state"] in ("not_sent", "cancelled")))
    if card["delivery_state"] == "revoked" and card["message_id"]:
        needed = not (latest is not None
                      and latest["op"] == "revoke"
                      and (latest["state"] == "delivered"
                           or (latest["state"] == "not_sent"
                               and not epoch_changed and not force
                               and resend_exhausted(db, card["card_id"]))))
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


def _build_spec(db, card, content, op, rev, cfg, now, waiting=frozenset()) -> tuple[dict, str | None]:
    """Persist the view/token identity and return (spec, optional part-budget hold).

    Borrow the active transaction; do not change the input card/content mappings.
    The caller persists the returned sealed plan before any file publication.
    """
    cur = db.execute(
        """INSERT INTO notification_view_manifests(
             card_id,render_rev,source_generation,presentation_generation,
             digest,shown,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (card["card_id"], rev, card["source_generation"],
         card["presentation_generation"], 1 if card["kind"] == "digest" else 0,
         json.dumps(content["shown"], ensure_ascii=False), now))
    content = {**content, "manifest_id": cur.lastrowid}
    scope = (notify_scope.stored_scope(card)
             if card["transport"] in ("slack", "lineworks")
             or (card["message_id"] and card["channel_id"])
             else (notify_scope.delivery_scope(cfg) or {}))
    event_ids = _ready_intent_ids(db, card, waiting=waiting)
    correlation = secrets.token_hex(16)
    spec = {
        "schema": f"mcs-card-render/v{notify_scope.TRANSPORT_VERSIONS[card['transport']]}",
        "delivery_id": str(uuid.uuid4()),
        "logical_intent_id": card["card_key"],
        "card_key": card["card_key"], "kind": card["kind"], "op": op,
        "render_rev": rev,
        "source_generation": card["source_generation"],
        "presentation_generation": card["presentation_generation"],
        "ui_revision": card["ui_revision"],
        "delivery": {**scope, "intent_event_ids": event_ids,
                     "route_epoch": notify_scope.route_epoch(cfg),
                     "correlation": correlation},
    }
    # update/revoke target the bound Discord message — self-contained so
    # a worker never needs a registry/snapshot lookup to aim
    for k in ("message_id", "thread_id"):
        if card[k]:
            spec["delivery"][k] = card[k]
    context = {} if card["kind"] == "patient_history" else action_context.render_context(db, card)
    # failed/deleted threads can never carry the body — those cards
    # keep the 📄 button so the full text stays reachable. Slack honors
    # the same card_thread switch: its posted ts is the thread root, so
    # the body lands as channel-visible replies (T9), not an ephemeral
    # answer only the clicker can see.
    present = presenter(card["transport"])
    thread_on = card["kind"] != "patient_history" and (notify_scope.notify_cfg(cfg).get("card_thread") is True
                 or present.ALWAYS_THREAD)
    # a logical-grouping "thread" (LINE WORKS) has no failed state, so
    # a failed thread part never stops its body/overflow parts
    in_thread_body = thread_on and (
        present.ALWAYS_THREAD
        or card["thread_state"] not in ("failed", "deleted"))
    containers, footer = present.face(content["containers"],
                                      content["footer"], op, cfg)
    # per-post 💊 buttons outrank the optional list buttons: reserve
    # their component slots (section + accessory) before row 4 fills up
    # Discord answers a card-face click in the channel, so where the
    # thread body carries the 💊 buttons (THREAD_DRUG_ACTIONS) the card
    # face gets none — drug details stay inside the thread (owner rule)
    post_mids = (_post_action_targets(db, card, content, sum(
                     len(r) + 1 for r in _action_rows(
                         db, card, content, now, context, in_thread_body=in_thread_body,
                         cfg=cfg, mint=False)))
                 if present.POST_ACTIONS and card["kind"] == "thread"
                 and card["delivery_state"] != "revoked"
                 and (in_thread_body or not present.DRUG_ROW_NEEDS_THREAD)
                 and not (present.THREAD_DRUG_ACTIONS and in_thread_body)
                 else [])
    spec["parts"] = {
        "containers": containers,
        "action_rows": _action_rows(db, card, content, now, context,
                                    in_thread_body=in_thread_body,
                                    reserve=2 * len(post_mids), cfg=cfg),
        "footer": footer
                  + [{"type": "meta", "correlation": correlation}],
        "manifest_id": content["manifest_id"],
        "page": content["page"], "pages": content["pages"],
        "context": context,
        "preview_text": content["preview_text"],
    }
    def set_provider_version(parts):
        headers = [c for c in parts["containers"] if c.get("provider_scope") == "header"]
        if headers:
            parts["provider_states_version"] = (4 if "processing_status" in headers[0]
                                                else 3 if "visible_providers" in headers[0] else 2)
        elif any("provider_states" in c for c in parts["containers"]):
            parts["provider_states_version"] = 1
        else:
            parts.pop("provider_states_version", None)

    set_provider_version(spec["parts"])
    post_source_id = own_post(card)
    if positive(post_source_id):
        spec["parts"]["post_source_id"] = post_source_id
    if card["kind"] == "signal" and present.SOURCE_THREAD and card["thread_id"]:
        target = signal_thread_target(db, card, cfg)
        if target is not None and card["thread_id"] == target["thread_id"] and notify_scope.scope_matches(card, target):
            spec["parts"]["source_thread"] = True
    if reply_source(card) and card["thread_id"]:
        spec["parts"]["source_thread"] = True
    if any("<@" in (f.get("text") or "") for f in footer):
        # the footer names members as <@id> mentions — a worker must
        # render them without pinging; one that predates this key
        # rejects the spec (held) instead of sending live mentions
        spec["parts"]["mentions"] = "silent"
    if thread_on:
        spec["parts"]["thread_name"] = (_digest_thread_name(now)
                                        if card["kind"] == "digest"
                                        else _thread_name(db, card))
    # the body travels inside the spec as individually journaled
    # durable parts — the card stays a summary surface while the
    # thread carries the untruncated shown-set text (T7). Where the
    # transport shows bodies on the card, the posts the face holds whole
    # leave the thread (attachments and the rest stay there).
    face_posts, face_mids = [], frozenset()
    if (in_thread_body and op != "revoke" and card["kind"] == "thread"
            and card["layout"] >= 3 and (getattr(present, "BODY_ON_FACE", False)
                                         or getattr(present, "FACE_STAMPS", False))):
        face_posts, face_mids = notify_parts.face_posts(db, card, spec["parts"], content, present)
    spec["parts"], budget_hold = notify_parts.build_manifest(
        db, card, spec, content, in_thread_body, cfg=cfg, face_mids=face_mids, waiting=waiting)
    set_provider_version(spec["parts"])
    if any(b["id"] == "meds" for row in spec["parts"]["action_rows"] for b in row):
        spec["parts"]["drug_view_navigation"] = True
    if present.THREAD_DRUG_ACTIONS and spec["parts"].get("thread_body_parts"):
        spec["parts"]["thread_drug_actions"] = True
    own = {}
    if post_mids:
        own["post_actions"] = _post_actions(db, card, content, post_mids, now)
    if face_posts:
        own["face_posts"] = face_posts
    if present.ACCENT_URGENT and content.get("urgent"):
        own["accent"] = "urgent"
    if own:
        # an older worker holds a spec carrying this key until restarted
        spec["parts"][card["transport"]] = own
    return spec, budget_hold


def _post_action_targets(db, card, content, row_cost) -> list:
    """[(container index, message id)] of the shown medication posts that
    get a 💊 beside their own post line, page order, at most
    MAX_POST_ACTIONS and only while the component budget leaves room."""
    import structured_view
    if not positive(card["project_id"]):
        return []
    rules = [i for i, c in enumerate(content["containers"])
             if c.get("rule") and c.get("type") == "text"]
    room = (MAX_COMPONENTS - sum(c["type"] != "meta" for c in content["containers"])
            - sum(f["type"] == "text" for f in content["footer"]) - row_cost) // 2
    out = []
    for at, mid in zip(rules, content["shown"]):
        if len(out) >= min(MAX_POST_ACTIONS, max(0, room)):
            break
        if positive(mid) and any(structured_view.medication_entries(db, mid)):
            out.append((at, mid))
    return out


def _post_actions(db, card, content, targets, now) -> list:
    """Mint each target's read-only 💊 token the same way as the card's
    buttons (same source/manifest/ui binding)."""
    need = {"source_gen": card["source_generation"],
            "manifest_id": content["manifest_id"],
            "ui_rev": card["ui_revision"]}
    return [{"at": at, "button": {
                "id": "meds", "ui": "button", "style": "secondary",
                "label": "💊 薬剤",
                "token": action_tokens.mint(db, card["card_id"], "meds",
                                     {"message_id": mid}, need, now)}}
            for at, mid in targets]


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
    notify_parts.seed_parts(db, spec, now)
    db.executemany(
        """UPDATE notification_intent_cards
           SET delivery_id=?, required_render_rev=?
           WHERE card_id=? AND event_id=? AND state='pending'""",
        [(spec["delivery_id"], rev, card["card_id"], event_id)
         for event_id in spec["delivery"]["intent_event_ids"]])
    db.execute(
        """UPDATE notification_cards SET source_generation=?,
             source_fp=?, presentation_generation=?, content_fp=?,
             desired_render_rev=?, updated_at=? WHERE card_id=?""",
        (card["source_generation"], card["source_fp"],
         card["presentation_generation"], card["content_fp"],
         rev, now, card["card_id"]))


def issue_render(db, card_id, cfg, now, specs, force=False):
    """Issue and append a spec inside the caller-owned transaction, returning its ID.

    Never open/close db or publish files; the caller publishes specs after commit.
    Issue the next render when the display model demands one. Precondition per §4: no unsettled attempt may own the card —
    a granted/unknown attempt keeps exclusive send rights until its
    factual result lands, so issuance here never races an HTTP call."""
    card = _render_gates(db, card_id, cfg)
    if card is None:
        return None
    if card["kind"] in ("signal", "digest") and not notify_scope.signals_notify(cfg):
        # begin denies these anyway (signal_notify_off, not final), so a
        # queued render would sit live forever and block re-issue once
        # notify is back on — cancel it so re-enabling issues a fresh
        # delivery_id instead
        _cancel_open_renders(db, card_id, now)
        return None
    # LINE WORKS has no native thread to bind: its signal cards keep
    # the channel route they had before source-thread delivery.
    if card["kind"] == "signal" and card["delivery_state"] != "revoked" \
            and card["transport"] in ("slack", "discord"):
        card, changed, wait = _bind_signal_thread(db, card, cfg, now)
        if wait:
            _cancel_open_renders(db, card_id, now)
            return None
        _clear_thread_hold(db, "event_id IN (SELECT event_id FROM notification_intent_cards "
                               "WHERE card_id=? AND state='pending')", card_id)
        if changed:
            # a render queued for the old route is never claimed again
            # (begin denies source_thread_changed) and would otherwise
            # count as live and block the replacement until content drift
            _cancel_open_renders(db, card_id, now)
        force = force or changed
    if reply_source(card) and card["delivery_state"] != "revoked":
        card, wait = _bind_reply_thread(db, card, now)
        if wait:
            _cancel_open_renders(db, card_id, now)
            return None
        _clear_thread_hold(db, "event_id IN (SELECT event_id FROM notification_intent_cards "
                               "WHERE card_id=? AND state='pending')", card_id)
        # Previously sealed channel fallbacks (or stale remote identities)
        # must not keep a live slot after a safe initial thread binding.
        for queued in db.execute(
                "SELECT delivery_id,spec_json FROM notification_renders "
                "WHERE card_id=? AND state IN ('queued','held')", (card_id,)).fetchall():
            if not reply_thread_matches(db, card, loads_dict(queued["spec_json"])):
                _cancel_render(db, queued["delivery_id"], now)
                force = True
    op = _render_op(card)
    if op is None:
        return None      # revoked & never delivered: nothing to render
    latest = db.execute(
        "SELECT * FROM notification_renders WHERE card_id=? "
        "ORDER BY render_rev DESC LIMIT 1", (card_id,)).fetchone()
    if op == "revoke" and latest is not None and latest["op"] == "revoke" \
            and latest["state"] == "delivered":
        return None      # delete already landed — _render_needed would
                         # say no regardless of drift, so skip content
    waiting = frozenset()
    if card["kind"] == "patient_history":
        card = notify_history.refresh(db, card, cfg, now)
        if card["transport"] == "lineworks" and card["message_id"] is not None:
            # Later count updates owe no third room post. Only unresolved
            # intents with no delivery binding may be suppressed here.
            for pending in db.execute("SELECT event_id FROM notification_intent_cards WHERE card_id=? "
                    "AND state='pending' AND delivery_id IS NULL", (card_id,)).fetchall():
                db.execute("UPDATE notification_intent_cards SET state='suppressed' WHERE event_id=? AND card_id=?",
                           (pending[0], card_id))
                complete_intent(db, pending[0], now)
            return None
    content_options = {}
    if card["kind"] == "thread":
        import processing_route
        content_options["visible_providers"] = processing_route.visible_providers()
    content = card_content(db, card, cfg=cfg, **content_options)
    if (card["transport"] == "lineworks" and card["kind"] == "thread"
            and not content.get("urgent")):
        from notify_render import structured_block, extraction_failed, progress_state
        pending = db.execute("SELECT message_id,first_seen FROM messages WHERE project_id=? "
                             "AND (message_id=? OR parent_id=?)",
                             (card["project_id"], card["root_message_id"], card["root_message_id"])).fetchall()
        delivered = notify_parts.delivered_members(db, card, legacy=True) | notify_parts.delivered_members(db, card, legacy=False)
        for name in notify_parts.prior_body_posts(db, card):
            match = notify_parts.BODY_POST_KEY.fullmatch(name)
            if match:
                delivered.add(int(match.group(1)))
        waiting = {row["message_id"] for row in pending
                   if row["message_id"] not in delivered
                   and row["first_seen"] and 0 <= now - row["first_seen"] < LINEWORKS_SUMMARY_WAIT_S
                   and structured_block(db, row["message_id"]) is None
                   and not extraction_failed(db, row["message_id"])
                   and (progress_state(db, row["message_id"], cfg) or {}).get("state") != "attention"}
        if waiting:
            ready = {row["message_id"] for row in pending} - waiting
            if not ready:
                return None
            # Publish ready posts without consuming newer posts' own deadlines.
            # The global source fingerprint remains intact for stale-action guards;
            # shown sets and intent binding describe only this render's coverage.
            content = card_content(db, card, cfg=cfg, waiting=waiting, **content_options)
            if not (ready - delivered) and _presentation_fp(card, content, cfg) == card["content_fp"]:
                return None  # only the still-waiting posts changed
    gens = _generation_drift(card, content, cfg)
    # Old Slack parts were bounded before escaping, and can be definitively
    # rejected even when their source/card fingerprint has not changed.
    # Rebuild only a proven oversized failed plan; pending/unknown parts still
    # pass through the existing in-flight gate below without being released.
    force = force or notify_parts.needs_wire_replan(db, card, latest)
    if not _render_needed(db, card, latest, gens, cfg, now, force, waiting):
        return None                        # delivered/terminal & no drift
    if op == "update" and _parts_in_flight(db, latest):
        return None                        # a later sweep issues it
    rev = (latest["render_rev"] + 1) if latest is not None else 1
    _cancel_open_renders(db, card_id, now)
    card = {**card, **gens}
    spec, budget_hold = _build_spec(db, card, content, op, rev, cfg, now, waiting)
    _persist_render(db, card, spec, op, rev, now)
    if budget_hold:
        db.execute("UPDATE notification_renders SET state='held',parts_state='held' WHERE delivery_id=?", (spec["delivery_id"],))
        db.execute("UPDATE notification_render_parts SET state='held',error_code=? WHERE delivery_id=?",
                   (budget_hold, spec["delivery_id"]))
        db.execute("UPDATE notification_cards SET delivery_state='update_failed' WHERE card_id=?", (card_id,))
        return None
    specs.append(spec)
    return spec["delivery_id"]


def _thread_name(db, card) -> str:
    name = patient_name(db, card["project_id"]) \
        or f"project {card['project_id']}"
    d = None
    if card["root_message_id"]:
        r = db.execute("SELECT posted_at FROM messages WHERE message_id=?",
                       (card["root_message_id"],)).fetchone()
        if r:
            d = mmdd(r["posted_at"])
    if (not d or "?" in d) and card["kind"] != "thread" \
            and isinstance(card["created_at"], (int, float)):
        # signal cards have no root post: the day the card was made, in JST
        from datetime import datetime
        from mcs_queries import JST
        d = f"{datetime.fromtimestamp(card['created_at'], JST):%m-%d}"
    title = f"💬 {name}" + (f" — {d}" if d and "?" not in d else "")
    return title if len(title) <= 100 else title[:99] + "…"


def _digest_thread_name(now) -> str:
    # the digest is keyed by its JST day, never the host's local zone
    from datetime import datetime
    from mcs_queries import JST
    return f"💬 アラート — {datetime.fromtimestamp(now, JST):%m-%d}"


# A not_sent that is a begin denial (denied_*) or a rate-limit rejection
# (Slack ratelimited, LINE WORKS rate_limited, HTTP 429) proves the
# transport is healthy and nothing was written — it never spends the
# MAX_RESEND budget, or a burst of 429s would park a never-delivered
# card as update_failed for good.
_REAL_FAILURE = ("(a.error_code IS NULL OR (a.error_code NOT LIKE 'denied_%'"
                 " AND a.error_code NOT IN"
                 " ('ratelimited','rate_limited','http_429')))")


def resend_exhausted(db, card_id: int) -> bool:
    """MAX_RESEND consecutive real not_sents on a card suspends auto-retry —
    denied begins (error_code 'denied_*') are not send failures and never
    count, nor do rate-limit rejections (_REAL_FAILURE). Drift, a
    route_epoch bump, or an operator resolve re-opens."""
    return db.execute(
        """SELECT COUNT(*) c FROM notification_delivery_attempts a
           JOIN notification_renders r ON r.delivery_id=a.delivery_id
           WHERE r.card_id=? AND a.state='not_sent'
             AND r.render_rev > COALESCE((
               SELECT MAX(done.render_rev) FROM notification_renders done
               JOIN notification_delivery_attempts sent
                 ON sent.delivery_id=done.delivery_id
               WHERE done.card_id=r.card_id AND sent.state='delivered'), 0)
             AND """ + _REAL_FAILURE,
        (card_id,)).fetchone()["c"] >= MAX_RESEND


def complete_intent(db, event_id, now) -> str | None:
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
        owner = db.execute(
            "SELECT o.kind,b.frozen_payload FROM notify_outbox o "
            "LEFT JOIN notification_intent_batches b ON b.event_id=o.event_id "
            "WHERE o.event_id=?", (event_id,)).fetchone()
        if owner is not None and owner["kind"] == "attachment_followup":
            try:
                frozen = json.loads(owner["frozen_payload"])
            except (ValueError, TypeError, RecursionError):
                return None
            if not isinstance(frozen, dict) or not positive(frozen.get("attachment_id")) \
                    or not positive(frozen.get("message_id")) \
                    or not valid_hash(frozen.get("attachment_sha256")):
                return None
            delivered = db.execute(
                """SELECT 1 FROM notification_intent_cards ic
                   JOIN notification_renders r ON r.delivery_id=ic.delivery_id
                   JOIN notification_cards c ON c.card_id=ic.card_id
                   JOIN notification_render_parts p ON p.delivery_id=r.delivery_id
                   JOIN attachments a ON a.attachment_id=p.attachment_id
                   JOIN messages m ON m.message_id=a.message_id
                   WHERE ic.event_id=? AND ic.state='delivered'
                     AND r.card_id=c.card_id AND r.state='delivered'
                     AND r.transport=c.transport
                     AND r.profile IS c.profile AND r.application_id IS c.application_id
                     AND r.team_id IS c.team_id AND r.channel_id IS c.channel_id
                     AND c.delivery_state!='revoked' AND c.thread_state='created'
                     AND m.project_id=c.project_id AND m.message_id=?
                     AND m.body_state IS NOT 'deleted' AND a.state='downloaded'
                     AND a.sha256=? AND p.attachment_id=?
                     AND p.kind='attachment_part' AND p.state='delivered'
                     AND p.remote_id IS NOT NULL AND p.remote_id!='' AND p.payload_sha256=?
                     AND EXISTS(SELECT 1 FROM notification_render_parts t
                         WHERE t.delivery_id=r.delivery_id AND t.kind='thread'
                           AND t.state='delivered' AND t.remote_id=c.thread_id)
                   LIMIT 1""",
                (event_id, frozen.get("message_id"), frozen["attachment_sha256"],
                 frozen["attachment_id"], frozen["attachment_sha256"])).fetchone()
            if delivered is None:
                return None
        db.execute(
            "UPDATE notify_outbox SET state='accepted',next_try=NULL,"
            "accepted_ref=?,updated_at=? WHERE event_id=?",
            (f"cards:{len(rows)}", now, event_id))
        # Cards carry text, while attachments retain the existing file
        # transport. Include files downloaded before card acceptance too.
        from attachment_followups import enqueue_ready_attachment_followups_tx
        enqueue_ready_attachment_followups_tx(db, event_id, now)
        return "accepted"
    db.execute(
        "UPDATE notify_outbox SET state='suppressed',next_try=NULL,"
        "updated_at=? WHERE event_id=?", (now, event_id))
    return "suppressed"


# ---------- dispatch (intent -> cards -> renders) ----------

def _resolve_targets(db, ev, payload, *, cfg=None) -> list:
    """Frozen intent -> [(card_key, coverage, anchor)] fan-out."""
    kind = ev["kind"]
    if kind == "patient_history":
        return notify_history.targets(db, ev["project_id"], payload, notify_scope.active_transport(cfg), scope=notify_scope.delivery_scope(cfg))
    if kind == "attachment_followup":
        aid, mid = payload.get("attachment_id"), payload.get("message_id")
        if not positive(aid) or not positive(mid) or not positive(ev["project_id"]):
            return []
        rows = db.execute(
            """SELECT DISTINCT c.card_id,a.sha256,c.thread_state FROM attachments a
               JOIN messages m ON m.message_id=a.message_id
               JOIN notification_intent_cards ic ON ic.state='delivered'
               JOIN notification_intent_batches b ON b.event_id=ic.event_id
               JOIN notify_outbox o ON o.event_id=ic.event_id
               JOIN notification_cards c ON c.card_id=ic.card_id
               WHERE a.attachment_id=? AND m.message_id=? AND m.project_id=?
                 AND a.state='downloaded' AND a.local_path IS NOT NULL
                 AND m.body_state IS NOT 'deleted' AND c.project_id=m.project_id
                 AND NOT EXISTS(SELECT 1 FROM patients patient
                     WHERE patient.project_id=m.project_id AND patient.is_archived=1)
                 AND c.kind='thread'
                 AND NOT (c.layout>=4 AND m.message_id<>c.root_message_id
                          AND (NOT json_valid(c.anchor_key)
                               OR json_extract(c.anchor_key,'$.source') IS NULL))
                 AND c.delivery_state IN ('delivered','update_failed')
                 AND c.message_id IS NOT NULL
                 AND ((c.thread_state='created' AND c.thread_id IS NOT NULL)
                   OR (c.thread_state='failed' AND EXISTS(
                     SELECT 1 FROM notification_render_parts t
                     WHERE t.delivery_id=ic.delivery_id AND t.kind='thread'
                       AND t.state='not_sent' AND t.remote_id IS NULL)
                     AND NOT EXISTS(SELECT 1 FROM notification_render_parts p
                       WHERE p.attachment_id=a.attachment_id
                         AND p.kind='attachment_part'
                         AND (p.state NOT IN ('held','not_sent')
                              OR p.remote_id IS NOT NULL))))
                 AND o.kind='new_messages' AND o.state='accepted'
                 AND EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(b.frozen_payload)
                     THEN b.frozen_payload ELSE '{}' END,'$.message_ids') x
                     WHERE x.type='integer' AND x.value=m.message_id)
                 AND EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(ic.coverage)
                     THEN ic.coverage ELSE '[]' END) x
                     WHERE x.type='integer' AND x.value=m.message_id)
               ORDER BY c.card_id""", (aid, mid, ev["project_id"])).fetchall()
        return [{"card_id": r["card_id"], "coverage": [mid],
                 "attachment_sha256": r["sha256"],
                 "text_fallback": r["thread_state"] == "failed"} for r in rows
                if valid_hash(r["sha256"])]
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
            membership = db.execute("SELECT content FROM message_metadata WHERE message_id=? AND source='capture'", (mid,)).fetchone()
            role = ((loads_dict(membership[0]) or {}).get("_initial_import") or {}) if membership else {}
            if role.get("role") == "history":
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


# progress.wait records why a pending intent was pushed out; written in
# the same UPDATE as updated_at so a reader can tell the record is the
# row's latest (any later writer moves updated_at). Existing receipts are
# kept, an unparseable one under invalid_progress, as HOLD_PROGRESS_SET.
_WAIT_PROGRESS_SET = (
    "progress=json_set(CASE WHEN progress IS NULL OR progress='' THEN '{}' "
    "WHEN json_valid(progress) AND json_type(progress)='object' "
    "THEN progress ELSE json_object('invalid_progress',progress) END,"
    "'$.wait',json_object('reason',?,'at',?))")


def _reseat(db, event_id, now, reason: str) -> None:
    """Push a still-pending card-owned intent's next re-examination out
    by RESEAT_S so it does not stay due (and hog flush slots) forever,
    recording why (a RESEAT_REASONS code)."""
    db.execute(
        f"UPDATE notify_outbox SET next_try=?,{_WAIT_PROGRESS_SET},"
        "updated_at=? WHERE event_id=?",
        (now + RESEAT_S, reason, now, now, event_id))


def _notice_spec(event_id, parts, cfg, scope, rev, transport) -> dict:
    """A self-contained card-less notice: no buttons, tokens, manifest
    row or thread — only the sealed part plan (card, plus the LINE WORKS
    overflow body parts behind a thread part)."""
    correlation = secrets.token_hex(16)
    key = f"v1|notice|{event_id}"
    present = presenter(transport)
    if hasattr(present, "overflow"):
        parts = fit_parts(parts, present.CARD_LIMIT)
    body = {"containers": parts["containers"], "action_rows": [],
            "footer": [f for f in parts["footer"] if f.get("type") == "text"]
                      + [{"type": "meta", "correlation": correlation}]}
    main = next((c.get("text", "").split("\n")[-1] for c in parts["containers"]
                 if c.get("type") == "text"), "日次集計")
    body["preview_text"] = parts.get("preview_text") or " ".join(main.split())[:600] or "MCS 日次集計"
    spec = {
        "schema": f"mcs-card-render/v{notify_scope.TRANSPORT_VERSIONS[transport]}",
        "delivery_id": str(uuid.uuid4()),
        "logical_intent_id": key, "card_key": key, "op": "notice",
        "render_rev": rev, "source_generation": 0,
        "presentation_generation": 0, "ui_revision": 0,
        "delivery": {**scope, "intent_event_ids": [event_id],
                     "route_epoch": notify_scope.route_epoch(cfg),
                     "correlation": correlation},
        "parts": body}
    payload = canonical({k: body[k] for k in
                         ("containers", "footer", "action_rows", "preview_text")})
    manifest = [{"part_id": "card", "kind": "card", "index": 0,
                 "sha256": hashlib.sha256(payload).hexdigest(),
                 "bytes": len(payload)}]
    keyed = (present.overflow(body, notify_parts.split_body_chunks)
             if hasattr(present, "overflow") else [])
    if keyed:
        manifest.append({"part_id": "thread", "kind": "thread", "index": 1,
                         "name": key, "sha256": notify_parts.text_sha256(key)})
        body["thread_body_parts"] = [c for _k, c in keyed]
        manifest += [{"part_id": f"body:{i + 1:04d}", "kind": "body_part",
                      "index": i + 2, "name": k, "sha256": notify_parts.text_sha256(c),
                      "bytes": len(c.encode("utf-8"))}
                     for i, (k, c) in enumerate(keyed)]
    body["manifest"] = manifest
    return spec


def _persist_notice_render(db, spec, event_id, now) -> bool:
    """Persist a sealed card-less notice and seed its parts in the borrowed transaction.

    The caller owns the transaction and publishes the spec after commit.
    """
    delivery = spec["delivery"]
    db.execute(
        """INSERT INTO notification_renders(
             delivery_id,card_id,op,render_rev,manifest_id,
             route_epoch,profile,application_id,guild_id,
             channel_id,transport,team_id,spec_json,payload_hash,
             correlation,state,parts_state,intent_event_id,
             created_at,updated_at)
           VALUES(?,NULL,'notice',?,NULL,?,?,?,?,?,?,?,?,?,?,
                  'queued','pending',?,?,?)""",
        (spec["delivery_id"], spec["render_rev"], delivery["route_epoch"],
         delivery.get("profile"), delivery.get("application_id"),
         delivery.get("guild_id"), delivery.get("channel_id"),
         delivery.get("transport", "discord"), delivery.get("team_id"),
         canonical(spec).decode(), payload_hash(spec), delivery["correlation"],
         event_id, now, now))
    notify_parts.seed_parts(db, spec, now)
    if any(not chunk.strip() for chunk in spec["parts"].get("thread_body_parts", [])):
        reason = "blank_part_unrepresentable"
        db.execute("UPDATE notification_renders SET state='held',parts_state='held' "
                   "WHERE delivery_id=?", (spec["delivery_id"],))
        db.execute("UPDATE notification_render_parts SET state='held',error_code=? "
                   "WHERE delivery_id=?", (reason, spec["delivery_id"]))
        db.execute("UPDATE notify_outbox SET state='failed',next_try=NULL,"
                   + HOLD_PROGRESS_SET + ",updated_at=? WHERE event_id=?",
                   (reason, now, event_id))
        return False
    return True


def _notice_renders(db, event_id) -> list:
    return db.execute(
        "SELECT * FROM notification_renders WHERE card_id IS NULL "
        "AND op='notice' AND intent_event_id=? ORDER BY render_rev",
        (event_id,)).fetchall()


def _notice_failures(db, event_id) -> int:
    """Real failed sends of this event's notices — begin denials
    (``denied_*``) are not send attempts and never spend the budget."""
    return db.execute(
        """SELECT count(*) FROM notification_delivery_attempts a
           JOIN notification_renders r ON r.delivery_id=a.delivery_id
           WHERE r.card_id IS NULL AND r.intent_event_id=?
             AND a.state='not_sent'
             AND """ + _REAL_FAILURE,
        (event_id,)).fetchone()[0]


def _notice_unsent(db, renders) -> bool:
    """Provably never delivered: every render queued/not_sent/cancelled
    and no attempt granted or unknown."""
    ids = [r["delivery_id"] for r in renders]
    if any(r["state"] not in ("queued", "not_sent", "cancelled")
           for r in renders):
        return False
    return not ids or not db.execute(
        "SELECT 1 FROM notification_delivery_attempts WHERE delivery_id IN "
        f"({','.join('?' * len(ids))}) AND state IN ('granted','unknown')",
        ids).fetchone()


def _close_notice(db, event_id, renders, now, state="suppressed") -> None:
    for r in renders:
        if r["state"] in ("queued", "held"):
            _cancel_render(db, r["delivery_id"], now)
    db.execute("UPDATE notify_outbox SET state=?,next_try=NULL,"
               "updated_at=? WHERE event_id=?", (state, now, event_id))


def _cancel_moved_notices(db, event_id, renders, cfg, now) -> list:
    """Cancel never-sent queued notices whose route the config left; return the fresh renders."""
    # never sent (queued, no attempt): a fresh render aims at the current route
    moved = [r for r in renders if r["state"] == "queued"
             and _notice_route_moved(r, cfg) and _notice_unsent(db, [r])]
    for r in moved:
        _cancel_render(db, r["delivery_id"], now)
    return _notice_renders(db, event_id) if moved else renders


def _notice_route_moved(render, cfg) -> bool:
    """A queued notice aimed at a route the config no longer has (epoch,
    transport or destination changed) — begin would deny it forever."""
    scope = notify_scope.delivery_scope(cfg) or {}
    return (render["route_epoch"] != notify_scope.route_epoch(cfg)
            or render["transport"] != notify_scope.active_transport(cfg)
            or any(render[k] != scope.get(k) for k in
                   ("application_id", "channel_id", "team_id", "guild_id")
                   if k in scope)
            # begin checks the full scope (profile included) for these
            or (render["transport"] in ("slack", "lineworks")
                and not notify_scope.scope_matches(render, scope)))


def _dispatch_notice(ledger, ev, cfg, now) -> dict:
    """Card-less notice intents (📊 daily summary): seal once, issue a
    ``notice`` render, and on re-entry finish it — delivered → accepted,
    a real failure → a fresh render up to MAX_RESEND, then held. Turned
    off: a stale/disabled day is suppressed; cards switched off while
    provably unsent fall back to the frozen text exactly once."""
    import notify_digest
    db = connection(ledger)
    specs: list = []
    event_id = ev["event_id"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT event_id,kind,payload,state,route FROM notify_outbox "
            "WHERE event_id=?", (event_id,)).fetchone()
        if row is None or row["route"] != "interactive" \
                or row["state"] not in ("pending", "failed"):
            return {"skipped": True}
        renders = _notice_renders(db, event_id)
        if notify_scope.interactive_enabled(cfg):
            renders = _cancel_moved_notices(db, event_id, renders, cfg, now)
        if any(r["state"] == "delivered" for r in renders):
            db.execute("UPDATE notify_outbox SET state='accepted',"
                       "next_try=NULL,accepted_ref='notice',updated_at=? "
                       "WHERE event_id=?", (now, event_id))
            mark_snapshot_dirty(db)
            return {"accepted": True}
        newer = db.execute(
            "SELECT 1 FROM notify_outbox WHERE kind=? AND event_id>?",
            (row["kind"], event_id)).fetchone()
        if notify_digest.settings(cfg) is None or newer:
            # an undelivered summary has no value once disabled or a
            # newer day exists — never sent late
            if _notice_unsent(db, renders):
                _close_notice(db, event_id, renders, now)
                mark_snapshot_dirty(db)
                return {"suppressed": True}
        elif not notify_scope.interactive_enabled(cfg) and _notice_unsent(db, renders):
            target = cfg.get("notify_target")
            if not (isinstance(target, str) and target.strip()):
                # no text destination to fall back to — never send late
                _close_notice(db, event_id, renders, now)
                mark_snapshot_dirty(db)
                return {"suppressed": True}
            # the kill switch: provably unsent → the frozen text, once
            _close_notice(db, event_id, renders, now, state="pending")
            db.execute("DELETE FROM notification_intent_batches "
                       "WHERE event_id=?", (event_id,))
            db.execute("UPDATE notify_outbox SET route='text',next_try=? "
                       "WHERE event_id=?", (now, event_id))
            mark_snapshot_dirty(db)
            return {"reverted": True}
        live = [r for r in renders if r["state"] in LIVE_RENDER]
        if live or not notify_scope.interactive_enabled(cfg) \
                or notify_digest.settings(cfg) is None or newer:
            specs.extend(json.loads(r["spec_json"]) for r in live
                         if r["state"] == "queued" and r["spec_json"]
                         and not r["spec_published"])
            _reseat(db, event_id, now,
                    "notice_render_live" if live
                    else "notice_interactive_off"
                    if not notify_scope.interactive_enabled(cfg)
                    else "notice_digest_unset"
                    if notify_digest.settings(cfg) is None
                    else "notice_superseded")
        elif _notice_failures(db, event_id) >= MAX_RESEND:
            db.execute("UPDATE notify_outbox SET state='failed',"
                       f"next_try=NULL,{HOLD_PROGRESS_SET},updated_at=? "
                       "WHERE event_id=?",
                       ("resend_exhausted", now, event_id))
            mark_snapshot_dirty(db)
            return {"error": "resend_exhausted"}
        else:
            scope = notify_scope.delivery_scope(cfg)
            if scope is None:
                return {"error": "delivery_scope_missing"}
            try:
                frozen = json.loads(row["payload"])
                parts = frozen["parts"]
                ok = isinstance(parts.get("containers"), list) \
                    and isinstance(parts.get("footer"), list)
            except (ValueError, TypeError, KeyError, AttributeError):
                ok = False
            if not ok:
                db.execute("UPDATE notify_outbox SET state='failed',"
                           f"next_try=NULL,{HOLD_PROGRESS_SET},updated_at=? "
                           "WHERE event_id=?",
                           ("payload_invalid", now, event_id))
                return {"error": "payload_invalid"}
            transport = notify_scope.active_transport(cfg)
            db.execute(
                "INSERT OR IGNORE INTO notification_intent_batches("
                "event_id,frozen_payload,payload_hash,route_epoch,sealed_at,"
                "transport,scope_json) VALUES(?,?,?,?,?,?,?)",
                (event_id, canonical(frozen).decode(), payload_hash(frozen),
                 notify_scope.route_epoch(cfg), now, transport,
                 canonical(scope).decode()
                 if transport in ("slack", "lineworks") else None))
            spec = _notice_spec(event_id, parts, cfg, scope,
                                len(renders) + 1, transport)
            if _persist_notice_render(db, spec, event_id, now):
                specs.append(spec)
                _reseat(db, event_id, now, "notice_render_issued")
        mark_snapshot_dirty(db)
    notify_spool.publish_committed(ledger, db, specs, now)
    return {"dispatched": bool(specs)}


def urgent_thread_target(ledger, ev, cfg):
    """Return only this source's delivered companion-thread binding in the current scope.

    Without source threads (LINE WORKS) the notice is a room post headed
    with the patient, so its target is the delivery scope itself."""
    scope = notify_scope.delivery_scope(cfg)
    if not notify_scope.interactive_enabled(cfg) or scope is None:
        return None
    if not presenter(notify_scope.active_transport(cfg)).SOURCE_THREAD:
        return scope
    if notify_scope.active_transport(cfg) not in ("slack", "discord"):
        return None
    try:
        payload = json.loads(ev["payload"])
        mid, base = payload["message_id"], payload["base_event_id"]
    except (ValueError, TypeError, KeyError):
        return None
    return _source_thread_target(ledger.db, ev["project_id"], mid, cfg, base=base)


def _source_thread_target(db, project_id, mid, cfg, *, base=None):
    scope = notify_scope.delivery_scope(cfg)
    if not notify_scope.interactive_enabled(cfg) or scope is None or notify_scope.active_transport(cfg) not in ("slack", "discord"):
        return None
    if action_context.source_hash(db, mid, project_id) is None:
        return None
    root = _thread_root(db, mid, project_id)
    rows = db.execute("""SELECT DISTINCT c.* FROM notification_cards c
        JOIN notification_intent_cards ic ON ic.card_id=c.card_id
        JOIN json_each(CASE WHEN json_valid(ic.coverage) THEN ic.coverage ELSE '[]' END) j
        JOIN notify_outbox o ON o.event_id=ic.event_id
        WHERE (? IS NULL OR ic.event_id=?) AND o.kind='new_messages' AND ic.state='delivered' AND j.type='integer' AND j.value=?
          AND c.kind='thread' AND c.project_id=? AND c.root_message_id=?
          AND c.delivery_state IN ('delivered','update_failed') AND c.message_id IS NOT NULL
          AND c.thread_state='created' AND c.thread_id IS NOT NULL
          AND (NOT json_valid(c.anchor_key) OR json_extract(c.anchor_key,'$.source') IS NULL)""",
        (base, base, mid, project_id, root)).fetchall()
    bound = [dict(row) for row in rows if notify_scope.scope_matches(row, scope)
             and (row["delivery_state"] == "delivered" or _thread_binding_creates(db, row))]
    if len(bound) != 1:
        return None
    card = bound[0]
    if card["transport"] == "slack" and card["thread_id"] != card["message_id"]:
        return None
    return {**scope, "message_id": card["message_id"], "thread_id": card["thread_id"]}


def _signal_evidence_mid(db, card):
    """The latest full evidence message of the card's signals, or None
    when any signal's evidence is missing or unverifiable."""
    signals = notify_scope.latest_signals(db, anchor_keys(card), card["project_id"])
    candidates = []
    for signal in signals.values():
        mid, message = signal_evidence(db, signal["content"])
        if message is None or action_context.source_hash(db, mid, card["project_id"]) is None:
            return None
        stamp = db.execute("SELECT posted_at_ts FROM messages WHERE message_id=? AND project_id=?", (mid, card["project_id"])).fetchone()[0]
        candidates.append((stamp or 0, mid))
    return max(candidates)[1] if candidates else None


def signal_thread_target(db, card, cfg):
    """Choose the latest full evidence message's verified patient thread."""
    mid = _signal_evidence_mid(db, card)
    if mid is None:
        return None
    return _source_thread_target(db, card["project_id"], mid, cfg)


SIGNAL_SOURCE_HOLD_MAX_S = 6 * 3600   # wait this long for a provable source thread


def _thread_binding_creates(db, card):
    """Delivered creates proving this card message and companion thread in the same scope."""
    if not card["message_id"] or not card["thread_id"]:
        return []
    return [row for row in db.execute("""SELECT r.* FROM notification_renders r
        JOIN notification_render_parts c ON c.delivery_id=r.delivery_id AND c.part_id='card'
        JOIN notification_render_parts t ON t.delivery_id=r.delivery_id AND t.part_id='thread'
        WHERE r.card_id=? AND r.op='create' AND r.state='delivered'
          AND c.state='delivered' AND c.remote_id=? AND t.state='delivered' AND t.remote_id=?
        ORDER BY r.render_rev DESC""", (card["card_id"], card["message_id"], card["thread_id"]))
        if notify_scope.scope_matches(row, notify_scope.stored_scope(card))]


def _signal_route_witness(raw):
    """Content-free source/card route evidence from a sealed spec or its GC witness."""
    spec = loads_dict(raw)
    if spec is None:
        return None
    if set(spec) == {"signal_thread_route", "thread_id"}:
        route, thread = spec["signal_thread_route"], spec["thread_id"]
        return spec if (route == "card" and thread is None or route == "source"
                        and isinstance(thread, str) and thread) else None
    delivery, parts = spec.get("delivery"), spec.get("parts")
    if not isinstance(delivery, dict) or not isinstance(parts, dict) or "schema" not in spec:
        return None
    thread = delivery.get("thread_id")
    if parts.get("source_thread") is True and isinstance(thread, str) and thread:
        return {"signal_thread_route": "source", "thread_id": thread}
    if parts.get("source_thread") is None and thread is None:
        return {"signal_thread_route": "card", "thread_id": None}
    return None


def _signal_thread_route(db, card):
    """Identify a bound signal route without guessing from missing historical specs."""
    if (card["transport"] == "slack" and card["thread_state"] == "created"
            and card["delivery_state"] in ("delivered", "update_failed")
            and card["message_id"] and card["thread_id"] == card["message_id"]):
        return "card"
    rows = _thread_binding_creates(db, card)
    if card["message_id"] is None:
        row = db.execute("SELECT * FROM notification_renders WHERE card_id=? AND op='create' "
                         "AND state IN ('queued','held') ORDER BY render_rev DESC LIMIT 1",
                         (card["card_id"],)).fetchone()
        rows = [row] if row is not None and notify_scope.scope_matches(row, notify_scope.stored_scope(card)) else []
    for row in rows:
        witness = _signal_route_witness(row["spec_json"])
        if witness is not None and (witness["signal_thread_route"] == "card"
                                    or witness["thread_id"] == card["thread_id"]):
            return witness["signal_thread_route"]
    return None


def _clear_thread_hold(db, where: str, *params) -> None:
    """Drop a resolved thread hold from progress so health no longer reports
    it; other keys and updated_at (the wait's freshness anchor) stay."""
    db.execute("UPDATE notify_outbox SET progress=json_remove(progress,'$.thread_hold') "
               f"WHERE {where} AND CASE WHEN json_valid(progress) "
               "THEN json_type(progress,'$.thread_hold') END IS NOT NULL", params)


def reply_thread_target(db, card):
    """Return the canonical root's current thread in the reply's exact scope."""
    if not reply_source(card):
        return None
    root_key = card["card_key"].rsplit("|reply:", 1)[0]
    root = db.execute("SELECT * FROM notification_cards WHERE card_key=?", (root_key,)).fetchone()
    if (root is not None and root["kind"] == "thread" and not reply_source(root)
            and root["project_id"] == card["project_id"]
            and root["root_message_id"] == card["root_message_id"]
            and root["delivery_state"] in ("delivered", "update_failed")
            and root["message_id"] and root["thread_state"] == "created" and root["thread_id"]
            # a Slack thread is rooted at the card's own ts; Discord's is its own channel
            and (root["transport"] != "slack" or root["thread_id"] == root["message_id"])
            and notify_scope.scope_matches(root, notify_scope.stored_scope(card))):
        return dict(root)
    return None


def reply_thread_matches(db, card, spec) -> bool:
    """Check sealed reply remote identities against its current canonical root."""
    if not isinstance(spec, dict) or not isinstance(spec.get("delivery"), dict) \
            or not isinstance(spec.get("parts"), dict):
        return False
    root = reply_thread_target(db, card)
    delivery = spec["delivery"]
    parts = spec["parts"]
    return (root is not None and card["thread_state"] == "created"
            and card["thread_id"] == root["thread_id"]
            and parts.get("source_thread") is True
            and delivery.get("thread_id") == root["thread_id"]
            and delivery.get("message_id") == card["message_id"]
            and notify_scope.scope_matches(card, delivery))


def wake_ready_reply_holds(db, now, limit: int = 200) -> int:
    """Re-arm reply-card intents parked on their parent's thread once that
    thread is proven (delivered card, created thread, same root/scope).

    A held reply waits RESEAT_S; without this the parent's receipt would
    leave it parked for up to an hour. Only never-issued children qualify
    (no live render, no remote identity); dispatch re-checks every gate.
    """
    rows = db.execute(
        "SELECT DISTINCT o.event_id, c.card_id FROM notify_outbox o "
        "JOIN notification_intent_cards ic ON ic.event_id=o.event_id AND ic.state='pending' "
        "JOIN notification_cards c ON c.card_id=ic.card_id "
        "WHERE o.state='pending' AND o.next_try > ? AND json_valid(o.progress) "
        "AND json_extract(o.progress,'$.thread_hold')='source_thread_not_ready' "
        "AND instr(c.card_key,'|reply:') > 0 AND c.message_id IS NULL "
        "AND c.delivery_state!='revoked' AND ic.delivery_id IS NULL "
        # narrow to parents already proven before the LIMIT, so an older
        # unready backlog can never starve a ready reply behind it
        "AND EXISTS (SELECT 1 FROM notification_cards p "
        "WHERE p.card_key=substr(c.card_key,1,instr(c.card_key,'|reply:')-1) "
        "AND p.kind='thread' AND p.project_id=c.project_id AND p.transport=c.transport "
        "AND p.delivery_state IN ('delivered','update_failed') AND p.message_id IS NOT NULL "
        "AND p.thread_state='created' AND p.thread_id IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM notification_renders r WHERE r.card_id=c.card_id "
        "AND r.state IN (" + ",".join("?" * len(LIVE_RENDER)) + ")) "
        "ORDER BY o.event_id LIMIT ?", (now, *LIVE_RENDER, limit)).fetchall()
    woken = set()
    for event_id, card_id in rows:
        if event_id in woken:
            continue
        card = notify_scope.card_row(db, card_id)
        if card is not None and reply_thread_target(db, card) is not None:
            db.execute("UPDATE notify_outbox SET next_try=? WHERE event_id=? AND state='pending'",
                       (now, event_id))
            woken.add(event_id)
    return len(woken)


def _hold_pending_for_thread(db, card_id, now) -> None:
    """Defer the card's pending intents until its source thread is ready."""
    for event in db.execute("SELECT event_id FROM notification_intent_cards WHERE card_id=? "
                            "AND state='pending'", (card_id,)).fetchall():
        db.execute("UPDATE notify_outbox SET progress=?,next_try=?,updated_at=? WHERE event_id=?",
                   (json.dumps({"thread_hold": "source_thread_not_ready"}), now + RESEAT_S, now, event[0]))


def _bind_reply_thread(db, card, now) -> tuple[dict, bool]:
    """Bind an undelivered reply once; retain stale bindings and wait for proof."""
    root = reply_thread_target(db, card)
    if (root is not None and card["thread_state"] == "created"
            and card["thread_id"] == root["thread_id"]):
        return card, False
    if root is not None and not card["thread_id"] and not card["message_id"]:
        db.execute("UPDATE notification_cards SET thread_id=?,thread_state='created',updated_at=? "
                   "WHERE card_id=?", (root["thread_id"], now, card["card_id"]))
        return notify_scope.card_row(db, card["card_id"]), False
    # Native messages cannot move threads. Keep any delivered/unknown remote
    # identity intact, and never turn an unproven parent into a channel post.
    _hold_pending_for_thread(db, card["card_id"], now)
    return card, True


def _bind_signal_thread(db, card, cfg, now) -> tuple[dict, bool, bool]:
    """Return (card, changed, wait) after borrowing the caller's transaction.

    The input card is unchanged; a moved route returns its updated copy.
    An unchanged route may render, while wait keeps issuance paused.
    """
    target = signal_thread_target(db, card, cfg)
    if target is None:
        waited = now - (card["created_at"] or now)
        if notify_scope.notify_cfg(cfg).get("card_thread") is not True:
            # no new thread will open here (a thread created while the
            # switch was on is still used above): nothing to wait for
            waited = SIGNAL_SOURCE_HOLD_MAX_S + 1
        if waited <= SIGNAL_SOURCE_HOLD_MAX_S:
            _hold_pending_for_thread(db, card["card_id"], now)
            return card, False, True
        # a post whose card never arrives (history/probe import, a
        # suppressed notification) would hold the alert forever: after
        # the bounded wait it goes to the channel instead (owner
        # 2026-10-08 — an unreachable alert is worse than one outside
        # its source thread). A bound thread that is gone (deleted, or
        # its evidence post deleted) falls back the same way.
        if card["thread_id"] is None:
            return card, False, False
        route = _signal_thread_route(db, card)
        if route == "card":
            return card, False, False
        if route is None:
            return card, False, True
        return _move_signal(db, card, None, now)
    if card["thread_id"] == target["thread_id"] and card["thread_state"] == "created":
        return card, False, False
    return _move_signal(db, card, target["thread_id"], now)


def _move_signal(db, card, thread_id, now) -> tuple[dict, bool, bool]:
    """Re-home a signal card in ``thread_id`` (the channel when None).
    Native posts cannot move threads: the old post and its audit rows
    stay, its controls retire, and the replacement is created on the new
    route. Return (new_card, changed, wait) without changing the input;
    wait is true while delivered parts are still settling. Borrow the
    caller's transaction without committing or publishing.
    """
    latest = db.execute("SELECT * FROM notification_renders WHERE card_id=? ORDER BY render_rev DESC LIMIT 1", (card["card_id"],)).fetchone()
    if _parts_in_flight(db, latest):
        return card, False, True
    state = "created" if thread_id else "none"
    db.execute("UPDATE notification_action_tokens SET expires_at=? WHERE card_id=? AND expires_at>?", (now, card["card_id"], now))
    db.execute("UPDATE notification_cards SET message_id=NULL,thread_id=?,thread_state=?,delivery_state='pending',updated_at=? WHERE card_id=?", (thread_id, state, now, card["card_id"]))
    return {**card, "message_id": None, "thread_id": thread_id,
            "thread_state": state, "delivery_state": "pending"}, True, False


def _dispatch_urgent_notice(ledger, ev, cfg, now):
    """Journal a thread-only alert; an unavailable thread never becomes a channel post."""
    import alert_view
    import notify_urgent
    db, event_id = ledger.db, ev["event_id"]
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is None or row["state"] not in ("pending", "failed"):
            return {"skipped": True}
        renders = _cancel_moved_notices(db, event_id, _notice_renders(db, event_id), cfg, now)
        checked = notify_urgent.check_delivery(ledger, cfg, row, now=now)
        if not checked["ok"]:
            if _notice_unsent(db, renders):
                _close_notice(db, event_id, renders, now)
                return {"suppressed": True}
            _reseat(db, event_id, now, "alert_outcome_pending")
            return {"parked": "alert_outcome_pending"}
        scope = urgent_thread_target(ledger, row, cfg)
        if scope is not None:
            _clear_thread_hold(db, "event_id=?", event_id)
        if scope is None:
            # only Slack/Discord reach here: a room transport's target is its scope
            reason = "source_thread_not_ready"
            age = now - (row["created_at"] or now)
            if age > SIGNAL_SOURCE_HOLD_MAX_S:
                # the thread never came (card_thread off, the post's card
                # suppressed): a thread-only alert parked forever would
                # occupy a flush slot and the daily quota for nothing
                if _notice_unsent(db, renders):
                    _close_notice(db, event_id, renders, now)
                    return {"suppressed": reason}
                # a granted/unknown send must stay open for its receipt
                held = "alert_render_live" if any(render["state"] in LIVE_RENDER for render in renders) else "alert_outcome_pending"
                _reseat(db, event_id, now, held)
                return {"parked": held}
            # the card usually lands within minutes; afterwards an hourly
            # look is enough and keeps the due queue free for other sends
            db.execute("UPDATE notify_outbox SET next_try=?,progress=?,updated_at=? WHERE event_id=?",
                       (now + (60 if age < 600 else RESEAT_S), json.dumps({"thread_hold": reason}), now, event_id))
            return {"parked": reason}
        live = [render for render in renders if render["state"] in LIVE_RENDER]
        if live:
            specs.extend(json.loads(render["spec_json"]) for render in live
                         if render["state"] == "queued" and not render["spec_published"])
            _reseat(db, event_id, now, "alert_render_live")
        elif _notice_failures(db, event_id) >= MAX_RESEND:
            db.execute("UPDATE notify_outbox SET state='failed',next_try=NULL," + HOLD_PROGRESS_SET +
                       ",updated_at=? WHERE event_id=?", ("resend_exhausted", now, event_id))
            return {"error": "resend_exhausted"}
        else:
            parts = alert_view.render_parts(checked, db)
            present = presenter(notify_scope.active_transport(cfg))
            if not present.SOURCE_THREAD:
                parts = present.urgent_room_head(parts)
            spec = _notice_spec(event_id, parts, cfg, scope, len(renders) + 1, notify_scope.active_transport(cfg))
            if present.SOURCE_THREAD:
                spec["parts"]["thread_notice"] = True
            payload = json.loads(row["payload"])
            db.execute("INSERT OR IGNORE INTO notification_intent_batches("
                       "event_id,frozen_payload,payload_hash,route_epoch,sealed_at,transport,scope_json) "
                       "VALUES(?,?,?,?,?,?,?)", (event_id, canonical(payload).decode(), payload_hash(payload),
                       notify_scope.route_epoch(cfg), now, notify_scope.active_transport(cfg), canonical(notify_scope.delivery_scope(cfg)).decode()))
            if _persist_notice_render(db, spec, event_id, now):
                specs.append(spec)
                db.execute("UPDATE notify_outbox SET route='interactive',progress=NULL WHERE event_id=?", (event_id,))
                _reseat(db, event_id, now, "alert_render_issued")
        mark_snapshot_dirty(db)
    notify_spool.publish_committed(ledger, db, specs, now)
    return {"dispatched": bool(specs)}


def dispatch_intent(ledger, ev, cfg, now=None) -> dict:
    """Freeze an interactive intent: sealed batch -> card fan-out ->
    immutable render -> atomic spec publication. Idempotent re-entry on
    a sealed intent repairs missing spec files and re-checks completion."""
    now = time.time() if now is None else now
    if ev["kind"] == "urgent_notice":
        return _dispatch_urgent_notice(ledger, ev, cfg, now)
    if ev["kind"] in NOTICE_KINDS:
        return _dispatch_notice(ledger, ev, cfg, now)
    db = connection(ledger)
    root = notify_spool.data_root(ledger)
    dirs = notify_spool.notify_dirs(root)
    notify_spool.ensure_dirs(root)
    scope = notify_scope.delivery_scope(cfg)
    epoch = notify_scope.route_epoch(cfg)
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
        if row["kind"] == "signal" and not notify_scope.signals_notify(cfg):
            if batch is None:
                db.execute(
                    "UPDATE notify_outbox SET state='suppressed',"
                    "next_try=NULL,updated_at=? WHERE event_id=?",
                    (now, event_id))
                mark_snapshot_dirty(db)
                return {"suppressed": True}
            # sealed with signals off: re-examine hourly, never every
            # flush — a due-forever intent would starve outbox_due slots
            _reseat(db, event_id, now, "signals_off_sealed")
            return {"skipped": True}
        if batch is not None:
            if notify_scope.interactive_enabled(cfg) and batch["transport"] != notify_scope.active_transport(cfg):
                return {"error": "transport_mismatch"}
            # off: notify_scope.active_transport() falls back to discord, so the
            # stored scope can't match — fall through to reseat instead
            if notify_scope.interactive_enabled(cfg) \
                    and batch["transport"] in ("slack", "lineworks") \
                    and batch["scope_json"] != canonical(scope).decode():
                return {"error": "scope_mismatch"}
            # A held signal resumes after its original source thread becomes
            # deliverable; no fresh event or new patient room is necessary.
            if row["kind"] in ("signal", "patient_history"):
                for pending in db.execute("SELECT card_id FROM notification_intent_cards WHERE event_id=? AND state='pending'", (event_id,)).fetchall():
                    issue_render(db, pending[0], cfg, now, specs)
                outcome["dispatched"] = bool(specs)
            elif row["kind"] == "new_messages":
                # Initial root and replies share one intent. A root receipt
                # can leave a reply without any render; resume only those
                # unbound replies when the already sealed event re-enters.
                for pending in db.execute(
                        "SELECT card_id FROM notification_intent_cards WHERE event_id=? "
                        "AND state='pending' AND delivery_id IS NULL", (event_id,)).fetchall():
                    card = notify_scope.card_row(db, pending[0])
                    if card is not None and reply_source(card):
                        issue_render(db, pending[0], cfg, now, specs)
                outcome["dispatched"] = bool(specs)
            elif row["kind"] == "attachment_followup":
                # Retry only unbound work after existing attempts/parts settle.
                # A bound held/unknown plan remains owned by its receipt path.
                if not notify_parts.late_attachment_thread(cfg):
                    return {"error": "attachment_thread_unavailable"}
                for pending in db.execute(
                        "SELECT card_id FROM notification_intent_cards WHERE event_id=? "
                        "AND state='pending' AND delivery_id IS NULL", (event_id,)).fetchall():
                    card = notify_scope.card_row(db, pending[0])
                    if card is None or card["delivery_state"] == "revoked" \
                            or card["thread_state"] != "created" or not card["thread_id"]:
                        return {"error": "attachment_thread_unavailable"}
                    issue_render(db, pending[0], cfg, now, specs, force=True)
                outcome["dispatched"] = bool(specs)
            # sealed already — a flush re-entry repairs + completes
            if complete_intent(db, event_id, now) is None:
                _reseat(db, event_id, now, "intent_cards_unsettled")
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
                    f"next_try=NULL,{HOLD_PROGRESS_SET},updated_at=? "
                    "WHERE event_id=?",
                    ("payload_invalid", now, event_id))
                return {"error": "payload_invalid"}
            targets = _resolve_targets(db, row, frozen, cfg=cfg)
            if row["kind"] == "new_messages":
                targets = _split_reply_targets(db, targets, scope, cfg)
            if row["kind"] == "attachment_followup":
                if not notify_parts.late_attachment_thread(cfg):
                    return {"error": "attachment_thread_unavailable"}
                targets = [target for target in targets
                           if notify_scope.scope_matches(notify_scope.card_row(db, target["card_id"]), scope)]
                if len(targets) != 1:
                    return {"error": "attachment_thread_unavailable"}
                if targets[0]["text_fallback"]:
                    return {"error": "attachment_thread_unavailable", "text_fallback": True}
                frozen = {**frozen, "attachment_sha256": targets[0]["attachment_sha256"]}
            db.execute(
                "INSERT INTO notification_intent_batches("
                "event_id,frozen_payload,payload_hash,route_epoch,sealed_at,"
                "transport,scope_json) VALUES(?,?,?,?,?,?,?)",
                (event_id, canonical(frozen).decode(),
                 payload_hash(frozen), epoch, now, notify_scope.active_transport(cfg),
                 canonical(scope).decode() if notify_scope.active_transport(cfg) in ("slack", "lineworks") else None))
            card_ids = []
            covered = set()
            for target in targets:
                cid = (target["card_id"] if row["kind"] == "attachment_followup"
                       else _card_for(db, target, scope, now))
                db.execute(
                    "INSERT OR IGNORE INTO notification_intent_cards("
                    "event_id,card_id,coverage) VALUES(?,?,?)",
                    (event_id, cid,
                     json.dumps(target["coverage"],
                                ensure_ascii=False)))
                # still archived: a revoked card can never deliver this
                # coverage — suppress it so the intent can complete
                db.execute(
                    "UPDATE notification_intent_cards SET state='suppressed' "
                    "WHERE event_id=? AND card_id=? AND state='pending' "
                    "AND EXISTS(SELECT 1 FROM notification_cards c WHERE "
                    "c.card_id=? AND c.delivery_state='revoked')",
                    (event_id, cid, cid))
                card_ids.append(cid)
                covered.update(target["coverage"])
            if card_ids:
                if row["kind"] == "patient_history":
                    notify_history.coalesce_tx(db, notify_scope.card_row(db, card_ids[0]), scope, epoch, now)
                for cid in card_ids:
                    issue_render(db, cid, cfg, now, specs,
                                  force=row["kind"] == "attachment_followup")
                if row["kind"] == "new_messages":
                    from notify_urgent import capture_initial
                    capture_initial(ledger, cfg, {
                        "event_id": event_id, "kind": row["kind"],
                        "project_id": row["project_id"],
                        "payload": json.dumps({**frozen, "message_ids": sorted(covered)}),
                    }, now=now)
                _reseat(db, event_id, now, "intent_cards_issued")
                outcome.update(dispatched=True, cards=len(card_ids))
            else:
                # nothing deliverable — suppress like a stale text send
                db.execute(
                    "UPDATE notify_outbox SET state='suppressed',"
                    "next_try=NULL,updated_at=? WHERE event_id=?",
                    (now, event_id))
                outcome["suppressed"] = True
            complete_intent(db, event_id, now)
        mark_snapshot_dirty(db)
    notify_spool.publish_specs(db, dirs, specs, now)
    return outcome


def revert_to_text(ledger, event_id, now=None, *, expected=None) -> bool:
    """Kill-switch path: an UNSEALED interactive intent is provably
    unsent (no batch row, no card, no attempt), so converting its route
    to text can never double-deliver. A sealed intent is untouched."""
    now = time.time() if now is None else now
    db = connection(ledger)
    predicate = ""
    params = [now, event_id]
    if expected is not None:
        try:
            params.extend(expected[key] for key in ("state", "next_try", "attempts", "payload", "progress"))
        except (KeyError, IndexError, TypeError):
            return False
        predicate = " AND state IS ? AND next_try IS ? AND attempts IS ? AND payload IS ? AND progress IS ?"
    with db:
        cur = db.execute(
            """UPDATE notify_outbox SET route='text',updated_at=?
               WHERE event_id=? AND route='interactive' AND kind!='patient_history'
                 AND NOT EXISTS(SELECT 1 FROM notification_intent_batches b
                                WHERE b.event_id=notify_outbox.event_id)""" + predicate,
            params)
        return cur.rowcount == 1


# ---------- command-side apply ----------


def rerender_message_cards(ledger, cfg, project_id, message_id,
                           now=None) -> list:
    """Issue the next render of the live thread card holding a message
    (root or reply) — its footer shows a task/report that just landed.
    The drain's bounded sweep alone may not reach it among many cards."""
    if not positive(project_id) or not positive(message_id):
        return []
    db = connection(ledger)
    now = time.time() if now is None else now
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        root = _thread_root(db, message_id, project_id)
        membership = db.execute("SELECT content FROM message_metadata WHERE message_id=? AND source='capture'", (message_id,)).fetchone()
        role = ((loads_dict(membership[0]) or {}).get("_initial_import") or {}) if membership else {}
        if role.get("role") == "history":
            for history in db.execute("SELECT * FROM notification_cards WHERE kind='patient_history' AND project_id=? "
                    "AND delivery_state!='revoked'", (project_id,)).fetchall():
                notify_history.analysis_changed(db, history, cfg, now)
        for r in db.execute(
                "SELECT card_id FROM notification_cards WHERE kind='thread' "
                "AND project_id=? AND root_message_id=? "
                "AND delivery_state!='revoked'",
                (project_id, root)).fetchall():
            issue_render(db, r["card_id"], cfg, now, specs)
        if specs:
            mark_snapshot_dirty(db)
    notify_spool.publish_committed(ledger, db, specs, now)
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
    if not notify_scope.interactive_enabled(cfg):
        return 0
    from datetime import datetime
    from mcs_queries import JST
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, JST)
    today = local.date().isoformat()
    db = connection(ledger)
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
    args = (today, notify_scope.active_transport(cfg), today)
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
                      r.due_date, m.sender_name, m.organization, m.posted_at {pending}
               ORDER BY r.due_date, r.request_id LIMIT ?""",
            (*args, limit)).fetchall()
        for r in rows:
            stage = "due" if r["due_date"] == today else "overdue"
            head = (f"⏰ 期限リマインド（本日 {r['due_date']}）" if stage == "due"
                    else f"⚠ 期限切れ（期限 {r['due_date']}）")
            header = preview_header(db, r["project_id"], r)
            text = (plain_notice(preview_line(header, f"{_plain(r['title'])} — {head}"), 600)
                    + f" — 担当 {_plain(r['assignee'] or '未設定')}")
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


def task_reminder_stale(ledger, cfg, event_id, now=None) -> str | None:
    """Send-time recheck of a queued task reminder: None while it is still
    true, otherwise the stale reason, or 'outside_hours' to wait."""
    if not notify_scope.interactive_enabled(cfg):
        return "interactive_off"
    from datetime import datetime
    from mcs_queries import JST
    local = datetime.fromtimestamp(time.time() if now is None else now, JST)
    db = connection(ledger)
    mark = db.execute(
        "SELECT request_id,stage,due_date FROM notification_task_reminders "
        "WHERE event_id=? AND stage IN ('due','overdue')",
        (event_id,)).fetchone()
    if mark is None:
        return "reminder_record_missing"
    today = local.date().isoformat()
    # a due-day text says 本日 — the next day only the overdue one is true
    if (mark["due_date"] != today) if mark["stage"] == "due" \
            else not mark["due_date"] < today:
        return "reminder_stage_passed"
    live = db.execute(
        """SELECT 1 FROM requests r
           JOIN messages m ON m.message_id=r.source_message_id
            AND m.project_id=r.project_id
           WHERE r.request_id=? AND r.status IN ('open','in_progress')
             AND r.due_date=?
             AND EXISTS (SELECT 1 FROM notification_cards c
                 WHERE c.kind='thread' AND c.project_id=r.project_id
                   AND c.transport=? AND c.delivery_state!='revoked'
                   AND c.root_message_id IN (m.message_id, m.parent_id))""",
        (mark["request_id"], mark["due_date"],
         notify_scope.active_transport(cfg))).fetchone()
    if live is None:
        return "task_not_pending"
    if not REMINDER_HOURS[0] <= local.hour < REMINDER_HOURS[1]:
        return "outside_hours"
    return None


def _plain(text) -> str:
    return plain_notice(text, 200)


# ---------- sweep / GC / watchdog / health ----------

def revoke_card(db, card_id, now) -> list:
    """Revocation decision: flag the card once, then suppress any still-
    pending coverage — it can never legitimately deliver now, and an
    intent must not stay pending forever on a revoked card. Returns the
    (delivery_id, transport) renders whose unsent parts it held; the
    caller withdraws their spec files after commit."""
    cur = db.execute(
        "UPDATE notification_cards SET delivery_state='revoked',"
        "revoked_at=?,updated_at=? WHERE card_id=? "
        "AND delivery_state!='revoked' AND revoked_at IS NULL",
        (now, now, card_id))
    if not cur.rowcount:
        return []
    # a queued-but-unsent render must not linger claimable — a worker
    # picking it up would post a card for an archived/dead unit
    _cancel_open_renders(db, card_id, now)
    # A delivered card's unsent thread/body/attachment parts would
    # otherwise resume from the retained spec. 'held' marks them
    # planned-but-blocked; parts the worker already started keep their
    # journaled truth for reconcile.
    withdrawn = db.execute(
        "SELECT DISTINCT r.delivery_id,r.transport FROM notification_renders r "
        "JOIN notification_render_parts p ON p.delivery_id=r.delivery_id "
        "WHERE r.card_id=? AND p.state='pending' AND p.kind!='card'",
        (card_id,)).fetchall()
    for r in withdrawn:
        db.execute(
            "UPDATE notification_render_parts SET state='held',"
            "error_code='card_revoked',updated_at=? WHERE delivery_id=? "
            "AND state='pending' AND kind!='card'", (now, r["delivery_id"]))
        notify_parts.update_state(db, r["delivery_id"], now)
    db.execute(
        "UPDATE notification_intent_cards SET state='suppressed' "
        "WHERE card_id=? AND state='pending'", (card_id,))
    for r in db.execute(
            "SELECT DISTINCT event_id FROM notification_intent_cards "
            "WHERE card_id=?", (card_id,)).fetchall():
        complete_intent(db, r["event_id"], now)
    return [(r["delivery_id"], r["transport"]) for r in withdrawn]


def sweep(ledger, cfg, limit=100, now=None) -> dict:
    """Tick-side change detection over live cards: edited/deleted
    thread messages, signal state transitions, archive revokes,
    defer_until arrival, and renders stuck unpublished. Bounded per
    call: at most limit ordinary cards and limit history checks, with
    at most two history renders.
    Each group rotates independently; cards with an unsettled attempt
    are left to the settle path."""
    db = connection(ledger)
    now = time.time() if now is None else now
    root = notify_spool.data_root(ledger)
    dirs = notify_spool.notify_dirs(root)
    notify_spool.ensure_dirs(root)
    specs = []
    withdrawn = []
    scanned = updated = 0
    with db:
        db.execute("BEGIN IMMEDIATE")
        cards = db.execute(
            """SELECT card_id,kind,project_id,delivery_state FROM
                 notification_cards
               WHERE kind!='patient_history' AND delivery_state IN ('pending','delivered',
                                        'update_failed','delivery_unknown',
                                        'message_deleted','revoked')
               ORDER BY updated_at,card_id LIMIT ?""", (max(0, limit),)).fetchall()
        cards += db.execute(
            """SELECT card_id,kind,project_id,delivery_state FROM notification_cards
               WHERE kind='patient_history' AND delivery_state IN ('pending','delivered',
                                        'update_failed','delivery_unknown',
                                        'message_deleted','revoked')
               ORDER BY updated_at,card_id LIMIT ?""", (max(0, limit),)).fetchall()
        scanned = len(cards)
        history_renders = 0
        for row in cards:
            card = notify_scope.card_row(db, row["card_id"])
            if card is None:
                continue
            if positive(card["project_id"]) and db.execute(
                    "SELECT 1 FROM patients WHERE project_id=? "
                    "AND is_archived=1", (card["project_id"],)).fetchone():
                withdrawn += revoke_card(db, card["card_id"], now)
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
            history_due = card["kind"] == "patient_history" and notify_history.refresh_needed(db, card, now)
            history_deferred = history_due and history_renders >= 2
            if card["kind"] != "patient_history" or history_due and not history_deferred:
                issue_render(db, card["card_id"], cfg, now, specs)
                history_renders += card["kind"] == "patient_history"
            if card["kind"] == "patient_history":
                notify_history.emit_completion_tx(ledger, notify_scope.card_row(db, card["card_id"]), now)
            after = notify_scope.card_row(db, card["card_id"])
            if (after["source_generation"], after["presentation_generation"],
                    after["desired_render_rev"]) != before:
                updated += 1
            # Rotate inert history too; quota-deferred work keeps its place
            # ahead of the two renders just processed in this separate window.
            if not history_deferred:
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
    # The worker never reads the DB: a withdrawn spec file is what stops
    # it sending revoked parts. Withdraw before publishing, so a failed
    # publish (disk full, permissions) cannot leave it in place; a crash
    # before this leaves gc to do it.
    for delivery_id, transport in withdrawn:
        with suppress(OSError):
            os.unlink(os.path.join(dirs[transport + "_render"],
                                   delivery_id + ".json"))
    notify_spool.publish_specs(db, dirs, specs, now)
    return {"scanned": scanned, "updated": updated,
            "republished": len(specs)}


def gc(ledger, cfg=None, now=None, limit=500) -> dict:
    """Delete expired action tokens and strip PHI-bearing spec_json
    (containers and thread_body_parts) once nothing can replay it:
    superseded renders (cancelled/not_sent with a delivered successor)
    and delivered renders whose durable parts are all settled and no
    active restore hold covers the render or its card — in both cases
    only with no unsettled attempt on the card and no pending intent
    binding. Delivered signal creates retain only a content-free thread-route
    witness. DB readers of spec_json (dispatch re-entry, sweep watchdog,
    recover) republish queued/held renders or delivered plans with
    pending parts; the latter cannot enter this clearing path. The
    worker and reconcile read the spec file, which a pending part plan
    keeps. Slack attachment edits rejected before writing keep both specs
    until their unknown result is resolved. Rows, hashes,
    correlations and part metadata stay for audit. Bounded per call —
    the rest waits for the next tick."""
    db = connection(ledger)
    now = time.time() if now is None else now
    with db:
        tokens = db.execute(
            "DELETE FROM notification_action_tokens WHERE expires_at<?",
            (now,)).rowcount
        cleared = 0
        for r in db.execute(
                """SELECT r.delivery_id,r.op,r.state,r.spec_json,c.kind FROM notification_renders r
                   LEFT JOIN notification_cards c ON c.card_id=r.card_id
                   WHERE r.spec_json IS NOT NULL
                     AND CASE WHEN json_valid(r.spec_json) THEN NOT (
                       json_type(r.spec_json,'$.signal_thread_route') IS NOT NULL
                       AND json_type(r.spec_json,'$.thread_id') IS NOT NULL
                       AND (SELECT COUNT(*) FROM json_each(r.spec_json))=2) ELSE 1 END
                     AND NOT EXISTS (
                       SELECT 1 FROM notification_delivery_attempts a
                       JOIN notification_renders active
                         ON active.delivery_id=a.delivery_id
                       WHERE active.card_id=r.card_id
                         AND a.state IN ('granted','unknown'))
                     AND NOT EXISTS (
                       SELECT 1 FROM notification_intent_cards ic
                       WHERE ic.delivery_id=r.delivery_id AND ic.state='pending')
                     AND NOT EXISTS (
                       SELECT 1 FROM notification_render_parts p
                       WHERE p.delivery_id=r.delivery_id AND r.transport='slack'
                         AND p.kind='attachment_part' AND p.state='unknown'
                         AND p.error_code IN ('edit_target_unverified','edit_history_unverified'))
                     AND ((r.card_id IS NULL AND r.op='notice'
                           AND r.state IN ('cancelled','not_sent'))
                          OR (r.state IN ('cancelled','not_sent')
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
            witness = (_signal_route_witness(r["spec_json"]) if r["kind"] == "signal"
                       and r["op"] == "create" and r["state"] == "delivered" else None)
            db.execute(
                "UPDATE notification_renders SET spec_json=?,"
                "updated_at=? WHERE delivery_id=?",
                (canonical(witness).decode() if witness is not None else None, now, r["delivery_id"]))
            cleared += 1
        # spec files for terminal renders can go — a begin against them
        # would be denied anyway; unknown stays for investigation. A
        # render whose durable parts are still pending keeps its spec:
        # the worker never reads the DB, so the file is the only copy
        # its restart-resume can replay — removing it would strand
        # parts the journal could still finish.
        removed = 0
        root = notify_spool.data_root(ledger)
        dirs = notify_spool.notify_dirs(root)
        for r in db.execute(
                "SELECT delivery_id,transport FROM notification_renders r "
                "WHERE state IN ('delivered','not_sent','cancelled') "
                "AND spec_published=1 AND parts_state!='pending' "
                "AND NOT EXISTS (SELECT 1 FROM notification_render_parts p "
                "WHERE p.delivery_id=r.delivery_id AND p.state='pending') "
                "AND NOT EXISTS (SELECT 1 FROM notification_render_parts p "
                "WHERE p.delivery_id=r.delivery_id AND r.transport='slack' "
                "AND p.kind='attachment_part' AND p.state='unknown' "
                "AND p.error_code IN ('edit_target_unverified','edit_history_unverified')) "
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
    # Names are random uuids, so pick candidates by age, oldest first
    # — a name-sorted window would stall once it held only fresh files.
    # Outside the write transaction: ~20k entries must not hold the
    # ledger lock a card click or tick needs.
    results = 0
    expired = []
    try:
        with os.scandir(dirs["cmd_results"]) as it:
            for e in it:
                if not e.name.endswith(".json"):
                    continue
                with suppress(OSError):
                    mtime = e.stat(follow_symlinks=False).st_mtime
                    if now - mtime > action_tokens.TOKEN_WRITE_S:
                        expired.append((mtime, e.path))
    except OSError:
        pass
    expired.sort()
    for _mtime, path in expired[:limit]:
        with suppress(OSError):
            os.unlink(path)
            results += 1
    return {"tokens": tokens, "spec_json_cleared": cleared,
            "spec_files": removed, "result_files": results}
