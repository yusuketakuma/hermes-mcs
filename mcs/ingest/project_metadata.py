#!/usr/bin/env python3
"""Opt-in bounded project/group metadata acquisition and local artifact sync.

Contract evidence: docs/roadmap/mcs-api-survey.md and official public chunks
HB7B2UZS / 3X6DIYSL (members), UHQUQDWW / 5QYZEKQD (medication),
M2MZSSUK (observation services at 23700..25300, value formatter at 180200),
4WFEVJDT / IGJY24TN (group consultation lists). These are client contracts,
not proof of server-side read/session preservation.

No scheduler, publication, extraction, patient-group association or new schema.
sync_metadata requires enabled=True and resolves project/karte from the ledger.
Names require separate owner opt-in; photos/contact fields are never retained.
CLI: project_metadata.py sync|view --database PATH --project-id ID --dataset NAME
     [--item-id ID] (sync additionally requires --read-only-get --token-cache PATH).
     care_team names need --retain-names (sync) and --show-names (view); both default off.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import TypeAlias, TypedDict

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import _mcs_path  # noqa: F401

from mcs_adapter import MCSError, SchemaError, _valid_id

DATASETS = ("care_team", "medication_periods", "observation_items",
            "observation_values", "consultations")
ARTIFACT_KIND = "project_metadata_v1"
MAX_ROWS = 250
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


def _rows(value, cap: int = MAX_ROWS) -> list[dict[str, JSON]]:
    if (not isinstance(value, list) or len(value) > cap
            or any(not isinstance(row, dict) for row in value)):
        raise SchemaError("project metadata: collection invalid")
    return value


def _item(row) -> dict[str, JSON]:
    """Keep only the public client's lab-test definition fields."""
    if not isinstance(row, dict) or not _valid_id(row.get("id")):
        raise SchemaError("project metadata: lab item invalid")
    return {"id": row["id"], **{key: _string(row.get(key)) for key in
            ("name", "analyte_tag", "input_type", "unit")}}


def normalize_rows(dataset: str, rows, *, retain_names: bool = False) -> list[dict[str, JSON]]:
    """Allowlist a raw collection; absent optional fields stay unknown (None)."""
    if dataset not in DATASETS or type(retain_names) is not bool:
        raise ValueError("invalid metadata normalization options")
    out = []
    for row in _rows(rows):
        if dataset == "care_team":
            if not _valid_id(row.get("id")):
                raise SchemaError("project metadata: member id invalid")
            station = row.get("station")
            if station is not None and not isinstance(station, dict):
                raise SchemaError("project metadata: station invalid")
            cats = row.get("specialist_categories")
            professions: list[JSON] | None = (None if cats is None else
                [_string(c.get("name")) for c in _rows(cats, 50)])
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
            for medicine in _rows(row.get("medicine_informations"), 20):
                if not _valid_id(medicine.get("id")):
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
            if not _valid_id(row.get("id")):
                raise SchemaError("project metadata: consultation id invalid")
            value = {"id": row["id"], "purpose": _string(row.get("purpose")),
                     "status": _string(row.get("status")),
                     "is_unread": _boolean(row.get("is_unread"))}
        out.append(value)
    return out


def fetch_metadata(get, dataset: str, entity_id: int, *, item_id: int | None = None,
                   max_pages: int = 5, per_page: int = 50,
                   retain_names: bool = False,
                   project_type: str | None = None) -> MetadataFetch:
    """Bounded GET walk; only a terminal consistent snapshot can be complete.

    Missing timestamp is tolerated for a single terminal page, never for a
    continued walk. No speculative pagination parameters on the singleton
    medication endpoint. Failed walks expose no partial clinical/identity rows.
    """
    if (dataset not in DATASETS or not _valid_id(entity_id)
            or (dataset == "consultations" and project_type != "group")
            or type(max_pages) is not int or not 1 <= max_pages <= 5
            or type(per_page) is not int or not 1 <= per_page <= 50
            or type(retain_names) is not bool
            or (dataset == "observation_values" and not _valid_id(item_id))
            or (dataset != "observation_values" and item_id is not None)):
        raise ValueError("invalid metadata fetch options")
    key = "users" if dataset == "care_team" else dataset
    root = "projects" if dataset in ("care_team", "consultations") else "kartes"
    tail = "members" if dataset == "care_team" else dataset
    if dataset == "observation_values":
        tail = f"observation_items/{item_id}/values"
    path = f"/{root}/{entity_id}/{tail}"
    result: MetadataFetch = {"complete": False, "rows": [], "pages": 0, "timestamp": None,
              "reason": None, "http_status": None, "definition": None}
    items, seen = [], set()
    total, total_pages = None, None
    try:
        for page in range(1, max_pages + 1):
            params: dict[str, int | str] = {} if dataset == "medication_periods" else {
                "page": page, "per_page": per_page, "include_paginate_totals": 0}
            if dataset == "care_team":
                params["user_type"] = "any"
            if dataset == "observation_items":
                params["include_all"] = 1
            if dataset == "observation_values" and page == 1:
                params["include_meta"] = 1
            if result["timestamp"] is not None:
                params["timestamp"] = result["timestamp"]
            raw = get(path, params, extend_session=False)
            result["pages"] = page
            rows = _rows(raw.get(key), MAX_ROWS if dataset == "medication_periods" else per_page)
            if dataset == "observation_values" and page == 1 and "observation_item" in raw:
                definitions = normalize_rows("observation_items", [raw["observation_item"]])
                definition_item = definitions[0].get("lab_test_item")
                if not isinstance(definition_item, dict) or definition_item.get("id") != item_id:
                    raise SchemaError("project metadata: value item mismatch")
                result["definition"] = definitions[0]
            if dataset == "medication_periods":
                if "paginate" in raw:
                    raise SchemaError("project metadata: unexpected medication pagination")
                items = normalize_rows(dataset, rows)
                result["complete"] = True
                break
            pag = raw.get("paginate")
            if not isinstance(pag, dict) or type(pag.get("has_next")) is not bool:
                raise SchemaError("project metadata: pagination invalid")
            has_next = pag["has_next"]
            for field, expected in (("current_page", page), ("per_page", per_page)):
                if field in pag and (type(pag[field]) is not int or pag[field] != expected):
                    raise SchemaError("project metadata: page mismatch")
            ts = pag.get("timestamp")
            if ts is not None and not _valid_id(ts):
                raise SchemaError("project metadata: timestamp invalid")
            if page == 1:
                result["timestamp"] = ts
            elif ts != result["timestamp"]:
                raise SchemaError("project metadata: snapshot changed")
            for field, previous in (("total_entries", total), ("total_pages", total_pages)):
                if field in pag:
                    if (type(pag[field]) is not int or pag[field] < 0
                            or (previous is not None and previous != pag[field])):
                        raise SchemaError("project metadata: totals changed")
            total = pag.get("total_entries", total)
            total_pages = pag.get("total_pages", total_pages)
            normalized = normalize_rows(dataset, rows, retain_names=retain_names)
            for row in normalized:
                lab = row.get("lab_test_item")
                identity = lab.get("id") if isinstance(lab, dict) else row.get("id")
                if type(identity) is int:
                    if identity in seen:
                        raise SchemaError("project metadata: duplicate identity")
                    seen.add(identity)
            items.extend(normalized)
            if total is not None and (len(items) > total or
                    (has_next and len(items) >= total) or (not has_next and len(items) != total)):
                raise SchemaError("project metadata: count mismatch")
            if total_pages is not None and (
                    (has_next and total_pages <= page) or
                    (not has_next and total_pages != page
                     and not (page == 1 and total_pages == 0 and not items))):
                raise SchemaError("project metadata: terminal mismatch")
            if not has_next:
                result["complete"] = True
                break
            if not rows:
                raise SchemaError("project metadata: empty continued page")
            if ts is None:
                result["reason"] = "snapshot_missing"
                break
        else:
            result["reason"] = "page_limit"
    except MCSError as error:
        result["reason"] = error.kind if error.kind in {
            "schema_error", "http_error", "forbidden", "session_expired",
            "network_error", "deadline_exceeded"} | (
                {"scope_changed"} if dataset == "consultations" else set()) else "fetch_error"
        result["http_status"] = error.status
    if result["complete"]:
        result["rows"] = items
    else:
        result["definition"] = None
    return result


def metadata_target(db, project_id: int, dataset: str) -> MetadataTarget | None:
    """Resolve only stored inventory evidence; no user-supplied patient/group links."""
    if not _valid_id(project_id) or dataset not in DATASETS:
        raise ValueError("invalid metadata target")
    row = db.execute("SELECT project_type,karte_id FROM patients WHERE project_id=?",
                     (project_id,)).fetchone()
    if row is None:
        return None
    if dataset == "consultations":
        return {"scope": "group", "entity_id": project_id} if row["project_type"] == "group" else None
    if dataset == "care_team":
        return {"scope": "project", "entity_id": project_id}
    if row["project_type"] == "medical" and _valid_id(row["karte_id"]):
        return {"scope": "karte", "entity_id": row["karte_id"]}
    return None


def sync_metadata(ledger, adapter, project_id: int, dataset: str, *,
                  enabled: bool = False, item_id: int | None = None,
                  retain_names: bool = False, max_pages: int = 5,
                  per_page: int = 20, now: float | None = None) -> dict[str, JSON]:
    """Explicit storage-only sync; no publication, coverage, notifications or login.

    One artifact records each attempted fetch. Last complete data is not
    overwritten by failed/partial attempts; the view resolves freshness separately.
    """
    target = metadata_target(ledger.db, project_id, dataset)
    if type(enabled) is not bool:
        raise ValueError("enabled must be boolean")
    if now is not None and (type(now) not in (int, float) or not math.isfinite(now) or now < 0):
        raise ValueError("invalid metadata acquisition time")
    if not enabled:
        return {"state": "disabled", "artifact_id": None}
    if target is None:
        return {"state": "unknown", "reason": "association_unverified", "artifact_id": None}
    if not adapter._token:
        return {"state": "unknown", "reason": "cached_session_required", "artifact_id": None}
    get = adapter._get
    if dataset == "consultations":
        def get(path: str, params: dict[str, int | str], *,
                extend_session: bool) -> dict[str, JSON]:
            if metadata_target(ledger.db, project_id, dataset) != target:
                raise MCSError("scope_changed")
            return adapter._get(path, params, extend_session=extend_session)
    result = fetch_metadata(get, dataset, target["entity_id"], item_id=item_id,
                            retain_names=retain_names, max_pages=max_pages, per_page=per_page,
                            project_type="group" if dataset == "consultations" else None)
    if dataset == "consultations" and metadata_target(ledger.db, project_id, dataset) != target:
        result.update(complete=False, rows=[], definition=None, reason="scope_changed")
    payload = {"contract": "project-metadata/1", "dataset": dataset, **target,
               "item_id": item_id, "attempted_at": time.time() if now is None else now,
               "names_retained": retain_names, **result}
    if dataset == "consultations":
        payload["project_type"] = "group"
    aid = ledger.artifact_add(ARTIFACT_KIND, json.dumps(payload, ensure_ascii=False, allow_nan=False),
                              project_id=project_id)
    state = "complete" if result["complete"] else "failed"
    if dataset == "consultations" and result["reason"] in ("snapshot_missing", "page_limit"):
        state = "partial"
    return {"state": state,
            "reason": result["reason"], "artifact_id": aid}


def main(argv: list[str] | None = None) -> int:
    """Executable opt-in sync or read-only local view; never print API responses."""
    from ledger import Ledger, LedgerReader
    from mcs_adapter import MCSAdapter
    from mcs_util import acquire_run_lock
    from project_metadata_view import get_project_metadata

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sync", "view"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--dataset", choices=DATASETS, required=True)
    parser.add_argument("--item-id", type=int)
    parser.add_argument("--read-only-get", action="store_true")
    parser.add_argument("--token-cache")
    parser.add_argument("--retain-names", action="store_true",
                        help="sync: store care_team names (owner opt-in)")
    parser.add_argument("--show-names", action="store_true",
                        help="view: show names only if they were retained")
    args = parser.parse_args(argv)
    if (not _valid_id(args.project_id)
            or (args.dataset == "observation_values" and not _valid_id(args.item_id))
            or (args.dataset != "observation_values" and args.item_id is not None)):
        parser.error("valid project and dataset-specific item IDs required")
    if not os.path.isfile(args.database):
        parser.error("existing database required")
    if args.command == "sync" and (not args.read_only_get or not args.token_cache):
        parser.error("sync requires --read-only-get and --token-cache")
    if args.command == "view" and (args.read_only_get or args.token_cache):
        parser.error("view does not accept communication options")
    if ((args.retain_names or args.show_names) and args.dataset != "care_team"
            or args.retain_names and args.command != "sync"
            or args.show_names and args.command != "view"):
        parser.error("--retain-names is sync-only and --show-names view-only, care_team only")
    lock_fd = None
    store = None
    try:
        if args.command == "sync":
            lock_fd = acquire_run_lock(os.path.join(
                os.path.dirname(os.path.abspath(args.database)), "run.lock"))
            if lock_fd is None:
                print(json.dumps({"state": "held", "reason": "run_lock_busy"}))
                return 1
        store = Ledger(args.database) if args.command == "sync" else LedgerReader(args.database)
        if args.command == "view":
            result = get_project_metadata(store.db, args.project_id, args.dataset,
                                          item_id=args.item_id, show_names=args.show_names)
        else:
            adapter = MCSAdapter(token_cache=args.token_cache)
            adapter._token = adapter._read_cache()
            adapter.set_deadline(time.monotonic() + 25)
            result = sync_metadata(store, adapter, args.project_id, args.dataset,
                                   enabled=True, item_id=args.item_id,
                                   retain_names=args.retain_names)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result["state"] in ("complete", "empty") else 1
    finally:
        if store is not None:
            store.close()
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
