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
from notify_card_state import (
    LIVE_RENDER as LIVE_RENDER,
    MAX_RESEND as MAX_RESEND,
    RESEAT_S as RESEAT_S,
    _REAL_FAILURE as _REAL_FAILURE,
    _WAIT_PROGRESS_SET as _WAIT_PROGRESS_SET,
    _cancel_open_renders as _cancel_open_renders,
    _cancel_render as _cancel_render,
    _parts_in_flight as _parts_in_flight,
    _reseat as _reseat,
    _restore_hold_active as _restore_hold_active,
    clear_snapshot_dirty as clear_snapshot_dirty,
    complete_intent as complete_intent,
    connection as connection,
    mark_snapshot_dirty as mark_snapshot_dirty,
    release_holds as release_holds,
    resend_exhausted as resend_exhausted,
    snapshot_dirty as snapshot_dirty,
    unsettled_attempt as unsettled_attempt
)
from notify_card_threads import (
    SIGNAL_SOURCE_HOLD_MAX_S as SIGNAL_SOURCE_HOLD_MAX_S,
    _bind_reply_thread as _bind_reply_thread,
    _bind_signal_thread as _bind_signal_thread,
    _clear_thread_hold as _clear_thread_hold,
    _hold_pending_for_thread as _hold_pending_for_thread,
    _move_signal as _move_signal,
    _signal_evidence_mid as _signal_evidence_mid,
    _signal_route_witness as _signal_route_witness,
    _signal_thread_route as _signal_thread_route,
    _source_thread_target as _source_thread_target,
    _thread_binding_creates as _thread_binding_creates,
    _thread_root as _thread_root,
    reply_thread_matches as reply_thread_matches,
    reply_thread_target as reply_thread_target,
    signal_thread_target as signal_thread_target,
    urgent_thread_target as urgent_thread_target,
    wake_ready_reply_holds as wake_ready_reply_holds
)
from notify_card_renders import (
    LINEWORKS_SUMMARY_WAIT_S as LINEWORKS_SUMMARY_WAIT_S,
    MAX_BUTTONS as MAX_BUTTONS,
    MAX_COMPONENTS as MAX_COMPONENTS,
    MAX_POST_ACTIONS as MAX_POST_ACTIONS,
    MAX_ROWS as MAX_ROWS,
    _action_rows as _action_rows,
    _build_spec as _build_spec,
    _digest_thread_name as _digest_thread_name,
    _generation_drift as _generation_drift,
    _manual_attachment_hold_ready as _manual_attachment_hold_ready,
    _persist_render as _persist_render,
    _post_action_targets as _post_action_targets,
    _post_actions as _post_actions,
    _presentation_fp as _presentation_fp,
    _ready_intent_ids as _ready_intent_ids,
    _render_gates as _render_gates,
    _render_needed as _render_needed,
    _render_op as _render_op,
    _stamp_enabled as _stamp_enabled,
    _stamp_target as _stamp_target,
    _thread_name as _thread_name,
    issue_render as issue_render
)
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

from mcs_queries import HOLD_PROGRESS_SET
from mcs_requests import canonical, payload_hash, positive
from mcs_util import loads_dict, strict_json, valid_hash, valid_text
from safe_io import read_private_bytes
from notify_render import (
    card_content as card_content, content_fingerprint as content_fingerprint,
    anchor_keys, preview_header, preview_line, fit_parts,
    plain_notice, presenter, reply_source, own_post as own_post)
import notify_parts
import notify_action_tokens as action_tokens


# outbox kinds that become interactive cards when notify.interactive is
# on; everything else (ops alerts, semantic notices) stays legacy text.
# kinds delivered as one card-less ``op=notice`` render (no card row,
# manifest, tokens or thread) — see _dispatch_notice
NOTICE_KINDS = frozenset({"daily_digest"})


DISPATCH_GROUP_LIMIT = 10  # the ordinary flush window, without a new configuration knob


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


# ---------- cards / renders ----------


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
    """Keep Slack replies in root threads; Discord follows the configured switches."""
    transport = notify_scope.active_transport(cfg)
    return (transport == "slack" or (transport in REPLY_CARD_TRANSPORTS
            and notify_scope.notify_cfg(cfg).get("reply_cards", REPLY_CARDS_DEFAULT) is True
            and notify_scope.notify_cfg(cfg).get("card_thread") is True))


def _split_reply_targets(db, targets, scope, cfg):
    """Give each later reply of a layout-4 root its own card in the root's thread.

    Slack roots use reply cards even for old layouts. Discord retains its
    stored-layout behavior. The root target keeps the whole coverage for
    intent completion and the root-thread attachment route."""
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
        if scope.get("transport") != "slack" and ((row["layout"] or 1) < 4 if row is not None else not on):
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


def _dispatch_connection(ledger):
    db = connection(ledger)
    if db.in_transaction:
        raise ValueError("notification_dispatch_transaction_active")
    return db


def dispatch_intent(ledger, ev, cfg, now=None) -> dict:
    """Freeze an interactive intent: sealed batch -> card fan-out ->
    immutable render -> atomic spec publication. Idempotent re-entry on
    a sealed intent repairs missing spec files and re-checks completion."""
    db = _dispatch_connection(ledger)
    now = time.time() if now is None else now
    if ev["kind"] == "urgent_notice":
        return _dispatch_urgent_notice(ledger, ev, cfg, now)
    if ev["kind"] in NOTICE_KINDS:
        return _dispatch_notice(ledger, ev, cfg, now)
    root = notify_spool.data_root(ledger)
    dirs = notify_spool.notify_dirs(root)
    notify_spool.ensure_dirs(root)
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        outcome = _dispatch_intent_tx(ledger, ev, cfg, now, specs)
    notify_spool.publish_specs(db, dirs, specs, now)
    return outcome


def _dispatch_intent_tx(ledger, ev, cfg, now, specs) -> dict:
    """Freeze one ordinary intent in the caller's write transaction."""
    spec_start = len(specs)
    db = connection(ledger)
    scope = notify_scope.delivery_scope(cfg)
    epoch = notify_scope.route_epoch(cfg)
    outcome = {"dispatched": False, "cards": 0}
    event_id = ev["event_id"]
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
            outcome["dispatched"] = len(specs) > spec_start
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
            outcome["dispatched"] = len(specs) > spec_start
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
            outcome["dispatched"] = len(specs) > spec_start
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
    return outcome


def dispatch_intents(ledger, events, cfg, now=None, *, deadline=None) -> list:
    """Commit up to one flush window of ordinary intents before publishing their specs."""
    if len(events) > DISPATCH_GROUP_LIMIT or any(
            ev["kind"] != "new_messages" or ev["route"] != "interactive" for ev in events):
        raise ValueError("notification_dispatch_group_invalid")
    if not events:
        return []
    db = _dispatch_connection(ledger)
    root = notify_spool.data_root(ledger)
    dirs = notify_spool.notify_dirs(root)
    notify_spool.ensure_dirs(root)
    specs, outcomes = [], []
    now = time.time() if now is None else now
    with db:
        db.execute("BEGIN IMMEDIATE")
        for ev in events:
            if deadline is not None and time.monotonic() >= deadline:
                outcomes.append({"deadline": True})
                continue
            row = db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (ev["event_id"],)).fetchone()
            if row is None or row["kind"] != "new_messages" or row["route"] != "interactive" \
                    or row["state"] not in ("pending", "failed"):
                outcomes.append({"skipped": True})
                continue
            if row["project_id"] and ledger.is_archived(row["project_id"]):
                outcomes.append({"archived": dict(row)})
                continue
            if notify_spool.restore_pending(root) is not None:
                outcomes.append({"restore_pending": True})
                continue
            before = len(specs)
            db.execute("SAVEPOINT notification_dispatch_item")
            try:
                outcome = _dispatch_intent_tx(ledger, row, cfg, now, specs)
            except Exception as error:
                db.execute("ROLLBACK TO notification_dispatch_item")
                del specs[before:]
                outcomes.append(error)
            else:
                outcomes.append(outcome)
            finally:
                db.execute("RELEASE notification_dispatch_item")
    notify_spool.publish_specs(db, dirs, specs, now)
    return outcomes


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
    binding. Delivered signal creates retain a content-free thread-route
    witness; verified split-body repairs retain only coverage hashes.
    DB readers of spec_json (dispatch re-entry, sweep watchdog,
    recover) republish queued/held renders or delivered plans with
    pending parts; the latter cannot enter this clearing path. The
    worker and reconcile read the spec file, which a pending part plan
    keeps. Any part with an unknown outcome keeps both specs across all
    transports until its result is resolved. Definitive Slack body length
    rejections keep both sealed copies until matching delivery proof exists. Rows, hashes,
    correlations and part metadata stay for audit. Each clearing pass examines
    at most limit candidates; a private restart-safe cursor rotates past
    retained rows without changing delivery facts."""
    db = connection(ledger)
    now = time.time() if now is None else now
    root = notify_spool.data_root(ledger)
    dirs = notify_spool.notify_dirs(root)
    database = os.stat(db.execute("PRAGMA database_list").fetchone()["file"])
    cursor = {"version": 1, "database": [database.st_dev, database.st_ino],
              "positions": {"spec_json": None, "spec_files": None}}
    try:
        saved = strict_json(read_private_bytes(os.path.join(dirs["flags"], "notify_gc_cursor.json"), 4096))
        if (not isinstance(saved, dict) or saved.keys() != cursor.keys()
                or type(saved["version"]) is not int or saved["version"] != 1
                or saved["database"] != cursor["database"]
                or not isinstance(saved["database"], list)
                or any(type(value) is not int for value in saved["database"])
                or not isinstance(saved["positions"], dict)
                or saved["positions"].keys() != cursor["positions"].keys()):
            raise ValueError("invalid_gc_cursor")
        for after in saved["positions"].values():
            if after is not None and (not isinstance(after, list) or len(after) != 2
                    or notify_scope._finite(after[0]) is None or not valid_text(after[1], 128)):
                raise ValueError("invalid_gc_cursor")
        cursor = saved
    except (OSError, ValueError, TypeError, RecursionError):
        pass  # Cursor loss only repeats bounded read-only proof checks.
    with db:
        tokens = db.execute(
            "DELETE FROM notification_action_tokens WHERE expires_at<?",
            (now,)).rowcount
        cleared = 0
        coverage_cache = {}
        proof_state = (set(), set(), coverage_cache)

        def candidates(query, phase):
            after = cursor["positions"][phase]
            if limit <= 0:
                return
            bounds = " AND (r.updated_at,r.delivery_id)>(?,?)" if after else ""
            rows = db.execute(query + bounds + " ORDER BY r.updated_at,r.delivery_id LIMIT ?",
                              (*after, limit) if after else (limit,)).fetchall()
            position = [rows[-1]["updated_at"], rows[-1]["delivery_id"]] if len(rows) == limit else None
            if position is not None and (notify_scope._finite(position[0]) is None
                                        or not valid_text(position[1], 128)):
                position = None
            cursor["positions"][phase] = position
            retained = notify_scope.unresolved_length_deliveries(
                db, [row["delivery_id"] for row in rows], proof_state=proof_state)
            yield from (row for row in rows if row["delivery_id"] not in retained)

        for r in candidates(
                f"""SELECT r.delivery_id,r.op,r.state,r.spec_json,r.updated_at,c.kind FROM notification_renders r
                   LEFT JOIN notification_cards c ON c.card_id=r.card_id
                   WHERE r.spec_json IS NOT NULL
                     AND CASE WHEN json_valid(r.spec_json) THEN NOT (
                       json_type(r.spec_json,'$.signal_thread_route') IS NOT NULL
                       AND json_type(r.spec_json,'$.thread_id') IS NOT NULL
                       AND (SELECT COUNT(*) FROM json_each(r.spec_json))=2)
                       AND NOT {notify_scope.BODY_COVERAGE_WITNESS_SQL}
                       AND NOT {notify_scope.ATTACHMENT_IDENTITY_WITNESS_SQL} ELSE 1 END
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
                       WHERE p.delivery_id=r.delivery_id AND p.state='unknown')
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
                   """, "spec_json"):
            witness = (_signal_route_witness(r["spec_json"]) if r["kind"] == "signal"
                       and r["op"] == "create" and r["state"] == "delivered" else None)
            if witness is None and r['op'] == 'update':
                witness = notify_scope.body_coverage_witness(db, r['delivery_id'], coverage_cache)
            attachment = notify_parts.attachment_identity_witness(db, r['delivery_id'])
            if attachment is not None:
                if witness is not None and 'signal_thread_route' in witness:
                    continue
                witness = {**(witness or {}), 'attachment_identity': attachment}
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
        for r in candidates(
                "SELECT delivery_id,transport,updated_at FROM notification_renders r "
                "WHERE state IN ('delivered','not_sent','cancelled') "
                "AND spec_published=1 AND parts_state!='pending' "
                "AND NOT EXISTS (SELECT 1 FROM notification_render_parts p "
                "WHERE p.delivery_id=r.delivery_id AND p.state='pending') "
                "AND NOT EXISTS (SELECT 1 FROM notification_render_parts p "
                "WHERE p.delivery_id=r.delivery_id AND p.state='unknown') ",
                "spec_files"):
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
    # Publish only after the page's DB transaction committed. Failed cursor
    # publication repeats a page; it never authorizes deleting a held spec.
    os.makedirs(dirs["flags"], mode=0o700, exist_ok=True)
    notify_spool.publish_file(dirs["flags"], "notify_gc_cursor.json", canonical(cursor))
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
