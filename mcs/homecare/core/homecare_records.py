"""Staff and patient register commands with field validation, version checks and an audit trail."""
from __future__ import annotations

from datetime import date
import re
import time

from mcs_util import search_fold
from homecare_store import Conflict, audit, transaction

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PHONE = re.compile(r"^[0-9+\-() ]*$")
_POSTAL = re.compile(r"^(\d{3}-?\d{4})?$")

STAFF_FIELDS = ("name", "kana", "tailscale_login", "admin", "active", "qualifications")
PATIENT_FIELDS = {
    "name": 60, "kana": 60, "birth_date": None, "postal_code": 8, "address": 200, "residence": 100,
    "phone": 20, "keyperson_name": 60, "keyperson_relation": 20, "keyperson_phone": 20,
    "preferred_contact": None, "contact_phone": 20, "contact_note": 200, "access_note": 500,
    "caution_note": 500, "status": None}
CONTACTS = ("patient", "keyperson", "facility", "other")
STATUSES = ("active", "suspended", "ended")


def calendar_date(value):
    """True for a ``YYYY-MM-DD`` string naming a real day (so 2026-02-31 is refused, 2028-02-29 kept)."""
    if not isinstance(value, str) or not _DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class InvalidFields(ValueError):
    """Input the register rejects; ``fields`` names each field to correct."""

    def __init__(self, fields):
        super().__init__("invalid_fields")
        self.fields = sorted(set(fields))


def unknown_fields(values, fields):
    """``["values"]`` when ``values`` has a key outside ``fields``: an unknown key is never echoed back."""
    return ["values"] if any(key not in fields for key in values) else []


def text_field(values, bad, field, limit, *, required=False):
    """``values[field]`` stripped, or "" with ``field`` added to ``bad`` when it is not text within ``limit``."""
    value = values.get(field, "")
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value.strip()) > limit or required and not value.strip():
        bad.append(field)
        return ""
    return value.strip()


def _clean_staff(values, registry, held):
    """``held`` keeps qualifications of a module that is not enabled right now from being refused."""
    bad = unknown_fields(values, STAFF_FIELDS)
    row = {"name": text_field(values, bad, "name", 60, required=True), "kana": text_field(values, bad, "kana", 60)}
    login = values.get("tailscale_login")
    if login in (None, ""):
        row["tailscale_login"] = None
    elif not isinstance(login, str) or not 3 <= len(login.strip()) <= 254 or " " in login.strip():
        bad.append("tailscale_login")
    else:
        row["tailscale_login"] = login.strip().lower()
    for flag, default in (("admin", False), ("active", True)):
        row[flag] = values.get(flag, default)
        if type(row[flag]) is not bool:
            bad.append(flag)
    codes = values.get("qualifications", [])
    if not isinstance(codes, list) or not codes or len(set(map(str, codes))) != len(codes) \
            or any(not isinstance(code, str) or code not in registry.qualifications and code not in held
                   for code in codes):
        bad.append("qualifications")
    if bad:
        raise InvalidFields(bad)
    return {**row, "admin": int(row["admin"]), "active": int(row["active"])}, sorted(codes)


def _clean_patient(values):
    bad = unknown_fields(values, PATIENT_FIELDS)
    row = {field: text_field(values, bad, field, limit, required=field == "name")
           for field, limit in PATIENT_FIELDS.items() if limit}
    for field in ("phone", "keyperson_phone", "contact_phone"):
        if not _PHONE.match(row[field]):
            bad.append(field)
    if not _POSTAL.match(row["postal_code"]):
        bad.append("postal_code")
    birth = values.get("birth_date") or None
    if birth is not None and not calendar_date(birth):
        bad.append("birth_date")
    row["birth_date"] = birth
    row["preferred_contact"] = values.get("preferred_contact", "patient")
    if row["preferred_contact"] not in CONTACTS:
        bad.append("preferred_contact")
    else:
        # The preferred contact must be reachable by phone (same rule as the pharmacy board it replaces).
        required = {"patient": ("phone",), "keyperson": ("keyperson_name", "keyperson_relation", "keyperson_phone")}
        bad.extend(field for field in required.get(row["preferred_contact"], ("contact_phone",))
                   if not row[field] or field.endswith("phone") and not any(c.isdigit() for c in row[field]))
    row["status"] = values.get("status", "active")
    if row["status"] not in STATUSES:
        bad.append("status")
    if bad:
        raise InvalidFields(bad)
    return row


def page(fetch, params, *, size):
    """One page of a newest-first history as ``{"items", "more"}``; ``params["offset"]`` skips the entries the
    screen already shows, so every stored entry stays in reach however long the history grows."""
    offset = params.get("offset", 0)
    if isinstance(offset, str) and offset.isdigit():
        offset = int(offset)
    if type(offset) is not int or not 0 <= offset <= 10**9:
        raise ValueError("bad_request")
    rows = fetch(size + 1, offset)
    return {"items": rows[:size], "more": len(rows) > size}


def record_id(value):
    """A positive integer id from JSON or a query string; anything else is a bad request."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ValueError("bad_request")
    return value


def require_actor(db, actor, *, admin=False):
    """The actor must be active staff; ``admin`` also needs the core administration right."""
    row = db.execute("SELECT admin FROM staff WHERE staff_id=? AND active=1",
                     (actor,)).fetchone() if type(actor) is int else None
    if row is None:
        raise ValueError("actor_required")
    if admin and not row["admin"]:
        raise ValueError("admin_required")


def staff_for_login(db, login):
    """The active staff member a Tailscale login belongs to: ``{"staff_id", "name", "admin"}``, or None."""
    if not isinstance(login, str) or not login.strip():
        return None
    row = db.execute("SELECT staff_id, name, admin FROM staff WHERE tailscale_login=? AND active=1",
                     (login.strip().lower(),)).fetchone()
    return None if row is None else {**dict(row), "admin": bool(row["admin"])}


def any_staff(db):
    return db.execute("SELECT 1 FROM staff").fetchone() is not None


def get(db, kind, record_id):
    table, key = ("staff", "staff_id") if kind == "staff" else ("patients", "patient_id")
    row = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (record_id,)).fetchone()
    if row is None:
        raise ValueError("not_found")
    row = dict(row)
    if kind == "staff":
        row["qualifications"] = qualifications(db, record_id)
    return row


def qualifications(db, staff_id):
    return [row["code"] for row in db.execute(
        "SELECT code FROM staff_qualifications WHERE staff_id=? ORDER BY code", (staff_id,))]


def create(db, kind, values, *, actor, registry):
    if not isinstance(values, dict):
        raise InvalidFields(["values"])
    with transaction(db):
        first = kind == "staff" and db.execute("SELECT 1 FROM staff").fetchone() is None
        if not first:
            # Only the very first staff member is registered without an actor, and becomes the administrator.
            require_actor(db, actor, admin=kind == "staff")
        now = time.time()
        if kind == "staff":
            row, codes = _clean_staff(values | ({"admin": True, "active": True} if first else {}), registry, ())
            _unique_login(db, row, None)
            created_id = _insert(db, "staff", row, now)
            _set_qualifications(db, created_id, codes)
        else:
            created_id = _insert(db, "patients", _clean_patient(values), now)
        created = get(db, kind, created_id)
        audit(db, created_id if first else actor, f"{kind}.create", created_id, None, created)
    return created


def update(db, kind, record_id, values, *, expected_version, actor, registry):
    if not isinstance(values, dict) or type(expected_version) is not int:
        raise InvalidFields(["values"])
    with transaction(db):
        require_actor(db, actor, admin=kind == "staff")
        before = get(db, kind, record_id)
        if before["version"] != expected_version:
            raise Conflict("version_conflict")
        if kind == "staff":
            current = {field: before[field] for field in STAFF_FIELDS} | {
                "admin": bool(before["admin"]), "active": bool(before["active"])}
            row, codes = _clean_staff(current | values, registry, before["qualifications"])
            _unique_login(db, row, record_id)
            if record_id == actor:
                # Nobody locks themselves out of the register, by role or by the login that identifies them.
                locked = [field for field in ("active", "admin") if not row[field]]
                # A first login may be added (that is how an existing administrator prepares for resident mode);
                # one already set is changed by another administrator.
                if before["tailscale_login"] and row["tailscale_login"] != before["tailscale_login"]:
                    locked.append("tailscale_login")
                if locked:
                    raise InvalidFields(locked)
            _require_assignments_still_valid(db, registry, record_id, row["active"], codes)
            _set_qualifications(db, record_id, codes)
            table, key = "staff", "staff_id"
        else:
            row = _clean_patient({field: before[field] for field in PATIENT_FIELDS} | values)
            table, key = "patients", "patient_id"
        db.execute(f"UPDATE {table} SET {','.join(f'{field}=?' for field in row)},version=version+1,updated_at=? "
                   f"WHERE {key}=? AND version=?", (*row.values(), time.time(), record_id, expected_version))
        after = get(db, kind, record_id)
        audit(db, actor, f"{kind}.update", record_id, before, after)
    return after


def _require_assignments_still_valid(db, registry, staff_id, active, codes):
    """Hand a patient over first: leaving, or losing the qualification a held role needs, is refused."""
    for row in db.execute("SELECT DISTINCT role FROM care_team WHERE staff_id=? AND ended_at IS NULL", (staff_id,)):
        spec = registry.roles.get(row["role"])
        # A disabled module's roles cannot be checked and stay as they are, like its qualifications.
        if spec is not None and not (active and set(spec.requires) & set(codes)):
            raise ValueError("still_assigned")


def _insert(db, table, row, now):
    return db.execute(f"INSERT INTO {table}({','.join(row)},created_at,updated_at) VALUES"
                      f"({','.join('?' * len(row))},?,?)", (*row.values(), now, now)).lastrowid


def _set_qualifications(db, staff_id, codes):
    db.execute("DELETE FROM staff_qualifications WHERE staff_id=?", (staff_id,))
    db.executemany("INSERT INTO staff_qualifications(staff_id,code) VALUES(?,?)", [(staff_id, code) for code in codes])


def _unique_login(db, row, record_id):
    if row["tailscale_login"] and db.execute(
            "SELECT 1 FROM staff WHERE tailscale_login=? AND staff_id IS NOT ?",
            (row["tailscale_login"], record_id)).fetchone():
        raise InvalidFields(["tailscale_login"])


def staff_list(db):
    return [{**dict(row), "qualifications": qualifications(db, row["staff_id"])}
            for row in db.execute("SELECT * FROM staff ORDER BY active DESC, kana, name, staff_id")]


def patient_list(db, *, query=""):
    if not isinstance(query, str) or len(query) > 100:
        raise ValueError("bad_query")
    rows = db.execute("SELECT * FROM patients ORDER BY status='ended', kana, name, patient_id").fetchall()
    terms = [search_fold(term) for term in query.split()]
    return [dict(row) for row in rows
            if all(term in search_fold(row["name"] + " " + row["kana"]) or term == str(row["patient_id"])
                   for term in terms)]
