"""Own confirmed stamp intent, admission and restore-safe single-attempt execution.

Read and formatting contracts live beside this module. The caller supplies the
writer connection, live configuration, runtime data root and authenticated adapter.
Admission shares the command receipt transaction; network runs only after commit
under the collector's run lock. External attempt witnesses survive DB restoration.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from mcs_contract import MCSError
from mcs_util import canonical, payload_hash, positive, valid_hash, valid_text
from message_observations import decode_metadata, get_message_metadata
import notify_scope
from safe_io import atomic_write, fsync_dir, publish_new_bytes, read_private_bytes
from stamp_contract import REACTION_LABELS, STAMP_EMOJI, normalize_reactions, options

KIND = "mcs_stamp_operation_v1"
INSTANCE_KIND = "mcs_stamp_instance_v1"
MAX_SCOPE_OPERATIONS = 512
MAX_FORM_POSTS = 20
INTENT_MAX_AGE_S = 1200  # One 10-minute collection interval plus bounded collection time.
TERMINAL = frozenset({"confirmed", "rejected"})
_TOKEN = re.compile(r"[0-9a-f]{32}")


class StampRefused(ValueError):
    """A module-owned rejection code safe for the operation receipt."""


def enabled(cfg, actor=None):
    value = options(cfg)
    # On by default once the MCS account and the operator are configured;
    # `"enabled": false` turns it off. Neither identity is ever guessed.
    return (value.get("enabled", True) is True and positive(value.get("account_user_id"))
            and isinstance(value.get("operator"), str) and notify_scope.interactive_enabled(cfg)
            and value["operator"].split(":", 1)[0] == notify_scope.active_transport(cfg)
            and (actor is None or actor == value["operator"]))


def _own(metadata):
    at = metadata.get("reactions_observed_at")
    if (metadata.get("reactions_status") != "observed" or metadata.get("last_error")
            or type(at) not in (int, float) or not math.isfinite(at) or at <= 0):
        return {"type": None, "state": "unknown", "observed_at": None}
    try:
        rows = normalize_reactions(metadata.get("reactions"))
    except ValueError:
        return {"type": None, "state": "unknown", "observed_at": None}
    own = [r["type"] for r in rows if r["self_reacted"]]
    if len(own) > 1 or any(k not in REACTION_LABELS for k in own):
        return {"type": None, "state": "unknown", "observed_at": at}
    return {"type": own[0] if own else None, "state": "observed", "observed_at": at}


def _latest(db, mid):
    row = db.execute("SELECT content FROM artifacts WHERE kind=? AND message_id=? "
                     "ORDER BY artifact_id DESC LIMIT 1", (KIND, mid)).fetchone()
    return json.loads(row[0]) if row else None


def pending_status(db, project_id, root):
    return db.execute(
        "SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
        "WHERE a.kind=? AND m.project_id=? AND (m.message_id=? OR m.parent_id=?) "
        "AND (NOT json_valid(a.content) OR json_extract(a.content,'$.state') "
        "IN ('queued','unknown','started')) LIMIT 1", (KIND, project_id, root, root)).fetchone() is not None


def form(db, card, cfg, actor, now, token) -> dict:
    """Read selectable posts from the exact rendered manifest on a borrowed snapshot."""
    if not enabled(cfg, actor):
        return {"error": "stamp_not_allowed"}
    if card["kind"] != "thread" or card["delivery_state"] == "revoked":
        return {"error": "stamp_target_invalid"}
    patient = db.execute("SELECT is_archived FROM patients WHERE project_id=?", (card["project_id"],)).fetchone()
    tok = db.execute("SELECT * FROM notification_action_tokens WHERE token=?", (token,)).fetchone()
    if (patient is None or patient[0] or tok is None or tok["action"] not in ("stamp", "stamp_press")
            or tok["card_id"] != card["card_id"] or tok["expires_at"] <= now
            or tok["need_source_gen"] != card["source_generation"]):
        return {"error": "stamp_target_changed"}
    scope = notify_scope.delivery_scope(cfg)
    if scope is None or not notify_scope.scope_matches(card, scope):
        return {"error": "scope_mismatch"}
    manifest = db.execute("SELECT shown FROM notification_view_manifests WHERE manifest_id=? "
                          "AND card_id=? AND source_generation=?", (tok["need_manifest_id"],
                          card["card_id"], card["source_generation"])).fetchone()
    try:
        mids = json.loads(manifest[0]) if manifest else []
    except (ValueError, TypeError):
        mids = []
    if not isinstance(mids, list) or any(not positive(m) for m in mids):
        return {"error": "stamp_target_invalid"}
    posts = []
    for mid in mids[:MAX_FORM_POSTS]:
        row = db.execute("SELECT message_id,parent_id,body_state,content_hash,posted_at,sender_name "
                         "FROM messages WHERE message_id=? AND project_id=?", (mid, card["project_id"])).fetchone()
        if (row is None or row["body_state"] != "full" or not valid_hash(row["content_hash"])
                or (mid != card["root_message_id"] and row["parent_id"] != card["root_message_id"])):
            continue
        own = _own(get_message_metadata(db, mid))
        prior = _latest(db, mid)
        if prior and prior.get("state") not in TERMINAL:
            own["state"] = "pending"
        at = row["posted_at"] or ""
        attached = db.execute("SELECT 1 FROM attachments WHERE message_id=? LIMIT 1", (mid,)).fetchone()
        # The sender tells same-minute replies apart; the existing layout is kept
        # (LINE WORKS numbers choices itself, so the label carries no number).
        sender = " ".join(str(row["sender_name"] or "").split())[:12]
        posts.append({"opt": f"p{len(posts) + 1}", "message_id": mid,
                      "source_hash": row["content_hash"], "own": own,
                      "label": f"{at[5:16].replace('T', ' ')} "
                               + ("根元" if mid == card["root_message_id"] else "返信")
                               + (f" {sender}" if sender else "")
                               + ("（添付あり）" if attached else "")})
    if not posts:
        return {"error": "stamp_target_invalid"}
    # types keep MCS's own order and names; emoji is a separate key so older UIs ignore it.
    result = {"posts": posts, "types": [list(kv) for kv in REACTION_LABELS.items()],
              "emoji": dict(STAMP_EMOJI),
              "project_id": card["project_id"], "card_id": card["card_id"], "token": token,
              "mcs_user_id": options(cfg)["account_user_id"]}
    # Other posts' re-observation must not void a confirm: the selected post's
    # own state is matched separately against expected_own at admission.
    result["form_hash"] = payload_hash({**result, "posts": [
        {k: v for k, v in p.items() if k != "own"} for p in posts]})
    return result


def validate_operation(req, base):
    fields = {"card_id", "token", "message_id", "source_hash", "form_hash", "expected_own",
              "reason", "mcs_user_id", "op", "reaction_type", "confirmed_at"}
    if req.keys() - (base | fields):
        return "unknown_field"
    if any(not positive(req.get(k)) for k in ("card_id", "message_id", "mcs_user_id")):
        return "stamp_target_invalid"
    if (not isinstance(req.get("token"), str) or _TOKEN.fullmatch(req["token"]) is None
            or not valid_hash(req.get("source_hash")) or not valid_hash(req.get("form_hash"))):
        return "stamp_target_invalid"
    if not valid_text(req.get("reason"), 200):
        return "reason_required"
    at = req.get("confirmed_at")
    # Absent only on envelopes from before confirmed_at existed; an explicit null is malformed.
    if "confirmed_at" in req and (type(at) not in (int, float) or not math.isfinite(at) or at <= 0):
        return "stamp_target_invalid"
    own = req.get("expected_own")
    if (not isinstance(own, dict) or set(own) != {"type", "observed_at"}
            or (own["type"] is not None and (not isinstance(own["type"], str)
                                             or own["type"] not in REACTION_LABELS))
            or type(own["observed_at"]) not in (int, float)
            or not math.isfinite(own["observed_at"]) or own["observed_at"] <= 0):
        return "stamp_observation_required"
    if req.get("op") == "add":
        return (None if isinstance(req.get("reaction_type"), str)
                and req["reaction_type"] in REACTION_LABELS else "stamp_type_invalid")
    if req.get("op") == "cancel" and "reaction_type" not in req and own["type"] is not None:
        return None
    return "stamp_operation_invalid"


def _private_dir(path):
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    meta = path.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_mode & 0o077 or meta.st_uid != os.getuid():
        raise StampRefused("stamp_witness_invalid")
    # Repair a prior mkdir whose parent fsync failed before allowing any wire call.
    fsync_dir(path.parent)
    return path


def _json(path):
    from mcs_requests import parse_command
    value = parse_command(read_private_bytes(path))
    if not isinstance(value, dict):
        raise StampRefused("stamp_witness_invalid")
    return value


def _namespace(db, root, *, create=False):
    directory = Path(root) / "stamp_operations"
    saved = db.execute("SELECT content FROM artifacts WHERE kind=? ORDER BY artifact_id DESC LIMIT 1",
                       (INSTANCE_KIND,)).fetchone()
    if saved is not None:
        expected = json.loads(saved[0])["namespace"]
        if not isinstance(expected, str) or str(uuid.UUID(expected)) != expected:
            raise StampRefused("stamp_restore_hold")
        if _json(directory / "instance.json") != {"namespace": expected}:
            raise StampRefused("stamp_restore_hold")
        _private_dir(directory)
        return directory, expected
    if not create:
        raise StampRefused("stamp_restore_hold")
    _private_dir(directory)
    marker = directory / "instance.json"
    if not marker.exists():
        publish_new_bytes(marker, canonical({"namespace": str(uuid.uuid4())}))
    value = _json(marker)
    namespace = value.get("namespace")
    if set(value) != {"namespace"} or str(uuid.UUID(namespace)) != namespace:
        raise StampRefused("stamp_witness_invalid")
    fsync_dir(directory)
    # A DB copied with pre-existing operations needs its original namespace.
    if db.execute("SELECT 1 FROM artifacts WHERE kind=? LIMIT 1", (KIND,)).fetchone():
        raise StampRefused("stamp_restore_hold")
    db.execute("INSERT INTO artifacts(kind,content,meta,created_at) VALUES(?,?,'{}',?)",
               (INSTANCE_KIND, canonical(value).decode(), time.time()))
    return directory, namespace


def enqueue_tx(db, req, cfg, root, now):
    """Validate and enqueue on the receipt owner's transaction; never perform HTTP."""
    if not enabled(cfg, req["actor"]) or root is None:
        return "stamp_not_allowed", {}
    card = notify_scope.card_row(db, req["card_id"])
    if card is None:
        return "card_missing", {}
    current = form(db, card, cfg, req["actor"], now, req["token"])
    if current.get("error"):
        return current["error"], {}
    if (current["form_hash"] != req["form_hash"] or req["mcs_user_id"] != current["mcs_user_id"]
            or req["project_id"] != current["project_id"]):
        return "stamp_target_changed", {}
    post = next((p for p in current["posts"] if p["message_id"] == req["message_id"]), None)
    if (post is None or post["source_hash"] != req["source_hash"]
            or post["own"]["state"] != "observed"
            or req["expected_own"] != {k: post["own"][k] for k in ("type", "observed_at")}):
        return "stamp_target_changed", {}
    try:
        directory, namespace = _namespace(db, root, create=True)
        _, witnesses = _scope_witnesses(directory, req, namespace)
        if len(witnesses) >= MAX_SCOPE_OPERATIONS:
            return "stamp_witness_capacity", {}
        if any(w["state"] not in TERMINAL for w in witnesses.values()):
            return "stamp_result_unknown", {}
    except (OSError, ValueError, KeyError, TypeError):
        return "stamp_restore_hold", {}
    # The intent ages from the human confirm, not from when the runner got to it.
    confirmed = req.get("confirmed_at", now)
    if not now - INTENT_MAX_AGE_S <= confirmed <= now + 60:
        return "stamp_intent_expired", {}
    value = {"request": req, "namespace": namespace, "state": "queued", "queued_at": min(confirmed, now),
             "frontier": payload_hash(witnesses)}
    db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
               "VALUES(?,?,?,?,?,?)", (KIND, req["project_id"], req["message_id"],
               canonical(value).decode(), canonical({"command_id": req["command_id"]}).decode(), now))
    return None, {"cmd": req["cmd"], "stamp_state": "queued", "scheduled": True, "stamp_op": req["op"]}


def _scope_witnesses(directory, req, namespace):
    scope = _private_dir(directory / f"{req['project_id']}-{req['message_id']}-{req['mcs_user_id']}")
    found = {}
    with os.scandir(scope) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_SCOPE_OPERATIONS * 2:
                raise StampRefused("stamp_witness_capacity")
            if entry.name.startswith((".alias-", ".atomic.")) and entry.name.endswith(".tmp"):
                # Unpublished safe_io staging may survive a crash; it grants no attempt.
                read_private_bytes(scope / entry.name)
                continue
            if len(found) >= MAX_SCOPE_OPERATIONS or not entry.name.endswith(".json"):
                raise StampRefused("stamp_witness_capacity")
            key = entry.name[:-5]
            if str(uuid.UUID(key)) != key:
                raise StampRefused("stamp_witness_invalid")
            value = _json(scope / entry.name)
            if (value.get("namespace") != namespace or value.get("command_id") != key
                    or value.get("scope") != [req[k] for k in ("project_id", "message_id", "mcs_user_id")]
                    or not valid_hash(value.get("payload_hash"))
                    or value.get("state") not in {"started", "unknown", "confirmed", "rejected"}):
                raise StampRefused("stamp_witness_invalid")
            found[key] = value
    return scope, found


def _approved(db, req):
    from mcs_requests import parse_command, validate
    try:
        if validate(req) is not None:
            return False
        row = db.execute("SELECT payload_hash,outcome,receipt_json FROM command_receipts "
                         "WHERE command_id=?", (req["command_id"],)).fetchone()
        if row is None or row["outcome"] != "applied" or row["payload_hash"] != payload_hash(req):
            return False
        receipt = parse_command(row["receipt_json"])
        return (isinstance(receipt, dict) and receipt.get("scheduled") is True
                and receipt.get("cmd") == "ops.mcs_reaction" and receipt.get("outcome") == "applied"
                and receipt.get("command_id") == req["command_id"]
                and receipt.get("payload_hash") == row["payload_hash"]
                and receipt.get("actor") == req["actor"] and receipt.get("project_id") == req["project_id"])
    except (ValueError, KeyError, TypeError):
        return False


def _save_outcome(ledger, artifact_id, value, state, error=None):
    value = {**value, "state": state, "error": error, "updated_at": time.time(),
             "result_published": False}
    req = value["request"]
    if not _approved(ledger.db, req):
        raise StampRefused("stamp_receipt_invalid")
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=? AND kind=?",
                          (canonical(value).decode(), artifact_id, KIND))
        row = ledger.db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                                (req["command_id"],)).fetchone()
        if row:
            receipt = json.loads(row[0])
            receipt.update(stamp_state=state, stamp_error=error)
            ledger.db.execute("UPDATE command_receipts SET receipt_json=? WHERE command_id=?",
                              (canonical(receipt).decode(), req["command_id"]))
    return value


def _publish_result(ledger, root, value):
    """Publish committed status on the existing private followup channel."""
    import notify_spool
    req = value["request"]
    if not _approved(ledger.db, req):
        raise StampRefused("stamp_receipt_invalid")
    row = ledger.db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                            (req["command_id"],)).fetchone()
    if row is None:
        return
    result = json.loads(row[0])
    if result.get("stamp_state") == "rejected":
        result.update(outcome="rejected", error=result.get("stamp_error") or "stamp_target_changed")
    notify_spool.ensure_dirs(root)
    notify_spool.publish_file(notify_spool.notify_dirs(root)["cmd_results"],
                              req["command_id"] + ".json", canonical(result))
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content=json_set(content,'$.result_published',json('true')) "
                          "WHERE kind=? AND json_extract(meta,'$.command_id')=?",
                          (KIND, req["command_id"]))


def _fresh_post(adapter, ledger, req):
    row = ledger.db.execute("SELECT parent_id FROM messages WHERE message_id=? AND project_id=?",
                            (req["message_id"], req["project_id"])).fetchone()
    if row is None:
        raise StampRefused("source_missing")
    post = adapter.fetch_message_metadata(req["project_id"], req["message_id"], parent_id=row[0])
    if (post.message_id != req["message_id"] or post.project_id != req["project_id"]
            or post.parent_id != row[0] or post.body_state != "full"
            or hashlib.sha256((post.body_html or "").encode()).hexdigest() != req["source_hash"]):
        raise StampRefused("source_changed")
    now = time.time()
    captured = {key: {"value": value, "observed_at": now} for key, value in post.metadata.items()}
    own = _own(decode_metadata((canonical(captured).decode(), now,
                               ",".join(post.metadata_errors) or None)))
    if own["state"] != "observed":
        raise StampRefused("stamp_observation_required")
    ledger.save_metadata_shadow(post, publish=True)
    return own["type"]


def _check_live_intent(db, req, cfg, root, now, value):
    if not enabled(cfg, req["actor"]) or options(cfg)["account_user_id"] != req["mcs_user_id"]:
        raise StampRefused("stamp_not_allowed")
    import notify_spool
    if notify_spool.restore_pending(root) is not None:
        raise StampRefused("stamp_restore_hold")
    if now - value["queued_at"] > INTENT_MAX_AGE_S:
        raise StampRefused("stamp_intent_expired")
    card = notify_scope.card_row(db, req["card_id"])
    if card is None:
        raise StampRefused("card_missing")
    current = form(db, card, cfg, req["actor"], now, req["token"])
    if current.get("error"):
        raise StampRefused(current["error"])
    if current["project_id"] != req["project_id"] or not any(
            p["message_id"] == req["message_id"] and p["source_hash"] == req["source_hash"]
            for p in current["posts"]):
        raise StampRefused("stamp_target_changed")


def _check_reconciliation(db, req, cfg, root):
    if not enabled(cfg, req["actor"]) or options(cfg)["account_user_id"] != req["mcs_user_id"]:
        raise StampRefused("stamp_not_allowed")
    import notify_spool
    if notify_spool.restore_pending(root) is not None:
        raise StampRefused("stamp_restore_hold")
    card = notify_scope.card_row(db, req["card_id"])
    scope = notify_scope.delivery_scope(cfg)
    if (card is None or card["project_id"] != req["project_id"]
            or card["delivery_state"] == "revoked" or scope is None
            or not notify_scope.scope_matches(card, scope)):
        raise StampRefused("scope_mismatch")


def _execute(ledger, adapter, cfg, root, row, now, deadline):
    value = json.loads(row["content"])
    req = value["request"]
    if (not _approved(ledger.db, req) or row["project_id"] != req["project_id"]
            or row["message_id"] != req["message_id"]):
        raise StampRefused("stamp_receipt_invalid")
    directory, namespace = _namespace(ledger.db, root)
    if namespace != value["namespace"]:
        raise StampRefused("stamp_restore_hold")
    scope, witnesses = _scope_witnesses(directory, req, namespace)
    prior = witnesses.get(req["command_id"])
    digest = payload_hash(req)
    if prior and prior["payload_hash"] != digest:
        raise StampRefused("stamp_witness_invalid")
    if prior is None and value["state"] != "queued":
        raise StampRefused("stamp_witness_invalid")
    if prior is not None and prior["state"] in TERMINAL:
        # Restore replays the original observed outcome, not the post's later state.
        _save_outcome(ledger, row["artifact_id"], value, prior["state"])
        return prior["state"]
    try:
        # Reconciliation remains GET-only after intent expiry; authorization and
        # patient scope still apply, but a stale token never grants another POST.
        if prior is None:
            _check_live_intent(ledger.db, req, cfg, root, now, value)
            if len(witnesses) >= MAX_SCOPE_OPERATIONS:
                raise StampRefused("stamp_witness_capacity")
        else:
            _check_reconciliation(ledger.db, req, cfg, root)
        if any(k != req["command_id"] and w["state"] not in TERMINAL for k, w in witnesses.items()):
            raise StampRefused("stamp_result_unknown")
        # the core module stays byte-identical (its digest feeds extraction fingerprints)
        from self_identity import _station_self_id as station_self_id, normalize_sender_id
        profile = adapter.self_profile()
        account = normalize_sender_id(profile.get("sender_id"))
        if account is None:
            # /users/self may omit the id; the fresh own-station roster's single
            # is_self member is the same session's identity. Absent or ambiguous stays unknown.
            account = station_self_id(adapter.station_staffs(profile.get("stations") or [], strict_self=True))
        if account is None or account != req["mcs_user_id"]:
            raise StampRefused("stamp_account_mismatch")
        actual = _fresh_post(adapter, ledger, req)
        if prior is None:
            _check_live_intent(ledger.db, req, cfg, root, time.time(), value)
    except StampRefused as exc:
        # A pending restore holds the approved intent (drain keeps it queued) instead of ending it.
        if prior is None and str(exc) != "stamp_restore_hold":
            _save_outcome(ledger, row["artifact_id"], value, "rejected", str(exc))
            return "rejected"
        raise
    desired = req.get("reaction_type") if req["op"] == "add" else None
    path = scope / (req["command_id"] + ".json")
    witness = {"command_id": req["command_id"], "payload_hash": digest,
               "namespace": namespace, "scope": [req[k] for k in ("project_id", "message_id", "mcs_user_id")],
               "state": "started"}
    if prior is not None:
        # Even after DB rollback, an attempted operation is reconciled by GET only.
        state = "confirmed" if actual == desired else "unknown"
    elif actual == desired:
        state = "confirmed"
    else:
        if (value["state"] != "queued" or actual != req["expected_own"]["type"]
                or value.get("frontier") != payload_hash(witnesses)):
            _save_outcome(ledger, row["artifact_id"], value, "rejected", "stamp_target_changed")
            return "rejected"
        if time.monotonic() + 2 >= deadline:
            raise TimeoutError("stamp_wait_deadline")
        publish_new_bytes(path, canonical(witness))
        refused = False
        try:
            try:
                adapter.write_reaction(req["message_id"], req.get("reaction_type"), cancel=req["op"] == "cancel")
            except MCSError as error:
                # A definite client refusal; session, throttling and server errors stay unknown.
                if not (type(error) is MCSError and error.kind == "http_error"
                        and isinstance(error.status, int) and 400 <= error.status < 500
                        and error.status not in (401, 403, 408, 429)):
                    raise
                refused = True
            actual = _fresh_post(adapter, ledger, req)
            if actual == desired:
                state = "confirmed"
            elif refused and actual == req["expected_own"]["type"]:
                # Refused and read back unchanged: terminal, so the post is not locked forever.
                state = "rejected"
            else:
                state = "unknown"
        except Exception:
            state = "unknown"
    witness["state"] = state
    if prior is None and not path.exists():
        publish_new_bytes(path, canonical(witness))
    else:
        atomic_write(str(path), lambda f: f.write(canonical(witness).decode()), mode=0o600, makedirs=False)
    _save_outcome(ledger, row["artifact_id"], value, state,
                  "stamp_server_rejected" if state == "rejected" else None)
    return state


def drain(ledger, adapter, cfg, root, deadline):
    """Execute bounded queued work and reconcile unknowns; caller holds run.lock."""
    counts = {"confirmed": 0, "unknown": 0, "rejected": 0, "held": 0}
    if not enabled(cfg):
        return counts
    # Distinct windows stop old unknowns starving newly approved other posts.
    seen = set()
    for state in ("queued", "unknown", "publish"):
        condition = ("json_extract(content,'$.state') IN ('confirmed','rejected') "
                     "AND json_extract(content,'$.result_published') IS NOT 1"
                     if state == "publish" else "json_extract(content,'$.state')=?")
        rows = ledger.db.execute("SELECT artifact_id,project_id,message_id,content FROM artifacts WHERE kind=? "
                                 "AND json_valid(content) AND " + condition +
                                 " ORDER BY COALESCE(json_extract(content,'$.updated_at'),created_at),artifact_id "
                                 "LIMIT 4", (KIND,) if state == "publish" else (KIND, state)).fetchall()
        for row in rows:
            if row["artifact_id"] in seen:
                continue
            seen.add(row["artifact_id"])
            if time.monotonic() + 5 >= deadline:
                return counts
            try:
                if state != "publish":
                    counts[_execute(ledger, adapter, cfg, root, row, time.time(), deadline)] += 1
            except Exception:
                # Invalid/absent external state never grants a fresh wire attempt.
                counts["held"] += 1
                value = json.loads(row["content"])
                if (isinstance(value, dict) and isinstance(value.get("request"), dict)
                        and _approved(ledger.db, value["request"])):
                    _save_outcome(ledger, row["artifact_id"], value, state, "stamp_check_held")
                else:
                    # Move an unsaveable row behind the others so it cannot starve the window.
                    with ledger.db:
                        ledger.db.execute("UPDATE artifacts SET content=json_set(content,'$.updated_at',?) "
                                          "WHERE artifact_id=? AND kind=?", (time.time(), row["artifact_id"], KIND))
            value = json.loads(ledger.db.execute(
                "SELECT content FROM artifacts WHERE artifact_id=?", (row["artifact_id"],)).fetchone()[0])
            try:
                _publish_result(ledger, root, value)
                if value.get("state") in {"confirmed", "unknown"}:
                    import notify_cards
                    notify_cards.rerender_message_cards(
                        ledger, cfg, value["request"]["project_id"], value["request"]["message_id"])
            except Exception:
                # The durable outcome stays authoritative if followup/render publication fails.
                counts["held"] += 1
    return counts
