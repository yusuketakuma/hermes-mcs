"""The pharmacy module's routes under api/hc/pharmacy/, reached through the core's dispatcher."""
from __future__ import annotations

from homecare_records import page, record_id as _id
import pharmacy_prescriptions as prescriptions


def read(db, route, params):
    if route == "prescriptions":
        patient_id = _id(params.get("patient"))
        return page(lambda limit: prescriptions.for_patient(db, patient_id, limit=limit), params, size=20)
    raise ValueError("bad_request")


def write(db, op, body, *, actor):
    if op == "prescription.create":
        return {"record": prescriptions.create(db, body.get("values"), actor=actor)}
    if op == "prescription.update":
        return {"record": prescriptions.update(db, _id(body.get("id")), body.get("values"),
                                               expected_version=body.get("expected_version"), actor=actor)}
    raise ValueError("bad_request")


def sheet(db, patient_id):
    """The pharmacy part of the visit sheet: the latest prescriptions."""
    return {"label": "最近の処方", "prescriptions": prescriptions.for_patient(db, patient_id, limit=3)}

