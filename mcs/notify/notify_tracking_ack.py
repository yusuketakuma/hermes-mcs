"""Close exact historical length-rejection incidents without settling or replaying delivery."""
from __future__ import annotations

import time

import notify_scope
from mcs_requests import canonical, payload_hash, replay_receipt, valid_uuid
from mcs_util import strict_json, valid_hash, valid_text
from stats_math import finite_number

COMMAND = "ops.card_part_tracking_ack"
MAX_TARGETS = 20
MAX_RECEIPTS = 4096
_TARGET_KEYS = {"delivery_id", "part_id", "successor_id", "proof"}


def _valid_target(value):
    return (type(value) is dict and set(value) == _TARGET_KEYS
            and valid_uuid(value["delivery_id"]) and valid_uuid(value["successor_id"])
            and valid_text(value["part_id"], 64) and valid_hash(value["proof"]))


def validate(req):
    """Validate the existing human-approved command envelope and bounded exact targets."""
    if (type(req) is not dict or set(req) != {
            "version", "cmd", "command_id", "actor", "reason", "human_confirmed", "targets"}
            or type(req.get("version")) is not int or req["version"] != 1
            or req.get("cmd") != COMMAND or not valid_uuid(req.get("command_id"))):
        return "tracking_ack_request_invalid"
    if req.get("human_confirmed") is not True:
        return "human_confirmation_required"
    if not valid_text(req.get("actor"), 120) or not valid_text(req.get("reason"), 2000):
        return "tracking_ack_confirmation_invalid"
    targets = req.get("targets")
    if (type(targets) is not list or not 1 <= len(targets) <= MAX_TARGETS
            or not all(_valid_target(item) for item in targets)
            or len({(item["delivery_id"], item["part_id"]) for item in targets}) != len(targets)):
        return "tracking_ack_targets_invalid"
    return None


def target(db, delivery_id, part_id, *, cfg, successor_id=None):
    """Prove one old not-sent part and a concrete same-scope delivered successor."""
    if (not valid_uuid(delivery_id) or not valid_text(part_id, 64)
            or successor_id is not None and not valid_uuid(successor_id)):
        return {"error": "tracking_ack_targets_invalid"}
    render = notify_scope.render_row(db, delivery_id)
    sealed = notify_scope._body_render_identity(render)
    if sealed is None or render["op"] not in ("create", "update"):
        return {"error": "tracking_ack_delivery_unverified"}
    card = notify_scope.card_row(db, render["card_id"])
    scope = notify_scope.delivery_scope(cfg)
    if (card is None or card["revoked_at"] is not None
            or card["delivery_state"] in ("revoked", "message_deleted")
            or not notify_scope.scope_matches(card, scope)
            or not notify_scope.scope_matches(render, scope)):
        return {"error": "tracking_ack_scope_changed"}
    if db.execute("SELECT 1 FROM notification_restore_holds WHERE released_at IS NULL "
                  "AND (card_id=? OR delivery_id=?) LIMIT 1",
                  (card["card_id"], delivery_id)).fetchone():
        return {"error": "tracking_ack_restore_held"}
    part = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                      (delivery_id, part_id)).fetchone()
    if (part is None or part["kind"] != "body_part" or part["state"] != "not_sent"
            or part["error_code"] not in notify_scope._LENGTH_ERRORS
            or part["remote_id"] is not None or part["remote_file_id"] is not None
            or not valid_hash(part["payload_sha256"]) or not valid_text(part["attempt_id"], 128)):
        return {"error": "tracking_ack_part_ineligible"}
    attempt = db.execute("SELECT * FROM notification_delivery_attempts WHERE attempt_id=? "
                         "AND delivery_id=?", (part["attempt_id"], delivery_id)).fetchone()
    if (attempt is None or attempt["state"] not in ("delivered", "not_sent")
            or not finite_number(attempt["finished_at"])
            or db.execute("SELECT 1 FROM notification_delivery_attempts WHERE delivery_id=? "
                          "AND state IN ('granted','unknown') LIMIT 1", (delivery_id,)).fetchone()):
        return {"error": "tracking_ack_attempt_unverified"}
    later = db.execute(
        "SELECT * FROM notification_renders WHERE card_id=? AND render_rev>? "
        "AND state='delivered' AND transport='slack' "
        + ("AND delivery_id=? " if successor_id else "")
        + "ORDER BY render_rev LIMIT 33",
        (card["card_id"], render["render_rev"], successor_id) if successor_id
        else (card["card_id"], render["render_rev"])).fetchall()
    successor, root = None, None
    # Bound the first-plan search; later rechecks pin its concrete delivery ID.
    for candidate in later[:32]:
        identity = notify_scope._body_render_identity(candidate)
        if identity is None or not valid_uuid(candidate["delivery_id"]):
            continue
        if not notify_scope.scope_matches(candidate, scope):
            continue
        witness = db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? "
                             "AND kind='card' AND state='delivered' AND remote_id=?",
                             (candidate["delivery_id"], card["message_id"])).fetchall()
        if len(witness) == 1 and valid_text(witness[0]["remote_id"], 200):
            successor, root = identity, dict(witness[0])
            break
    if successor is None:
        return {"error": "tracking_ack_successor_unverified"}
    stable_card = {key: card[key] for key in (
        "card_id", "card_key", "kind", "project_id", "root_message_id", "anchor_key",
        "transport", "profile", "application_id", "team_id", "guild_id", "channel_id",
        "message_id", "thread_id", "thread_state")}
    proof = payload_hash([COMMAND, 1, dict(part), dict(attempt), sealed,
                          stable_card, successor, root, notify_scope.route_epoch(cfg)])
    return {"delivery_id": delivery_id, "part_id": part_id,
            "successor_id": successor["delivery_id"], "proof": proof}


def build_plan(db, delivery_id, part_id, *, cfg):
    """Return a read-only single-target command template without granting approval."""
    item = target(db, delivery_id, part_id, cfg=cfg)
    return item if "error" in item else {"version": 1, "cmd": COMMAND, "targets": [item]}


def _receipt_valid(receipt, digest, *, applied_only=True):
    keys = {"kind", "version", "command_id", "payload_hash", "outcome", "actor", "reason",
            "human_confirmed", "targets", "processed_at", "delivery_outcome", "residual", "followup"}
    if (type(receipt) is not dict or set(receipt) - (keys | {"error"}) or not keys <= set(receipt)
            or receipt["kind"] != COMMAND or receipt["payload_hash"] != digest
            or receipt["outcome"] not in (("applied",) if applied_only else ("applied", "rejected"))
            or not finite_number(receipt["processed_at"]) or receipt["followup"] != "none"):
        return False
    req = {key: receipt[key] for key in ("version", "command_id", "actor", "reason", "human_confirmed", "targets")}
    req["cmd"] = COMMAND
    if receipt["outcome"] == "applied":
        return (validate(req) is None and payload_hash(req) == digest
                and receipt["delivery_outcome"] == "not_sent" and receipt["residual"] == "unrecovered")
    return not applied_only


def receipt_targets(db):
    """Index bounded operator receipts once per borrowed health snapshot."""
    rows = db.execute("SELECT command_id,payload_hash,receipt_json FROM command_receipts "
                      "WHERE outcome='applied' AND CASE WHEN length(receipt_json)<=16384 "
                      "AND json_valid(receipt_json) THEN json_extract(receipt_json,'$.kind') END=? "
                      "ORDER BY processed_at DESC LIMIT ?", (COMMAND, MAX_RECEIPTS + 1)).fetchall()
    if len(rows) > MAX_RECEIPTS:
        raise ValueError("tracking_ack_observation_incomplete")
    index = {}
    for row in rows:
        receipt = strict_json(row["receipt_json"])
        if (not _receipt_valid(receipt, row["payload_hash"])
                or receipt["command_id"] != row["command_id"]):
            raise ValueError("tracking_ack_receipt_unverified")
        for item in receipt["targets"]:
            key = (item["delivery_id"], item["part_id"])
            index.setdefault(key, []).append(item)
    return index


def acknowledged(db, row, index, *, cfg):
    """Reprove each stored target; an old applied receipt never substitutes for current evidence."""
    return any(target(db, item["delivery_id"], item["part_id"], cfg=cfg,
                      successor_id=item["successor_id"]) == item
               for item in index.get((row["delivery_id"], row["part_id"]), ()))


def apply(ledger, req, cfg=None, now=None):
    """Append only a tracking receipt under the existing command transaction and fences."""
    import notify_cards
    import notify_spool
    from mcs_util import load_config
    if type(req) is not dict or not valid_uuid(req.get("command_id")):
        raise ValueError("bad_command_id")
    cfg = load_config() if cfg is None else cfg
    db = notify_cards.connection(ledger)
    now = time.time() if now is None else now
    digest = payload_hash(req)
    with db:
        db.execute("BEGIN IMMEDIATE")
        found, prior = replay_receipt(db, req["command_id"], digest)
        if found:
            if prior == {"outcome": "rejected", "error": "command_id_conflict"}:
                return prior
            if not _receipt_valid(prior, digest, applied_only=False):
                raise ValueError("tracking_ack_receipt_unverified")
            return prior
        error = validate(req)
        if error is None and notify_spool.restore_pending(notify_spool.data_root(ledger)) is not None:
            error = "tracking_ack_restore_held"
        if error is None:
            for item in req["targets"]:
                current = target(db, item["delivery_id"], item["part_id"], cfg=cfg,
                                 successor_id=item["successor_id"])
                if current != item:
                    error = "tracking_ack_stale_plan"
                    break
        receipt = {"kind": COMMAND, "version": 1, "command_id": req["command_id"],
                   "payload_hash": digest, "outcome": "rejected" if error else "applied",
                   "actor": req.get("actor"), "reason": req.get("reason"),
                   "human_confirmed": req.get("human_confirmed"), "targets": req.get("targets", []),
                   "processed_at": now, "delivery_outcome": "not_sent" if not error else "unchanged",
                   "residual": "unrecovered", "followup": "none"}
        if error:
            receipt["error"] = error
        db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                   (req["command_id"], digest, None, None, receipt["outcome"], canonical(receipt).decode(), now))
        notify_cards.mark_snapshot_dirty(db)
    return receipt
