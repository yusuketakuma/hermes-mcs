"""Persist the first observed history cohort without changing delivery receipts."""
import json
import math
import re
import uuid

import history_walk
from mcs_util import loads_dict, payload_hash

META_KEY = "_initial_import"
_FIELDS = frozenset({"version", "cohort_id", "boundary_ts", "boundary_source",
                     "started_at", "capture_phase", "saved_count", "updated_at"})


def load(db, project_id):
    """Return one patient's immutable cohort, or reject malformed saved state."""
    row = db.execute("SELECT initial_import FROM patients WHERE project_id=?", (project_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        state = json.loads(row[0])
        if (not isinstance(state, dict) or state.keys() - {"notification_cancelled", "capture_proof"} != _FIELDS
                or "notification_cancelled" in state and state["notification_cancelled"] is not True
                or type(state["version"]) is not int or state["version"] != 1
                or not isinstance(state["cohort_id"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", state["cohort_id"])
                or type(state["boundary_ts"]) is not int or state["boundary_ts"] <= 0
                or state["boundary_source"] not in ("server", "local")
                or state["capture_phase"] not in ("capturing", "complete", "held")
                or "capture_proof" in state and not history_walk.capture_proof(
                    state["capture_proof"], project_id, state["cohort_id"])
                or type(state["saved_count"]) is not int or state["saved_count"] < 0
                or any(type(state[k]) not in (int, float) or not math.isfinite(state[k])
                       or state[k] < 0 for k in ("started_at", "updated_at"))):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("initial_import_invalid") from None
    return state


def failed_history_restart(db, project_id, job):
    """Plan an explicit failed full-history retry without inventing a cohort."""
    if job is None or job["state"] != "failed":
        return None
    payload = loads_dict(job["payload"] or "{}")
    if not isinstance(payload, dict) or type(payload.get("since")) is not int or payload["since"] != 0:
        return None
    if (type(payload.get("page", 1)) is not int
            or payload.get("page", 1) < 1
            or type(payload.get("pages", 10)) is not int or not 1 <= payload.get("pages", 10) <= 40
            or type(payload.get("stalls", 0)) is not int or payload.get("stalls", 0) < 0
            or any(key in payload and type(payload[key]) is not bool for key in ("trickle", "notify"))
            or "notify_source" in payload and payload["notify_source"] != "discovery"):
        raise ValueError("invalid_history_payload")
    try:
        json.dumps(payload, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid_history_payload") from None
    floor = db.execute("SELECT history_floor FROM patients WHERE project_id=?", (project_id,)).fetchone()
    value = floor[0] if floor and floor[0] is not None else 0
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 and value != -1:
        raise ValueError("invalid_history_floor")
    state = load(db, project_id)
    if state is not None and "walk" in payload:
        history_walk.validate(payload, project_id, state["cohort_id"])
    # A legacy patient has no cohort; a first fetch can fail before saving a
    # walk. Neither case manufactures a cohort or invalidates saved messages.
    payload.pop("walk", None)
    payload.update(page=1, stalls=0, trickle=False)
    return payload


def restart_history_tx(db, project_id, job_id, payload, now):
    """Restart one failed history row and its cohort's blocked fetches in the caller TX."""
    if not db.in_transaction:
        raise RuntimeError("history_restart_transaction_required")
    changed = db.execute(
        "UPDATE fetch_jobs SET state='pending',attempts=0,reason_code=NULL,payload=?,next_try=?,updated_at=? "
        "WHERE job_id=? AND project_id=? AND kind='history' AND message_id=0 AND state='failed'",
        (json.dumps(payload, ensure_ascii=False, allow_nan=False), now, now, job_id, project_id)).rowcount
    if not changed:
        raise ValueError("history_restart_changed")
    state = load(db, project_id)
    if state is not None:
        join, where = history_predicate()
        members = f"SELECT m.message_id FROM messages m {join} WHERE m.project_id=? AND {where}"
        db.execute("UPDATE fetch_jobs SET state='pending',attempts=0,reason_code=NULL,next_try=?,updated_at=? "
                   "WHERE project_id=? AND kind IN ('reply','thread') AND state='failed' "
                   f"AND (message_id IN ({members}) OR parent_id IN ({members}))",
                   (now, now, project_id, project_id, state["cohort_id"], project_id, state["cohort_id"]))
        state.update(capture_phase="capturing", updated_at=now)
        state.pop("capture_proof", None)
        _write(db, project_id, state)


def retire_verified_thread_tx(db, parent, now, *, replies=None):
    """Retire a failed thread only when fresh full replies match already durable source rows."""
    if not db.in_transaction:
        raise RuntimeError("thread_retire_transaction_required")
    replies = parent.replies if replies is None else replies
    if (parent.body_state != "full" or type(parent.reply_count) is not int or parent.reply_count <= 0
            or len(replies) < parent.reply_count or len({r.message_id for r in replies}) != len(replies)
            or parent.message_id in {r.message_id for r in replies}
            or any(r.body_state != "full" or r.project_id != parent.project_id
                   or r.parent_id != parent.message_id for r in replies)):
        return False
    # A successful fetch is not yet a successful save. Check the exact raw
    # content and parent binding under the writer reservation before retiring.
    for message in [parent, *replies]:
        row = db.execute("SELECT project_id,parent_id,body_state,body_html,reply_count FROM messages WHERE message_id=?",
                         (message.message_id,)).fetchone()
        if (row is None or row["project_id"] != parent.project_id or row["body_state"] != "full"
                or row["body_html"] != message.body_html
                or row["parent_id"] != message.parent_id
                or message is parent and row["reply_count"] != parent.reply_count):
            return False
    if db.execute("SELECT 1 FROM messages WHERE project_id=? AND parent_id=? AND body_state IS NOT 'full' LIMIT 1",
                  (parent.project_id, parent.message_id)).fetchone():
        return False
    return bool(db.execute("UPDATE fetch_jobs SET state='done',reason_code=NULL,updated_at=? "
                           "WHERE kind='thread' AND state='failed' AND project_id=? AND message_id=? AND parent_id=?",
                           (now, parent.project_id, parent.message_id, parent.message_id)).rowcount)


def _write(db, project_id, state):
    db.execute("UPDATE patients SET initial_import=? WHERE project_id=?",
               (json.dumps(state, ensure_ascii=False), project_id))


def emit_tx(ledger, project_id, state, ids=()):
    """Reserve an aggregate intent, never an individual delivered receipt."""
    current = load(ledger.db, project_id)
    if state.get("notification_cancelled") is True or current and current.get("notification_cancelled") is True:
        return None
    return ledger._outbox_insert("patient_history", project_id, {
        "source": "initial_history", "cohort_id": state["cohort_id"],
        "message_ids": list(ids)}, route="interactive")


def cancel_notifications_tx(db, project_id, cohort_id):
    """Stop one matching cohort's future notifications without stopping capture or analysis."""
    state = load(db, project_id)
    if state is None or state["cohort_id"] != cohort_id:
        return False
    state["notification_cancelled"] = True
    _write(db, project_id, state)
    return True


def start_tx(ledger, project_id, boundary_ts, now, *, boundary_source):
    """Reserve a new patient's cohort and initial display in the registration TX."""
    if load(ledger.db, project_id) is not None:
        return
    if type(boundary_ts) is not int or boundary_ts <= 0 or boundary_source not in ("server", "local"):
        raise ValueError("initial_import_boundary_invalid")
    state = {"version": 1, "cohort_id": uuid.uuid4().hex,
             "boundary_ts": boundary_ts, "boundary_source": boundary_source,
             "started_at": now, "capture_phase": "capturing", "saved_count": 0,
             "updated_at": now}
    _write(ledger.db, project_id, state)
    emit_tx(ledger, project_id, state)


def classify_tx(ledger, project_id, ids, now):
    """Pin each first-observed post's role; later body/time corrections keep it."""
    state = load(ledger.db, project_id)
    if state is None:
        return set()
    history, added = set(), 0
    ids = list(dict.fromkeys(ids))
    for offset in range(0, len(ids), 900):
        group = ids[offset:offset + 900]
        for row in ledger.db.execute(
                "SELECT m.message_id,m.posted_at_ts,d.content FROM messages m "
                "LEFT JOIN message_metadata d ON d.message_id=m.message_id AND d.source='capture' "
                "WHERE m.project_id=? AND m.message_id IN (" + ','.join('?' * len(group)) + ')',
                (project_id, *group)).fetchall():
            try:
                content = json.loads(row["content"]) if row["content"] is not None else {}
                if not isinstance(content, dict):
                    raise ValueError
                role = content.get(META_KEY)
                if role is not None and (not isinstance(role, dict)
                        or set(role) != {"cohort_id", "role"}
                        or role["cohort_id"] != state["cohort_id"]
                        or role["role"] not in ("history", "live")):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("initial_import_membership_invalid") from None
            if role is None:
                stamp = row["posted_at_ts"]
                role = {"cohort_id": state["cohort_id"],
                        "role": "history" if type(stamp) is int and 0 < stamp < state["boundary_ts"] else "live"}
                content[META_KEY] = role
                ledger.db.execute(
                    "INSERT INTO message_metadata(message_id,source,content,checked_at) "
                    "VALUES(?,'capture',?,?) ON CONFLICT(message_id,source) DO UPDATE SET content=excluded.content",
                    (row["message_id"], json.dumps(content, ensure_ascii=False), 0))
                added += role["role"] == "history"
            if role["role"] == "history":
                history.add(row["message_id"])
    if added:
        state["saved_count"] += added
        state["updated_at"] = now
        _write(ledger.db, project_id, state)
        dirty_source_tx(ledger.db, project_id, next(iter(history)), now, new_member=True)
    return history


def history_predicate(alias="m", metadata_alias="im"):
    """SQL join/filter shared by bounded aggregate queries and single-post checks."""
    return (f"JOIN message_metadata {metadata_alias} ON {metadata_alias}.message_id={alias}.message_id "
            f"AND {metadata_alias}.source='capture'",
            f"json_extract({metadata_alias}.content,'$.{META_KEY}.role')='history' "
            f"AND json_extract({metadata_alias}.content,'$.{META_KEY}.cohort_id')=?")


def message_ids(db, project_id):
    """Read cohort IDs only when a caller actually needs the entire membership."""
    state = load(db, project_id)
    if state is None:
        return []
    join, where = history_predicate()
    return [r[0] for r in db.execute(
        f"SELECT m.message_id FROM messages m {join} WHERE m.project_id=? AND {where} ORDER BY m.message_id",
        (project_id, state["cohort_id"]))]


def progress(db, project_id):
    """Read the small capture snapshot; extraction completion is owned separately."""
    return load(db, project_id)


def acquisition_status(db, project_id):
    """Bind whole-history acquisition evidence to the caller's read snapshot."""
    if not db.in_transaction:
        raise RuntimeError("history_snapshot_required")
    if type(project_id) is not int or project_id <= 0:
        raise ValueError("history_scope_invalid")
    row = db.execute("SELECT fetch_state,fetch_reason,history_floor,"
                     "CASE WHEN length(CAST(initial_import AS BLOB))<=16384 THEN initial_import END,"
                     "length(CAST(initial_import AS BLOB)) FROM patients WHERE project_id=?",
                     (project_id,)).fetchone()
    if row is None:
        return {"complete": False, "signature": payload_hash([project_id, "missing"])}
    fetch, reason, floor, raw, size = row
    valid = size is None or type(size) is int and size <= 16384
    capture = None
    if valid:
        try:
            capture = load(db, project_id)
        except (ValueError, TypeError, KeyError):
            valid = False
    jobs = list(db.execute("SELECT kind,COUNT(*),MAX(job_id),SUM(state IS NOT 'done') "
                           "FROM fetch_jobs WHERE project_id=? AND kind IN ('history','thread','reply') "
                           "GROUP BY kind ORDER BY kind", (project_id,)))
    complete = bool(valid and fetch == "complete" and not reason and floor == -1
                    and (capture is None or capture.get("capture_phase") == "complete"
                         and history_walk.capture_proof(capture.get("capture_proof"), project_id,
                                                        capture["cohort_id"]))
                    and not any(row[3] for row in jobs))
    return {"complete": complete,
            "signature": payload_hash([project_id, fetch, reason, floor, raw, size, valid,
                                       [list(row) for row in jobs]])}


def refresh_capture_tx(ledger, project_id, now):
    """Publish capture completion only after verified history and all fetch work settle."""
    state = load(ledger.db, project_id)
    if state is None or (state["capture_phase"] == "complete" and history_walk.capture_proof(
            state.get("capture_proof"), project_id, state["cohort_id"])):
        return
    join, where = history_predicate()
    jobs = ledger.db.execute(
        "SELECT j.kind,j.state,j.reason_code,j.payload FROM fetch_jobs j WHERE j.project_id=? "
        "AND (j.kind='history' OR (j.kind IN ('reply','thread') AND j.message_id IN ("
        f"SELECT m.message_id FROM messages m {join} WHERE m.project_id=? AND {where})))",
        (project_id, project_id, state["cohort_id"])).fetchall()
    proof = next((loads_dict(r["payload"]) for r in jobs if r["kind"] == "history" and r["state"] == "done"
                  and history_walk.accepted(loads_dict(r["payload"]) or {}, project_id, state["cohort_id"])), None)
    if (state["capture_phase"] == "complete" and proof is None
            and not any(r["kind"] == "history" and r["state"] != "done" for r in jobs)):
        # This feature is unshipped: an earlier candidate's completion is not new evidence.
        ledger.job_add_tx("history", project_id, payload={"since": 0, "page": 1, "pages": 3,
                          "trickle": False, "notify": True, "notify_source": "discovery"}, reset_pending=True)
    complete = ledger.history_floor(project_id) == -1 and proof is not None
    blocked = any(r["state"] == "failed" for r in jobs)
    pending = any(r["state"] != "done" for r in jobs)
    incomplete = ledger.db.execute(
        f"SELECT 1 FROM messages m {join} WHERE m.project_id=? AND {where} "
        "AND (m.body_state IS NULL OR (m.body_state NOT IN ('full','deleted') AND (m.body_state!='unknown' OR COALESCE(m.parent_id,0)!=0))) LIMIT 1",
        (project_id, state["cohort_id"])).fetchone()
    phase = "held" if blocked else "complete" if complete and not pending and not incomplete else "capturing"
    if state["capture_phase"] != phase:
        state.update(capture_phase=phase, updated_at=now)
        if phase == "complete":
            state["capture_proof"] = {k: proof["walk"][k] for k in ("v", "scope", "digest")}
        _write(ledger.db, project_id, state)
        emit_tx(ledger, project_id, state)


def dirty_source_tx(db, project_id, message_id, now, *, new_member=False):
    """Mark edited history for adoption without rescanning its cohort in the save TX."""
    row = db.execute("SELECT content FROM message_metadata WHERE message_id=? AND source='capture'", (message_id,)).fetchone()
    content = json.loads(row[0]) if row else {}
    if (content.get(META_KEY) or {}).get("role") != "history":
        return
    for card in db.execute("SELECT card_id,ui_state FROM notification_cards WHERE kind='patient_history' "
                           "AND project_id=? AND delivery_state!='revoked'", (project_id,)).fetchall():
        ui = json.loads(card["ui_state"]) if card["ui_state"] else {}
        ui.setdefault("history_dirty_since", now)
        if not new_member or (ui.get("history_latest") or {}).get("phase") == "complete":
            ui["history_source_dirty"] = True
        db.execute("UPDATE notification_cards SET ui_state=? WHERE card_id=?", (json.dumps(ui), card["card_id"]))
