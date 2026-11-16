"""Post-restore delivery reconciliation — journal vs restored DB.

A ledger.db restore rewinds card/render/attempt rows while external
effects persist: messages already posted, the delivery worker's
append-only journal (data/*_state/journal-*.jsonl), and published
spec files. Restored state alone cannot tell "delivered" from "never
sent" — this module is the gate that reconciles them before senders
resume:

- a journal ``result`` row is a fact; its recorded receipt envelope is
  re-applied through ``apply_transport_receipt`` so the existing
  identity checks (scope, render_rev, payload_hash, route_epoch,
  correlation) decide — a mismatch fails closed into a hold;
- external evidence (``started``/``result``) whose attempt or render
  row the restore erased -> the scope is held, never resent;
- a DB ``granted``/``unknown`` attempt with no journal rows -> hold
  (``unknown_attempt`` — the journal may itself be lost);
- a provable pre-HTTP attempt (no ``started`` row in an untainted
  journal) -> ``not_sent``; resending is safe;
- an unparsable journal line taints its file: conclusions built on
  absent evidence upgrade to holds.

Every verdict lands in ``data/restore_reconcile.json`` (the durable
operator receipt distinguishing delivered/not_sent/unknown/held), an
ops ``update_notice`` alerts held scopes, and the ``restore_pending``
marker — which blocks all send grants — clears only after the receipt
is durable. Holds release via ``ops.card_resolve`` (operator-verified
rebind/resume) or a factual receipt settling the attempt; parts made
``unknown`` by a restore beyond the journal horizon release via
``ops.card_part_resolve`` or their own late receipt, except Discord group
parts with no verifiable surface attempt, which the receipt lists under
``parts_unresolvable`` for manual investigation; they become resolvable when
their surface later settles or is resolved (promote_group_parts).

A tainted journal keeps the marker (reconcile re-runs every tick and
alerts only for newly recorded holds). Nothing here clears it
automatically: the operator procedure — stop the gateway, preserve a
copy, verify the held scopes in the channel, then remove only the
corrupt line — is in hermes_plugin/README.md.
"""
from __future__ import annotations

import json
import os
import time
import uuid

import backup_names
import notify_cards as cards
import notify_scope
import notify_spool
from notify_scope import card_row
from mcs_queries import HOLD_PROGRESS_SET
from mcs_util import loads_dict
import notify_transport
import notify_journal
import notify_parts
import notify_recovery

_RESULT_VALUES = ("delivered", "not_sent", "unknown")
_TERMINAL = ("delivered", "not_sent", "cancelled")


def _read_spec(dirs: dict, delivery_id) -> dict | None:
    """The published spec file survives a DB restore (it lives outside
    the DB) — the only link from a lost delivery_id back to its intent
    event ids."""
    if not isinstance(delivery_id, str) or not delivery_id:
        return None
    safe = "".join(c if c.isalnum() or c in "._-" else "_"
                   for c in delivery_id)
    for t in notify_scope.SUPPORTED_TRANSPORTS:
        try:
            with open(os.path.join(dirs[t + "_render"], safe + ".json"),
                      "rb") as fh:
                spec = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(spec, dict) and spec.get("delivery_id") == delivery_id:
            return spec
    return None


def _spec_events(spec) -> list:
    delivery = spec.get("delivery") if isinstance(spec, dict) else None
    ids = delivery.get("intent_event_ids") \
        if isinstance(delivery, dict) else []
    return [e for e in (ids or []) if type(e) is int]


def _apply_hold(db, aid, delivery_id, reason, dirs, now,
                scope=None) -> dict:
    """Freeze one disputed scope — render held, card delivery_unknown,
    pending linked intents quarantined — plus a durable
    notification_restore_holds row the operator resolves against."""
    render = notify_scope.render_row(db, delivery_id) if delivery_id else None
    card = card_row(db, render["card_id"]) \
        if render is not None and render["card_id"] is not None else None
    if scope is None:
        scope = (dict(notify_scope.stored_scope(render)) if render is not None
                 else {})
    spec = _read_spec(dirs, delivery_id)
    events = _spec_events(spec)
    if not scope:
        d = spec.get("delivery") if isinstance(spec, dict) else {}
        scope = {k: d.get(k) for k in
                 ("profile", "application_id", "guild_id",
                  "channel_id", "team_id", "transport")
                 if d.get(k) is not None} if isinstance(d, dict) else {}
    card_id = card["card_id"] if card is not None else None
    with db:
        db.execute("BEGIN IMMEDIATE")
        if render is not None and render["state"] not in _TERMINAL:
            db.execute(
                "UPDATE notification_renders SET state='held',"
                "updated_at=? WHERE delivery_id=?",
                (now, delivery_id))
        if card is not None and card["delivery_state"] != "revoked":
            db.execute(
                "UPDATE notification_cards SET delivery_state="
                "'delivery_unknown',last_delivery_error=?,updated_at=? "
                "WHERE card_id=?",
                (f"restore_hold:{reason}", now, card["card_id"]))
        for eid in events:
            # never sent forward again until an operator releases —
            # state='failed' with next_try NULL leaves the due queue
            db.execute(
                "UPDATE notify_outbox SET state='failed',next_try=NULL,"
                f"{HOLD_PROGRESS_SET},updated_at=? "
                "WHERE event_id=? AND state='pending'",
                ("restore_hold", now, eid))
        existing = db.execute(
            "SELECT 1 FROM notification_restore_holds WHERE "
            "released_at IS NULL AND attempt_id IS ? AND delivery_id IS ?",
            (aid, delivery_id)).fetchone()
        if existing is None:
            db.execute(
                "INSERT INTO notification_restore_holds("
                "card_id,delivery_id,attempt_id,events_json,reason,"
                "scope_json,held_at) VALUES(?,?,?,?,?,?,?)",
                (card_id, delivery_id, aid,
                 json.dumps(events), reason,
                 json.dumps(scope, sort_keys=True), now))
        cards.mark_snapshot_dirty(db)
    return {"attempt_id": aid, "delivery_id": delivery_id,
            "verdict": "held", "detail": reason, "card_id": card_id,
            "scope": scope, "events_held": len(events),
            "spec_linked": bool(events)}


def _republish_receipt(ledger, db, cfg, aid, delivery_id, info,
                       result, dirs, now) -> dict:
    """Re-apply a journal-proven outcome through the normal receipt
    path — _receipt_check does the identity comparison (scope, rev,
    hash, epoch, correlation); a reject becomes a hold."""
    env = next((r["receipt_envelope"] for r in reversed(info["rows"])
                if isinstance(r.get("receipt_envelope"), dict)), None)
    if env is None:
        return _apply_hold(db, aid, delivery_id, "no_envelope",
                           dirs, now)
    result_row = next((r for r in reversed(info["rows"])
                       if r.get("phase") == "result"
                       and r.get("result") in _RESULT_VALUES), None)
    receipt = dict(env)
    receipt["command_id"] = str(uuid.uuid4())
    receipt["attempt_id"] = aid
    receipt["result"] = result
    receipt.pop("message_id", None)
    receipt.pop("error_code", None)
    if result_row is not None:
        if result_row.get("message_id"):
            receipt["message_id"] = str(result_row["message_id"])
        if result_row.get("error_code"):
            receipt["error_code"] = result_row["error_code"]
    elif result == "not_sent":
        receipt["error_code"] = "restore_unfinished"
    elif result == "unknown":
        receipt["error_code"] = "worker_crash"
    out = notify_transport.apply_transport_receipt(
        ledger, receipt, cfg, now=now)
    if out.get("applied"):
        return {"attempt_id": aid, "delivery_id": delivery_id,
                "verdict": "settled", "detail": result}
    return _apply_hold(db, aid, delivery_id,
                       f"receipt_rejected:{out.get('error')}",
                       dirs, now)


def _reconcile_part(ledger, db, cfg, aid, info, dirs, now) -> dict:
    """One journaled durable-part attempt vs the restored render-parts
    rows. Part attempts never live in notification_delivery_attempts —
    their home is notification_render_parts, seeded with the render
    itself, so a part row surviving the restore is the authority."""
    rows = info["rows"]
    last = rows[-1]
    delivery_id = last.get("delivery_id")
    part_id = last.get("part_id")
    render = notify_scope.render_row(db, delivery_id) if delivery_id else None
    if render is None:
        return _apply_hold(db, aid, delivery_id, "part_lost:no_render",
                           dirs, now)
    part = db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? "
        "AND part_id=?", (delivery_id, part_id)).fetchone() \
        if isinstance(part_id, str) else None
    if part is None:
        return _apply_hold(db, aid, delivery_id, "part_lost:no_row",
                           dirs, now)
    results = [r for r in rows
               if r.get("phase") == "result"
               and r.get("result") in _RESULT_VALUES]
    distinct = {(r["result"], str(r.get("remote_id")))
                for r in results}
    if len(distinct) > 1:
        return _apply_hold(db, aid, delivery_id, "journal_conflict",
                           dirs, now)
    result_row = results[-1] if results else None
    if info["tainted"] and not results \
            and not any(r.get("phase") == "started" for r in rows):
        return _apply_hold(db, aid, delivery_id,
                           "journal_corrupt:pre_http", dirs, now)
    if part["state"] in ("delivered", "not_sent", "held"):
        if result_row is None:
            return {"attempt_id": aid, "delivery_id": delivery_id,
                    "verdict": "consistent",
                    "detail": f"already_{part['state']}"}
        same = part["state"] == result_row["result"] and (
            part["state"] != "delivered"
            or str(part["remote_id"]) == str(result_row.get("remote_id")))
        if same:
            return {"attempt_id": aid, "delivery_id": delivery_id,
                    "verdict": "consistent",
                    "detail": f"already_{part['state']}"}
        return _apply_hold(db, aid, delivery_id, "journal_conflict",
                           dirs, now)
    # pending part — republish the journal's fact through the checked path
    if result_row is not None:
        env = next((r["receipt_envelope"] for r in reversed(rows)
                    if r.get("phase") == "result"
                    and isinstance(r.get("receipt_envelope"), dict)), None)
        result = result_row["result"]
    else:
        env = next((r["receipt_envelope"] for r in reversed(rows)
                    if isinstance(r.get("receipt_envelope"), dict)), None)
        rec = next((r for r in reversed(rows)
                    if r.get("phase") == "receipt"
                    and r.get("result") in _RESULT_VALUES), None)
        if rec is not None:
            result = rec["result"]   # a receipt row is recorded fact
        elif any(r.get("phase") == "started" for r in rows):
            result = "unknown"
        else:
            result = "not_sent"
    if not isinstance(env, dict):
        env = {"version": 1, "op": "part_receipt",
               "attempt_id": aid, "delivery_id": delivery_id,
               "render_rev": render["render_rev"],
               "payload_hash": render["payload_hash"],
               "route_epoch": render["route_epoch"],
               "correlation": render["correlation"],
               "part_id": part_id, "kind": part["kind"],
               "profile": render["profile"],
               "application_id": render["application_id"],
               "channel_id": render["channel_id"]}
        if render["guild_id"]:
            env["guild_id"] = render["guild_id"]
        if render["transport"] in ("slack", "lineworks"):
            env.update(version=notify_scope.TRANSPORT_VERSIONS[render["transport"]],
                       transport=render["transport"],
                       team_id=render["team_id"])
    receipt = dict(env)
    receipt["command_id"] = str(uuid.uuid4())
    receipt["attempt_id"] = aid
    receipt["result"] = result
    receipt.pop("remote_id", None)
    receipt.pop("error_code", None)
    if result_row is not None:
        if result_row.get("remote_id"):
            receipt["remote_id"] = str(result_row["remote_id"])
        if result_row.get("error_code"):
            receipt["error_code"] = result_row["error_code"]
    elif result == "unknown":
        receipt["error_code"] = "worker_crash"
    else:
        receipt["error_code"] = "restore_unfinished"
    out = notify_transport.apply_part_receipt(
        ledger, receipt, cfg, now=now)
    if out.get("applied"):
        return {"attempt_id": aid, "delivery_id": delivery_id,
                "verdict": "settled", "detail": result}
    return _apply_hold(db, aid, delivery_id,
                       f"receipt_rejected:{out.get('error')}", dirs, now)


def _reconcile_attempt(ledger, db, cfg, aid, info, dirs, now) -> dict:
    """One journaled attempt vs the restored DB rows."""
    rows = info["rows"]
    if aid.startswith("p:") or rows[-1].get("part_id"):
        # durable part attempts settle render_parts rows, never the
        # card-attempt table (T7)
        return _reconcile_part(ledger, db, cfg, aid, info, dirs, now)
    phases = {r["phase"] for r in rows if r.get("phase") in notify_journal.PHASES}
    results = [r for r in rows
               if r.get("phase") == "result"
               and r.get("result") in _RESULT_VALUES]
    distinct = {(r["result"], str(r.get("message_id")))
                for r in results}
    result_row = results[-1] if results else None
    delivery_id = (result_row.get("delivery_id")
                   if result_row is not None
                   else rows[-1].get("delivery_id"))
    attempt = db.execute(
        "SELECT * FROM notification_delivery_attempts "
        "WHERE attempt_id=?", (aid,)).fetchone()
    render = notify_scope.render_row(db, delivery_id) if delivery_id else None

    if len(distinct) > 1:
        return _apply_hold(db, aid, delivery_id, "journal_conflict",
                           dirs, now)

    if attempt is None or render is None:
        # the restore erased the DB side — the journal is the only
        # witness left; provable not_sent resumes, everything else
        # holds
        if result_row is not None and result_row["result"] == "not_sent":
            return {"attempt_id": aid, "delivery_id": delivery_id,
                    "verdict": "not_sent",
                    "detail": "attempt_lost:not_sent"}
        if not results and "started" not in phases \
                and not info["tainted"]:
            return {"attempt_id": aid, "delivery_id": delivery_id,
                    "verdict": "not_sent",
                    "detail": "attempt_lost:pre_http"}
        reason = ("attempt_lost:" + result_row["result"]
                  if result_row is not None
                  else "attempt_lost:started" if "started" in phases
                  else "journal_corrupt:pre_http")
        return _apply_hold(db, aid, delivery_id, reason, dirs, now)

    if info["tainted"] and "started" not in phases and not results:
        return _apply_hold(db, aid, delivery_id,
                           "journal_corrupt:pre_http", dirs, now)

    if attempt["state"] in ("delivered", "not_sent"):
        if result_row is None:
            return {"attempt_id": aid, "delivery_id": delivery_id,
                    "verdict": "consistent",
                    "detail": f"already_{attempt['state']}"}
        same = (result_row["result"] == attempt["state"]
                and (result_row["result"] != "delivered"
                     or str(attempt["message_id"])
                     == str(result_row.get("message_id"))))
        if same:
            return {"attempt_id": aid, "delivery_id": delivery_id,
                    "verdict": "consistent",
                    "detail": f"already_{result_row['result']}"}
        return _apply_hold(db, aid, delivery_id, "journal_conflict",
                           dirs, now)

    # granted / unknown — the restore happened between grant and
    # settlement; re-apply the journal's fact through the receipt path
    if result_row is not None:
        result = result_row["result"]
    elif "started" in phases:
        result = "unknown"
    else:
        result = "not_sent"
    return _republish_receipt(ledger, db, cfg, aid, delivery_id, info,
                              result, dirs, now)


BEYOND_HORIZON = "restore_beyond_journal_horizon"
# A Discord file_group part that cannot share a verified surface attempt is
# made ``unknown`` without one: ops.card_part_resolve cannot plan it, and the
# receipt, notice and part_hold_report name why it needs manual investigation.
GROUP_SPEC_UNVERIFIED = "restore_group_spec_unverified"
GROUP_SURFACE_MISSING = "restore_group_surface_missing"
GROUP_SURFACE_UNRESOLVABLE = "restore_group_surface_unresolvable"
UNRESOLVABLE = (GROUP_SPEC_UNVERIFIED, GROUP_SURFACE_MISSING, GROUP_SURFACE_UNRESOLVABLE)


def _discord_group_surfaces(db, delivery_id):
    """part_id -> surface for every Discord ``file_group`` anchor and member
    (they share the surface's wire attempt); ``None`` when the render has no
    group; GROUP_SPEC_UNVERIFIED when its spec does not bind to the sealed
    render and card the way group recovery requires, so no grouped part is
    given an attempt of its own."""
    if not db.execute("SELECT 1 FROM notification_render_parts WHERE delivery_id=? "
                      "AND kind='file_group' LIMIT 1", (delivery_id,)).fetchone():
        return None
    try:
        spec = notify_recovery.discord_bound_spec(db, notify_scope.render_row(db, delivery_id))
        return {pid: item["surface"] for item in spec["parts"]["manifest"]
                if item["kind"] == "file_group"
                for pid in (item["part_id"], *item["members"])}
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return GROUP_SPEC_UNVERIFIED


def _surface_attempt(db, delivery_id, surface):
    """(attempt_id, None) when the group can be resolved through its surface —
    a delivered surface, or an unknown one resolved first — else (None, reason)."""
    row = db.execute("SELECT state,attempt_id FROM notification_render_parts "
                     "WHERE delivery_id=? AND part_id=?", (delivery_id, surface)).fetchone()
    if row is None:
        return None, GROUP_SURFACE_MISSING
    if row["state"] not in ("delivered", "unknown") or not row["attempt_id"]:
        return None, GROUP_SURFACE_UNRESOLVABLE
    return row["attempt_id"], None


def promote_group_parts(db, now, delivery_id=None) -> list:
    """Re-evaluate report-only Discord group parts (``unknown``, an UNRESOLVABLE
    reason, no attempt) inside the caller's transaction.

    Once their sealed spec binds and their surface carries a usable attempt
    (it settled late, or an operator resolved it), they take that attempt with
    ``restore_beyond_journal_horizon``: exactly what a resolvable surface gave
    them at reconcile, so the surface's receipt or ``ops.card_part_resolve``
    settles them. Runs from every beyond-horizon reconcile and from every
    settled Discord surface (notify_transport.settle_part_receipt); a part
    already promoted, or still unresolvable, is left unchanged."""
    codes = UNRESOLVABLE
    rows = db.execute(
        "SELECT p.delivery_id,p.part_id,p.kind,p.error_code,r.card_id "
        "FROM notification_render_parts p JOIN notification_renders r "
        "ON r.delivery_id=p.delivery_id WHERE r.transport='discord' "
        "AND p.state='unknown' AND p.attempt_id IS NULL "
        f"AND p.error_code IN ({','.join('?' * len(codes))}) "
        "AND (? IS NULL OR p.delivery_id=?) ORDER BY p.delivery_id,p.idx",
        (*codes, delivery_id, delivery_id)).fetchall()
    promoted, surfaces = [], {}
    for row in rows:
        if row["delivery_id"] not in surfaces:
            surfaces[row["delivery_id"]] = _discord_group_surfaces(db, row["delivery_id"])
        group = surfaces[row["delivery_id"]]
        if not isinstance(group, dict) or row["part_id"] not in group:
            continue
        aid, reason = _surface_attempt(db, row["delivery_id"], group[row["part_id"]])
        if reason is None and db.execute(
                "UPDATE notification_render_parts SET error_code=?,attempt_id=?,updated_at=? "
                "WHERE delivery_id=? AND part_id=? AND state='unknown' AND attempt_id IS NULL "
                "AND error_code=?",
                (BEYOND_HORIZON, aid, now, row["delivery_id"], row["part_id"],
                 row["error_code"])).rowcount:
            promoted.append(_part_entry(row, aid, BEYOND_HORIZON))
    return promoted


def _unknown_beyond_horizon_parts(db, restored_at, now) -> list:
    """Mark the pending thread/body/attachment/file-group parts of each card's
    latest delivered render from the restored backup ``unknown``.

    No receipt for them can arrive from a journal that no longer reaches the
    backup, and pending parts of the latest render block every later update
    (notify_card_state.PARTS_IN_FLIGHT_SQL). A pending thread is marked too,
    although only an unknown one blocks: it may already exist remotely, and
    an update issued past it could create a second thread. ``unknown`` with
    the worker's own part attempt id makes them operator-resolvable through
    ``ops.card_part_resolve`` while a real late receipt still settles them.
    A Discord file group's anchor and members take their surface's attempt id
    (the card's, or the body_part's after it is marked), exactly as the
    surface's own receipt would settle them. When the spec does not bind,
    the surface row is missing, or the surface is neither delivered nor
    unknown with an attempt, they become ``unknown`` with no attempt and a
    reason in UNRESOLVABLE: still blocking, never planned, and reported for
    manual investigation until promote_group_parts finds their surface usable. Older superseded renders never block an update and
    are left alone; a Slack file_group that never started keeps its own
    ``no_journal_started`` path. A missing record is not proof of
    non-delivery, so nothing becomes not_sent. Returns the parts this run
    changed; re-runs find no pending parts left and change nothing."""
    from adapters.common import envelopes
    rows = db.execute(
        "SELECT p.delivery_id,p.part_id,p.kind,p.attempt_id,r.card_id,r.transport "
        "FROM notification_render_parts p JOIN notification_renders r "
        "ON r.delivery_id=p.delivery_id WHERE r.state='delivered' "
        "AND r.card_id IS NOT NULL AND (? IS NULL OR r.created_at <= ?) "
        "AND r.render_rev=(SELECT MAX(x.render_rev) FROM notification_renders x "
        "WHERE x.card_id=r.card_id) "
        "AND p.kind IN ('thread','body_part','attachment_part','file_group') "
        "AND p.state='pending' AND NOT (r.transport='slack' "
        "AND p.kind='file_group' AND p.attempt_id IS NULL) "
        "ORDER BY p.delivery_id,p.idx", (restored_at, restored_at)).fetchall()
    marked = []

    def mark(row, aid, reason=BEYOND_HORIZON):
        changed = db.execute(
            "UPDATE notification_render_parts SET state='unknown',"
            "error_code=?,attempt_id=?,updated_at=? "
            "WHERE delivery_id=? AND part_id=? AND state='pending'",
            (reason, aid, now, row["delivery_id"], row["part_id"])).rowcount
        if changed:
            marked.append(_part_entry(row, aid, reason))

    with db:
        db.execute("BEGIN IMMEDIATE")
        grouped, unresolvable, surfaces = [], [], {}
        for row in rows:
            if row["transport"] == "discord" and row["kind"] in ("attachment_part", "file_group"):
                if row["delivery_id"] not in surfaces:
                    surfaces[row["delivery_id"]] = _discord_group_surfaces(db, row["delivery_id"])
                group = surfaces[row["delivery_id"]]
                if group == GROUP_SPEC_UNVERIFIED:
                    unresolvable.append((row, group))
                    continue
                if group is not None and row["part_id"] in group:
                    grouped.append((row, group[row["part_id"]]))
                    continue
            mark(row, row["attempt_id"] or envelopes.part_attempt_id(
                row["delivery_id"], row["part_id"]))
        # surfaces first (above), so a pending body_part surface already
        # carries the attempt id its group shares
        for row, surface in grouped:
            aid, reason = _surface_attempt(db, row["delivery_id"], surface)
            if reason:
                unresolvable.append((row, reason))
            else:
                mark(row, aid)
        for row, reason in unresolvable:
            mark(row, None, reason)
        # a re-run (the marker stays while the journal is incomplete) finds no
        # pending parts left, but a surface may have become usable since
        promoted = promote_group_parts(db, now)
        for delivery_id in sorted({h["delivery_id"] for h in (*marked, *promoted)}):
            notify_parts.update_state(db, delivery_id, now)
    return marked


def _part_entry(row, aid, reason):
    return {"delivery_id": row["delivery_id"], "part_id": row["part_id"],
            "kind": row["kind"], "card_id": row["card_id"], "attempt_id": aid,
            "reason": reason}


def _beyond_horizon_parts(db) -> tuple[list, list]:
    """Every part still ``unknown`` from a restore beyond the journal horizon,
    so a re-run's receipt still lists what awaits ``ops.card_part_resolve``
    (first) and what it cannot plan and needs manual investigation (second)."""
    codes = (BEYOND_HORIZON, *UNRESOLVABLE)
    parts = [_part_entry(r, r["attempt_id"], r["error_code"]) for r in db.execute(
        "SELECT p.delivery_id,p.part_id,p.kind,p.attempt_id,p.error_code,r.card_id "
        "FROM notification_render_parts p JOIN notification_renders r "
        "ON r.delivery_id=p.delivery_id WHERE p.state='unknown' "
        f"AND p.error_code IN ({','.join('?' * len(codes))}) "
        "ORDER BY p.delivery_id,p.idx", codes).fetchall()]
    return ([p for p in parts if p["reason"] == BEYOND_HORIZON],
            [p for p in parts if p["reason"] != BEYOND_HORIZON])


def _hold_rows(db) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM notification_restore_holds").fetchone()[0]


def reconcile_after_restore(ledger, cfg, now=None) -> dict:
    """Journal <-> restored DB comparison + hold/settle for every
    disputed delivery scope. Clears the restore marker only after the
    reconcile receipt is durable. Idempotent."""
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    root = notify_spool.data_root(ledger)
    dirs = notify_spool.notify_dirs(root)
    notify_spool.ensure_dirs(root)
    marker = notify_spool.restore_pending(root) or {}
    if marker.get("unreadable"):
        raise ValueError("restore_marker_unreadable")
    consent = marker.get("phase") == "awaiting_consent"
    # Every restore path reaches the senders through this reconcile, and the
    # paid-request records rewound with the deliveries: cloud sends stop here
    # too, before the marker can be cleared. An adopted backup keeps its
    # consent marker for good while its writers run, so it is paused by "at".
    # A reconcile_incomplete marker is this module's own, not a restore.
    if ("restored_at" in marker and marker.get("by") != "reconcile_incomplete"
            or consent and notify_spool.restore_awaiting_consent(root) is None):
        import cloud_policy
        with db:
            cloud_policy.pause_for_restore(db, marker.get("restored_at", marker.get("at")), cfg, now)
    if consent:
        # Consent-hold marker: no swap has happened, so there is nothing
        # to reconcile — and clearing it here would silently drop the
        # sender hold the consent gate relies on.
        return {"v": 1, "at": now, "skipped": "awaiting_consent",
                "counts": {}, "verdicts": [], "held": [],
                "events_held": 0}
    # O5: the journal keeps settled evidence only back to its capped horizon.
    # A backup older than that cannot be reconciled from it — hold every
    # unsettled render and make the latest delivered renders' pending parts unknown.
    beyond_horizon = ("restored_at" in marker
                      and marker.get("by") != "reconcile_incomplete"
                      and backup_names.predates_journal_horizon(
                          marker.get("backup_path"), marker.get("restored_at")))
    attempts, journal_incomplete = notify_journal.scan_journals(dirs)
    holds_before = _hold_rows(db)
    verdicts, held = [], []
    for aid in sorted(attempts):
        try:
            v = _reconcile_attempt(ledger, db, cfg, aid, attempts[aid],
                                   dirs, now)
        except Exception as e:
            journal_incomplete = True
            v = _apply_hold(db, aid, None,
                            f"reconcile_crash:{type(e).__name__}",
                            dirs, now)
        verdicts.append(v)
        if v["verdict"] == "held":
            held.append(v)
    # reverse direction: granted/unknown attempts the journal never saw
    # (a lost journal file can prove nothing — hold fail-closed)
    for row in db.execute(
            "SELECT attempt_id,delivery_id FROM "
            "notification_delivery_attempts WHERE state IN "
            "('granted','unknown')").fetchall():
        if row["attempt_id"] in attempts:
            continue
        v = _apply_hold(db, row["attempt_id"], row["delivery_id"],
                        "unknown_attempt", dirs, now)
        verdicts.append(v)
        held.append(v)
    # Only intents that existed at the restore point are unverifiable. Later
    # events just wait in flush's restore gate and resume when it clears.
    restored_at = (marker or {}).get("restored_at")
    if type(restored_at) not in (int, float):
        restored_at = None     # unknown restore point: hold every intent
    if beyond_horizon:
        # renders created after the restore are not from the old backup
        disputed = {h["delivery_id"] for h in held}
        for row in db.execute(
                "SELECT delivery_id FROM notification_renders WHERE state NOT IN "
                "('delivered','not_sent','cancelled','held') "
                "AND (? IS NULL OR created_at <= ?)",
                (restored_at, restored_at)).fetchall():
            if row["delivery_id"] in disputed:
                continue
            v = _apply_hold(db, None, row["delivery_id"],
                            BEYOND_HORIZON, dirs, now)
            verdicts.append(v)
            held.append(v)
        parts_marked = _unknown_beyond_horizon_parts(db, restored_at, now)
        parts_held, parts_unresolvable = _beyond_horizon_parts(db)
    else:
        parts_marked, parts_held, parts_unresolvable = [], [], []
    # an unlinked delivered/unknown effect means the spec file that
    # would map delivery->intent is gone — hold every pending
    # interactive event rather than guess which would duplicate
    unlinked = sum(
        1 for h in held if h["detail"].startswith(("attempt_lost",))
        and not h.get("spec_linked"))
    mass_held = 0
    if unlinked or beyond_horizon:
        mass_held = db.execute(
            "UPDATE notify_outbox SET state='failed',next_try=NULL,"
            f"{HOLD_PROGRESS_SET},updated_at=? "
            "WHERE state='pending' AND route='interactive' "
            "AND (? IS NULL OR created_at IS NULL OR created_at <= ?)",
            ("restore_unlinked" if unlinked else "restore_hold",
             now, restored_at, restored_at)).rowcount
        db.commit()
    # Text sends have no independent transport journal. A restored pending
    # receipt cannot prove that its content was never posted. Keep these
    # intents quarantined even after the card reconciliation marker clears;
    # card_resolve must not acquire authority over unscoped text deliveries.
    text_held = []
    new_text_holds = 0
    import notify_flush
    import notify_sender
    for ev in db.execute(
            "SELECT * FROM notify_outbox WHERE COALESCE(route,'text')='text' "
            "AND state IN ('pending','failed')").fetchall():
        if ev["kind"] in notify_sender.RESTORE_BYPASS_ALERT_KINDS:
            continue
        if marker and ev["next_try"] is not None and (
                restored_at is None or ev["created_at"] is None
                or ev["created_at"] <= restored_at):
            notify_flush.hold_event(
                ledger, ev, cfg, reason="restore_text_unverified", rescue=False)
            new_text_holds += 1
        else:
            # unreadable progress (bad, too deep, not an object) is skipped
            progress = loads_dict(ev["progress"] or "{}")
            if progress is None \
                    or progress.get("hold_reason") != "restore_text_unverified":
                continue
        text_held.append({"event_id": ev["event_id"], "kind": ev["kind"],
                          "reason": "restore_text_unverified"})
    counts = {}
    for v in verdicts:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    receipt = {
        "v": 1, "at": now,
        "restored_at": marker.get("restored_at"),
        "by": marker.get("by"), "backup_path": marker.get("backup_path"),
        "counts": counts, "verdicts": verdicts,
        "held": [{"attempt_id": h["attempt_id"],
                  "delivery_id": h["delivery_id"],
                  "reason": h["detail"], "card_id": h.get("card_id"),
                  "scope": h.get("scope") or {}}
                 for h in held],
        "events_held": sum(h.get("events_held", 0) for h in held)
                        + mass_held + len(text_held),
        "text_held": text_held,
        "parts_held": parts_held,
        "parts_unresolvable": parts_unresolvable,
        "journal_incomplete": journal_incomplete,
    }
    notify_spool.publish_file(root, notify_spool.RESTORE_RECEIPT,
                       cards.canonical(receipt))
    if journal_incomplete and not marker:
        notify_spool.mark_restored(root, by="reconcile_incomplete", now=now)
    elif not journal_incomplete:
        notify_spool.clear_restore_pending(root)
    # a tainted journal keeps the marker, so reconcile re-runs every
    # tick — alert only when this run recorded a new hold, never repeat
    # the same notice for holds already announced
    # (a stop between the part writes and this notice is not re-announced,
    # as for render holds; the receipt and part_hold_report still list them);
    # mass_held counts only events this run moved out of pending
    if ((held and _hold_rows(db) > holds_before) or new_text_holds or parts_marked
            or mass_held):
        ledger.outbox_add("update_notice", None, {
            "notice_code": "delivery_reconcile_held",
            "text": "[MCS] DB復元後の配送照合で未解決の配送があります"
                    f"（card held={len(held)}件、part unknown={len(parts_held)}件、"
                    f"part 要調査={len(parts_unresolvable)}件、"
                    f"text held={len(text_held)}件、"
                    f"未配送の操作通知 held={mass_held}件）。"
                    "restore_reconcile.json を確認してください。"
                    "カードは ops.card_resolve、スレッド・本文・添付は "
                    "ops.card_part_resolve で確認・解除し、"
                    "要調査の添付は送信記録と照合できないため ops.card_part_resolve では解除できず、"
                    "送信先と通知ワーカーの記録の個別調査が必要です。"
                    "text は個別の配送確認と復旧方針の判断が必要です。"})
        cards.mark_snapshot_dirty(db)
        db.commit()
    return receipt
