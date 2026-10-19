"""Validate registered MCS datasets and resolve their scope from borrowed inventory reads."""
from __future__ import annotations

import hashlib
import json
import math
from typing import TypeAlias, TypedDict

from mcs_contract import SchemaError, valid_id


DATASETS = ("care_team", "medication_periods", "observation_items",
            "observation_values", "consultations")



CLINICAL_DATASETS = ("medication_periods", "observation_items", "observation_values")



SCOPED_DATASETS = ("consultations", *CLINICAL_DATASETS)



FETCH_ERROR_KINDS = frozenset({"schema_error", "http_error", "forbidden", "session_expired",
                               "network_error", "deadline_exceeded", "capacity_limit"})



PARTIAL_REASONS = ("snapshot_missing", "page_limit")



ARTIFACT_KIND = "project_metadata_v1"



MAX_ROWS = 250



MAX_VALUE_ROWS = 10000



def accepted_reasons(dataset: str) -> frozenset:
    """Every failure reason an attempt artifact of `dataset` may carry; readers reject the rest.

    Stored artifacts outlive the writer, so a reason is only ever added
    here — removing one would make already-written attempts unreadable."""
    return FETCH_ERROR_KINDS | {"fetch_error", *PARTIAL_REASONS} | (
        {"scope_changed"} if dataset in SCOPED_DATASETS else set())



def attempt_state(dataset: str, complete: bool, reason) -> str:
    """complete | partial | failed for one fetch attempt."""
    if complete:
        return "complete"
    return "partial" if dataset == "consultations" and reason in PARTIAL_REASONS else "failed"



JSON: TypeAlias = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]



class MetadataFetch(TypedDict):
    complete: bool
    rows: list[dict[str, JSON]]
    pages: int
    timestamp: int | None
    reason: str | None
    http_status: int | None
    definition: dict[str, JSON] | None



class MetadataTarget(TypedDict):
    scope: str
    entity_id: int



REFERENCE_FIELDS = tuple(
    f"{edge}_reference_limit_{component}"
    for edge in ("upper", "lower")
    for component in ("scalar", "max", "min", "left", "right"))



def _string(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 600:
        raise SchemaError("project metadata: text invalid")
    return value



def _number(value) -> int | float | None:
    if value is None:
        return None
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise SchemaError("project metadata: number invalid")
    return value



def _boolean(value) -> bool | None:
    if value is not None and type(value) is not bool:
        raise SchemaError("project metadata: boolean invalid")
    return value



def collection_rows(value, cap: int = MAX_ROWS) -> list[dict[str, JSON]]:
    if (not isinstance(value, list) or len(value) > cap
            or any(not isinstance(row, dict) for row in value)):
        raise SchemaError("project metadata: collection invalid")
    return value



def _item(row) -> dict[str, JSON]:
    """Keep only the public client's lab-test definition fields."""
    if not isinstance(row, dict) or not valid_id(row.get("id")):
        raise SchemaError("project metadata: lab item invalid")
    return {"id": row["id"], **{key: _string(row.get(key)) for key in
            ("name", "analyte_tag", "input_type", "unit")}}



def normalize_rows(dataset: str, rows, *, retain_names: bool = False) -> list[dict[str, JSON]]:
    """Allowlist a raw collection; absent optional fields stay unknown (None)."""
    if dataset not in DATASETS or type(retain_names) is not bool:
        raise ValueError("invalid metadata normalization options")
    out = []
    for row in collection_rows(rows, MAX_VALUE_ROWS if dataset == "observation_values" else MAX_ROWS):
        if dataset == "care_team":
            if not valid_id(row.get("id")):
                raise SchemaError("project metadata: member id invalid")
            station = row.get("station")
            if station is not None and not isinstance(station, dict):
                raise SchemaError("project metadata: station invalid")
            cats = row.get("specialist_categories")
            professions: list[JSON] | None = (None if cats is None else
                [_string(c.get("name")) for c in collection_rows(cats, 50)])
            value: dict[str, JSON] = {"id": row["id"], "type": _string(row.get("type")),
                     "professions": professions,
                     "facility": _string(station.get("name")) if station is not None else None,
                     "is_director": _boolean(row.get("is_director")),
                     "is_self": _boolean(row.get("is_self"))}
            if retain_names:
                value["last_name"] = _string(row.get("last_name"))
                value["first_name"] = _string(row.get("first_name"))
        elif dataset == "medication_periods":
            medicines = []
            for medicine in collection_rows(row.get("medicine_informations"), 20):
                if not valid_id(medicine.get("id")):
                    raise SchemaError("project metadata: medicine id invalid")
                medicines.append({"id": medicine["id"], "name": _string(medicine.get("name"))})
            value = {"begin_date": _string(row.get("begin_date")),
                     "end_date": _string(row.get("end_date")),
                     "medicine_informations": medicines}
        elif dataset == "observation_items":
            value = {"lab_test_item": _item(row.get("lab_test_item")),
                     **{key: _number(row.get(key)) for key in REFERENCE_FIELDS}}
        elif dataset == "observation_values":
            value = {"observation_issued_at": _string(row.get("observation_issued_at")),
                     **{key: _number(row.get(key)) for key in
                        ("scalar", "max", "min", "left", "right")}}
        else:
            if not valid_id(row.get("id")):
                raise SchemaError("project metadata: consultation id invalid")
            value = {"id": row["id"], "purpose": _string(row.get("purpose")),
                     "status": _string(row.get("status")),
                     "is_unread": _boolean(row.get("is_unread"))}
        out.append(value)
    return out



def metadata_target(db, project_id: int, dataset: str) -> MetadataTarget | None:
    """Resolve only stored inventory evidence; no user-supplied patient/group links."""
    if not valid_id(project_id) or dataset not in DATASETS:
        raise ValueError("invalid metadata target")
    row = db.execute("SELECT project_type,karte_id FROM patients WHERE project_id=?",
                     (project_id,)).fetchone()
    if row is None:
        return None
    if dataset == "consultations":
        return {"scope": "group", "entity_id": project_id} if row["project_type"] == "group" else None
    if dataset == "care_team":
        return {"scope": "project", "entity_id": project_id}
    if row["project_type"] == "medical" and valid_id(row["karte_id"]):
        return {"scope": "karte", "entity_id": row["karte_id"]}
    return None



def clinical_definition_fingerprint(karte_id, items):
    """Prove equal normalized clinical definitions independently of fetch timestamps."""
    rows = normalize_rows("observation_items", list(items.values()))
    for row in rows:
        for field in REFERENCE_FIELDS:
            if type(row[field]) is float and row[field].is_integer():
                row[field] = int(row[field])
    return hashlib.sha256(json.dumps(["karte", karte_id, sorted(
        rows, key=lambda row: row["lab_test_item"]["id"])],
        sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()

