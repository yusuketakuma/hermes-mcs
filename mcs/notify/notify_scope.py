"""Read notification scope, configuration and health populations on a borrowed connection."""
from __future__ import annotations

import json
import math
import time
from mcs_requests import positive

TRANSPORT_VERSIONS = {"discord": 1, "slack": 2, "lineworks": 3}
SUPPORTED_TRANSPORTS = tuple(TRANSPORT_VERSIONS)
INTERACTIVE_KINDS = frozenset({"new_messages", "signal", "daily_digest", "urgent_notice", "patient_history"})

def scope_fields(transport: str) -> tuple[str, ...]:
    return ("profile", "application_id",
            "guild_id" if transport == "discord" else "team_id", "channel_id")


def stored_scope(row) -> dict[str, str | None]:
    transport = row["transport"]
    scope = {k: row[k] for k in scope_fields(transport)}
    if transport in ("slack", "lineworks"):
        scope["transport"] = transport
    return scope


def scope_matches(card, origin) -> bool:
    origin = origin or {}
    transport = card["transport"]
    if transport not in SUPPORTED_TRANSPORTS \
            or transport != origin.get("transport", "discord"):
        return False
    if transport in ("slack", "lineworks") and "guild_id" in origin:
        return False
    if transport in ("slack", "lineworks"):
        return all(card[k] and card[k] == origin.get(k)
                   for k in scope_fields(transport))
    return all(not card[k] or card[k] == origin.get(k)
               for k in scope_fields(transport))


def card_row(db, card_id: int):
    """Read a card dict or None; borrow db without transaction or connection changes."""
    r = db.execute("SELECT * FROM notification_cards WHERE card_id=?",
                   (card_id,)).fetchone()
    return dict(r) if r is not None else None


def render_row(db, delivery_id):
    """Read the sealed render row on borrowed db; None means unknown delivery.

    Never mutate db or own its transaction/connection lifetime."""
    return db.execute(
        "SELECT * FROM notification_renders WHERE delivery_id=?",
        (delivery_id,)).fetchone()


def latest_signals(db, keys: list, project_id=None) -> dict:
    """Read key -> artifact/content from the newest signal row on borrowed db.

    Missing or invalid latest rows stay absent; older states never revive.
    """
    from mcs_signals import signal_content
    out = {}
    for k in keys:
        if not isinstance(k, str) or not k:
            continue
        row = db.execute(
            """SELECT artifact_id, project_id, content FROM artifacts
               WHERE kind='signal_v1' AND json_valid(meta)
                 AND json_extract(meta,'$.key')=?
               ORDER BY artifact_id DESC LIMIT 1""", (k,)).fetchone()
        if row is None:
            continue
        # The newest row is authoritative even when unreadable: do not
        # revive an older open signal after a malformed terminal transition.
        content = signal_content(row["content"], row["project_id"])
        if (content is not None
                and (project_id is None or content["project_id"] == project_id)):
            out[k] = {"artifact_id": row["artifact_id"],
                      "content": content}
    return out


def card_projects(db, render, card) -> list:
    """Read positive patient IDs from stored render/coverage, never operator input.

    Borrow db; an empty result proves no patient scope and never means unrestricted.
    """
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
                sigs = latest_signals(db, [k for k in cov
                                          if type(k) is str])
                projects.update(
                    s["content"].get("project_id") for s in sigs.values())
    return sorted(p for p in projects if positive(p))


def notify_cfg(cfg: dict) -> dict:
    n = (cfg or {}).get("notify")
    return n if isinstance(n, dict) else {}


def interactive_enabled(cfg: dict) -> bool:
    return notify_cfg(cfg).get("interactive") in SUPPORTED_TRANSPORTS


def signals_notify(cfg: dict) -> bool:
    signals = (cfg or {}).get("signals")
    return isinstance(signals, dict) and signals.get("notify") is True


def active_transport(cfg) -> str:
    transport = notify_cfg(cfg).get("interactive")
    return transport if transport in SUPPORTED_TRANSPORTS else "discord"


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
    if transport in ("slack", "lineworks"):
        if "guild_id" in d:
            return None
        out["transport"] = transport
    return out


RESEAT_REASONS = frozenset({
    "notice_render_live", "notice_interactive_off", "notice_digest_unset",
    "notice_superseded", "notice_render_issued", "alert_outcome_pending",
    "alert_render_live", "alert_render_issued", "signals_off_sealed",
    "intent_cards_unsettled", "intent_cards_issued"})


_ATTENTION_REASONS = frozenset({
    "alert_outcome_pending", "intent_card_delivery_unknown",
    "card_parts_unknown", "unknown"})


PENDING_ATTENTION_S = 6 * 3600


OUTBOX_PENDING_WHERE = (
    "state IN ('pending','failed') AND next_try IS NOT NULL "
    # IS keeps the exclusion two-valued: a signal without digest:1 (missing,
    # null, false, malformed) is counted, never dropped by NOT(NULL)
    "AND NOT (kind='signal' AND attempts IS 0 AND next_try>? AND CASE WHEN "
    "json_valid(payload) THEN json_extract(payload,'$.digest') END IS 1)")


_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def _code(value) -> str | None:
    return value if (isinstance(value, str) and 0 < len(value) <= 64
                     and set(value) <= _CODE_CHARS) else None


def _pending_reason(progress_raw, updated_at, delivery_unknown: bool,
                    parts: int = 0) -> str:
    """Why one pending row waits. Current card state first: an intent card
    whose delivery is unknown, or a delivered render with an unknown part
    (``parts`` 2; 1 = parts still in flight, see _parts_in_flight). Then a
    structured hold (a thread hold is cleared once its thread is bound),
    then pending parts, then the row's last recorded wait.
    A wait written before the row's latest update is stale; no record is
    unrecorded; an unreadable progress is unknown."""
    try:
        progress = json.loads(progress_raw) if progress_raw else {}
    except (ValueError, TypeError, RecursionError):
        return "unknown"
    if not isinstance(progress, dict) or "invalid_progress" in progress:
        return "unknown"
    if delivery_unknown:
        return "intent_card_delivery_unknown"
    if parts >= 2:
        return "card_parts_unknown"
    wait = _wait_reason(progress.get("wait"), updated_at)
    if "hold_reason" in progress:
        code = _code(progress["hold_reason"])
        return f"hold_reason:{code}" if code else "unknown"
    # a resolved hold is removed where the thread is bound (_clear_thread_hold)
    if "thread_hold" in progress:
        code = _code(progress["thread_hold"])
        return f"thread_hold:{code}" if code else "unknown"
    if parts:
        return "card_parts_pending"
    return wait


def _wait_reason(wait, updated_at) -> str:
    if wait is None:
        return "unrecorded"
    reason = wait.get("reason") if isinstance(wait, dict) else None
    if not isinstance(reason, str) or reason not in RESEAT_REASONS:
        return "unknown"
    at, row_at = _finite(wait.get("at")), _finite(updated_at)
    if at is None or row_at is None:
        return "unknown"
    # json renders the double with 15 significant digits
    return reason if abs(at - row_at) < 1e-3 else "stale"


def _finite(value) -> float | None:
    """A real, finite timestamp as float, else None (bool, NaN, inf and
    integers too large for a double are not timestamps)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def pending_breakdown(db, now: float, *, checkpoint=lambda: None) -> dict:
    """Pending deliveries in health's population, by why each waits.
    ``total`` always equals the population count; every row lands in
    exactly one reason, so the reasons add up to it."""
    # one statement: the population and each row's unknown-delivery flag
    # come from the same read, streamed rather than loaded whole
    checkpoint()
    rows = db.execute(
        "SELECT o.created_at,o.updated_at,o.progress,EXISTS("
        "  SELECT 1 FROM notification_intent_cards ic "
        "  JOIN notification_cards c ON c.card_id=ic.card_id "
        "  WHERE ic.event_id=o.event_id AND ic.state='pending' "
        "    AND c.delivery_state='delivery_unknown'),"
        # the parts gate of the card's latest render, as _parts_in_flight
        " (SELECT MAX(CASE WHEN p.state='unknown' THEN 2 ELSE 1 END) "
        "  FROM notification_intent_cards ic "
        "  JOIN notification_renders r ON r.card_id=ic.card_id "
        "  JOIN notification_render_parts p ON p.delivery_id=r.delivery_id "
        "  WHERE ic.event_id=o.event_id AND ic.state='pending' "
        "    AND r.state='delivered' AND r.render_rev=(SELECT MAX(render_rev) "
        "      FROM notification_renders WHERE card_id=ic.card_id) "
        "    AND ((p.kind IN ('body_part','attachment_part') "
        "          AND p.state IN ('pending','unknown')) "
        "      OR (p.kind='thread' AND p.state='unknown'))) "
        f"FROM notify_outbox o WHERE {OUTBOX_PENDING_WHERE}", (now,))
    total = 0
    by_reason: dict = {}
    oldest = None
    attention = 0
    for created_at, updated_at, progress, delivery_unknown, parts in rows:
        checkpoint()
        total += 1
        reason = _pending_reason(progress, updated_at, bool(delivery_unknown),
                                 parts if isinstance(parts, int) else 0)
        created = _finite(created_at)
        age = max(0.0, now - created) if created is not None else None
        slot = by_reason.setdefault(reason, {"count": 0, "oldest_age_s": None})
        slot["count"] += 1
        if age is not None:
            slot["oldest_age_s"] = max(slot["oldest_age_s"] or 0.0, age)
            oldest = max(oldest or 0.0, age)
        if (reason in _ATTENTION_REASONS or reason.startswith("hold_reason:")
                or (age is not None and age >= PENDING_ATTENTION_S)):
            attention += 1
    checkpoint()
    return {"total": total, "oldest_age_s": oldest,
            "by_reason": by_reason, "attention": attention}


def retired_transports(cfg) -> tuple[str, ...]:
    """Transports no longer selected by notify.interactive: their
    unsettled rows stay recorded (never auto-settled — a granted send
    may be remote) but no longer count as live delivery work. Without a
    config (None) nothing is treated as retired."""
    if cfg is None or not interactive_enabled(cfg):
        return ()
    active = active_transport(cfg)
    return tuple(t for t in SUPPORTED_TRANSPORTS if t != active)


def layout_migration_counts(db) -> list:
    """Read-only counts of retained layouts and cards blocked by unknown parts."""
    return [dict(row) for row in db.execute("""
        WITH unknown_cards AS (
            SELECT DISTINCT r.card_id
            FROM notification_render_parts p
            JOIN notification_renders r USING(delivery_id)
            WHERE p.state='unknown'
        )
        SELECT c.transport,c.layout,c.delivery_state,COUNT(*) AS cards,
               SUM(CASE WHEN u.card_id IS NOT NULL THEN 1 ELSE 0 END) AS unknown_parts
        FROM notification_cards c LEFT JOIN unknown_cards u ON u.card_id=c.card_id
        GROUP BY c.transport,c.layout,c.delivery_state
        ORDER BY c.transport,c.layout,c.delivery_state""")]


def health_cards(db, cfg=None) -> dict:
    """The health.json 'cards' section — counts and ages plus the
    bounded unsettled-attempt worklist an operator resolves via
    ops.card_resolve (ids only, no patient data). Attempts on a retired
    transport (after a notify.interactive switch) are reported only as
    ``retired_unsettled`` and leave the live counts."""
    now = time.time()
    retired = retired_transports(cfg)
    marks = ",".join("?" * len(retired))
    live = f" AND r.transport NOT IN ({marks})" if retired else ""
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
        "SELECT COUNT(*) c,MIN(a.created_at) o FROM "
        "notification_delivery_attempts a "
        "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
        "WHERE a.state IN ('granted','unknown')" + live, retired
    ).fetchone()
    retired_unsettled = db.execute(
        "SELECT COUNT(*) FROM notification_delivery_attempts a "
        "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
        f"WHERE a.state IN ('granted','unknown') AND r.transport IN ({marks})",
        retired).fetchone()[0] if retired else 0
    last = db.execute(
        "SELECT MAX(updated_at) FROM notification_renders "
        "WHERE state='delivered'").fetchone()[0]
    unknown = db.execute(
        "SELECT COUNT(*) FROM notification_renders WHERE state='unknown'"
    ).fetchone()[0]
    # Unsettled attempts as an actionable list, oldest first — each row
    # carries the exact scope ops.card_resolve validates against, so
    # resolving is: check the remote channel -> submit the approval
    # flow. Never auto-resend: a granted attempt may already be remote.
    worklist = db.execute(
        "SELECT a.attempt_id,a.delivery_id,a.state,a.message_id,"
        "a.error_code,a.created_at,r.card_id,r.transport,r.profile,"
        "r.application_id,r.guild_id,r.channel_id,r.team_id "
        "FROM notification_delivery_attempts a "
        "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
        "WHERE a.state IN ('granted','unknown')" + live +
        " ORDER BY a.created_at LIMIT 20", retired).fetchall()
    retired_items = [
        {"attempt_id": w["attempt_id"], "delivery_id": w["delivery_id"],
         "state": w["state"], "transport": w["transport"],
         "age_s": round(now - w["created_at"], 1),
         "resolve_scope": stored_scope(w)}
        for w in db.execute(
            "SELECT a.attempt_id,a.delivery_id,a.state,a.created_at,"
            "r.transport,r.profile,r.application_id,r.guild_id,"
            "r.channel_id,r.team_id FROM notification_delivery_attempts a "
            "JOIN notification_renders r ON r.delivery_id=a.delivery_id "
            f"WHERE a.state IN ('granted','unknown') AND r.transport IN ({marks}) "
            "ORDER BY a.created_at LIMIT 20", retired)] if retired else []
    return {
        "pending": states.get("pending", 0),
        "delivered": states.get("delivered", 0),
        "update_failed": states.get("update_failed", 0),
        "delivery_unknown": states.get("delivery_unknown", 0),
        "message_deleted": states.get("message_deleted", 0),
        "revoked": states.get("revoked", 0),
        "oldest_pending_age_s": round(now - oldest, 1) if oldest else 0,
        "layout_inventory": layout_migration_counts(db),
        "unknown_parts": [dict(row) for row in db.execute(
            "SELECT r.delivery_id,r.card_id,r.transport,r.payload_hash AS manifest_sha256,"
            "p.part_id,p.payload_sha256 FROM notification_render_parts p "
            "JOIN notification_renders r USING(delivery_id) WHERE p.state='unknown' "
            "ORDER BY p.updated_at LIMIT 20")],
        "unknown_part_resolution": "card_part_resolve_planで部品を確認し、人承認付きops.card_part_resolveで解除してください。",
        "renders_queued": queued,
        "renders_unknown": unknown,
        "attempts_unsettled": unsettled["c"],
        "retired_unsettled": retired_unsettled,
        # ids + scope only: still resolvable via ops.card_resolve
        "retired_items": retired_items,
        "oldest_unsettled_age_s": (round(now - unsettled["o"], 1)
                                 if unsettled["o"] else 0),
        "unsettled": [
            {"attempt_id": w["attempt_id"],
             "delivery_id": w["delivery_id"],
             "state": w["state"],
             "card_id": w["card_id"],
             "transport": w["transport"],
             "channel_id": w["channel_id"],
             "message_id": w["message_id"],
             "error_code": w["error_code"],
             "age_s": round(now - w["created_at"], 1),
             "resolve_scope": stored_scope(w)}
            for w in worklist],
        "last_delivered_at": last or 0,
    }
