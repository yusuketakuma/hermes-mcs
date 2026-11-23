"""Visits to a patient: planned, done or cancelled, with a module-declared kind, a version check and an audit trail."""
from __future__ import annotations

import re
import time

from homecare_records import InvalidFields, calendar_date, get, require_actor, text_field
from homecare_store import Conflict, audit, transaction

FIELDS = ("patient_id", "staff_id", "kind", "visit_date", "start_time", "status", "note")
STATUSES = ("planned", "done", "cancelled")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _clean(db, registry, values, held_kind=None):
    """``held_kind`` keeps a disabled module's visit kind on an existing visit from being refused."""
    bad = [key for key in values if key not in FIELDS]
    row = {"note": text_field(values, bad, "note", 500)}
    for field in ("patient_id", "staff_id"):
        row[field] = values.get(field)
        if not (row[field] is None and field == "staff_id") and (type(row[field]) is not int or row[field] <= 0):
            bad.append(field)
    row["kind"] = values.get("kind")
    if row["kind"] not in registry.visit_kinds and row["kind"] != held_kind:
        bad.append("kind")
    row["visit_date"] = values.get("visit_date")
    if not calendar_date(row["visit_date"]):
        bad.append("visit_date")
    row["start_time"] = values.get("start_time") or None
    if row["start_time"] is not None and (not isinstance(row["start_time"], str) or not _TIME.match(row["start_time"])):
        bad.append("start_time")
    row["status"] = values.get("status", "planned")
    if row["status"] not in STATUSES:
        bad.append("status")
    elif row["status"] == "done" and row["staff_id"] is None:
        # A visit that happened names who made it.
        bad.append("staff_id")
    if "patient_id" not in bad and db.execute("SELECT 1 FROM patients WHERE patient_id=?",
                                              (row["patient_id"],)).fetchone() is None:
        bad.append("patient_id")
    if "staff_id" not in bad and row["staff_id"] is not None and db.execute(
            "SELECT 1 FROM staff WHERE staff_id=? AND active=1", (row["staff_id"],)).fetchone() is None:
        bad.append("staff_id")
    if bad:
        raise InvalidFields(bad)
    return row


def visit(db, visit_id):
    row = db.execute("SELECT v.*, s.name AS staff_name FROM visits v LEFT JOIN staff s USING(staff_id) "
                     "WHERE v.visit_id=?", (visit_id,)).fetchone()
    if row is None:
        raise ValueError("not_found")
    return dict(row)


def create(db, registry, values, *, actor):
    if not isinstance(values, dict):
        raise InvalidFields(["values"])
    with transaction(db):
        require_actor(db, actor)
        row = _clean(db, registry, values)
        now = time.time()
        visit_id = db.execute(f"INSERT INTO visits({','.join(row)},created_at,updated_at) VALUES"
                              f"({','.join('?' * len(row))},?,?)", (*row.values(), now, now)).lastrowid
        created = visit(db, visit_id)
        audit(db, actor, "visit.create", visit_id, None, created)
    return created


def update(db, registry, visit_id, values, *, expected_version, actor):
    """Edit a visit; the patient stays fixed, so a visit never moves to another patient's history."""
    if not isinstance(values, dict) or type(expected_version) is not int or "patient_id" in values:
        raise InvalidFields(["values"])
    with transaction(db):
        require_actor(db, actor)
        before = visit(db, visit_id)
        if before["version"] != expected_version:
            raise Conflict("version_conflict")
        row = _clean(db, registry, {field: before[field] for field in FIELDS} | values, before["kind"])
        db.execute(f"UPDATE visits SET {','.join(f'{field}=?' for field in row)},version=version+1,updated_at=? "
                   "WHERE visit_id=? AND version=?", (*row.values(), time.time(), visit_id, expected_version))
        after = visit(db, visit_id)
        audit(db, actor, "visit.update", visit_id, before, after)
    return after


def for_patient(db, patient_id, *, limit=50):
    """A patient's visits, newest date first."""
    get(db, "patient", patient_id)
    return [dict(row) for row in db.execute(
        "SELECT v.*, s.name AS staff_name FROM visits v LEFT JOIN staff s USING(staff_id) WHERE v.patient_id=? "
        "ORDER BY v.visit_date DESC, v.start_time IS NULL, v.start_time DESC, v.visit_id DESC LIMIT ?",
        (patient_id, limit))]


def for_day(db, visit_date):
    """Every visit on one date, in time order, with the patient's name and address for planning the day."""
    if not calendar_date(visit_date):
        raise ValueError("bad_request")
    return [dict(row) for row in db.execute(
        "SELECT v.*, s.name AS staff_name, p.name AS patient_name, p.address, p.residence FROM visits v "
        "JOIN patients p USING(patient_id) LEFT JOIN staff s USING(staff_id) WHERE v.visit_date=? "
        "ORDER BY v.start_time IS NULL, v.start_time, v.visit_id", (visit_date,))]
