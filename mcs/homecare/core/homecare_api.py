"""The core's JSON routes under api/hc/: reads, and writes that need an actor and a version.

Module routes will live under api/hc/<module>/ and reach core records only through the core's functions.
"""
from __future__ import annotations

from datetime import date

import homecare_links as links
import homecare_records as records
import homecare_route as route
import homecare_team as team
import homecare_visits as visits
from homecare_records import record_id as _id

# Fixed, non-clinical codes the browser may see.
ERRORS = frozenset({"version_conflict", "already_linked", "not_linked", "already_assigned", "actor_required",
                    "admin_required", "not_qualified", "role_not_allowed", "still_assigned", "team_changed", "invalid_fields", "not_found",
                    "project_not_allowed", "route_unavailable", "route_failed",
                    "too_many_stops", "bad_request"})


def read(db, route, params, *, registry, mcs_patients):
    """GET api/hc/<route>; ``mcs_patients`` lists the MCS rooms inside the launch scope."""
    module, _, sub_route = route.partition("/")
    if sub_route:
        return registry.module(module).api.read(db, sub_route, params)
    if route == "visits":
        if params.get("date"):
            return {"items": visits.for_day(db, params["date"])}
        patient_id = _id(params.get("patient"))
        return records.page(lambda limit: visits.for_patient(db, patient_id, limit=limit), params, size=50)
    if route == "sheet":
        return sheet(db, _id(params.get("patient")), registry=registry, mcs_patients=mcs_patients)
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


def write(db, op, body, *, registry, allowed, mcs_patients, route_settings=None):
    """POST api/hc/<op>; the body names its actor, and updates carry the version they edit.

    A new link must name an MCS room in the current snapshot and scope; an unlink only needs the scope, so a
    link to a room that has since left the snapshot can still be corrected.
    """
    if not isinstance(body, dict):
        raise ValueError("bad_request")
    actor = body.get("actor")
    module, _, sub_op = op.partition("/")
    if sub_op:
        return registry.module(module).api.write(db, sub_op, body, actor=actor)
    if op == "route.plan":
        # Sends addresses out, so it is a deliberate POST with an actor, never a page load.
        staff = body.get("staff")
        return route.plan(db, route_settings, body.get("date"), actor=actor,
                          staff_id=None if staff in (None, "") else _id(staff))
    if op == "visit.create":
        return {"record": visits.create(db, registry, body.get("values"), actor=actor)}
    if op == "visit.update":
        return {"record": visits.update(db, registry, _id(body.get("id")), body.get("values"),
                                        expected_version=body.get("expected_version"), actor=actor)}
    if op in ("staff.create", "patient.create"):
        return {"record": records.create(db, op.split(".")[0], body.get("values"), actor=actor, registry=registry)}
    if op in ("staff.update", "patient.update"):
        kind = op.split(".")[0]
        return {"record": records.update(db, kind, _id(body.get("id")), body.get("values"),
                                         expected_version=body.get("expected_version"), actor=actor,
                                         registry=registry)}
    if op == "team.assign":
        role, holders = body.get("role"), body.get("holders")
        if not isinstance(role, str) or not isinstance(holders, list):
            raise ValueError("bad_request")
        return {"assignment_id": team.assign(db, registry, _id(body.get("patient")), _id(body.get("staff")), role,
                                             expected_holders=[_id(holder) for holder in holders], actor=actor)}
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


def sheet(db, patient_id, *, registry, mcs_patients, today=None):
    """What to have in hand on a visit: the record, care team, recent and next visits, each module's part and
    the linked MCS room when it is inside the launch scope."""
    patient = records.get(db, "patient", patient_id)
    project = links.project_of(db, patient_id)
    scope = {row["project_id"]: row for row in mcs_patients()}
    return {"patient": patient, "team": team.current(db, patient_id),
            "next_visits": visits.upcoming(db, patient_id, today or date.today().isoformat()),
            "recent_visits": visits.done(db, patient_id),
            "modules": {module.name: module.api.sheet(db, patient_id)
                        for module in registry.modules if module.api is not None},
            "mcs": {"project_id": project, "patient_name": scope[project]["patient_name"]}
            if project in scope else {"project_id": None, "patient_name": None}}


def error_body(exc):
    """The browser-safe error for an exception a route raised."""
    if isinstance(exc, records.InvalidFields):
        return {"error": "invalid_fields", "fields": exc.fields}
    code = str(exc)
    return {"error": code if code in ERRORS else "bad_request"}

