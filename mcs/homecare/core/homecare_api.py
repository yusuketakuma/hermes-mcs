"""The core's JSON routes under api/hc/: reads, and writes that need an actor and a version.

Module routes will live under api/hc/<module>/ and reach core records only through the core's functions.
"""
from __future__ import annotations

import homecare_links as links
import homecare_records as records
import homecare_team as team

# Fixed, non-clinical codes the browser may see.
ERRORS = frozenset({"version_conflict", "already_linked", "not_linked", "already_assigned", "actor_required",
                    "admin_required", "not_qualified", "role_not_allowed", "still_assigned", "invalid_fields", "not_found",
                    "project_not_allowed", "bad_request"})


def read(db, route, params, *, registry, mcs_patients):
    """GET api/hc/<route>; ``mcs_patients`` lists the MCS rooms inside the launch scope."""
    if route == "meta":
        return registry.describe()
    if route == "staff":
        return {"items": records.staff_list(db)}
    if route == "patients":
        linked = links.current(db)
        return {"items": [{**row, "project_id": linked.get(row["patient_id"])}
                          for row in records.patient_list(db, query=params.get("q", ""))]}
    if route == "patient":
        patient_id = _id(params.get("patient"))
        row = records.get(db, "patient", patient_id)
        scope = {row["project_id"]: row for row in mcs_patients()}
        project = links.project_of(db, patient_id)
        return {"patient": row,
                "mcs": {"project_id": project, "patient_name": scope[project]["patient_name"]}
                if project in scope else {"project_id": project, "patient_name": None},
                "history": links.history(db, patient_id),
                "team": team.current(db, patient_id),
                "candidates": [] if project else links.candidates(db, patient_id, scope.values())}
    if route == "linked":
        project = _id(params.get("project"))
        if project not in {row["project_id"] for row in mcs_patients()}:
            raise ValueError("project_not_allowed")
        patient = links.patient_of(db, project)
        return {"patient": None if patient is None else records.get(db, "patient", patient)}
    raise ValueError("bad_request")


def write(db, op, body, *, registry, allowed, mcs_patients):
    """POST api/hc/<op>; the body names its actor, and updates carry the version they edit.

    A new link must name an MCS room in the current snapshot and scope; an unlink only needs the scope, so a
    link to a room that has since left the snapshot can still be corrected.
    """
    if not isinstance(body, dict):
        raise ValueError("bad_request")
    actor = body.get("actor")
    if op in ("staff.create", "patient.create"):
        return {"record": records.create(db, op.split(".")[0], body.get("values"), actor=actor, registry=registry)}
    if op in ("staff.update", "patient.update"):
        kind = op.split(".")[0]
        return {"record": records.update(db, kind, _id(body.get("id")), body.get("values"),
                                         expected_version=body.get("expected_version"), actor=actor,
                                         registry=registry)}
    if op == "team.assign":
        role = body.get("role")
        if not isinstance(role, str):
            raise ValueError("bad_request")
        return {"assignment_id": team.assign(db, registry, _id(body.get("patient")), _id(body.get("staff")), role,
                                             actor=actor)}
    if op == "team.end":
        team.end(db, registry, _id(body.get("assignment")), actor=actor)
        return {"ok": True}
    if op in ("link", "unlink"):
        project = _id(body.get("project"))
        if allowed is not None and project not in allowed \
                or op == "link" and project not in {row["project_id"] for row in mcs_patients()}:
            raise ValueError("project_not_allowed")
        getattr(links, op)(db, _id(body.get("patient")), project, actor=actor, reason=body.get("reason", ""))
        return {"ok": True}
    raise ValueError("bad_request")


def error_body(exc):
    """The browser-safe error for an exception a route raised."""
    if isinstance(exc, records.InvalidFields):
        return {"error": "invalid_fields", "fields": exc.fields}
    code = str(exc)
    return {"error": code if code in ERRORS else "bad_request"}


def _id(value):
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ValueError("bad_request")
    return value
