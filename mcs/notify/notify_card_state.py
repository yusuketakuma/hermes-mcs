"""Notification card delivery state and recovery invariants."""
from __future__ import annotations
import json
import time
from mcs_requests import positive
from mcs_util import valid_hash


LIVE_RENDER = ("queued", "sending", "unknown", "held")


RESEAT_S = 3600


MAX_RESEND = 3


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


def unsettled_attempt(db, card_id: int):
    return db.execute(
        """SELECT a.attempt_id, a.state FROM notification_delivery_attempts a
           JOIN notification_renders r ON r.delivery_id=a.delivery_id
           WHERE r.card_id=? AND a.state IN ('granted','unknown')
           ORDER BY a.created_at DESC LIMIT 1""", (card_id,)).fetchone()


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
        "AND ((kind IN ('body_part','attachment_part','file_group') "
        "AND state IN ('pending','unknown')) OR (kind='thread' AND state='unknown')) "
        "LIMIT 1", (latest["delivery_id"],)).fetchone() is not None


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
                     AND (NOT EXISTS(SELECT 1 FROM notification_render_parts g
                            WHERE g.delivery_id=r.delivery_id AND g.kind='file_group')
                          OR EXISTS(SELECT 1 FROM notification_render_parts g
                            WHERE g.delivery_id=r.delivery_id AND g.kind='file_group'
                              AND g.state='delivered' AND g.remote_id=c.message_id))
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
