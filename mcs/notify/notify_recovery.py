"""Human-approved notification recovery from sealed identities and delivery evidence.

Recovery owns command receipts, its transaction and post-commit spec publication;
factual settlement is borrowed from transport and never sends. Stored restore
holds and journals constrain operator evidence before any card or part is rebound.
"""
from __future__ import annotations

import json
import math
import time

from mcs_requests import canonical, payload_hash, positive, replay_receipt, valid_uuid
import notify_cards as cards
import notify_spool
import notify_journal
import notify_transport
from notify_scope import (card_projects, card_row, render_row, stored_scope,
                          scope_fields, scope_matches, retired_transports,
                          TRANSPORT_VERSIONS, SUPPORTED_TRANSPORTS)

_RESOLVE_RESULTS = ("mark_not_sent", "mark_delivered")
_PREFLIGHT_PROOF = "local_preflight_rejected"
_PREFLIGHT_ERRORS = ("edit_target_unverified", "edit_history_unverified")
_DEPENDENCY_PROOF = "dependency_rejected"
_DEPENDENCY_ERROR = "stage_receipt_missing"


# receipt note for an operator-declared LINE WORKS remote absence
_LINEWORKS_ABSENT_LIMIT = "操作者の申告。未送信の確定ではなく、重複送信のリスクが残ります。"


def _latest_delivery_id(db, card_id):
    """Row of the card's newest render (``delivery_id``), or None."""
    return db.execute("SELECT delivery_id FROM notification_renders WHERE card_id=? "
                      "ORDER BY render_rev DESC LIMIT 1", (card_id,)).fetchone()


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
        found, old = replay_receipt(db, req["command_id"], digest)
        if found:
            return old
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
            receipt["evidence_limit"] = _LINEWORKS_ABSENT_LIMIT
        db.execute(
            "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
            (req["command_id"], digest, None, None,
             receipt["outcome"], canonical(receipt).decode(), now))
        cards.mark_snapshot_dirty(db)
    notify_spool.publish_committed(ledger, db, specs, now)
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


def card_part_resolve_plan(ledger, delivery_id, part_id, *, include_related=False) -> dict:
    """Read sealed identity on a borrowed ledger/db without writes or source text.

    Unknown parts and Slack groups with a recorded local dependency refusal
    are eligible; the latter require a separate positive journal proof at apply.
    Missing or ineligible parts return an error; transaction ownership stays
    with the caller, including snapshot readers and the recovery transaction."""
    db = cards.connection(ledger)
    render = render_row(db, delivery_id)
    part = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                      (delivery_id, part_id)).fetchone()
    if render is None or part is None:
        return {"error": "unknown_part"}
    dependency = (render["transport"] == "slack" and part["kind"] == "file_group"
                  and part["part_id"] == "file_group" and part["state"] == "not_sent"
                  and part["error_code"] == _DEPENDENCY_ERROR
                  and part["attempt_id"] == f"p:{delivery_id.replace('-', '')}:file_group")
    if part["kind"] == "card" or (part["state"] != "unknown" and not dependency) or not part["attempt_id"]:
        return {"error": "part_not_resolvable"}
    if render["transport"] == "discord" and part["kind"] == "attachment_part":
        try:
            group = _discord_recovery_group(db, {"delivery_id": delivery_id, "part_id": part_id,
                                                 "attempt_id": part["attempt_id"]})
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return {"error": "file_group_identity_unverified"}
        if group is not None:
            anchor = db.execute("SELECT state,attempt_id FROM notification_render_parts "
                                "WHERE delivery_id=? AND part_id=?",
                                (delivery_id, group["part_id"])).fetchone()
            if anchor is None or anchor["state"] != "unknown" or anchor["attempt_id"] != part["attempt_id"]:
                return {"error": "file_group_identity_unverified"}
            # Approval covers the entire sealed group; individual member commands cannot match this plan.
            return card_part_resolve_plan(db, delivery_id, group["part_id"], include_related=include_related)
    plan = {"version": 1, "cmd": "ops.card_part_resolve", **stored_scope(render),
            "transport": render["transport"],
            "delivery_id": delivery_id, "part_id": part_id, "attempt_id": part["attempt_id"],
            "manifest_sha256": render["payload_hash"], "payload_sha256": part["payload_sha256"],
            "expected_state": part["state"]}
    if include_related:
        if render["transport"] != "slack" or render["op"] != "update" or render["card_id"] is None:
            return {"error": "related_parts_unresolvable"}
        latest = _latest_delivery_id(db, render["card_id"])
        if latest["delivery_id"] != delivery_id:
            return {"error": "related_parts_unresolvable"}
        related = []
        for row in _preflight_unsettled_parts(db, render["card_id"]):
            if (row["delivery_id"] != delivery_id or row["state"] != "unknown"
                    or row["kind"] != "attachment_part" or row["error_code"] not in _PREFLIGHT_ERRORS):
                return {"error": "related_parts_unresolvable"}
            other = card_part_resolve_plan(db, delivery_id, row["part_id"])
            if "error" in other:
                return other
            if row["part_id"] != part_id:
                related.append(other)
        if related:
            plan["related_plans"] = related
    return plan


# every fixed reason a producer stores on a held or unknown part; others count as 'unknown'
PART_HOLD_REASONS = ("part_budget_exceeded", "thread_cleanup_budget_exceeded",
                     "blank_part_unrepresentable", "attachment_upload_identity_unverified", "card_revoked",
                     *_PREFLIGHT_ERRORS, _DEPENDENCY_ERROR,
                     "timeouterror", "connectionerror", "worker_crash", "journal_incomplete",
                     "edit_reference_missing", "edit_file_size_mismatch", "edit_file_unverified",
                     "file_target_unverified", "part_owner_ambiguous", "msg_too_long",
                     "body_part_wire_too_long", "attachment_unavailable", "attachment_mismatch",
                     "file_group_unverified", "file_group_missing", "file_group_duplicate",
                     "message_mismatch", "missing_remote_id",
                     "edit_response_unverified", "edit_result_unknown", "edit_thread_unverified",
                     "card_target_missing", "body_part_missing",
                     "thread_root_missing", "thread_scope_mismatch", "thread_capability_blocked",
                     "workspace_unverified", "sdk_capability_missing", "retry_policy_unknown",
                     "prefetch_failed", "unsupported_part", "slack_attachment_incomplete",
                     "scope_mismatch", "no_target", "bad_render", "bad_slack_response",
                     "operator_part_resolve")


def recovery_guidance(kind, transport, error_code, attempt_id) -> dict:
    """Describe existing recovery requirements without creating an approval payload."""
    known = PART_HOLD_REASONS
    code = error_code if error_code in known else "not_recorded" if error_code is None else "unknown"
    card = kind == "card"
    available = (transport in SUPPORTED_TRANSPORTS and kind in ("card", "thread", "body_part", "attachment_part", "file_group")
                 and (kind != "file_group" or transport in ("slack", "discord"))
                 and isinstance(attempt_id, str) and bool(attempt_id.strip()))
    not_sent = {"worker_stopped": True,
                "proof_any_of": ["no_journal_started", "api_rejected", "remote_absent"],
                "journal_must_not_contradict": True}
    if transport == "lineworks":
        not_sent["remote_absent_requires_duplicate_risk_accepted"] = not card
        not_sent["remote_absence_is_not_delivery_proof"] = True
    if not card and transport == "slack" and code in _PREFLIGHT_ERRORS:
        not_sent["conditional_proof"] = _PREFLIGHT_PROOF
        not_sent["conditional_proof_requires_current_sealed_preflight"] = True
    if kind == "file_group" and transport == "slack" and code == _DEPENDENCY_ERROR:
        not_sent["proof_any_of"] = [_DEPENDENCY_PROOF]
    return {"error_code": code, "available": available,
            "reason": None if available else "attempt_or_transport_unverified",
            "command": "ops.card_resolve" if card else "ops.card_part_resolve",
            "preview": None if card else "card_part_plan",
            "next_action": "inspect_remote_and_journal" if available else "inspect_worker_journal",
            "requirements": {"human_confirmation": True, "actor": True, "reason": True,
                             "evidence_fields": ["method", "ref"],
                             "worker_stopped_for_all_results": not card,
                             "mark_delivered": {"remote_identity": "message_id" if card else
                                                "card_message_id_and_exact_file_set" if kind == "file_group" else "remote_id"},
                             "mark_not_sent": not_sent},
            "final_scope_and_journal_validation_required": True,
            "automatic_resend": False}


def part_hold_report(ledger) -> dict:
    """Dry-run counts of held/unknown parts by fixed reason and of specs GC keeps; reads only.

    Every fixed reason is present (0 is observed, not missing); a NULL code
    is 'not_recorded' and any other code 'unknown'. ``resolvable`` counts
    unknown parts carrying an attempt id — a planning hint, not a
    guarantee: card_part_resolve_plan stays authoritative (it can still
    reject, e.g. an unverified Discord group identity), and the
    dependency-rejected ``not_sent`` file_groups it also plans are not
    counted here. The caller bounds time."""
    db = cards.connection(ledger)
    marks = ",".join("?" * len(PART_HOLD_REASONS))
    by_state = {state: dict.fromkeys((*PART_HOLD_REASONS, "not_recorded", "unknown"), 0)
                for state in ("held", "unknown")}
    resolvable = 0
    for row in db.execute(
            f"SELECT state, CASE WHEN error_code IN ({marks}) THEN error_code "
            "WHEN error_code IS NULL THEN 'not_recorded' ELSE 'unknown' END, COUNT(*), "
            "SUM(state='unknown' AND attempt_id IS NOT NULL AND attempt_id!='') "
            "FROM notification_render_parts WHERE kind!='card' AND state IN ('held','unknown') "
            "GROUP BY 1,2", PART_HOLD_REASONS):
        by_state[row[0]][row[1]] += row[2]
        resolvable += row[3]
    spec = db.execute(
        "SELECT COALESCE(SUM(spec_json IS NOT NULL),0), COALESCE(SUM(spec_published=1),0), "
        "COALESCE(SUM(EXISTS(SELECT 1 FROM notification_render_parts p WHERE p.delivery_id=r.delivery_id "
        "AND r.transport='slack' AND p.kind='attachment_part' AND p.state='unknown' "
        f"AND p.error_code IN ({','.join('?' * len(_PREFLIGHT_ERRORS))}))),0) "
        "FROM notification_renders r", _PREFLIGHT_ERRORS).fetchone()
    return {"parts": by_state, "resolvable": resolvable,
            "specs": {"stored_json": spec[0], "published_files": spec[1],
                      "kept_for_edit_check": spec[2]}}


def recovery_worklist(db, cfg=None, *, limit=20) -> dict:
    """Bounded operator worklist of parts awaiting human-reviewed recovery.

    Read-only on the caller's connection. Candidates are non-card parts
    that are ``unknown``, held awaiting attachment-identity confirmation,
    or dependency-rejected ``file_groups``; each is then verified through
    the same plan the approval command uses, so ``status``/``blocker``
    report real plan eligibility rather than a guess. Cancelled renders
    and retired transports are excluded. ``total`` counts every candidate
    before ``limit``; ``truncated`` marks a cut list. A Discord member
    redirected to its shared ``file_group`` anchor names the anchor in
    ``approval_part_id`` with ``group_approval`` — approval covers the
    whole sealed group, never the member alone."""
    now = time.time()
    retired = retired_transports(cfg)
    marks = ",".join("?" * len(retired))
    live = f" AND r.transport NOT IN ({marks})" if retired else ""
    where = ("p.kind!='card' AND r.state!='cancelled'" + live +
             " AND (p.state='unknown' "
             "OR (p.state='held' AND p.error_code='attachment_upload_identity_unverified') "
             "OR (p.state='not_sent' AND p.kind='file_group' AND p.error_code=?))")
    params = (*retired, _DEPENDENCY_ERROR)
    total = db.execute(
        "SELECT COUNT(*) FROM notification_render_parts p "
        "JOIN notification_renders r USING(delivery_id) WHERE " + where,
        params).fetchone()[0]
    items = []
    for row in db.execute(
            "SELECT p.delivery_id,p.part_id,p.kind,p.state,p.error_code,p.attempt_id,"
            "r.transport,COALESCE(p.updated_at,r.updated_at) observed_at "
            "FROM notification_render_parts p JOIN notification_renders r USING(delivery_id) "
            "WHERE " + where + " ORDER BY p.updated_at LIMIT ?",
            (*params, limit)):
        delivery_id, part_id = row["delivery_id"], row["part_id"]
        if row["state"] == "held":
            flow = "attachment_identity_confirm"
            plan = attachment_identity_confirm_plan(db, delivery_id, part_id)
        else:
            flow = "card_part_resolve"
            plan = card_part_resolve_plan(db, delivery_id, part_id)
        at = row["observed_at"]
        item = {"delivery_id": delivery_id, "part_id": part_id,
                "kind": row["kind"], "state": row["state"],
                "transport": row["transport"],
                "error_code": recovery_guidance(row["kind"], row["transport"],
                                                row["error_code"], row["attempt_id"])["error_code"],
                "attempt_id": row["attempt_id"],
                "age_s": (round(max(0, now - at), 1)
                          if type(at) in (int, float) and math.isfinite(at)
                          and 0 <= at <= now else None),
                "flow": flow}
        if "error" in plan:
            item["status"] = "blocked"
            item["blocker"] = plan["error"]
        else:
            item["status"] = "plannable"
            if plan["part_id"] != part_id:
                item["approval_part_id"] = plan["part_id"]
                item["group_approval"] = True
        items.append(item)
    return {"total": total, "truncated": total > len(items), "items": items}


def validate_card_part_resolve(req) -> str | None:
    """Validate human approval of one sealed part recovery."""
    from mcs_util import valid_text
    from mcs_util import valid_hash
    if not isinstance(req, dict):
        return "bad_command"
    transport = req.get("transport")
    if transport not in SUPPORTED_TRANSPORTS:
        return "bad_transport"
    allowed = {"version", "cmd", "command_id", "actor", "human_confirmed", "reason",
               "delivery_id", "attempt_id", "part_id", "manifest_sha256", "payload_sha256",
               "expected_state", "result", "remote_id", "evidence", "transport", "related_plans"} | set(scope_fields(transport))
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
    if req.get("expected_state") not in ("unknown", "not_sent") or req.get("result") not in _RESOLVE_RESULTS:
        return "bad_resolution"
    evidence = req.get("evidence")
    if not isinstance(evidence, dict) or evidence.keys() - {"method", "ref", "worker_stopped", "proof", "duplicate_risk_accepted", "files"}:
        return "bad_evidence"
    if not valid_text(evidence.get("method"), 200) or not valid_text(evidence.get("ref"), 500):
        return "bad_evidence"
    if evidence.get("worker_stopped") is not True:
        return "worker_not_proven_stopped"
    if "files" in evidence and (transport != "discord" or req.get("result") != "mark_delivered"
            or notify_transport.files_echo_error({"transport": transport, "files": evidence["files"]})):
        return "bad_evidence"

    if req["expected_state"] == "not_sent":
        if (transport != "slack" or req["part_id"] != "file_group" or req["result"] != "mark_not_sent"
                or evidence.get("proof") != _DEPENDENCY_PROOF or "remote_id" in req or "related_plans" in req):
            return "bad_dependency_resolution"
        return None
    if "related_plans" in req:
        if (transport != "slack" or req["result"] != "mark_not_sent"
                or evidence.get("proof") != _PREFLIGHT_PROOF):
            return "bad_related_resolution"
        from mcs_requests import MAX_COMMAND_BYTES
        from notify_parts import MAX_PARTS
        related = req["related_plans"]
        if not isinstance(related, list) or not 0 < len(related) < MAX_PARTS:
            return "bad_related_plans"
        if len(canonical(req)) > MAX_COMMAND_BYTES:
            return "command_too_large"
        keys = {"version", "cmd", "transport", "delivery_id", "part_id", "attempt_id",
                "manifest_sha256", "payload_sha256", "expected_state"} | set(scope_fields(transport))
        seen = {req["part_id"]}
        base = {key: value for key, value in req.items() if key != "related_plans"}
        for plan in related:
            if not isinstance(plan, dict) or plan.keys() != keys:
                return "bad_related_plan"
            if any(plan[key] != req[key] for key in keys - {"part_id", "attempt_id", "payload_sha256"}):
                return "related_plan_scope_mismatch"
            error = validate_card_part_resolve({**base, **plan})
            if error:
                return error
            if plan["part_id"] in seen:
                return "duplicate_related_part"
            seen.add(plan["part_id"])
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


def _preflight_journal_error(attempts, incomplete, req, render, part, *, result="unknown", kind="attachment_part"):
    # These Slack outcomes identify a local refusal or explicit API rejection.
    # An incomplete attempt or another outcome cannot prove a rejected edit,
    # even when the operator can see the original upload.
    import notify_cmds
    record = attempts.get(req["attempt_id"])
    if incomplete or not record or record["tainted"]:
        return "local_preflight_unproven"
    rows = record["rows"]
    phases = [row.get("phase") for row in rows]
    if len(rows) < 3 or phases[:2] != ["started", "result"] or any(p != "receipt" for p in phases[2:]):
        return "local_preflight_unproven"
    identity = {"attempt_id": req["attempt_id"], "delivery_id": req["delivery_id"],
                "part_id": req["part_id"], "kind": kind}
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
            **{key: render[key] for key in ("render_rev", "payload_hash", "route_epoch", "correlation")}}
    for index, row in enumerate(rows):
        if (any(row.get(key) != value for key, value in identity.items())
                or row.get("v") != 1 or row.get("worker_id") != worker):
            return "local_preflight_unproven"
        if index >= 1 and row.get("result") != result:
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
                    or env.get("result") != ("unknown" if index == 0 else result)
                    or env.get("remote_id") is not None
                    or env.get("error_code") != expected_error
                    or row.get("error_code") != expected_error):
                return "local_preflight_unproven"
        elif row.get("error_code") is not None or "receipt_envelope" in row:
            return "local_preflight_unproven"
    return None


def _discord_recovery_group(db, req):
    # Logical group parts share their surface's wire attempt, not a journal of their own.
    render = render_row(db, req["delivery_id"])
    if render is None or render["transport"] != "discord":
        return None
    part = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                      (req["delivery_id"], req["part_id"])).fetchone()
    if part is None:
        raise ValueError("missing part")
    if part["kind"] not in ("attachment_part", "file_group"):
        return None
    if not db.execute("SELECT 1 FROM notification_render_parts WHERE delivery_id=? "
                      "AND kind='file_group' LIMIT 1", (req["delivery_id"],)).fetchone():
        return None
    from adapters.common import spec as wire_spec
    from mcs_util import strict_json
    spec = wire_spec.validate(strict_json(render["spec_json"]))
    card = card_row(db, render["card_id"])
    if (card is None or not scope_matches(render, stored_scope(card))
            or spec["card_key"] != card["card_key"] or spec["logical_intent_id"] != card["card_key"]
            or payload_hash(spec) != render["payload_hash"]
            or any(spec.get(key) != render[key] for key in ("delivery_id", "render_rev", "op"))
            or not scope_matches(render, spec["delivery"])
            or any(spec["delivery"].get(key) != render[key] for key in ("route_epoch", "correlation"))):
        raise ValueError("unbound spec")
    groups = [item for item in spec["parts"]["manifest"]
              if item["kind"] == "file_group" and
              (item["part_id"] == req["part_id"] or req["part_id"] in item["members"])]
    if not groups and part["kind"] != "file_group":
        return None
    if len(groups) != 1:
        raise ValueError("unbound group")
    surface = groups[0]["surface"]
    row = db.execute("SELECT attempt_id FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                     (req["delivery_id"], surface)).fetchone()
    if row is None or row["attempt_id"] != req["attempt_id"] or part["attempt_id"] != req["attempt_id"]:
        raise ValueError("unbound attempt")
    return groups[0]


def _part_resolution_journal_error(ledger, req, *, journal_evidence=None):
    if journal_evidence is None:
        try:
            journal_evidence = notify_journal.scan_journals(notify_spool.notify_dirs(notify_spool.data_root(ledger)))
        except OSError:
            return "journal_unverifiable"
    attempts, incomplete = journal_evidence
    if req.get("expected_state") == "not_sent":
        db = cards.connection(ledger)
        render = render_row(db, req["delivery_id"])
        part = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                          (req["delivery_id"], req["part_id"])).fetchone()
        error = _preflight_journal_error(attempts, incomplete, req, render, part,
                                        result="not_sent", kind="file_group")
        return "dependency_rejection_unproven" if error else None
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
    try:
        group = _discord_recovery_group(cards.connection(ledger), req)
        surface = group["surface"] if group else None
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return "file_group_identity_unverified"
    rows = [row for record in attempts.values() for row in record["rows"]
            if row.get("delivery_id") == req["delivery_id"]
            and (row.get("part_id") == req["part_id"] if surface is None else
                 row.get("attempt_id") == req["attempt_id"] and
                 (row.get("part_id") == surface or surface == "card" and not row.get("part_id")))]
    delivered = [row for row in rows if row.get("phase") == "result" and row.get("result") == "delivered"]
    if req["result"] == "mark_delivered":
        if any((row.get("message_id", row.get("remote_id")) if surface == "card"
                else row.get("remote_id")) not in (None, "", req["remote_id"]) for row in delivered):
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


def _preflight_unsettled_parts(db, card_id):
    rows = db.execute("SELECT p.delivery_id,p.part_id,p.state,p.kind,p.error_code FROM notification_render_parts p "
                      "JOIN notification_renders r ON r.delivery_id=p.delivery_id "
                      "WHERE r.card_id=? AND p.state IN ('pending','unknown') ORDER BY p.delivery_id,p.part_id",
                      (card_id,)).fetchall()
    cancelled = _unstarted_cancelled_renders(db, card_id)
    return [row for row in rows if row["state"] == "unknown" or row["delivery_id"] not in cancelled]


def _preflight_recovery_targets(db, render, card, plans):
    if (card is None or render["state"] != "delivered" or card["delivery_state"] != "delivered"
            or not scope_matches(render, stored_scope(card)) or not card["message_id"]
            or card["thread_state"] != "created" or not card["thread_id"]):
        return None
    latest = _latest_delivery_id(db, card["card_id"])
    unsettled = _preflight_unsettled_parts(db, card["card_id"])
    expected = {(plan["delivery_id"], plan["part_id"]) for plan in plans}
    if (latest["delivery_id"] != render["delivery_id"] or len(expected) != len(plans)
            or any(plan["delivery_id"] != render["delivery_id"] for plan in plans)
            or {(row["delivery_id"], row["part_id"]) for row in unsettled} != expected
            or any(row["state"] != "unknown" for row in unsettled)):
        return None
    for kind, remote in (("card", card["message_id"]), ("thread", card["thread_id"])):
        if not db.execute("SELECT 1 FROM notification_render_parts WHERE delivery_id=? AND kind=? "
                          "AND state='delivered' AND remote_id=?",
                          (render["delivery_id"], kind, remote)).fetchone():
            return None
    targets, used_remotes = {}, set()
    for plan in plans:
        part = dict(db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                               (plan["delivery_id"], plan["part_id"])).fetchone())
        part["sha256"] = part["payload_sha256"]
        remote = _delivered_edit_target(db, card, part, render["render_rev"])
        if not remote or remote in used_remotes:
            return None
        from notify_scope import _attachment_identity_witness_proofs
        if (render["spec_json"] is not None
                and _attachment_identity_witness_proofs(render["spec_json"], render) is None):
            try:
                old = json.loads(render["spec_json"])
                matches = [p for p in old["parts"]["manifest"] if p["part_id"] == part["part_id"]]
                if (payload_hash(old) != render["payload_hash"] or len(matches) != 1
                        or matches[0].get("edit_only") is not True
                        or matches[0].get("prior_remote_id") != remote
                        or matches[0].get("sha256") != part["sha256"]):
                    return None
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                return None
        targets[part["part_id"]] = {**part, "prior_remote_id": remote}
        used_remotes.add(remote)
    return targets


def _human_delivered_part(db, delivery_id, part_id):
    # Reuse the common digest-checked human receipt verifier, including duplicate rejection.
    import notify_parts
    rows = db.execute("SELECT r.*,p.part_id,p.payload_sha256,p.attempt_id AS part_attempt,"
                      "p.error_code AS part_error,p.remote_id FROM notification_render_parts p "
                      "JOIN notification_renders r ON r.delivery_id=p.delivery_id "
                      "WHERE p.delivery_id=? AND p.part_id=? AND p.state='delivered'",
                      (delivery_id, part_id)).fetchall()
    return bool(notify_parts._manual_edit_receipts(db, rows).get((delivery_id, part_id)))


def _preflight_shared_file(db, card, part, remote, identities):
    # A private upload receipt proves preparation, not visibility on this card.
    expected = (part["part_id"], part["attachment_id"], part.get("sha256"),
                part.get("bytes"), part.get("name"))
    for proof in identities.get(remote, ()):
        if proof is None or proof[:5] != expected:
            continue
        if proof[5] not in ("private_stage", "private_operator_edit", "file", "collected", "operator_edit", "operator_verified"):
            continue
        shared = db.execute("SELECT p.delivery_id,p.error_code,p.state,p.remote_id FROM notification_renders r "
                            "JOIN notification_render_parts p ON p.delivery_id=r.delivery_id "
                            "WHERE r.card_id=? AND r.render_rev=? AND r.state='delivered' "
                            "AND p.kind='file_group' AND p.part_id='file_group'",
                            (card["card_id"], proof[6])).fetchone()
        if shared is None:
            if proof[5] not in ("private_stage", "private_operator_edit"):
                return True  # Legacy public uploads have no private-stage group.
        elif (shared[2] == "delivered" and shared[3] == card["message_id"]
              and (shared[1] is None or shared[1] == "operator_part_resolve"
                   and _human_delivered_part(db, shared[0], "file_group"))):
            return True
    return False


def _seal_preflight_update(db, candidates, delivery_id, render, card, targets):
    # The ordinary planner supplies current content and source/action gates.
    # Only unpublished, unclaimed candidates may be restricted and re-sealed;
    # the old manifest and its file hash are never reconstructed or replaced.
    if len(candidates) != 1 or not delivery_id or candidates[0].get("delivery_id") != delivery_id:
        return "preflight_replan_blocked"
    spec = candidates[0]
    current = render_row(db, delivery_id)
    delivery = spec.get("delivery", {})
    if (current is None or current["state"] not in ("queued", "held") or current["spec_published"] != 0
            or current["first_published_at"] is not None or current["card_id"] != card["card_id"]
            or current["render_rev"] <= render["render_rev"]
            or current["state"] == "held" and current["parts_state"] != "held"
            or current["payload_hash"] != payload_hash(spec) or current["spec_json"] != canonical(spec).decode()
            or spec.get("op") != "update" or not scope_matches(card, delivery)
            or delivery.get("message_id") != card["message_id"] or delivery.get("thread_id") != card["thread_id"]
            or db.execute("SELECT 1 FROM notification_delivery_attempts WHERE delivery_id=?", (delivery_id,)).fetchone()):
        return "preflight_replan_unsafe"
    manifest = spec.get("parts", {}).get("manifest", [])
    saved = {row["part_id"]: row for row in db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?", (delivery_id,))}
    if (len(manifest) != len(saved) or {p.get("part_id") for p in manifest} != set(saved)
            or {p.get("kind") for p in manifest} - {"card", "thread", "body_part", "attachment_part", "file_group"}
            or sum(p.get("kind") == "card" for p in manifest) != 1
            or sum(p.get("kind") == "thread" for p in manifest) != 1):
        return "preflight_replan_unsafe"
    groups = [part for part in manifest if part.get("kind") == "file_group"]
    grouped = (spec.get("parts", {}).get("slack") or {}).get("file_group") is True
    if groups or grouped:
        if (card["transport"] != "slack" or spec.get("kind") != "thread" or not grouped
                or len(groups) != 1 or groups[0] is not manifest[-1]
                or set(groups[0]) != {"part_id", "kind", "index", "sha256"}
                or groups[0]["part_id"] != "file_group"
                or not any(part["kind"] == "attachment_part" and not part.get("unavailable")
                           for part in manifest)):
            return "preflight_replan_unsafe"
    if grouped:
        from adapters.common.spec import file_group_sha256_matches
        if not file_group_sha256_matches(spec, groups[0].get("sha256")):
            return "preflight_replan_unsafe"
    import notify_parts
    identities = notify_parts._same_thread_delivered_parts(db, card)[2] if grouped else {}
    found_targets = set()
    used_remotes = set()
    held = current["state"] == "held"
    for part in manifest:
        row = saved.get(part.get("part_id"))
        if (row is None or row["state"] != ("held" if held else "pending") or row["attempt_id"] is not None
                or held and row["error_code"] != "attachment_upload_identity_unverified"
                or any(row[key] != part.get(field) for key, field in (
                    ("kind", "kind"), ("idx", "index"), ("payload_sha256", "sha256"),
                    ("bytes", "bytes"), ("name", "name"), ("attachment_id", "attachment_id")))):
            return "preflight_replan_unsafe"
        if part["kind"] in ("card", "thread", "file_group"):
            continue
        remote = _delivered_edit_target(db, card, part, render["render_rev"])
        if (not remote or remote in used_remotes or part.get("prior_remote_id") != remote or part.get("broadcast") is True
                or part.get("unavailable") is True
                or (part["kind"] == "attachment_part" and not part.get("caption"))):
            return "preflight_replan_unsafe"
        if (grouped and part["kind"] == "attachment_part"
                and not _preflight_shared_file(db, card, part, remote, identities)):
            return "preflight_replan_unsafe"
        if part["part_id"] in targets:
            target = targets[part["part_id"]]
            if any(part.get(key) != target[key] for key in ("kind", "attachment_id", "sha256", "prior_remote_id")):
                return "preflight_replan_unsafe"
            found_targets.add(part["part_id"])
        part["edit_only"] = True
        used_remotes.add(remote)
    if found_targets != set(targets):
        return "preflight_replan_unsafe"
    db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
               (canonical(spec).decode(), payload_hash(spec), delivery_id))
    if held:
        db.execute("UPDATE notification_renders SET state='queued',parts_state='pending' WHERE delivery_id=?",
                   (delivery_id,))
        db.execute("UPDATE notification_render_parts SET state='pending',error_code=NULL WHERE delivery_id=?",
                   (delivery_id,))
        db.execute("UPDATE notification_cards SET delivery_state='delivered' WHERE card_id=?", (card["card_id"],))
    return None


def _discord_file_group_resolution_error(db, render, card, group_row, req):
    """Discord ``file_group:<surface>`` anchors bind to a surface message
    (card or one body_part), not a slack-style thread file set — re-check
    the render↔spec↔card identity, then delegate surface/member binding
    to the transport's shared validator."""
    from adapters.common import spec as _spec
    from mcs_util import strict_json
    try:
        spec = _spec.validate(strict_json(render["spec_json"]))
        if (card is None or render["card_id"] != card["card_id"]
                or not scope_matches(render, stored_scope(card))
                or payload_hash(spec) != render["payload_hash"]
                or any(spec.get(key) != render[key] for key in ("delivery_id", "render_rev", "op"))
                or not scope_matches(render, spec["delivery"])
                or any(spec["delivery"].get(key) != render[key] for key in ("route_epoch", "correlation"))
                or spec["card_key"] != card["card_key"] or spec["logical_intent_id"] != card["card_key"]
                or card["delivery_state"] != "delivered"):
            return "file_group_identity_unverified"
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return "file_group_identity_unverified"
    return notify_transport._discord_group_part_error(db, render, group_row, req)


def _file_group_resolution_error(db, render, card, plan, req):
    group_row = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                           (plan["delivery_id"], plan["part_id"])).fetchone()
    if group_row["kind"] != "file_group":
        return None
    if render["transport"] == "discord":
        return _discord_file_group_resolution_error(db, render, card, group_row, req)
    from adapters.slack.cards import validate
    from mcs_util import strict_json
    try:
        spec = validate(strict_json(render["spec_json"]))
        manifest = spec["parts"]["manifest"]
        groups = [part for part in manifest if part["kind"] == "file_group"]
        if (card is None or render["transport"] != "slack" or spec["kind"] != "thread"
                or render["card_id"] != card["card_id"] or not scope_matches(render, stored_scope(card))
                or payload_hash(spec) != render["payload_hash"]
                or any(spec.get(key) != render[key] for key in ("delivery_id", "render_rev", "op"))
                or not scope_matches(render, spec["delivery"])
                or any(spec["delivery"].get(key) != render[key] for key in ("route_epoch", "correlation"))
                or spec["card_key"] != card["card_key"] or spec["logical_intent_id"] != card["card_key"]
                or len(groups) != 1 or groups[0]["part_id"] != plan["part_id"]
                or groups[0]["sha256"] != group_row["payload_sha256"]
                or not isinstance(card["message_id"], str) or not card["message_id"]
                or card["delivery_state"] != "delivered" or card["thread_state"] != "created"
                or not isinstance(card["thread_id"], str) or not card["thread_id"]
                or spec["delivery"].get("thread_id") not in (None, card["thread_id"])
                or spec["op"] == "update" and spec["delivery"].get("message_id") != card["message_id"]
                or req["result"] == "mark_delivered" and req["remote_id"] != card["message_id"]):
            return "file_group_identity_unverified"
        for kind, remote in (("card", card["message_id"]), ("thread", card["thread_id"])):
            if not db.execute("SELECT 1 FROM notification_render_parts WHERE delivery_id=? AND kind=? "
                              "AND state='delivered' AND remote_id=?",
                              (render["delivery_id"], kind, remote)).fetchone():
                return "file_group_identity_unverified"
        stages = [part for part in manifest if part["kind"] == "attachment_part" and not part.get("unavailable")]
        remotes = set()
        if not stages:
            return "file_group_dependencies_unverified"
        for stage in stages:
            row = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                             (render["delivery_id"], stage["part_id"])).fetchone()
            if (row is None or row["state"] != "delivered"
                    or row["error_code"] is not None and not (
                        row["error_code"] == "operator_part_resolve"
                        and _human_delivered_part(db, render["delivery_id"], stage["part_id"]))
                    or not isinstance(row["remote_id"], str) or not row["remote_id"]
                    or row["remote_id"] in remotes
                    or stage.get("prior_remote_id") not in (None, row["remote_id"])
                    or any(row[column] != stage.get(key) for column, key in (
                        ("kind", "kind"), ("idx", "index"), ("attachment_id", "attachment_id"),
                        ("payload_sha256", "sha256"), ("bytes", "bytes"), ("name", "name")))):
                return "file_group_dependencies_unverified"
            remotes.add(row["remote_id"])
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return "file_group_identity_unverified"
    return None


def _file_group_retry_error(db, render, card, candidates):
    # Approval covers the exact existing private FID set, never a newly arrived upload.
    from adapters.slack.cards import validate
    from mcs_util import strict_json
    original = strict_json(render["spec_json"])
    fields = ("part_id", "attachment_id", "sha256", "bytes", "name")
    stages = [part for part in original["parts"]["manifest"]
              if part["kind"] == "attachment_part" and not part.get("unavailable")]
    expected = [tuple(part.get(key) for key in fields) + (db.execute(
        "SELECT remote_id FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
        (render["delivery_id"], part["part_id"])).fetchone()[0],) for part in stages]
    if len(candidates) > 1:
        return "file_group_retry_scope_changed"
    for spec in candidates:
        try:
            validate(spec)
            actual = [tuple(part.get(key) for key in fields) + (part.get("prior_remote_id"),)
                      for part in spec["parts"]["manifest"]
                      if part["kind"] == "attachment_part" and not part.get("unavailable")]
            if (actual != expected or spec["op"] != "update" or spec["card_key"] != card["card_key"]
                    or not scope_matches(card, spec["delivery"])
                    or spec["delivery"].get("message_id") != card["message_id"]
                    or spec["delivery"].get("thread_id") != card["thread_id"]
                    or (spec["parts"].get("slack") or {}).get("file_group") is not True):
                return "file_group_retry_scope_changed"
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return "file_group_retry_scope_changed"
    return None


def apply_card_part_resolve(ledger, req, cfg=None, now=None) -> dict:
    """Resolve explicitly approved eligible parts through the normal receipt path."""
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    if cfg is None:
        import mcs_util
        cfg = mcs_util.load_config(mcs_util.CONF_PATH)
    digest, specs = payload_hash(req), []
    with db:
        db.execute("BEGIN IMMEDIATE")
        found, old = replay_receipt(db, req.get("command_id"), digest)
        if found:
            return old
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
            plans = [plan]
            if not error:
                for related in req.get("related_plans", ()):
                    saved = card_part_resolve_plan(db, related["delivery_id"], related["part_id"])
                    if saved != related:
                        error = "stale_part_plan"
                        break
                    plans.append(saved)
        if error is None:
            journal_evidence = None
            if len(plans) > 1:
                try:
                    journal_evidence = notify_journal.scan_journals(notify_spool.notify_dirs(notify_spool.data_root(ledger)))
                except OSError:
                    error = "journal_unverifiable"
            if error is None:
                for plan in plans:
                    error = _part_resolution_journal_error(ledger, {**req, **plan}, journal_evidence=journal_evidence)
                    if error:
                        break
        if error is None:
            render = render_row(db, req["delivery_id"])
            card = card_row(db, render["card_id"])
            preflight = req["evidence"].get("proof") == _PREFLIGHT_PROOF
            group_retry = (req["result"] == "mark_not_sent" and req["transport"] == "slack"
                           and db.execute(
                "SELECT kind FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                (req["delivery_id"], req["part_id"])).fetchone()[0] == "file_group")
            for plan in plans:
                error = _file_group_resolution_error(db, render, card, plan, req)
                if error:
                    break
            targets = _preflight_recovery_targets(db, render, card, plans) if preflight else None
            if not error and preflight and targets is None:
                error = "preflight_target_unverified"
        if error is None:
            db.execute("SAVEPOINT part_resolution")
            settlements = []
            for plan in plans:
                fact = {**stored_scope(render),
                        **{key: render[key] for key in ("delivery_id", "render_rev", "payload_hash", "route_epoch", "correlation")},
                        "part_id": plan["part_id"], "attempt_id": plan["attempt_id"],
                        "result": "delivered" if req["result"] == "mark_delivered" else "not_sent",
                        "remote_id": req.get("remote_id"), "error_code": "operator_part_resolve"}
                if "files" in req["evidence"]:
                    fact["files"] = req["evidence"]["files"]
                settled = notify_transport.settle_part_receipt(db, fact, now)
                if not settled["applied"]:
                    error = "part_settle_rejected"
                    break
                settlements.append(settled)
            candidates = []
            if error is None and preflight:
                if (notify_spool.restore_pending(notify_spool.data_root(ledger)) is not None
                        or cards.resend_exhausted(db, card["card_id"])):
                    error = "preflight_replan_blocked"
                else:
                    replacement = cards.issue_render(db, card["card_id"], cfg, now, candidates, force=True,
                                                     _preflight_candidate=True)
                    error = _seal_preflight_update(db, candidates, replacement, render, card, targets)
            elif (error is None and group_retry and card["delivery_state"] != "update_failed"
                  and notify_spool.restore_pending(notify_spool.data_root(ledger)) is None
                  and not cards.resend_exhausted(db, card["card_id"])):
                cards.issue_render(db, card["card_id"], cfg, now, candidates, force=True)
                error = _file_group_retry_error(db, render, card, candidates)
            if error is not None:
                db.execute("ROLLBACK TO part_resolution")
            db.execute("RELEASE part_resolution")
            if error is None:
                receipt.update(outcome="applied", scope=stored_scope(render),
                               projects=card_projects(db, render, card), part_state=settlements[0]["part_state"],
                               manifest_sha256=req["manifest_sha256"], payload_sha256=req["payload_sha256"],
                               attempt_id=req["attempt_id"], evidence=req["evidence"])
                if preflight:
                    receipt.update(replacement_delivery_id=replacement,
                                   replacement_manifest_sha256=payload_hash(candidates[0]),
                                   content_basis="current_content")
                    specs.extend(candidates)
                    if len(plans) > 1:
                        receipt["resolved_parts"] = [
                            {**plan, "part_state": item["part_state"],
                             "prior_remote_id": targets[plan["part_id"]]["prior_remote_id"]}
                            for plan, item in zip(plans, settlements)]
                if group_retry:
                    specs.extend(candidates)
                if req["transport"] == "lineworks" and req["evidence"].get("proof") == "remote_absent":
                    receipt["evidence_limit"] = _LINEWORKS_ABSENT_LIMIT
        if error is not None:
            receipt.update(outcome="rejected", error=error)
        db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                   (req["command_id"], digest, None, None, receipt["outcome"], canonical(receipt).decode(), now))
        if (error is None and not preflight and not group_retry and card is not None and card["delivery_state"] != "update_failed"
                and notify_spool.restore_pending(notify_spool.data_root(ledger)) is None
                and not cards.resend_exhausted(db, card["card_id"])):
            cards.issue_render(db, card["card_id"], cfg, now, specs, force=req["result"] == "mark_not_sent")
        cards.mark_snapshot_dirty(db)
    if specs:
        notify_spool.publish_specs(db, notify_spool.notify_dirs(notify_spool.data_root(ledger)), specs, now)
    return receipt


def _confirmed_history_rows(db, card, part_id, attachment_id, sha256, size,
                            through_rev=None):
    """Delivered rows that prove one attachment identity inside this card's thread."""
    return db.execute(
        """SELECT p.delivery_id,p.part_id,p.attachment_id,p.payload_sha256,p.bytes,p.name,
                  p.remote_id,r.render_rev FROM notification_render_parts p
           JOIN notification_renders r ON r.delivery_id=p.delivery_id
           WHERE r.card_id=? AND p.part_id=? AND p.kind='attachment_part'
             AND p.state='delivered' AND p.remote_id IS NOT NULL
             AND p.attachment_id=? AND p.payload_sha256=? AND p.bytes=?
             AND EXISTS(SELECT 1 FROM notification_render_parts t
                        WHERE t.delivery_id=r.delivery_id AND t.kind='thread'
                          AND t.state='delivered' AND t.remote_id=?)
             AND (? IS NULL OR r.render_rev<=?)
           ORDER BY p.delivery_id""",
        (card["card_id"], part_id, attachment_id, sha256, size, card["thread_id"],
         through_rev, through_rev)).fetchall()


def _confirmed_history_digest(rows):
    return payload_hash([tuple(row) for row in rows])


def attachment_identity_confirm_plan(ledger, delivery_id, part_id) -> dict:
    """Read the exact collected identity a held attachment part would be approved for.

    Plans one held attachment_part whose collected remote identity an operator may
    attest; never approves, settles or renders. Missing or ineligible targets return
    an error; the caller owns the transaction."""
    from mcs_util import valid_hash
    db = cards.connection(ledger)
    render = render_row(db, delivery_id)
    part = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                      (delivery_id, part_id)).fetchone()
    if (render is None or part is None or render["op"] != "update"
                or render["transport"] != "slack" or render["state"] != "held"
                or render["parts_state"] != "held" or render["card_id"] is None
                or part["kind"] != "attachment_part" or part["state"] != "held"
                or part["error_code"] != "attachment_upload_identity_unverified"
                or part["attempt_id"] is not None):
        return {"error": "part_not_confirmable"}
    card = card_row(db, render["card_id"])
    if card is None or card["transport"] != "slack" or not card["thread_id"] \
            or not scope_matches(render, stored_scope(card)):
        return {"error": "scope_unverified"}
    if (type(part["attachment_id"]) is not int or part["attachment_id"] <= 0
                or not valid_hash(part["payload_sha256"])
                or type(part["bytes"]) is not int or part["bytes"] < 0
                or not isinstance(part["name"], str) or not 0 < len(part["name"]) <= 200):
        return {"error": "attachment_identity_unverified"}
    current = db.execute(
        "SELECT attachment_id,sha256,bytes,name FROM attachments WHERE attachment_id=?",
        (part["attachment_id"],)).fetchone()
    if (current is None or current["sha256"] != part["payload_sha256"]
                or current["bytes"] != part["bytes"] or current["name"] != part["name"]):
        return {"error": "attachment_source_changed"}
    # The attested identity is the file: same attachment bytes delivered to this
    # thread under a single remote id and a single recorded name. A rename in the
    # new render is exactly the edit_only reuse this approval must unlock, so the
    # delivered name is bound, not the pending one. The whole delivered history is
    # digest-bound so a later row can never widen or quietly change the attestation.
    rows = _confirmed_history_rows(db, card, part_id, part["attachment_id"],
                                   part["payload_sha256"], part["bytes"])
    delivered = {(row["remote_id"], row["name"]) for row in rows}
    if not rows or len(delivered) != 1:
        return {"error": "remote_identity_unverified"}
    remote_id, delivered_name = delivered.pop()
    import notify_render
    return {"version": 1, "cmd": "ops.attachment_identity_confirm",
            **stored_scope(render), "transport": "slack",
            "card_id": card["card_id"], "card_key": card["card_key"],
            "thread_id": card["thread_id"], "message_id": card["message_id"],
            "root_message_id": card["root_message_id"],
            "source_fp": notify_render.source_fingerprint(db, card),
            "route_epoch": render["route_epoch"], "correlation": render["correlation"],
            "history_sha256": _confirmed_history_digest(rows),
            "through_rev": max(row["render_rev"] for row in rows),
            "delivery_id": delivery_id, "part_id": part_id,
            "attachment_id": part["attachment_id"], "sha256": part["payload_sha256"],
            "bytes": part["bytes"], "name": delivered_name, "remote_id": remote_id,
            "manifest_sha256": render["payload_hash"],
            "payload_sha256": part["payload_sha256"], "expected_state": "held"}


def validate_attachment_identity_confirm(req) -> str | None:
    """Validate human attestation of one collected attachment upload identity."""
    from mcs_util import valid_text, valid_hash
    if not isinstance(req, dict):
        return "bad_command"
    allowed = {"version", "cmd", "command_id", "actor", "human_confirmed", "reason",
               "transport", "card_id", "card_key", "thread_id", "message_id",
               "root_message_id", "source_fp", "route_epoch", "correlation",
               "history_sha256", "through_rev", "delivery_id", "part_id", "attachment_id",
               "sha256", "bytes", "name", "remote_id", "manifest_sha256",
               "payload_sha256", "expected_state", "evidence"} | set(scope_fields("slack"))
    if req.keys() - allowed:
        return "unknown_field"
    if type(req.get("version")) is not int or req["version"] != 1 \
            or req.get("cmd") != "ops.attachment_identity_confirm":
        return "bad_version"
    if req.get("transport") != "slack":
        return "bad_transport"
    if not valid_uuid(req.get("command_id")) or not valid_uuid(req.get("delivery_id")):
        return "bad_command_identity"
    if req.get("human_confirmed") is not True:
        return "human_confirmation_required"
    for key, limit in (("actor", 120), ("reason", 2000), ("part_id", 64),
                       ("name", 200), ("remote_id", 64)):
        if not valid_text(req.get(key), limit):
            return "bad_" + key
    if type(req.get("card_id")) is not int or req["card_id"] <= 0:
        return "bad_card_id"
    if type(req.get("root_message_id")) is not int or req["root_message_id"] <= 0:
        return "bad_root_message_id"
    if type(req.get("route_epoch")) is not int or req["route_epoch"] < 1:
        return "bad_route_epoch"
    if type(req.get("through_rev")) is not int or req["through_rev"] < 1:
        return "bad_through_rev"
    for key in ("card_key", "thread_id", "message_id", "correlation"):
        if not valid_text(req.get(key), 128):
            return "bad_" + key
    if type(req.get("attachment_id")) is not int or req["attachment_id"] <= 0:
        return "bad_attachment_id"
    if type(req.get("bytes")) is not int or req["bytes"] < 0:
        return "bad_bytes"
    for key in scope_fields("slack"):
        if not valid_text(req.get(key), 200):
            return "bad_" + key
    if any(not valid_hash(req.get(key)) for key in ("sha256", "manifest_sha256", "payload_sha256",
                                                    "history_sha256", "source_fp")):
        return "bad_hash"
    if req.get("expected_state") != "held":
        return "bad_expected_state"
    evidence = req.get("evidence")
    if not isinstance(evidence, dict) or evidence.keys() - {"method", "ref", "worker_stopped"}:
        return "bad_evidence"
    if not valid_text(evidence.get("method"), 200) or not valid_text(evidence.get("ref"), 500):
        return "bad_evidence"
    if evidence.get("worker_stopped") is not True:
        return "worker_not_proven_stopped"

    return None


def _seal_confirmed_update(db, delivery_id, card) -> dict | None:
    """Re-seal one unpublished held update whose held attachment parts are all
    attested by applied ops.attachment_identity_confirm receipts.

    The held spec never reached the wire, so its manifest may be restricted to
    edit_only reuse of the attested remote identities — never a fresh upload of
    files an approval already bound. Any unattested held part keeps the render
    held; stored rows and attempts are otherwise untouched."""
    import notify_parts
    from mcs_util import loads_dict
    current = render_row(db, delivery_id)
    if (current is None or current["state"] != "held" or current["parts_state"] != "held"
            or current["spec_published"] != 0 or current["first_published_at"] is not None
            or current["op"] != "update" or current["card_id"] != card["card_id"]
            or not scope_matches(current, stored_scope(card))
            or db.execute("SELECT 1 FROM notification_delivery_attempts WHERE delivery_id=?",
                          (delivery_id,)).fetchone()):
        return None
    saved = {row["part_id"]: row for row in db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?", (delivery_id,))}
    if any(row["state"] != "held" or row["attempt_id"] is not None
           or (row["kind"] not in ("card", "thread")
               and row["error_code"] != "attachment_upload_identity_unverified")
           for row in saved.values()):
        return None
    spec = loads_dict(current["spec_json"])
    manifest = spec.get("parts", {}).get("manifest") if isinstance(spec, dict) else None
    if (spec is None or payload_hash(spec) != current["payload_hash"] or not isinstance(manifest, list)
            or len(manifest) != len(saved) or {p.get("part_id") for p in manifest} != set(saved)
            or spec.get("delivery_id") != delivery_id or spec.get("render_rev") != current["render_rev"]):
        return None
    groups = [part for part in manifest if part.get("kind") == "file_group"]
    grouped = (spec.get("parts", {}).get("slack") or {}).get("file_group") is True
    if bool(groups) != grouped or len(groups) > 1:
        return None
    if grouped:
        from adapters.common.spec import file_group_sha256, file_group_sha256_matches
        if not file_group_sha256_matches(spec, groups[0].get("sha256")):
            return None
    confirmed = {(att_id, sha, size): (remote, name)
                 for _did, _pid, _rev, att_id, sha, size, name, remote in
                 notify_parts._confirmed_upload_identities(db, card)}
    used, renamed = set(), []
    for part in manifest:
        row = saved.get(part.get("part_id"))
        if (row is None or any(row[key] != part.get(field) for key, field in (
                ("kind", "kind"), ("idx", "index"), ("payload_sha256", "sha256"),
                ("bytes", "bytes"), ("attachment_id", "attachment_id")))):
            return None
        if part.get("kind") != "attachment_part":
            continue
        attested = confirmed.get((part.get("attachment_id"), part.get("sha256"), part.get("bytes")))
        remote, name = attested if attested is not None else (None, None)
        if (remote is None or remote in used or part.get("unavailable") is True
                or part.get("broadcast") is True):
            return None
        part["prior_remote_id"], part["edit_only"], part["name"] = remote, True, name
        used.add(remote)
        renamed.append((name, row["part_id"]))
    if grouped:
        from adapters.slack.cards import validate
        groups[0]["sha256"] = file_group_sha256(spec)
        try:
            validate(spec)
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return None
    for name, part_id in renamed:
        db.execute("UPDATE notification_render_parts SET name=? WHERE delivery_id=? AND part_id=?",
                   (name, delivery_id, part_id))
    if grouped:
        db.execute("UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=? AND part_id=?",
                   (groups[0]["sha256"], delivery_id, groups[0]["part_id"]))
    db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
               (canonical(spec).decode(), payload_hash(spec), delivery_id))
    db.execute("UPDATE notification_renders SET state='queued',parts_state='pending' WHERE delivery_id=?",
               (delivery_id,))
    db.execute("UPDATE notification_render_parts SET state='pending',error_code=NULL WHERE delivery_id=?",
               (delivery_id,))
    db.execute("UPDATE notification_cards SET delivery_state='delivered' WHERE card_id=?",
               (card["card_id"],))
    return spec


def apply_attachment_identity_confirm(ledger, req, cfg=None, now=None) -> dict:
    """Record an attested collected upload identity; never settles or sends."""
    db = cards.connection(ledger)
    now = time.time() if now is None else now
    if cfg is None:
        import mcs_util
        cfg = mcs_util.load_config(mcs_util.CONF_PATH)
    digest, specs = payload_hash(req), []
    with db:
        db.execute("BEGIN IMMEDIATE")
        found, old = replay_receipt(db, req.get("command_id"), digest)
        if found:
            return old
        error = validate_attachment_identity_confirm(req)
        receipt = {"kind": "ops.attachment_identity_confirm", "command_id": req.get("command_id"),
                   "actor": req.get("actor"), "reason": req.get("reason"),
                   "delivery_id": req.get("delivery_id"), "part_id": req.get("part_id"),
                   "processed_at": now}
        render = card = None
        if error is None:
            plan = attachment_identity_confirm_plan(db, req["delivery_id"], req["part_id"])
            error = plan.get("error")
            if not error and any(req.get(key) != value for key, value in plan.items()):
                error = "stale_part_plan"
            if not error:
                render = render_row(db, req["delivery_id"])
                card = card_row(db, render["card_id"])
                # An approval is rejected — never silently queued — while an
                # attempt owns the card, parts are in flight, a restore freezes
                # writes, or the journal cannot be read.
                import notify_render
                if (cards.unsettled_attempt(db, card["card_id"])
                        or _preflight_unsettled_parts(db, card["card_id"])
                        or cards._restore_hold_active(db, card_id=card["card_id"])):
                    error = "confirm_unsafe"
                elif notify_render.source_fingerprint(db, card) != req["source_fp"]:
                    error = "stale_source"
                else:
                    try:
                        attempts, incomplete = notify_journal.scan_journals(
                            notify_spool.notify_dirs(notify_spool.data_root(ledger)))
                    except OSError:
                        error = "journal_unverifiable"
                    else:
                        if (incomplete or any(
                                row.get("delivery_id") == req["delivery_id"]
                                and (record["tainted"]
                                     or row.get("result") in ("delivered", "unknown"))
                                for record in attempts.values()
                                for row in record["rows"])):
                            error = "journal_contradicts_proof"
        if error is None:
            receipt.update(outcome="applied", scope=stored_scope(render),
                           projects=card_projects(db, render, card),
                           card_id=req["card_id"], card_key=req["card_key"],
                           thread_id=req["thread_id"], message_id=req["message_id"],
                           root_message_id=req["root_message_id"], source_fp=req["source_fp"],
                           route_epoch=req["route_epoch"], correlation=req["correlation"],
                           history_sha256=req["history_sha256"], through_rev=req["through_rev"],
                           attachment_id=req["attachment_id"],
                           sha256=req["sha256"], bytes=req["bytes"], name=req["name"],
                           remote_id=req["remote_id"], manifest_sha256=req["manifest_sha256"],
                           payload_sha256=req["payload_sha256"], evidence=req["evidence"],
                           replan="withheld")
        else:
            receipt.update(outcome="rejected", error=error)
        db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                   (req["command_id"], digest, None, None,
                    receipt["outcome"], canonical(receipt).decode(), now))
        if error is None:
            sealed = _seal_confirmed_update(db, req["delivery_id"], card)
            if sealed is not None:
                specs.append(sealed)
                db.execute("UPDATE command_receipts SET receipt_json=? WHERE command_id=?",
                           (canonical({**receipt, "replan": "sealed"}).decode(), req["command_id"]))
                receipt["replan"] = "sealed"
        cards.mark_snapshot_dirty(db)
    if specs:
        notify_spool.publish_specs(db, notify_spool.notify_dirs(notify_spool.data_root(ledger)), specs, now)
    return receipt
