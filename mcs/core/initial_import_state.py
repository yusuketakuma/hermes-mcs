"""Persist the first observed history cohort without changing delivery receipts."""
import json
import math
import re
import uuid

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
        if (not isinstance(state, dict) or state.keys() != _FIELDS
                or type(state["version"]) is not int or state["version"] != 1
                or not isinstance(state["cohort_id"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", state["cohort_id"])
                or type(state["boundary_ts"]) is not int or state["boundary_ts"] <= 0
                or state["boundary_source"] not in ("server", "local")
                or state["capture_phase"] not in ("capturing", "complete", "held")
                or type(state["saved_count"]) is not int or state["saved_count"] < 0
                or any(type(state[k]) not in (int, float) or not math.isfinite(state[k])
                       or state[k] < 0 for k in ("started_at", "updated_at"))):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("initial_import_invalid") from None
    return state


def _write(db, project_id, state):
    db.execute("UPDATE patients SET initial_import=? WHERE project_id=?",
               (json.dumps(state, ensure_ascii=False), project_id))


def emit_tx(ledger, project_id, state, ids=()):
    """Reserve an aggregate intent, never an individual delivered receipt."""
    return ledger._outbox_insert("patient_history", project_id, {
        "source": "initial_history", "cohort_id": state["cohort_id"],
        "message_ids": list(ids)}, route="interactive")


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


def refresh_capture_tx(ledger, project_id, now):
    """Publish capture completion only after verified history and all fetch work settle."""
    state = load(ledger.db, project_id)
    if state is None:
        return
    join, where = history_predicate()
    jobs = ledger.db.execute(
        "SELECT j.kind,j.state,j.reason_code FROM fetch_jobs j WHERE j.project_id=? "
        "AND (j.kind='history' OR (j.kind IN ('reply','thread') AND j.message_id IN ("
        f"SELECT m.message_id FROM messages m {join} WHERE m.project_id=? AND {where})))",
        (project_id, project_id, state["cohort_id"])).fetchall()
    complete = (ledger.history_floor(project_id) == -1
                and any(r["kind"] == "history" and r["state"] == "done" for r in jobs))
    blocked = any(r["state"] == "failed" for r in jobs)
    pending = any(r["state"] != "done" for r in jobs)
    incomplete = ledger.db.execute(
        f"SELECT 1 FROM messages m {join} WHERE m.project_id=? AND {where} "
        "AND (m.body_state IS NULL OR m.body_state NOT IN ('full','deleted')) LIMIT 1",
        (project_id, state["cohort_id"])).fetchone()
    phase = "held" if blocked else "complete" if complete and not pending and not incomplete else "capturing"
    if state["capture_phase"] != phase:
        state.update(capture_phase=phase, updated_at=now)
        _write(ledger.db, project_id, state)
        emit_tx(ledger, project_id, state)
