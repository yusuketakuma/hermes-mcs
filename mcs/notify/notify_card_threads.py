"""Proven source and reply thread routing for notification cards."""
from __future__ import annotations
import notify_scope
import json
from mcs_util import loads_dict
from notify_render import (
    anchor_keys, signal_evidence,
    presenter, reply_source)
import notify_action_context as action_context
from notify_card_state import (
    LIVE_RENDER, RESEAT_S, _parts_in_flight
)

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


SIGNAL_SOURCE_HOLD_MAX_S = 6 * 3600


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
