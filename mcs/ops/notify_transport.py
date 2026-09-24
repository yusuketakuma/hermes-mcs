"""Interactive notification delivery grants, receipts, and operator recovery.

The runner settles factual transport outcomes atomically with card bindings,
coverage, and intent completion. Unknown delivery never becomes retryable
without a factual receipt or human-confirmed operator resolution.
"""
from __future__ import annotations

import json
import os
import time

from mcs_requests import canonical, payload_hash, positive, valid_uuid
import notify_cards as cards
from notify_render import _latest_signals

# receipt error_codes proving the bound Discord message no longer
# exists — an update/revoke against it must never retry as-is
MESSAGE_GONE = frozenset(
    {"http_404", "http_410", "unknown_message", "message_deleted",
     "gone"})

# ---------- settle (receipt / resolve shared finalization) ----------

def _not_sent_state(db, card, render, error_code):
    """(delivery_state, unbind_message_id) after a factual not_sent.

    - update/revoke reporting the bound message provably gone unbinds it:
      revoke reaches its goal (stays revoked), update becomes
      message_deleted so the next render re-creates rather than PATCHing
      a ghost forever.
    - a revoked card keeps its intent until the resend budget is spent.
    - a failed create stays pending (nothing was ever posted); a failed
      update on a live card keeps 'delivered' (old content still stands).
    - MAX_RESEND real failures suspend auto-retry as update_failed."""
    cur = card["delivery_state"]
    gone = render["op"] in ("update", "revoke") \
        and isinstance(error_code, str) and error_code in MESSAGE_GONE
    if cur == "revoked":
        state = "revoked"   # goal achieved iff gone, else retry pending
        if gone:
            return state, True
        # Failure never revives a revoked card. Issuance enforces the
        # retry budget while preserving the permanent revocation decision.
        return state, False
    if gone:
        return "message_deleted", True
    if cards._resend_exhausted(db, card["card_id"]):
        return "update_failed", False
    if render["op"] == "create":
        return "pending", False
    return cur, False

def _settle_attempt(db, attempt, render, result, now,
                    message_id=None, error_code=None) -> dict:
    """One attempt's factual result commits atomically with every
    dependent: render state, card message binding, coverage rows, and
    each owning intent's completion. A cancelled render's late success
    records the fact (message binding, applied rev) without reviving the
    cancelled render or rolling back a newer one."""
    aid = attempt["attempt_id"]
    card = (cards._card_row(db, render["card_id"])
            if render["card_id"] is not None else None)
    db.execute(
        "UPDATE notification_delivery_attempts SET state=?,"
        "message_id=?,error_code=?,finished_at=? WHERE attempt_id=?",
        (result, message_id, error_code, now, aid))
    if render["state"] != "cancelled":
        db.execute(
            "UPDATE notification_renders SET state=?,updated_at=? "
            "WHERE delivery_id=?",
            (result, now, render["delivery_id"]))
    if result == "delivered" and card is not None:
        applied = card["applied_render_rev"]
        conflict = False
        if render["op"] == "create":
            if card["message_id"] is None:
                db.execute(
                    """UPDATE notification_cards SET message_id=?,
                         profile=?,application_id=?,guild_id=?,
                         channel_id=?,transport=?,team_id=?,updated_at=? WHERE card_id=?""",
                    (str(message_id), render["profile"],
                     render["application_id"], render["guild_id"],
                     render["channel_id"], render["transport"], render["team_id"],
                     now, card["card_id"]))
                card["message_id"] = str(message_id)
            elif card["message_id"] != str(message_id):
                conflict = True
        elif card["message_id"] is not None \
                and str(message_id) != card["message_id"]:
            conflict = True
        if conflict:
            db.execute(
                "UPDATE notification_cards SET delivery_state="
                "'delivery_unknown',last_delivery_error=?,updated_at=? "
                "WHERE card_id=?",
                ("message_id_conflict", now, card["card_id"]))
        else:
            new_applied = max(applied, render["render_rev"])
            state = card["delivery_state"]
            if state not in ("revoked",):
                state = "delivered"
            db.execute(
                """UPDATE notification_cards SET applied_render_rev=?,
                     delivery_state=?,last_delivery_error=NULL,updated_at=?
                   WHERE card_id=?""",
                (new_applied, state, now, card["card_id"]))
        cov = "delivered"
    elif result == "not_sent":
        if card is not None:
            state, unbind_mid = _not_sent_state(db, card, render,
                                              error_code)
            db.execute(
                """UPDATE notification_cards SET delivery_state=?,
                     message_id=?, thread_id=?, thread_state=?,
                     last_delivery_error=?, updated_at=?
                   WHERE card_id=?""",
                (state,
                 None if unbind_mid else card["message_id"],
                 # the thread lived under the bound message — once the
                 # message is proven gone its thread binding is stale
                 # and must not carry into the re-created card
                 None if unbind_mid else card["thread_id"],
                 "none" if unbind_mid else card["thread_state"],
                 error_code, now, card["card_id"]))
        cov = "unbind"
    else:  # unknown — never auto-resend; waits for reconcile/resolve
        if card is not None and card["delivery_state"] not in (
                "revoked", "delivered"):
            db.execute(
                "UPDATE notification_cards SET delivery_state="
                "'delivery_unknown',last_delivery_error=?,updated_at=? "
                "WHERE card_id=?",
                (error_code or "unknown", now, card["card_id"]))
        cov = "keep"
    if cov == "delivered":
        db.execute(
            "UPDATE notification_intent_cards SET state='delivered' "
            "WHERE delivery_id=?", (render["delivery_id"],))
    elif cov == "unbind":
        db.execute(
            "UPDATE notification_intent_cards SET delivery_id=NULL,"
            "required_render_rev=0 WHERE delivery_id=? AND state='pending'",
            (render["delivery_id"],))
    for r in db.execute(
            "SELECT DISTINCT event_id FROM notification_intent_cards "
            "WHERE card_id=?", (render["card_id"],)).fetchall() \
            if render["card_id"] is not None else []:
        cards._complete_intent(db, r["event_id"], now)
    cards.mark_snapshot_dirty(db)
    return {"settled": result}


def apply_transport_begin(ledger, req, cfg, now=None) -> dict:
    """transport_begin: the runner re-verifies send eligibility and
    commits the attempt BEFORE any grant. A file claim alone is never
    send authorization; a denial is itself a durable not_sent attempt."""
    db = cards._db(ledger)
    now = time.time() if now is None else now
    cid = req["command_id"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT * FROM notification_delivery_attempts "
            "WHERE begin_command_id=?", (cid,)).fetchone()
        if old is not None:
            render = db.execute(
                "SELECT * FROM notification_renders WHERE delivery_id=?",
                (old["delivery_id"],)).fetchone()
            if not cards._scope_match(render, req):
                return {"granted": False, "error": "denied_scope_mismatch",
                        "command_id": cid}
            if old["attempt_id"] != req["attempt_id"] \
                    or old["delivery_id"] != req["delivery_id"] \
                    or old["worker_id"] != req["worker_id"] \
                    or any(render[k] != req.get(k) for k in
                           ("render_rev", "payload_hash", "route_epoch")):
                return {"granted": False, "error": "command_id_conflict",
                        "command_id": cid}
            return _begin_result(db, old)
        clash = db.execute(
            "SELECT 1 FROM notification_delivery_attempts "
            "WHERE attempt_id=?", (req["attempt_id"],)).fetchone()
        if clash is not None:
            return {"granted": False, "error": "attempt_id_conflict",
                    "command_id": cid}
        reason = _begin_check(db, req, cfg)
        if reason == "unknown_delivery":
            # nothing to reference — a denied attempt row needs the
            # render FK; the result file carries the audit instead.
            # The spec file is definitively dead — remove it so a
            # worker stops re-claiming bytes that can never grant.
            _unlink_spec(ledger, req["delivery_id"], req.get("transport", "discord"))
            return {"granted": False, "error": "denied_unknown_delivery",
                    "command_id": cid, "attempt_id": req["attempt_id"],
                    "delivery_id": req["delivery_id"]}
        state = "granted" if reason is None else "not_sent"
        db.execute(
            """INSERT INTO notification_delivery_attempts(
                 attempt_id,delivery_id,begin_command_id,state,
                 error_code,worker_id,created_at,finished_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (req["attempt_id"], req["delivery_id"], cid, state,
             None if reason is None else f"denied_{reason}",
             req["worker_id"], now, None if reason is None else now))
        if reason is None:
            db.execute(
                "UPDATE notification_renders SET state='sending',"
                "updated_at=? WHERE delivery_id=? AND state='queued'",
                (now, req["delivery_id"]))
        cards.mark_snapshot_dirty(db)
        result = _begin_result(db, db.execute(
            "SELECT * FROM notification_delivery_attempts "
            "WHERE begin_command_id=?", (cid,)).fetchone())
    if reason is not None and _denial_is_final(
            db, req["delivery_id"], reason):
        render = db.execute(
            "SELECT transport FROM notification_renders WHERE delivery_id=?",
            (req["delivery_id"],)).fetchone()
        _unlink_spec(ledger, req["delivery_id"], render["transport"])
    return result


_FINAL_DENIALS = frozenset({
    "hash_mismatch", "rev_mismatch", "epoch_mismatch",
    "render_cancelled", "card_revoked"})


def _denial_is_final(db, delivery_id, reason) -> bool:
    """Whether a denied begin means the spec bytes can never grant —
    stale/forged content, a cancelled render, a revoked card, or a
    render that already reached a terminal state. Transient answers
    (in_flight, interactive_off, sending) and scope_mismatch — the spec
    may simply be addressed to another worker — keep the file."""
    if reason in _FINAL_DENIALS:
        return True
    if reason == "not_queued":
        r = db.execute(
            "SELECT state FROM notification_renders WHERE delivery_id=?",
            (delivery_id,)).fetchone()
        return r is not None and r["state"] in (
            "delivered", "not_sent", "cancelled")
    return False


def _unlink_spec(ledger, delivery_id, transport) -> None:
    """Remove a definitively dead spec file. The runner owns
    discord_render; a missing file just means 'nothing to claim' —
    the watchdog republishes a live render's spec_json if needed."""
    try:
        os.unlink(os.path.join(
            cards.notify_dirs(cards.data_root(ledger))[transport + "_render"],
            str(delivery_id) + ".json"))
    except OSError:
        pass


def _begin_check(db, req, cfg) -> str | None:
    if not cards.interactive_enabled(cfg):
        return "interactive_off"
    render = db.execute(
        "SELECT * FROM notification_renders WHERE delivery_id=?",
        (req["delivery_id"],)).fetchone()
    if render is None:
        return "unknown_delivery"
    if render["transport"] != cards.active_transport(cfg) \
            or render["transport"] != req.get("transport", "discord"):
        return "transport_mismatch"
    if render["payload_hash"] != req["payload_hash"]:
        return "hash_mismatch"
    if render["render_rev"] != req["render_rev"]:
        return "rev_mismatch"
    if render["route_epoch"] != req["route_epoch"] \
            or render["route_epoch"] != cards.route_epoch(cfg):
        return "epoch_mismatch"
    if not cards._scope_match(render, req):
        return "scope_mismatch"
    if render["transport"] == "slack" \
            and not cards._scope_match(render, cards.delivery_scope(cfg)):
        return "scope_mismatch"
    if render["state"] == "cancelled":
        return "render_cancelled"
    if render["state"] != "queued":
        return "not_queued"
    card = cards._card_row(db, render["card_id"]) \
        if render["card_id"] is not None else None
    if card is not None:
        signals = cfg.get("signals")
        if card["kind"] in ("signal", "digest") and not (
                isinstance(signals, dict)
                and signals.get("notify") is True):
            return "signal_notify_off"
        if card["delivery_state"] == "revoked" \
                and render["op"] != "revoke":
            # a revoke render against a revoked card is exactly the
            # delete Discord is owed — every other op is refused
            return "card_revoked"
        if cards._unsettled_attempt(db, card["card_id"]):
            return "in_flight"
    return None


def _begin_result(db, attempt) -> dict:
    """The durable grant/denial record a worker consumes once — it
    echoes the render identity fields so the worker can verify the
    grant still matches the spec file it claimed."""
    render = db.execute(
        "SELECT * "
        "FROM notification_renders WHERE delivery_id=?",
        (attempt["delivery_id"],)).fetchone()
    granted = attempt["state"] == "granted"
    out = {"granted": granted, "command_id": attempt["begin_command_id"],
           "attempt_id": attempt["attempt_id"],
           "delivery_id": attempt["delivery_id"],
           "attempt_state": attempt["state"],
           "worker_id": attempt["worker_id"]}
    if render is not None:
        out.update(render_rev=render["render_rev"],
                   payload_hash=render["payload_hash"],
                   route_epoch=render["route_epoch"],
                   correlation=render["correlation"], op=render["op"])
        if render["transport"] == "slack":
            out.update(cards.stored_scope(render))
    if not granted:
        out["error"] = attempt["error_code"] or attempt["state"]
    return out


def apply_transport_receipt(ledger, req, cfg, now=None) -> dict:
    """transport_receipt: settle an attempt from the worker's factual
    report. Echo fields must match the stored grant/render — an
    arbitrary event_id or payload never marks anything accepted."""
    db = cards._db(ledger)
    now = time.time() if now is None else now
    specs = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        attempt = db.execute(
            "SELECT * FROM notification_delivery_attempts "
            "WHERE attempt_id=?", (req["attempt_id"],)).fetchone()
        render = None
        if attempt is not None:
            render = db.execute(
                "SELECT * FROM notification_renders WHERE delivery_id=?",
                (attempt["delivery_id"],)).fetchone()
        error = _receipt_check(attempt, render, req)
        if error:
            receipt = {"applied": False, "error": error,
                       "attempt_id": req["attempt_id"],
                       "delivery_id": req["delivery_id"]}
        else:
            prior = attempt["state"]
            result = req["result"]
            if prior in ("delivered", "not_sent"):
                # terminal already: same fact is idempotent, a different
                # fact is a conflict — never silently overwrite (§4)
                same = prior == result and (
                    result != "delivered"
                    or str(attempt["message_id"]) == str(req["message_id"]))
                receipt = {"applied": same,
                           "error": None if same else "attempt_conflict",
                           "conflict": not same,
                           "attempt_id": attempt["attempt_id"],
                           "delivery_id": attempt["delivery_id"],
                           "attempt_state": prior}
            else:
                _settle_attempt(db, attempt, render, result, now,
                                message_id=req.get("message_id"),
                                error_code=req.get("error_code"))
                receipt = {"applied": True, "attempt_id": attempt["attempt_id"],
                           "delivery_id": attempt["delivery_id"],
                           "attempt_state": result}
                if render["card_id"] is not None:
                    cards._issue_render(db, render["card_id"], cfg, now, specs)
        cards.mark_snapshot_dirty(db)
    if specs:
        root = cards.data_root(ledger)
        cards.ensure_dirs(root)
        cards._publish_specs(db, cards.notify_dirs(root), specs, now)
    return receipt


def _receipt_check(attempt, render, req) -> str | None:
    if attempt is None or render is None:
        return "unknown_attempt"
    if attempt["delivery_id"] != req["delivery_id"]:
        return "delivery_mismatch"
    for k, want in (("render_rev", render["render_rev"]),
                    ("payload_hash", render["payload_hash"]),
                    ("route_epoch", render["route_epoch"]),
                    ("correlation", render["correlation"])):
        if req.get(k) != want:
            return f"{k}_mismatch"
    if not cards._scope_match(render, req):
        return "scope_mismatch"
    if req["result"] == "delivered" \
            and not isinstance(req.get("message_id"), str):
        return "message_id_required"
    if req["result"] == "not_sent" \
            and not req.get("error_code"):
        return "error_code_required"
    return None


def apply_thread_receipt(ledger, req, cfg, now=None) -> dict:
    """thread_receipt: thread creation is independent of the primary
    card delivery — failure here never resends the card."""
    db = cards._db(ledger)
    now = time.time() if now is None else now
    with db:
        db.execute("BEGIN IMMEDIATE")
        render = db.execute(
            "SELECT * FROM notification_renders WHERE delivery_id=?",
            (req["delivery_id"],)).fetchone()
        if render is None or render["card_id"] is None:
            return {"applied": False, "error": "unknown_delivery"}
        if render["transport"] != req.get("transport", "discord") \
                or (render["transport"] == "slack"
                    and not cards._scope_match(render, req)):
            return {"applied": False, "error": "scope_mismatch"}
        card = cards._card_row(db, render["card_id"])
        if card["message_id"] is not None \
                and str(req["message_id"]) != card["message_id"]:
            return {"applied": False, "error": "message_id_mismatch"}
        tid, err = req.get("thread_id"), req.get("error_code")
        if isinstance(tid, str) and tid:
            if card["thread_state"] == "deleted":
                state, thread_id = "deleted", card["thread_id"]
            else:
                state = "created"
                thread_id = card["thread_id"] or tid   # bind once
        elif err == "thread_deleted":
            state, thread_id = "deleted", card["thread_id"]
        else:
            state, thread_id = "failed", card["thread_id"]
        db.execute(
            "UPDATE notification_cards SET thread_id=?,thread_state=?,"
            "updated_at=? WHERE card_id=?",
            (thread_id, state, now, card["card_id"]))
        cards.mark_snapshot_dirty(db)
        return {"applied": True, "thread_state": state,
                "thread_id": thread_id}


# ---------- ops.card_resolve (operator recovery, data/cmd only) ----------

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
    slack = req.get("version") == 2
    if slack:
        allowed = (allowed - {"guild_id"}) | {"transport", "team_id"}
    if req.keys() - allowed:
        return "unknown_field"
    if req.get("cmd") != "ops.card_resolve":
        return "unknown_cmd"
    if type(req.get("version")) is not int or req["version"] not in (1, 2):
        return "bad_version"
    if slack and req.get("transport") != "slack":
        return "bad_transport"
    if not valid_uuid(req.get("command_id")):
        return "bad_command_id"
    if req.get("human_confirmed") is not True:
        return "human_confirmation_required"
    from mcs_requests import _text
    if not _text(req.get("actor"), 120):
        return "bad_actor"
    if not _text(req.get("reason"), 2000):
        return "bad_reason"
    if not valid_uuid(req.get("delivery_id")):
        return "bad_delivery_id"
    if not _text(req.get("attempt_id"), 120):
        return "bad_attempt_id"
    if req.get("result") not in _RESOLVE_RESULTS:
        return "bad_result"
    for k in cards.scope_fields("slack" if slack else "discord"):
        if not _text(req.get(k), 200):
            return f"bad_{k}"
    if req["result"] == "mark_delivered" \
            and not _text(req.get("message_id"), 64):
        return "bad_message_id"
    ev = req.get("evidence")
    if not isinstance(ev, dict) or not ev:
        return "bad_evidence"
    if not _text(ev.get("method"), 200):
        return "bad_evidence_method"
    if not _text(ev.get("ref"), 500):
        return "bad_evidence_ref"
    if req["result"] == "mark_not_sent":
        if ev.get("worker_stopped") is not True:
            return "worker_not_proven_stopped"
        if ev.get("proof") not in ("no_journal_started", "api_rejected"):
            return "bad_proof"
    return None


def apply_card_resolve(ledger, req, cfg=None, now=None) -> dict:
    """Operator-only recovery for an unsettled attempt. Verifies the
    stored grant/render/card scope and evidence, then runs the SAME
    shared settlement as a normal receipt — resolve never sends."""
    db = cards._db(ledger)
    now = time.time() if now is None else now
    if cfg is None:
        try:
            from mcs_util import load_config
            cfg = load_config(os.path.join(os.path.expanduser("~/.mcs"),
                                           "config.json"))
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
        if error is None:
            attempt = db.execute(
                "SELECT * FROM notification_delivery_attempts "
                "WHERE attempt_id=?", (req["attempt_id"],)).fetchone()
            render = db.execute(
                "SELECT * FROM notification_renders WHERE delivery_id=?",
                (req["delivery_id"],)).fetchone() if attempt else None
            card = (cards._card_row(db, render["card_id"])
                    if render is not None
                    and render["card_id"] is not None else None)
            error = _resolve_check(attempt, render, card, req)
        if error is None:
            receipt["scope"] = cards.stored_scope(render)
            receipt["projects"] = _card_projects(db, render, card)
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
                _settle_attempt(db, attempt, render, result, now,
                                message_id=req.get("message_id"),
                                error_code=f"resolve:{req['result']}")
                receipt["outcome"] = "applied"
                receipt["attempt_state"] = result
                if card is not None:
                    cards._issue_render(db, card["card_id"], cfg, now, specs)
        else:
            receipt["outcome"] = "rejected"
            receipt["error"] = error
        db.execute(
            "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
            (req["command_id"], digest, None, None,
             receipt["outcome"], canonical(receipt).decode(), now))
        cards.mark_snapshot_dirty(db)
    if specs:
        root = cards.data_root(ledger)
        cards.ensure_dirs(root)
        cards._publish_specs(db, cards.notify_dirs(root), specs, now)
    return receipt


def _resolve_check(attempt, render, card, req) -> str | None:
    if attempt is None:
        return "unknown_attempt"
    if render is None or attempt["delivery_id"] != req["delivery_id"]:
        return "delivery_mismatch"
    if render["transport"] != req.get("transport", "discord"):
        return "scope_mismatch"
    for k in cards.scope_fields(render["transport"]):
        if render[k] != req.get(k):
            return "scope_mismatch"
    if req["result"] == "mark_delivered":
        # create binds a discovered message once; update/revoke must
        # match the message the card already knows
        if card is not None and render["op"] != "create" \
                and card.get("message_id") is not None \
                and str(req["message_id"]) != card["message_id"]:
            return "message_id_mismatch"
    return None


def _card_projects(db, render, card) -> list:
    """Project scope for the audit receipt — derived from the stored
    render/coverage, never from operator input."""
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
                sigs = _latest_signals(db, [k for k in cov
                                          if type(k) is str])
                projects.update(
                    s["content"].get("project_id") for s in sigs.values())
    return sorted(p for p in projects if positive(p))
