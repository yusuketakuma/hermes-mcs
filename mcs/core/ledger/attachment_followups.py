"""Queue attachment-only notification intents inside the caller's transaction.

Borrow SQLite only: never BEGIN, commit, close, publish, or send. Deduplication
and accepted-message coverage are evaluated at the original caller's save or
settlement point, so the attachment and its intent commit together.
"""
import json

def enqueue_attachment_followup_tx(db, attachment_id, project_id, message_id, now):
    """Insert one deduplicated followup on borrowed db; return whether inserted.

    The caller owns the active transaction, commit and connection lifetime.
    """
    if db.execute("""
        SELECT 1 FROM notify_outbox
        WHERE kind='attachment_followup' AND state != 'suppressed'
          AND json_valid(payload)
          AND json_extract(payload,'$.attachment_id')=?
        LIMIT 1""", (attachment_id,)).fetchone():
        return False
    db.execute("""
        INSERT INTO notify_outbox(kind,project_id,payload,state,next_try,
          route,created_at,updated_at)
        VALUES('attachment_followup',?,?,'pending',?,'text',?,?)
    """, (project_id, json.dumps({"attachment_id": attachment_id,
                                 "message_id": message_id}), now, now, now))
    return True


def enqueue_ready_attachment_followups_tx(db, event_id, now) -> int:
    """Queue downloaded files covered by accepted cards in this transaction."""
    rows = db.execute("""
        SELECT DISTINCT a.attachment_id,m.project_id,m.message_id
        FROM notify_outbox o
        JOIN notification_intent_batches b ON b.event_id=o.event_id
        JOIN notification_intent_cards ic ON ic.event_id=o.event_id
        JOIN json_each(b.frozen_payload,'$.message_ids') frozen
        JOIN json_each(ic.coverage) covered ON covered.value=frozen.value
        JOIN messages m ON m.message_id=frozen.value
        JOIN attachments a ON a.message_id=m.message_id
        WHERE o.event_id=? AND o.kind='new_messages' AND o.state='accepted'
          AND ic.state='delivered' AND frozen.type='integer'
          AND a.state='downloaded' AND COALESCE(a.local_path,'')!=''
          AND m.body_state IS NOT 'deleted'
          -- a file sealed into a card's durable part manifest is owned
          -- by the part pipeline (T7); the text followup would be a
          -- second send of the same file
          AND NOT EXISTS(SELECT 1 FROM notification_render_parts rp
                         WHERE rp.attachment_id=a.attachment_id
                           AND rp.state IN ('pending','delivered',
                                            'held','unknown'))
    """, (event_id,)).fetchall()
    return sum(enqueue_attachment_followup_tx(
        db, row["attachment_id"], row["project_id"], row["message_id"], now)
        for row in rows)


def enqueue_downloaded_attachment_followup_tx(db, attachment_id: int, project_id: int,
                                             message_id: int, now: float):
    """The body notice for this message was already ACCEPTED before
    this file finished downloading — it provably went out without
    the file, so queue an attachment-only follow-up receipt (F11).
    Deduped on payload.attachment_id: one follow-up per file ever."""
    sent = db.execute("""
          SELECT 1 FROM notify_outbox o
          WHERE o.kind='new_messages' AND o.state='accepted'
            AND o.updated_at < ? AND CASE
              WHEN EXISTS(SELECT 1 FROM notification_intent_batches b
                          WHERE b.event_id=o.event_id) THEN
                EXISTS(SELECT 1 FROM notification_intent_batches b
                  JOIN notification_intent_cards ic ON ic.event_id=b.event_id
                  JOIN json_each(b.frozen_payload,'$.message_ids') frozen
                  JOIN json_each(ic.coverage) covered ON covered.value=frozen.value
                  WHERE b.event_id=o.event_id AND ic.state='delivered'
                    AND frozen.type='integer' AND frozen.value=?)
              ELSE json_valid(o.payload) AND EXISTS(
                SELECT 1 FROM json_each(json_extract(o.payload,'$.message_ids'))
                WHERE value=?) END
          LIMIT 1""", (now, message_id, message_id)).fetchone()
    if not sent:
        return
    enqueue_attachment_followup_tx(
        db, attachment_id, project_id, message_id, now)
