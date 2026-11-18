"""Human-confirmed revisions for private previsit delivery."""
from __future__ import annotations

import json

from mcs_requests import canonical


ASSIGNMENT_KIND = "private_previsit_assignment_v1"
VISIT_DATE_KIND = "private_previsit_visit_date_v1"


def _latest(db, kind, project_id):
    row = db.execute(
        "SELECT artifact_id,content,meta FROM artifacts "
        "WHERE kind=? AND project_id=? ORDER BY artifact_id DESC LIMIT 1",
        (kind, project_id)).fetchone()
    if row is None:
        return None
    try:
        content = json.loads(row["content"])
        meta = json.loads(row["meta"] or "{}")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("previsit_state_corrupt") from None
    if (type(content) is not dict or type(meta) is not dict
            or type(meta.get("schema")) is not int or meta["schema"] != 1
            or type(meta.get("revision")) is not int or meta["revision"] < 1
            or type(meta.get("command_id")) is not str or not meta["command_id"]):
        raise ValueError("previsit_state_corrupt")
    return {"artifact_id": row["artifact_id"], "content": content, "meta": meta}


def _project(db, project_id):
    row = db.execute("SELECT is_archived FROM patients WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        return "bad_project_id"
    if row[0] != 0:
        return "patient_archived"
    return None


def _apply_revision(db, req, now, kind, content):
    error = _project(db, req["project_id"])
    if error:
        return error, {}
    try:
        latest = _latest(db, kind, req["project_id"])
    except ValueError as exc:
        return str(exc), {}
    current = latest["meta"]["revision"] if latest else 0
    if req["expected_revision"] != current:
        return "previsit_revision_stale", {"current_revision": current}
    revision = current + 1
    meta = {"schema": 1, "revision": revision, "command_id": req["command_id"]}
    cursor = db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,?,?,?,?)",
        (kind, req["project_id"], canonical(content).decode("utf-8"),
         canonical(meta).decode("utf-8"), now))
    return None, {"artifact_id": cursor.lastrowid, "revision": revision}


def apply_tx(db, req, now):
    """Apply one previsit registry revision inside the receipt transaction."""
    command = req["cmd"]
    if command == "ops.previsit_assignment_set":
        content = {"active": True, "staff_id": req["staff_id"],
                   "transport": req["transport"], "account_scope": req["account_scope"],
                   "recipient_id": req["recipient_id"]}
        return _apply_revision(db, req, now, ASSIGNMENT_KIND, content)
    if command == "ops.previsit_assignment_revoke":
        try:
            latest = _latest(db, ASSIGNMENT_KIND, req["project_id"])
        except ValueError as exc:
            return str(exc), {}
        if latest is None or latest["content"].get("active") is not True:
            return "previsit_assignment_missing", {}
        content = {"active": False}
        return _apply_revision(db, req, now, ASSIGNMENT_KIND, content)
    if command == "ops.previsit_visit_date_set":
        content = {"visit_date": req["visit_date"]}
        return _apply_revision(db, req, now, VISIT_DATE_KIND, content)
    return "unknown_ops_cmd", {}
