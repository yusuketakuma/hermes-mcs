"""Human-approved notification recovery from sealed identities and delivery evidence.

Recovery owns command receipts, its transaction and post-commit spec publication;
factual settlement is borrowed from transport and never sends. Stored restore
holds and journals constrain operator evidence before any card or part is rebound.
"""
from __future__ import annotations

import json
import time

from mcs_requests import canonical, payload_hash, positive, valid_uuid
import notify_cards as cards
import notify_spool
import notify_journal
import notify_transport
from notify_scope import (card_projects, card_row, render_row, stored_scope,
                          scope_fields, TRANSPORT_VERSIONS, SUPPORTED_TRANSPORTS)

_RESOLVE_RESULTS = ("mark_not_sent", "mark_delivered")


def validate_card_resolve(req) -> str | None:
    """Dedicated validator — deliberately NOT routed through
    mcs_requests.validate (its positive project_id rule does not apply:
    the project set is derived from the stored render/coverage)."""
    if not isinstance(req, dict):
        return "bad_command"
    allowed = {"version", "cmd", "command_id", "actor", "human_confirmed",
               "reason", "delivery_id", "attempt_id", "result",
               "profile", "application_id", "guild_id", "channel_id",
               "message_id", "evidence"}
    tenant = req.get("version") in (2, 3)
    if tenant:
        allowed = (allowed - {"guild_id"}) | {"transport", "team_id"}
    if req.keys() - allowed:
        return "unknown_field"
    if req.get("cmd") != "ops.card_resolve":
        return "unknown_cmd"
    if type(req.get("version")) is not int or req["version"] not in (1, 2, 3):
        return "bad_version"
    if tenant and req.get("transport") != {2: "slack", 3: "lineworks"}[req["version"]]:
        return "bad_transport"
    if not valid_uuid(req.get("command_id")):
        return "bad_command_id"
    if req.get("human_confirmed") is not True:
        return "human_confirmation_required"
    from mcs_util import valid_text
    if not valid_text(req.get("actor"), 120):
        return "bad_actor"
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    if not valid_uuid(req.get("delivery_id")):
        return "bad_delivery_id"
    if not valid_text(req.get("attempt_id"), 120):
        return "bad_attempt_id"
    if req.get("result") not in _RESOLVE_RESULTS:
        return "bad_result"
    for k in scope_fields(req["transport"] if tenant else "discord"):
        if not valid_text(req.get(k), 200):
            return f"bad_{k}"
    if req["result"] == "mark_delivered" \
            and not valid_text(req.get("message_id"), 64):
        return "bad_message_id"
    ev = req.get("evidence")
    if not isinstance(ev, dict) or not ev:
        return "bad_evidence"
    if not valid_text(ev.get("method"), 200):
        return "bad_evidence_method"
    if not valid_text(ev.get("ref"), 500):
        return "bad_evidence_ref"
    if req["result"] == "mark_not_sent":
        if ev.get("worker_stopped") is not True:
            return "worker_not_proven_stopped"
        # remote_absent: after a restore the journal alone cannot prove
        # non-delivery — the operator may instead attest the remote
        # channel lacks the message (checked against the remote itself)
        if ev.get("proof") not in ("no_journal_started", "api_rejected",
                                   "remote_absent"):
            return "bad_proof"
    return None


def apply_card_resolve(ledger, req, cfg=None, now=None) -> dict:
    """Operator-only recovery for an unsettled attempt. Verifies the
    stored grant/render/card scope and evidence, then runs the SAME
    shared settlement as a normal receipt — resolve never sends."""
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    if cfg is None:
        try:
            import mcs_util
            cfg = mcs_util.load_config(mcs_util.CONF_PATH)
        except Exception:
            cfg = {}
    digest = payload_hash(req)
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload_hash,receipt_json FROM command_receipts "
            "WHERE command_id=?", (req["command_id"],)).fetchone()
        if old:
            if old["payload_hash"] != digest:
                return {"outcome": "rejected", "error": "command_id_conflict"}
            return json.loads(old["receipt_json"])
        error = validate_card_resolve(req)
        receipt = {"kind": "ops.card_resolve", "command_id": req["command_id"],
                   "actor": req.get("actor"), "reason": req.get("reason"),
                   "delivery_id": req.get("delivery_id"),
                   "attempt_id": req.get("attempt_id"),
                   "processed_at": now}
        attempt = render = None
        hold = None
        if error is None:
            attempt = db.execute(
                "SELECT * FROM notification_delivery_attempts "
                "WHERE attempt_id=?", (req["attempt_id"],)).fetchone()
            render = render_row(db, req["delivery_id"]) \
                if attempt else None
            card = (card_row(db, render["card_id"])
                    if render is not None
                    and render["card_id"] is not None else None)
            error = _resolve_check(attempt, render, card, req)
            if error is None and attempt["state"] not in (
                    "delivered", "not_sent"):
                # the operator's proof never outranks the worker's own
                # witness — settling not_sent over a journaled send
                # would reissue it (double post)
                error = _journal_contradiction(ledger, req)
            if error == "unknown_attempt":
                # the restore may have erased the attempt row — a
                # recorded restore hold is the operator's handle for
                # rebind/resume, verified against the scope reconcile
                # captured at hold time
                hold = db.execute(
                    "SELECT * FROM notification_restore_holds "
                    "WHERE delivery_id=? AND released_at IS NULL "
                    "ORDER BY hold_id DESC LIMIT 1",
                    (req["delivery_id"],)).fetchone()
                if hold is not None:
                    card = card_row(db, hold["card_id"]) \
                        if hold["card_id"] is not None else None
                    error = _rebind_check(hold, card, req)
                    if error is None:
                        error = _journal_contradiction(ledger, req)
        if error is None and hold is not None:
            _rebind_apply(db, req, hold, card, now)
            cards.release_holds(db, delivery_id=req["delivery_id"],
                                card_id=hold["card_id"],
                                command_id=req["command_id"], now=now)
            receipt["scope"] = json.loads(hold["scope_json"] or "{}")
            receipt["projects"] = (
                [card["project_id"]]
                if card is not None and positive(card["project_id"])
                else [])
            receipt["outcome"] = "applied"
            receipt["attempt_state"] = (
                "delivered" if req["result"] == "mark_delivered"
                else "not_sent")
            receipt["rebound"] = True
        elif error is None:
            receipt["scope"] = stored_scope(render)
            receipt["projects"] = card_projects(db, render, card)
            if attempt["state"] in ("delivered", "not_sent"):
                same = (attempt["state"]
                        == ("delivered" if req["result"] == "mark_delivered"
                            else "not_sent")
                        and (req["result"] != "mark_delivered"
                             or str(attempt["message_id"]) == str(req["message_id"])))
                receipt["outcome"] = "applied" if same else "rejected"
                if not same:
                    receipt["error"] = "attempt_conflict"
                receipt["attempt_state"] = attempt["state"]
            else:
                result = ("delivered" if req["result"] == "mark_delivered"
                          else "not_sent")
                notify_transport.settle_attempt_tx(db, attempt, render, result, now,
                                message_id=req.get("message_id"),
                                error_code=f"resolve:{req['result']}")
                receipt["outcome"] = "applied"
                receipt["attempt_state"] = result
                if card is not None:
                    cards.issue_render(db, card["card_id"], cfg, now, specs)
        else:
            receipt["outcome"] = "rejected"
            receipt["error"] = error
        if req.get("transport") == "lineworks" and req.get("evidence", {}).get("proof") == "remote_absent":
            receipt["evidence_limit"] = "操作者の申告。未送信の確定ではなく、重複送信のリスクが残ります。"
        db.execute(
            "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
            (req["command_id"], digest, None, None,
             receipt["outcome"], canonical(receipt).decode(), now))
        cards.mark_snapshot_dirty(db)
    if specs:
        root = notify_spool.data_root(ledger)
        notify_spool.ensure_dirs(root)
        notify_spool.publish_specs(db, notify_spool.notify_dirs(root), specs, now)
    return receipt


def hold_events(hold) -> list[int] | None:
    """The outbox event ids a restore hold quarantined, or None when the
    stored list is not exactly a JSON list of positive event ids. The one
    reading shared by authorization and the rebind writes: a malformed
    entry is never skipped, coerced or partly honoured."""
    try:
        events = json.loads(hold["events_json"] or "[]")
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(events, list) or not all(positive(e) for e in events):
        return None
    return events


def _rebind_check(hold, card, req) -> str | None:
    """Verify an attempt-lost resolve against the scope reconcile
    captured when it held the scope — the render row is gone, so the
    hold record is the identity witness."""
    if hold["attempt_id"] != req["attempt_id"]:
        return "attempt_mismatch"
    if hold_events(hold) is None:
        return "hold_events_corrupt"
    try:
        scope = json.loads(hold["scope_json"] or "{}")
    except (ValueError, TypeError, RecursionError):
        return "hold_scope_corrupt"
    if not isinstance(scope, dict):
        return "hold_scope_corrupt"
    transport = scope.get("transport", "discord")
    if not isinstance(transport, str):
        return "hold_scope_corrupt"
    if TRANSPORT_VERSIONS.get(transport) != req.get("version") \
            or transport != req.get("transport", "discord"):
        return "scope_mismatch"
    for k in scope_fields(transport):
        if scope.get(k) != req.get(k):
            return "scope_mismatch"
    if req["result"] == "mark_delivered" and card is not None \
            and card["message_id"] is not None \
            and str(card["message_id"]) != str(req["message_id"]):
        return "message_id_conflict"
    return None


def _rebind_apply(db, req, hold, card, now) -> None:
    """Operator-verified settlement for a scope whose attempt row the
    restore erased. mark_delivered rebinds the card to the proven
    remote message (never resends); mark_not_sent proves the remote is
    absent and returns the card/events to the sendable queue."""
    delivered = req["result"] == "mark_delivered"
    render = render_row(db, req["delivery_id"])
    if render is not None:
        # A surviving render still owns the part plan and fan-out coverage.
        # The operator proves this card's result, never its sibling cards.
        notify_transport.settle_attempt_tx(db, {"attempt_id": req["attempt_id"]}, render,
                        "delivered" if delivered else "not_sent", now,
                        message_id=req.get("message_id") if delivered else None,
                        error_code=f"resolve:{req['result']}")
        return
    if card is not None and card["delivery_state"] == "revoked":
        # a revocation is permanent (the shared settle contract): a proven
        # delivery only binds the remote message the revoke must delete
        if delivered and card["message_id"] is None:
            db.execute(
                "UPDATE notification_cards SET message_id=?,updated_at=? "
                "WHERE card_id=?", (str(req["message_id"]), now, card["card_id"]))
    elif card is not None:
        if delivered:
            db.execute(
                "UPDATE notification_cards SET message_id=?,"
                "delivery_state='delivered',applied_render_rev="
                "MAX(applied_render_rev,desired_render_rev),"
                "last_delivery_error=NULL,updated_at=? WHERE card_id=?",
                (str(req["message_id"]), now, card["card_id"]))
        else:
            db.execute(
                "UPDATE notification_cards SET delivery_state='pending',"
                "last_delivery_error=?,updated_at=? WHERE card_id=?",
                (f"resolve:{req['result']}", now, card["card_id"]))
    db.execute(
        "UPDATE notification_renders SET state=?,updated_at=? "
        "WHERE delivery_id=? AND state='held'",
        ("delivered" if delivered else "not_sent",
         now, req["delivery_id"]))
    # _rebind_check refused a hold whose list is not positive ids
    for eid in hold_events(hold) or []:
        if delivered:
            # the remote card exists — the intent's work is done
            db.execute(
                "UPDATE notify_outbox SET state='accepted',"
                "next_try=NULL,updated_at=? WHERE event_id=? "
                "AND state='failed'", (now, eid))
        else:
            db.execute(
                "UPDATE notify_outbox SET state='pending',next_try=?,"
                "updated_at=? WHERE event_id=? AND state='failed' "
                "AND next_try IS NULL", (now, now, eid))


def _resolve_check(attempt, render, card, req) -> str | None:
    if attempt is None:
        return "unknown_attempt"
    if render is None or attempt["delivery_id"] != req["delivery_id"]:
        return "delivery_mismatch"
    if render["transport"] != req.get("transport", "discord"):
        return "scope_mismatch"
    for k in scope_fields(render["transport"]):
        if render[k] != req.get(k):
            return "scope_mismatch"
    # create binds a discovered message once; update/revoke must
    # match the message the card already knows
    if req["result"] == "mark_delivered" and card is not None \
            and render["op"] != "create" \
            and card.get("message_id") is not None \
            and str(req["message_id"]) != card["message_id"]:
        return "message_id_mismatch"
    return None


def _journal_contradiction(ledger, req) -> str | None:
    """Check an attempt-row resolve against the delivery journal (the
    same scan post-restore reconcile trusts). A delivered ``result``
    refutes any mark_not_sent and pins mark_delivered's message id; a
    ``started`` row or a tainted journal (a corrupt line could hide
    one) refutes the ``no_journal_started`` proof. An unreadable
    journal proves nothing — fail closed."""
    root = notify_spool.data_root(ledger)
    try:
        notify_spool.ensure_dirs(root)
        attempts, incomplete = notify_journal.scan_journals(
            notify_spool.notify_dirs(root))
    except OSError:
        return "journal_unverifiable"
    rec = attempts.get(req["attempt_id"]) or {"rows": [], "tainted": False}
    rows = list(rec["rows"])
    for aid, record in attempts.items():
        if aid != req["attempt_id"]:
            rows.extend(row for row in record["rows"]
                        if row.get("delivery_id") == req["delivery_id"] and row.get("part_id"))
    delivered = [r for r in rows if r.get("phase") == "result"
                 and r.get("result") == "delivered"]
    if req["result"] == "mark_delivered":
        # a delivered row that recorded no message id witnesses the send
        # but cannot contradict the operator's id
        ids = {str(r["message_id"]) for r in delivered
               if not r.get("part_id") and r.get("message_id") not in (None, "")}
        return "message_id_mismatch" \
            if ids - {str(req["message_id"])} else None
    if (delivered or any(r.get("part_id") not in (None, "", "card")
                         and r.get("phase") == "started" for r in rows)
            or cards.connection(ledger).execute(
                "SELECT 1 FROM notification_render_parts WHERE delivery_id=? "
                "AND (state='delivered' OR (kind!='card' AND state='unknown')) LIMIT 1",
                (req["delivery_id"],)).fetchone()):
        return "journal_contradicts_proof"
    if req["evidence"].get("proof") == "no_journal_started" and (
            any(r.get("phase") == "started" for r in rows)
            or rec["tainted"] or incomplete):
        return "journal_contradicts_proof"
    return None


def card_part_resolve_plan(ledger, delivery_id, part_id) -> dict:
    """Read sealed identity on a borrowed ledger/db without writes or source text.

    Missing or ineligible parts return an error; transaction ownership stays
    with the caller, including snapshot readers and the recovery transaction."""
    db = cards.connection(ledger)
    render = render_row(db, delivery_id)
    part = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                      (delivery_id, part_id)).fetchone()
    if render is None or part is None:
        return {"error": "unknown_part"}
    if part["kind"] == "card" or part["state"] != "unknown" or not part["attempt_id"]:
        return {"error": "part_not_resolvable"}
    return {"version": 1, "cmd": "ops.card_part_resolve", **stored_scope(render),
            "transport": render["transport"],
            "delivery_id": delivery_id, "part_id": part_id, "attempt_id": part["attempt_id"],
            "manifest_sha256": render["payload_hash"], "payload_sha256": part["payload_sha256"],
            "expected_state": "unknown"}


def validate_card_part_resolve(req) -> str | None:
    """Validate human approval of exactly one sealed unknown part."""
    from mcs_util import valid_text
    from mcs_util import valid_hash
    if not isinstance(req, dict):
        return "bad_command"
    transport = req.get("transport")
    if transport not in SUPPORTED_TRANSPORTS:
        return "bad_transport"
    allowed = {"version", "cmd", "command_id", "actor", "human_confirmed", "reason",
               "delivery_id", "attempt_id", "part_id", "manifest_sha256", "payload_sha256",
               "expected_state", "result", "remote_id", "evidence", "transport"} | set(scope_fields(transport))
    if req.keys() - allowed:
        return "unknown_field"
    if type(req.get("version")) is not int or req["version"] != 1 or req.get("cmd") != "ops.card_part_resolve":
        return "bad_version"
    if not valid_uuid(req.get("command_id")) or not valid_uuid(req.get("delivery_id")):
        return "bad_command_identity"
    if req.get("human_confirmed") is not True:
        return "human_confirmation_required"
    for key, limit in (("actor", 120), ("reason", 2000), ("attempt_id", 120), ("part_id", 64)):
        if not valid_text(req.get(key), limit):
            return "bad_" + key
    for key in scope_fields(transport):
        if not valid_text(req.get(key), 200):
            return "bad_" + key
    if any(not valid_hash(req.get(key)) for key in ("manifest_sha256", "payload_sha256")):
        return "bad_hash"
    if req.get("expected_state") != "unknown" or req.get("result") not in _RESOLVE_RESULTS:
        return "bad_resolution"
    evidence = req.get("evidence")
    if not isinstance(evidence, dict) or evidence.keys() - {"method", "ref", "worker_stopped", "proof", "duplicate_risk_accepted"}:
        return "bad_evidence"
    if not valid_text(evidence.get("method"), 200) or not valid_text(evidence.get("ref"), 500):
        return "bad_evidence"
    if evidence.get("worker_stopped") is not True:
        return "worker_not_proven_stopped"
    if req["result"] == "mark_delivered":
        if not valid_text(req.get("remote_id"), 64):
            return "remote_id_required"
    elif evidence.get("proof") not in ("no_journal_started", "api_rejected", "remote_absent"):
        return "bad_proof"
    elif transport == "lineworks" and evidence.get("proof") == "remote_absent" and evidence.get("duplicate_risk_accepted") is not True:
        return "duplicate_risk_acknowledgement_required"
    return None


def _part_resolution_journal_error(ledger, req):
    try:
        attempts, incomplete = notify_journal.scan_journals(notify_spool.notify_dirs(notify_spool.data_root(ledger)))
    except OSError:
        return "journal_unverifiable"
    rows = [row for record in attempts.values() for row in record["rows"]
            if row.get("delivery_id") == req["delivery_id"] and row.get("part_id") == req["part_id"]]
    delivered = [row for row in rows if row.get("phase") == "result" and row.get("result") == "delivered"]
    if req["result"] == "mark_delivered":
        if any(row.get("remote_id") not in (None, "", req["remote_id"]) for row in delivered):
            return "remote_id_mismatch"
        return None
    if delivered or incomplete:
        return "journal_contradicts_proof"
    proof = req["evidence"]["proof"]
    if proof == "no_journal_started" and any(row.get("phase") == "started" for row in rows):
        return "journal_contradicts_proof"
    if proof == "api_rejected" and not any(row.get("phase") == "result" and row.get("result") == "not_sent" for row in rows):
        return "api_rejection_unproven"
    return None


def apply_card_part_resolve(ledger, req, cfg=None, now=None) -> dict:
    """Resolve one unknown part through the normal receipt path under a human receipt."""
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    if cfg is None:
        import mcs_util
        cfg = mcs_util.load_config(mcs_util.CONF_PATH)
    digest, specs = payload_hash(req), []
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT payload_hash,receipt_json FROM command_receipts WHERE command_id=?",
                         (req.get("command_id"),)).fetchone()
        if old:
            return (json.loads(old["receipt_json"]) if old["payload_hash"] == digest
                    else {"outcome": "rejected", "error": "command_id_conflict"})
        error = validate_card_part_resolve(req)
        receipt = {"kind": "ops.card_part_resolve", "command_id": req.get("command_id"),
                   "actor": req.get("actor"), "reason": req.get("reason"),
                   "delivery_id": req.get("delivery_id"), "part_id": req.get("part_id"),
                   "processed_at": now}
        if error is None:
            plan = card_part_resolve_plan(db, req["delivery_id"], req["part_id"])
            error = plan.get("error")
            if not error and any(req.get(key) != value for key, value in plan.items()):
                error = "stale_part_plan"
        if error is None:
            error = _part_resolution_journal_error(ledger, req)
        if error is None:
            render = render_row(db, req["delivery_id"])
            card = card_row(db, render["card_id"])
            fact = {**stored_scope(render),
                    **{key: render[key] for key in ("delivery_id", "render_rev", "payload_hash", "route_epoch", "correlation")},
                    "part_id": req["part_id"], "attempt_id": req["attempt_id"],
                    "result": "delivered" if req["result"] == "mark_delivered" else "not_sent",
                    "remote_id": req.get("remote_id"), "error_code": "operator_part_resolve"}
            db.execute("SAVEPOINT part_resolution")
            settled = notify_transport.settle_part_receipt(db, fact, now)
            if not settled["applied"]:
                db.execute("ROLLBACK TO part_resolution")
                error = "part_settle_rejected"
            db.execute("RELEASE part_resolution")
            if error is None:
                receipt.update(outcome="applied", scope=stored_scope(render),
                               projects=card_projects(db, render, card), part_state=settled["part_state"],
                               manifest_sha256=req["manifest_sha256"], payload_sha256=req["payload_sha256"],
                               attempt_id=req["attempt_id"], evidence=req["evidence"])
                if req["transport"] == "lineworks" and req["evidence"].get("proof") == "remote_absent":
                    receipt["evidence_limit"] = "操作者の申告。未送信の確定ではなく、重複送信のリスクが残ります。"
                if (card is not None and card["delivery_state"] != "update_failed"
                        and notify_spool.restore_pending(notify_spool.data_root(ledger)) is None
                        and not cards.resend_exhausted(db, card["card_id"])):
                    cards.issue_render(db, card["card_id"], cfg, now, specs, force=req["result"] == "mark_not_sent")
        if error is not None:
            receipt.update(outcome="rejected", error=error)
        db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                   (req["command_id"], digest, None, None, receipt["outcome"], canonical(receipt).decode(), now))
        cards.mark_snapshot_dirty(db)
    if specs:
        notify_spool.publish_specs(db, notify_spool.notify_dirs(notify_spool.data_root(ledger)), specs, now)
    return receipt
