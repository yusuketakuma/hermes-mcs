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
                          scope_fields, scope_matches, TRANSPORT_VERSIONS, SUPPORTED_TRANSPORTS)

_RESOLVE_RESULTS = ("mark_not_sent", "mark_delivered")
_PREFLIGHT_PROOF = "local_preflight_rejected"
_PREFLIGHT_ERRORS = ("edit_target_unverified", "edit_history_unverified")


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
    if evidence.get("proof") == _PREFLIGHT_PROOF:
        if transport != "slack" or req["result"] != "mark_not_sent" or "remote_id" in req:
            return "bad_preflight_resolution"
        return None
    if req["result"] == "mark_delivered":
        if not valid_text(req.get("remote_id"), 64):
            return "remote_id_required"
    elif evidence.get("proof") not in ("no_journal_started", "api_rejected", "remote_absent"):
        return "bad_proof"
    elif transport == "lineworks" and evidence.get("proof") == "remote_absent" and evidence.get("duplicate_risk_accepted") is not True:
        return "duplicate_risk_acknowledgement_required"
    return None


def _preflight_journal_error(attempts, incomplete, req, render, part):
    # These two Slack outcomes are emitted only before chat.update. An
    # incomplete/interrupted attempt or any other outcome cannot establish
    # that boundary, even when the operator can see the original upload.
    import notify_cmds
    record = attempts.get(req["attempt_id"])
    if incomplete or not record or record["tainted"]:
        return "local_preflight_unproven"
    rows = record["rows"]
    phases = [row.get("phase") for row in rows]
    if len(rows) < 3 or phases[:2] != ["started", "result"] or any(p != "receipt" for p in phases[2:]):
        return "local_preflight_unproven"
    identity = {"attempt_id": req["attempt_id"], "delivery_id": req["delivery_id"],
                "part_id": req["part_id"], "kind": "attachment_part"}
    worker = rows[0].get("worker_id")
    if not isinstance(worker, str) or not worker:
        return "local_preflight_unproven"
    for aid, other in attempts.items():
        for row in other["rows"]:
            env = row.get("receipt_envelope")
            witnesses = [row, env] if isinstance(env, dict) else [row]
            if aid != req["attempt_id"] and any(
                    witness.get("delivery_id") == req["delivery_id"]
                    and witness.get("part_id") == req["part_id"] for witness in witnesses):
                return "local_preflight_unproven"
    echo = {**stored_scope(render), **identity, "version": 2, "op": "part_receipt",
            **{key: render[key] for key in ("render_rev", "payload_hash", "route_epoch", "correlation")},
            "result": "unknown"}
    for index, row in enumerate(rows):
        if (any(row.get(key) != value for key, value in identity.items())
                or row.get("v") != 1 or row.get("worker_id") != worker):
            return "local_preflight_unproven"
        if index >= 1 and row.get("result") != "unknown":
            return "local_preflight_unproven"
        if index == 0 and row.get("result") is not None:
            return "local_preflight_unproven"
        if row.get("remote_id") is not None:
            return "local_preflight_unproven"
        if index < 2:
            env = row.get("receipt_envelope")
            expected_error = part["error_code"] if index == 1 else None
            if (not isinstance(env, dict) or notify_cmds.validate_int(env)
                    or any(env.get(key) != value for key, value in echo.items())
                    or env.get("remote_id") is not None
                    or env.get("error_code") != expected_error
                    or row.get("error_code") != expected_error):
                return "local_preflight_unproven"
        elif row.get("error_code") is not None or "receipt_envelope" in row:
            return "local_preflight_unproven"
    return None


def _part_resolution_journal_error(ledger, req):
    try:
        attempts, incomplete = notify_journal.scan_journals(notify_spool.notify_dirs(notify_spool.data_root(ledger)))
    except OSError:
        return "journal_unverifiable"
    if req["evidence"].get("proof") == _PREFLIGHT_PROOF:
        db = cards.connection(ledger)
        render = render_row(db, req["delivery_id"])
        part = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                          (req["delivery_id"], req["part_id"])).fetchone()
        if (render["transport"] != "slack" or render["op"] != "update"
                or part["kind"] != "attachment_part" or part["error_code"] not in _PREFLIGHT_ERRORS):
            return "local_preflight_unproven"
        error = _preflight_journal_error(attempts, incomplete, req, render, part)
        if error:
            return error
        cancelled = _unstarted_cancelled_renders(db, render["card_id"])
        for record in attempts.values():
            for row in record["rows"]:
                witnesses = [row] + [row[key] for key in ("begin_envelope", "receipt_envelope")
                                     if isinstance(row.get(key), dict)]
                if any(isinstance(witness.get("delivery_id"), str)
                       and witness["delivery_id"] in cancelled for witness in witnesses) and (
                        row.get("phase") not in ("claimed", "begin", "denied")
                        or row.get("result") in ("delivered", "unknown")):
                    if _withdrawn_before_grant(db, record, cancelled):
                        continue
                    return "preflight_target_unverified"
        return None
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


def _withdrawn_before_grant(db, record, cancelled):
    # _settle_orphan_claim emits receipt directly from begin_sent. The
    # same error code also exists after a grant, so prove the entire path.
    import notify_cmds
    rows = record["rows"]
    if (record["tainted"] or len(rows) < 3 or rows[0].get("phase") != "claimed"
            or rows[-1].get("phase") != "receipt"
            or any(row.get("phase") != "begin" for row in rows[1:-1])):
        return False
    delivery_id, aid, worker = (rows[0].get(key) for key in ("delivery_id", "attempt_id", "worker_id"))
    if not isinstance(delivery_id, str) or delivery_id not in cancelled:
        return False
    render = render_row(db, delivery_id)
    echo = {**stored_scope(render), "version": 2, "attempt_id": aid,
            **{key: render[key] for key in ("delivery_id", "render_rev", "payload_hash", "route_epoch")}}
    first_begin = rows[1].get("begin_envelope")
    for index, row in enumerate(rows):
        if (row.get("v") != 1 or row.get("delivery_id") != delivery_id or row.get("attempt_id") != aid
                or row.get("worker_id") != worker or row.get("part_id") is not None or row.get("kind") is not None
                or row.get("message_id") is not None or row.get("remote_id") is not None):
            return False
        if index == len(rows) - 1:
            if (row.get("result") != "not_sent" or row.get("error_code") != "spec_withdrawn"
                    or "receipt_envelope" in row):
                return False
        elif row.get("result") is not None or row.get("error_code") is not None:
            return False
        elif index == 0:
            if row.get("render_rev") != render["render_rev"] or row.get("op") != render["op"]:
                return False
        else:
            begin, receipt = row.get("begin_envelope"), row.get("receipt_envelope")
            for env, expected in ((begin, {**echo, "op": "transport_begin", "worker_id": worker}),
                                  (receipt, {**echo, "op": "transport_receipt", "result": "unknown",
                                             "correlation": render["correlation"]})):
                if (not isinstance(env, dict) or notify_cmds.validate_int(env)
                        or any(env.get(key) != value for key, value in expected.items())
                        or env.get("message_id") is not None or env.get("error_code") is not None):
                    return False
            if begin != first_begin or row.get("command_id") != begin["command_id"]:
                return False
    attempts = db.execute("SELECT * FROM notification_delivery_attempts WHERE attempt_id=? OR begin_command_id=?",
                          (aid, first_begin["command_id"])).fetchall()
    if not attempts:
        return True
    if len(attempts) != 1:
        return False
    attempt = attempts[0]
    return (attempt["attempt_id"] == aid and attempt["state"] == "not_sent" and attempt["delivery_id"] == delivery_id
            and attempt["worker_id"] == worker and attempt["message_id"] is None
            and attempt["begin_command_id"] == first_begin["command_id"])


def _unstarted_cancelled_renders(db, card_id):
    # Cancellation permanently denies a fresh begin; a claim alone grants
    # nothing. Keep any send/part evidence blocking, including an old grant
    # that an idempotent begin replay could still return to its worker.
    return {row["delivery_id"] for row in db.execute("""
        SELECT r.delivery_id FROM notification_renders r
        WHERE r.card_id=? AND r.state='cancelled'
          AND EXISTS(SELECT 1 FROM notification_render_parts p
                     WHERE p.delivery_id=r.delivery_id AND p.state='pending')
          AND NOT EXISTS(SELECT 1 FROM notification_render_parts p WHERE p.delivery_id=r.delivery_id
                         AND (p.state!='pending' OR p.attempt_id IS NOT NULL OR p.remote_id IS NOT NULL))
          AND NOT EXISTS(SELECT 1 FROM notification_delivery_attempts a
                         WHERE a.delivery_id=r.delivery_id AND a.state!='not_sent')""", (card_id,))}


def _delivered_edit_target(db, card, part, through_rev):
    """Return one remote identity proven by same-scope, same-thread receipts."""
    rows = db.execute("""SELECT p.*,r.transport,r.profile,r.application_id,r.guild_id,r.team_id,r.channel_id
        FROM notification_render_parts p JOIN notification_renders r ON r.delivery_id=p.delivery_id
        WHERE r.card_id=? AND r.render_rev<=? AND r.state='delivered'
          AND p.kind=? AND p.state='delivered' AND p.remote_id IS NOT NULL AND p.error_code IS NULL
          AND EXISTS(SELECT 1 FROM notification_render_parts t WHERE t.delivery_id=r.delivery_id
                     AND t.kind='thread' AND t.state='delivered' AND t.remote_id=?)""",
        (card["card_id"], through_rev, part["kind"], card["thread_id"])).fetchall()
    remotes = set()
    for row in rows:
        if not scope_matches(row, stored_scope(card)) or row["name"] != part.get("name"):
            continue
        if part["kind"] == "attachment_part" and any(
                row[key] != part.get(field) for key, field in (
                    ("part_id", "part_id"), ("attachment_id", "attachment_id"),
                    ("payload_sha256", "sha256"), ("bytes", "bytes"))):
            continue
        if isinstance(row["remote_id"], str) and row["remote_id"]:
            remotes.add(row["remote_id"])
    return next(iter(remotes)) if len(remotes) == 1 else None


def _preflight_recovery_target(db, render, card, req):
    if (card is None or render["state"] != "delivered" or card["delivery_state"] != "delivered"
            or not scope_matches(render, stored_scope(card)) or not card["message_id"]
            or card["thread_state"] != "created" or not card["thread_id"]):
        return None
    latest = db.execute("SELECT delivery_id FROM notification_renders WHERE card_id=? ORDER BY render_rev DESC LIMIT 1",
                        (card["card_id"],)).fetchone()
    unsettled = db.execute("SELECT p.delivery_id,p.part_id,p.state FROM notification_render_parts p "
                          "JOIN notification_renders r ON r.delivery_id=p.delivery_id "
                          "WHERE r.card_id=? AND p.state IN ('pending','unknown')",
                          (card["card_id"],)).fetchall()
    cancelled = _unstarted_cancelled_renders(db, card["card_id"])
    unsettled = [row for row in unsettled if row["state"] == "unknown" or row["delivery_id"] not in cancelled]
    if (latest["delivery_id"] != render["delivery_id"] or len(unsettled) != 1
            or (unsettled[0]["delivery_id"], unsettled[0]["part_id"]) != (req["delivery_id"], req["part_id"])):
        return None
    for kind, remote in (("card", card["message_id"]), ("thread", card["thread_id"])):
        if not db.execute("SELECT 1 FROM notification_render_parts WHERE delivery_id=? AND kind=? "
                          "AND state='delivered' AND remote_id=?",
                          (render["delivery_id"], kind, remote)).fetchone():
            return None
    part = dict(db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                           (req["delivery_id"], req["part_id"])).fetchone())
    part["sha256"] = part["payload_sha256"]
    remote = _delivered_edit_target(db, card, part, render["render_rev"])
    if remote and render["spec_json"] is not None:
        try:
            old = json.loads(render["spec_json"])
            targets = [p for p in old["parts"]["manifest"] if p["part_id"] == part["part_id"]]
            if (payload_hash(old) != render["payload_hash"] or len(targets) != 1
                    or targets[0].get("edit_only") is not True
                    or targets[0].get("prior_remote_id") != remote
                    or targets[0].get("sha256") != part["sha256"]):
                return None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return None
    return {**part, "prior_remote_id": remote} if remote else None


def _seal_preflight_update(db, candidates, delivery_id, render, card, target):
    # The ordinary planner supplies current content and source/action gates.
    # Only unpublished, unclaimed candidates may be restricted and re-sealed;
    # the old manifest and its file hash are never reconstructed or replaced.
    if len(candidates) != 1 or not delivery_id or candidates[0].get("delivery_id") != delivery_id:
        return "preflight_replan_blocked"
    spec = candidates[0]
    current = render_row(db, delivery_id)
    delivery = spec.get("delivery", {})
    if (current is None or current["state"] != "queued" or current["spec_published"] != 0
            or current["first_published_at"] is not None or current["card_id"] != card["card_id"]
            or current["payload_hash"] != payload_hash(spec) or current["spec_json"] != canonical(spec).decode()
            or spec.get("op") != "update" or not scope_matches(card, delivery)
            or delivery.get("message_id") != card["message_id"] or delivery.get("thread_id") != card["thread_id"]
            or db.execute("SELECT 1 FROM notification_delivery_attempts WHERE delivery_id=?", (delivery_id,)).fetchone()):
        return "preflight_replan_unsafe"
    manifest = spec.get("parts", {}).get("manifest", [])
    saved = {row["part_id"]: row for row in db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?", (delivery_id,))}
    if (len(manifest) != len(saved) or {p.get("part_id") for p in manifest} != set(saved)
            or {p.get("kind") for p in manifest} - {"card", "thread", "body_part", "attachment_part"}
            or sum(p.get("kind") == "card" for p in manifest) != 1
            or sum(p.get("kind") == "thread" for p in manifest) != 1):
        return "preflight_replan_unsafe"
    found_target = False
    used_remotes = set()
    for part in manifest:
        row = saved.get(part.get("part_id"))
        if (row is None or row["state"] != "pending" or row["attempt_id"] is not None
                or any(row[key] != part.get(field) for key, field in (
                    ("kind", "kind"), ("idx", "index"), ("payload_sha256", "sha256"),
                    ("bytes", "bytes"), ("name", "name"), ("attachment_id", "attachment_id")))):
            return "preflight_replan_unsafe"
        if part["kind"] in ("card", "thread"):
            continue
        remote = _delivered_edit_target(db, card, part, render["render_rev"])
        if (not remote or remote in used_remotes or part.get("prior_remote_id") != remote or part.get("broadcast") is True
                or part.get("unavailable") is True
                or (part["kind"] == "attachment_part" and not part.get("caption"))):
            return "preflight_replan_unsafe"
        if part["part_id"] == target["part_id"]:
            if any(part.get(key) != target[key] for key in ("kind", "attachment_id", "sha256", "prior_remote_id")):
                return "preflight_replan_unsafe"
            found_target = True
        part["edit_only"] = True
        used_remotes.add(remote)
    if not found_target:
        return "preflight_replan_unsafe"
    db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
               (canonical(spec).decode(), payload_hash(spec), delivery_id))
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
            preflight = req["evidence"].get("proof") == _PREFLIGHT_PROOF
            target = _preflight_recovery_target(db, render, card, req) if preflight else None
            if preflight and target is None:
                error = "preflight_target_unverified"
        if error is None:
            fact = {**stored_scope(render),
                    **{key: render[key] for key in ("delivery_id", "render_rev", "payload_hash", "route_epoch", "correlation")},
                    "part_id": req["part_id"], "attempt_id": req["attempt_id"],
                    "result": "delivered" if req["result"] == "mark_delivered" else "not_sent",
                    "remote_id": req.get("remote_id"), "error_code": "operator_part_resolve"}
            db.execute("SAVEPOINT part_resolution")
            settled = notify_transport.settle_part_receipt(db, fact, now)
            if not settled["applied"]:
                error = "part_settle_rejected"
            candidates = []
            if error is None and preflight:
                if (notify_spool.restore_pending(notify_spool.data_root(ledger)) is not None
                        or cards.resend_exhausted(db, card["card_id"])):
                    error = "preflight_replan_blocked"
                else:
                    replacement = cards.issue_render(db, card["card_id"], cfg, now, candidates, force=True)
                    error = _seal_preflight_update(db, candidates, replacement, render, card, target)
            if error is not None:
                db.execute("ROLLBACK TO part_resolution")
            db.execute("RELEASE part_resolution")
            if error is None:
                receipt.update(outcome="applied", scope=stored_scope(render),
                               projects=card_projects(db, render, card), part_state=settled["part_state"],
                               manifest_sha256=req["manifest_sha256"], payload_sha256=req["payload_sha256"],
                               attempt_id=req["attempt_id"], evidence=req["evidence"])
                if preflight:
                    receipt.update(replacement_delivery_id=replacement,
                                   replacement_manifest_sha256=payload_hash(candidates[0]),
                                   content_basis="current_content")
                    specs.extend(candidates)
                if req["transport"] == "lineworks" and req["evidence"].get("proof") == "remote_absent":
                    receipt["evidence_limit"] = "操作者の申告。未送信の確定ではなく、重複送信のリスクが残ります。"
                if (not preflight and card is not None and card["delivery_state"] != "update_failed"
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
