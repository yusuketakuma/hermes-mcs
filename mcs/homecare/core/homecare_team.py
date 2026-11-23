"""A patient's care team: which staff member holds which module-declared role, with its history."""
from __future__ import annotations

import time

from homecare_records import get, qualifications, require_actor
from homecare_store import Conflict, audit, transaction


def current(db, patient_id):
    return [dict(row) for row in db.execute(
        "SELECT t.assignment_id,t.role,t.staff_id,s.name AS staff_name,t.started_at FROM care_team t "
        "JOIN staff s USING(staff_id) WHERE t.patient_id=? AND t.ended_at IS NULL ORDER BY t.role,t.started_at",
        (patient_id,))]


def assign(db, registry, patient_id, staff_id, role, *, expected_assignments, actor):
    """Give ``role`` to a qualified, active staff member; a single-holder role hands over only from the
    assignments the client saw (by assignment, so A → B → A since then is still a change)."""
    spec = registry.roles.get(role)
    if spec is None:
        raise ValueError("role_not_allowed")
    with transaction(db):
        require_actor(db, actor)
        get(db, "patient", patient_id)
        if not get(db, "staff", staff_id)["active"] or not set(spec.requires) & set(qualifications(db, staff_id)):
            raise ValueError("not_qualified")
        holders = [row for row in current(db, patient_id) if row["role"] == role]
        if any(row["staff_id"] == staff_id for row in holders):
            raise Conflict("already_assigned")
        if spec.single and sorted(row["assignment_id"] for row in holders) != sorted(expected_assignments):
            # Someone else handed the role over since the client loaded the team; never end a holder it did not see.
            raise Conflict("team_changed")
        now = time.time()
        for row in holders if spec.single else ():
            _end(db, row["assignment_id"], actor, now)
        assignment = db.execute("INSERT INTO care_team(patient_id,staff_id,role,started_at,started_by) "
                                "VALUES(?,?,?,?,?)", (patient_id, staff_id, role, now, actor)).lastrowid
        audit(db, actor, "care_team.assign", patient_id, None,
              {"assignment_id": assignment, "role": role, "staff_id": staff_id})
    return assignment


def end(db, registry, assignment_id, *, actor):
    with transaction(db):
        require_actor(db, actor)
        row = db.execute("SELECT * FROM care_team WHERE assignment_id=? AND ended_at IS NULL",
                         (assignment_id,)).fetchone()
        if row is None:
            raise ValueError("not_found")
        if row["role"] not in registry.roles:
            # A disabled module's assignments stay as they are until the module is enabled again.
            raise ValueError("role_not_allowed")
        _end(db, assignment_id, actor, time.time())


def _end(db, assignment_id, actor, now):
    row = dict(db.execute("SELECT * FROM care_team WHERE assignment_id=?", (assignment_id,)).fetchone())
    db.execute("UPDATE care_team SET ended_at=?,ended_by=? WHERE assignment_id=? AND ended_at IS NULL",
               (now, actor, assignment_id))
    audit(db, actor, "care_team.end", row["patient_id"], row, {**row, "ended_at": now, "ended_by": actor})
