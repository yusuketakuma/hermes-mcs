"""The pharmacy module's prescription history: a prescription and its drug lines, versioned and audited."""
from __future__ import annotations

import re
import time

from homecare_records import InvalidFields, calendar_date, get, require_actor, text_field
from homecare_store import Conflict, audit, transaction

FIELDS = ("patient_id", "issued_on", "institution", "prescriber", "note", "lines")
LINE_FIELDS = ("drug_name", "code_system", "drug_code", "dose", "usage", "days")
MAX_LINES = 30
_CODE = re.compile(r"^[0-9A-Za-z]{1,20}$")
_SYSTEM = re.compile(r"^[a-z][a-z0-9_]{0,19}$")


def _clean(db, values):
    bad = [key for key in values if key not in FIELDS]
    row = {"institution": text_field(values, bad, "institution", 100),
           "prescriber": text_field(values, bad, "prescriber", 60), "note": text_field(values, bad, "note", 500)}
    row["patient_id"] = values.get("patient_id")
    if type(row["patient_id"]) is not int or db.execute(
            "SELECT 1 FROM patients WHERE patient_id=?", (row["patient_id"],)).fetchone() is None:
        bad.append("patient_id")
    row["issued_on"] = values.get("issued_on")
    if not calendar_date(row["issued_on"]):
        bad.append("issued_on")
    lines = values.get("lines", [])
    cleaned = []
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        bad.append("lines")
        lines = []
    for index, line in enumerate(lines, 1):
        cleaned.append(_clean_line(line, bad, f"lines.{index}"))
    if bad:
        raise InvalidFields(bad)
    return row, cleaned


def _clean_line(line, bad, where):
    if not isinstance(line, dict) or any(key not in LINE_FIELDS for key in line):
        bad.append(where)
        return {}
    errors = []
    row = {"drug_name": text_field(line, errors, "drug_name", 100, required=True),
           "dose": text_field(line, errors, "dose", 60), "usage": text_field(line, errors, "usage", 100)}
    system, code = line.get("code_system") or None, line.get("drug_code") or None
    # A master code is kept with the system it belongs to, or not at all.
    if (system is None) != (code is None) or system is not None and (
            not isinstance(system, str) or not _SYSTEM.match(system) or not isinstance(code, str)
            or not _CODE.match(code)):
        errors.append("drug_code")
    row["code_system"], row["drug_code"] = system, code
    row["days"] = line.get("days")
    if row["days"] is not None and (type(row["days"]) is not int or not 1 <= row["days"] <= 365):
        errors.append("days")
    bad.extend(f"{where}.{field}" for field in errors)
    return row


def prescription(db, prescription_id):
    row = db.execute("SELECT * FROM pharmacy_prescriptions WHERE prescription_id=?", (prescription_id,)).fetchone()
    if row is None:
        raise ValueError("not_found")
    return {**dict(row), "lines": [dict(line) for line in db.execute(
        f"SELECT {','.join(LINE_FIELDS)} FROM pharmacy_prescription_lines WHERE prescription_id=? ORDER BY position",
        (prescription_id,))]}


def _write_lines(db, prescription_id, lines):
    db.execute("DELETE FROM pharmacy_prescription_lines WHERE prescription_id=?", (prescription_id,))
    db.executemany(f"INSERT INTO pharmacy_prescription_lines(prescription_id,position,{','.join(LINE_FIELDS)}) "
                   f"VALUES(?,?,{','.join('?' * len(LINE_FIELDS))})",
                   [(prescription_id, position, *(line[field] for field in LINE_FIELDS))
                    for position, line in enumerate(lines, 1)])


def create(db, values, *, actor):
    if not isinstance(values, dict):
        raise InvalidFields(["values"])
    with transaction(db):
        require_actor(db, actor)
        row, lines = _clean(db, values)
        now = time.time()
        prescription_id = db.execute(
            f"INSERT INTO pharmacy_prescriptions({','.join(row)},created_at,updated_at) VALUES"
            f"({','.join('?' * len(row))},?,?)", (*row.values(), now, now)).lastrowid
        _write_lines(db, prescription_id, lines)
        created = prescription(db, prescription_id)
        audit(db, actor, "pharmacy.prescription.create", prescription_id, None, created)
    return created


def update(db, prescription_id, values, *, expected_version, actor):
    """Replace a prescription's fields and lines; it stays with the patient it was recorded for."""
    if not isinstance(values, dict) or type(expected_version) is not int or "patient_id" in values:
        raise InvalidFields(["values"])
    with transaction(db):
        require_actor(db, actor)
        before = prescription(db, prescription_id)
        if before["version"] != expected_version:
            raise Conflict("version_conflict")
        row, lines = _clean(db, {field: before[field] for field in FIELDS} | values)
        db.execute(f"UPDATE pharmacy_prescriptions SET {','.join(f'{field}=?' for field in row)},"
                   "version=version+1,updated_at=? WHERE prescription_id=? AND version=?",
                   (*row.values(), time.time(), prescription_id, expected_version))
        _write_lines(db, prescription_id, lines)
        after = prescription(db, prescription_id)
        audit(db, actor, "pharmacy.prescription.update", prescription_id, before, after)
    return after


def for_patient(db, patient_id, *, limit=20):
    """A patient's prescriptions, newest first, with their lines."""
    get(db, "patient", patient_id)
    return [prescription(db, row["prescription_id"]) for row in db.execute(
        "SELECT prescription_id FROM pharmacy_prescriptions WHERE patient_id=? "
        "ORDER BY issued_on DESC, prescription_id DESC LIMIT ?", (patient_id, limit))]
