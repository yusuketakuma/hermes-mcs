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
rebind/resume) or a factual receipt settling the attempt.

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

import notify_cards as cards
import notify_transport

_RESULT_VALUES = ("delivered", "not_sent", "unknown")
_TERMINAL = ("delivered", "not_sent", "cancelled")


def _journal_dirs(dirs: dict) -> list:
    return [dirs[t + "_state"] for t in ("discord", "slack")]


def _scan_journals(dirs: dict) -> tuple[dict, bool]:
    """attempt_id -> {"rows": [...], "tainted": bool}, across every
    per-worker journal file in both transport state dirs.

    A file with an unparsable non-empty line is tainted: a torn tail
    is survivable, but a corrupt middle line could hide a 'started' or
    'result' row — absence-of-evidence verdicts are untrustworthy for
    any attempt recorded in it.
    """
    out: dict[str, dict] = {}
    incomplete = False
    for state_dir in _journal_dirs(dirs):
        names = sorted(n for n in os.listdir(state_dir)
                       if n.startswith("journal-") and n.endswith(".jsonl"))
        for name in names:
            file_rows: dict[str, list] = {}
            tainted = False
            try:
                with open(os.path.join(state_dir, name), "rb") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            row = json.loads(raw)
                        except (ValueError, RecursionError):
                            tainted = True
                            continue
                        if not isinstance(row, dict):
                            tainted = True
                            continue
                        aid = row.get("attempt_id")
                        if isinstance(aid, str) and aid:
                            file_rows.setdefault(aid, []).append(row)
                        else:
                            tainted = True
            except OSError:
                # An inaccessible journal can contain an effect lost by
                # the restored DB. Do not clear the global restore hold.
                raise
            incomplete = incomplete or tainted
            for aid, rows in file_rows.items():
                rec = out.setdefault(aid, {"rows": [], "tainted": False})
                rec["rows"].extend(rows)
                if tainted:
                    rec["tainted"] = True
    return out, incomplete


def _read_spec(dirs: dict, delivery_id) -> dict | None:
    """The published spec file survives a DB restore (it lives outside
    the DB) — the only link from a lost delivery_id back to its intent
    event ids."""
    if not isinstance(delivery_id, str) or not delivery_id:
        return None
    safe = "".join(c if c.isalnum() or c in "._-" else "_"
                   for c in delivery_id)
    for t in ("discord", "slack"):
        try:
            with open(os.path.join(dirs[t + "_render"], safe + ".json"),
                      "rb") as fh:
                spec = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError):
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
    render = db.execute(
        "SELECT * FROM notification_renders WHERE delivery_id=?",
        (delivery_id,)).fetchone() if delivery_id else None
    card = cards._card_row(db, render["card_id"]) \
        if render is not None and render["card_id"] is not None else None
    if scope is None:
        scope = (dict(cards.stored_scope(render)) if render is not None
                 else {})
    spec = _read_spec(dirs, delivery_id)
    events = _spec_events(spec)
    if scope is None or not scope:
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
                "updated_at=? WHERE event_id=? AND state='pending'",
                (now, eid))
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
    render = db.execute(
        "SELECT * FROM notification_renders WHERE delivery_id=?",
        (delivery_id,)).fetchone() if delivery_id else None
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
    if part["state"] in _RESULT_VALUES + ("held",):
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
        if render["transport"] == "slack":
            env.update(version=2, transport="slack",
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
    phases = {r.get("phase") for r in rows}
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
    render = db.execute(
        "SELECT * FROM notification_renders WHERE delivery_id=?",
        (delivery_id,)).fetchone() if delivery_id else None

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


def _hold_rows(db) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM notification_restore_holds").fetchone()[0]


def reconcile_after_restore(ledger, cfg, now=None) -> dict:
    """Journal <-> restored DB comparison + hold/settle for every
    disputed delivery scope. Clears the restore marker only after the
    reconcile receipt is durable. Idempotent."""
    db = cards._db(ledger)
    now = time.time() if now is None else now
    root = cards.data_root(ledger)
    dirs = cards.notify_dirs(root)
    cards.ensure_dirs(root)
    marker = cards.restore_pending(root) or {}
    if marker.get("unreadable"):
        raise ValueError("restore_marker_unreadable")
    if marker.get("phase") == "awaiting_consent":
        # Consent-hold marker: no swap has happened, so there is nothing
        # to reconcile — and clearing it here would silently drop the
        # sender hold the consent gate relies on.
        return {"v": 1, "at": now, "skipped": "awaiting_consent",
                "counts": {}, "verdicts": [], "held": [],
                "events_held": 0}
    attempts, journal_incomplete = _scan_journals(dirs)
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
    # an unlinked delivered/unknown effect means the spec file that
    # would map delivery->intent is gone — hold every pending
    # interactive event rather than guess which would duplicate
    unlinked = sum(
        1 for h in held if h["detail"].startswith(("attempt_lost",))
        and not h.get("spec_linked"))
    mass_held = 0
    if unlinked:
        mass_held = db.execute(
            "UPDATE notify_outbox SET state='failed',next_try=NULL,"
            "updated_at=? WHERE state='pending' AND route='interactive'",
            (now,)).rowcount
        db.commit()
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
                        + mass_held,
        "journal_incomplete": journal_incomplete,
    }
    cards.publish_file(root, cards.RESTORE_RECEIPT,
                       cards.canonical(receipt))
    if journal_incomplete and not marker:
        cards.mark_restored(root, by="reconcile_incomplete", now=now)
    elif not journal_incomplete:
        cards.clear_restore_pending(root)
    # a tainted journal keeps the marker, so reconcile re-runs every
    # tick — alert only when this run recorded a new hold, never repeat
    # the same notice for holds already announced
    if held and _hold_rows(db) > holds_before:
        ledger.outbox_add("update_notice", None, {
            "text": "[MCS] DB復元後の配送照合で未解決の配送があります"
                    f"（held={len(held)}件）。restore_reconcile.json と "
                    "ops.card_resolve で確認・解除してください"})
        cards.mark_snapshot_dirty(db)
        db.commit()
    return receipt
