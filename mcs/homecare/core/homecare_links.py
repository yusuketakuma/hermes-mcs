"""Human-confirmed links between pharmacy patients and MCS patient rooms, kept as an append-only history."""
from __future__ import annotations

import time

from mcs_util import search_fold
from homecare_records import get, require_actor
from homecare_store import Conflict, audit, transaction


def current(db):
    """{patient_id: project_id} for every link whose latest row is a link."""
    rows = db.execute("""SELECT l.patient_id,l.project_id,l.action FROM patient_mcs_links l
        JOIN (SELECT patient_id,project_id,max(link_id) AS link_id FROM patient_mcs_links
              GROUP BY patient_id,project_id) latest USING(link_id)""")
    return {row["patient_id"]: row["project_id"] for row in rows if row["action"] == "link"}


def project_of(db, patient_id):
    return current(db).get(patient_id)


def patient_of(db, project_id):
    return next((patient for patient, project in current(db).items() if project == project_id), None)


def link(db, patient_id, project_id, *, actor, reason=""):
    """One pharmacy patient ↔ one MCS room; a name match alone never creates a link."""
    _check(project_id, reason)
    with transaction(db):
        require_actor(db, actor)
        get(db, "patient", patient_id)
        links = current(db)
        if patient_id in links or project_id in links.values():
            raise Conflict("already_linked")
        _record(db, patient_id, project_id, "link", actor, reason)


def unlink(db, patient_id, project_id, *, actor, reason=""):
    _check(project_id, reason)
    with transaction(db):
        require_actor(db, actor)
        if current(db).get(patient_id) != project_id:
            raise Conflict("not_linked")
        _record(db, patient_id, project_id, "unlink", actor, reason)


def history(db, patient_id):
    return [dict(row) for row in db.execute(
        "SELECT link_id,project_id,action,reason,actor_staff_id,at FROM patient_mcs_links "
        "WHERE patient_id=? ORDER BY link_id DESC", (patient_id,))]


def candidates(db, patient_id, mcs_patients):
    """MCS rooms whose patient name matches after folding; suggestions for a person to confirm."""
    name = search_fold("".join(get(db, "patient", patient_id)["name"].split()))
    taken = set(current(db).values())
    return [{"project_id": row["project_id"], "patient_name": row["patient_name"]}
            for row in mcs_patients
            if row["project_id"] not in taken and row.get("patient_name")
            and search_fold("".join(row["patient_name"].split())) == name]


def _check(project_id, reason):
    if type(project_id) is not int or project_id <= 0:
        raise ValueError("bad_project")
    if not isinstance(reason, str) or len(reason) > 200:
        raise ValueError("bad_reason")


def _record(db, patient_id, project_id, action, actor, reason):
    cursor = db.execute("INSERT INTO patient_mcs_links(patient_id,project_id,action,reason,actor_staff_id,at) "
                        "VALUES(?,?,?,?,?,?)", (patient_id, project_id, action, reason.strip(), actor, time.time()))
    audit(db, actor, f"patient.{action}_mcs", patient_id, None,
          {"link_id": cursor.lastrowid, "project_id": project_id, "reason": reason.strip()})
