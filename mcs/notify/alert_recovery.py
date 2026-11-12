"""Verification window for self-healing system alerts.

Recoverable incidents are queued with a delayed first pickup
(``next_try_for``). When the drain reaches them, ``suppression_reason``
re-checks whether the condition already cleared: a healed incident is
suppressed silently and only a condition that survives the window
reaches the channel. Session and update notices are not recoverable
blips — session_expired is already post-relogin-failure — so they keep
their immediate path.
"""
import json
import os
import time

RECOVERABLE_KINDS = frozenset({"health_write_failed", "health_watch_stalled", "run_failed"})
VERIFY_S = 1200


def next_try_for(kind, now=None):
    """First-pickup delay for recoverable alert kinds; None = immediate."""
    if kind not in RECOVERABLE_KINDS:
        return None
    return (time.time() if now is None else now) + VERIFY_S


def _created_at(db, event_id):
    row = db.execute(
        "SELECT created_at FROM notify_outbox WHERE event_id=?",
        (event_id,)).fetchone()
    return row["created_at"] if row else None


def _health_write_recovered(created_at):
    """A health.json 'at' newer than the incident proves a later write
    landed — the failure was transient."""
    if created_at is None:
        return False
    try:
        import health_report
        import safe_io
        doc = safe_io.read_json_nofollow(
            health_report.HEALTH_FILE, health_report.STATUS_MAX_BYTES)
    except Exception:
        return False
    at = doc.get("at") if isinstance(doc, dict) else None
    return type(at) in (int, float) and at > created_at


def _health_watch_recovered(db, created_at):
    """A watcher observation newer than the incident proves it runs again."""
    if created_at is None:
        return False
    try:
        import health_policy
        import notify_spool
        import safe_io
        doc = safe_io.read_json_nofollow(
            os.path.join(notify_spool.data_root(db),
                         os.path.basename(health_policy.WATCH_STATUS_REL)),
            health_policy.STATUS_MAX_BYTES)
    except Exception:
        return False
    at = doc.get("watched_at") if isinstance(doc, dict) else None
    return type(at) in (int, float) and at > created_at


def _run_failed_recovered(db, ev, created_at):
    """A later run that did not fail proves the pipeline recovered."""
    try:
        payload = json.loads(ev["payload"])
    except (ValueError, TypeError, RecursionError):
        payload = {}
    run_id = payload.get("run_id") if isinstance(payload, dict) else None
    if type(run_id) is int:
        if db.execute(
                "SELECT 1 FROM runs WHERE run_id>? "
                "AND status IN ('ok','partial') LIMIT 1",
                (run_id,)).fetchone() is not None:
            return True
    if created_at is None:
        return False
    return db.execute(
        "SELECT 1 FROM runs WHERE finished_at>? "
        "AND status IN ('ok','partial') LIMIT 1",
        (created_at,)).fetchone() is not None


def suppression_reason(db, ev):
    """Reason a pending recoverable alert must not send, else None."""
    kind = ev["kind"]
    if kind not in RECOVERABLE_KINDS:
        return None
    created_at = _created_at(db, ev["event_id"])
    if kind == "health_write_failed" and _health_write_recovered(created_at):
        return "recovered_before_send"
    if kind == "health_watch_stalled" and _health_watch_recovered(db, created_at):
        return "recovered_before_send"
    if kind == "run_failed" and _run_failed_recovered(db, ev, created_at):
        return "recovered_before_send"
    return None
