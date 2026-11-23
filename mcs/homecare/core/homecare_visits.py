"""Visits to a patient: planned, done or cancelled, with a module-declared kind, a version check and an audit trail."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

from homecare_records import (InvalidFields, calendar_date, get, qualifications, require_actor, text_field,
                              unknown_fields)
from homecare_store import Conflict, audit, transaction

FIELDS = ("patient_id", "staff_id", "kind", "visit_date", "start_time", "status", "note")
STATUSES = ("planned", "done", "cancelled")
# Visit dates and times are wall-clock times at the site, which is in Japan.
SITE_TIME = timezone(timedelta(hours=9))
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Soonest first within a day: timed visits in time order, then untimed ones.
_IN_DAY = "v.start_time IS NULL, v.start_time, v.visit_id"


def _clean(db, registry, values, held_kind=None, held_staff=None):
    """``held_kind`` and ``held_staff`` keep an existing visit's disabled kind or since-retired staff member
    from being refused when they are left unchanged; a new choice must be enabled and active."""
    bad = unknown_fields(values, FIELDS)
    row = {"note": text_field(values, bad, "note", 500)}
    for field in ("patient_id", "staff_id"):
        row[field] = values.get(field)
        if not (row[field] is None and field == "staff_id") and (type(row[field]) is not int or row[field] <= 0):
            bad.append(field)
    row["kind"] = values.get("kind")
    if not isinstance(row["kind"], str) or row["kind"] not in registry.visit_kinds and row["kind"] != held_kind:
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
            "SELECT 1 FROM staff WHERE staff_id=? AND (active=1 OR staff_id=?)",
            (row["staff_id"], held_staff)).fetchone() is None:
        bad.append("staff_id")
    if bad:
        raise InvalidFields(bad)
    return row


def _require_clinical(db, registry, row, actor, before=None):
    """A done visit of a restricted kind is written by, and names, staff holding the kind's qualification;
    once done, only such staff may change it (cancelling it included)."""
    def holds(staff, kind):
        needed = registry.done_requires.get(kind)
        return not needed or bool(set(qualifications(db, staff)) & set(needed))

    if row["status"] == "done" and not (holds(actor, row["kind"]) and holds(row["staff_id"], row["kind"])) \
            or before is not None and before["status"] == "done" and not holds(actor, before["kind"]):
        raise ValueError("visit_not_qualified")


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
        _require_clinical(db, registry, row, actor)
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
        row = _clean(db, registry, {field: before[field] for field in FIELDS} | values, before["kind"],
                     before["staff_id"])
        _require_clinical(db, registry, row, actor, before)
        db.execute(f"UPDATE visits SET {','.join(f'{field}=?' for field in row)},version=version+1,updated_at=? "
                   "WHERE visit_id=? AND version=?", (*row.values(), time.time(), visit_id, expected_version))
        after = visit(db, visit_id)
        audit(db, actor, "visit.update", visit_id, before, after)
    return after


def for_patient(db, patient_id, *, limit=50, offset=0):
    """A patient's visits, newest date first."""
    get(db, "patient", patient_id)
    return [dict(row) for row in db.execute(
        _LISTED + "ORDER BY v.visit_date DESC, v.start_time IS NULL, v.start_time DESC, v.visit_id DESC "
        "LIMIT ? OFFSET ?", (patient_id, limit, offset))]


_LISTED = "SELECT v.*, s.name AS staff_name FROM visits v LEFT JOIN staff s USING(staff_id) WHERE v.patient_id=? "


def site_today():
    """Today's date at the site, whatever time zone this Mac is set to."""
    return datetime.now(SITE_TIME).date().isoformat()


def upcoming(db, patient_id, today, *, limit=3):
    """The next planned visits from ``today`` on, soonest first; an overdue plan is not a next visit."""
    return [dict(row) for row in db.execute(
        _LISTED + f"AND v.status='planned' AND v.visit_date>=? ORDER BY v.visit_date, {_IN_DAY} LIMIT ?",
        (patient_id, today, limit))]


def next_planned(db, today):
    """Each patient's next planned visit from ``today`` on, as ``{patient_id: {"visit_date", "start_time"}}``."""
    return {row["patient_id"]: {"visit_date": row["visit_date"], "start_time": row["start_time"]} for row in db.execute(
        "SELECT patient_id, visit_date, start_time FROM (SELECT v.patient_id, v.visit_date, v.start_time, "
        f"ROW_NUMBER() OVER (PARTITION BY v.patient_id ORDER BY v.visit_date, {_IN_DAY}) AS n FROM visits v "
        "WHERE v.status='planned' AND v.visit_date>=?) WHERE n=1", (today,))}


def done(db, patient_id, *, limit=3):
    """The latest visits that took place, newest first."""
    return [dict(row) for row in db.execute(
        _LISTED + "AND v.status='done' ORDER BY v.visit_date DESC, v.start_time IS NULL, v.start_time DESC, "
        "v.visit_id DESC LIMIT ?", (patient_id, limit))]


def started_at(visit) -> int:
    """When a visit began, as epoch seconds; a visit without a start time counts from the start of its day."""
    return int(datetime.fromisoformat(f"{visit['visit_date']}T{visit['start_time'] or '00:00'}").replace(
        tzinfo=SITE_TIME).timestamp())


def _day(value):
    if not calendar_date(value):
        raise ValueError("bad_request")
    return value


def for_day(db, visit_date):
    """Every visit on one date, in time order, with the patient's name, address and caution note for the day."""
    return [dict(row) for row in db.execute(
        "SELECT v.*, s.name AS staff_name, p.name AS patient_name, p.address, p.residence, p.caution_note "
        "FROM visits v JOIN patients p USING(patient_id) LEFT JOIN staff s USING(staff_id) WHERE v.visit_date=? "
        f"ORDER BY {_IN_DAY}", (_day(visit_date),))]


def week(db, first_day, *, days=7):
    """How many visits are planned, done and cancelled on each of ``days`` dates from ``first_day``."""
    start = date.fromisoformat(_day(first_day))
    if (date.max - start).days < days - 1:
        raise ValueError("bad_request")
    dates = [(start + timedelta(days=n)).isoformat() for n in range(days)]
    counts = {day: dict.fromkeys(STATUSES, 0) for day in dates}
    for row in db.execute("SELECT visit_date, status, COUNT(*) AS n FROM visits WHERE visit_date BETWEEN ? AND ? "
                          "GROUP BY visit_date, status", (dates[0], dates[-1])):
        counts[row["visit_date"]][row["status"]] = row["n"]
    return [{"date": day, **counts[day]} for day in dates]
