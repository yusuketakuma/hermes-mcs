#!/usr/bin/env python3
"""Opt-in bounded project/group metadata acquisition and local artifact sync.

Contract evidence: docs/roadmap/mcs-api-survey.md and official public chunks
HB7B2UZS / 3X6DIYSL (members), UHQUQDWW / 5QYZEKQD (medication),
M2MZSSUK (observation services at 23700..25300, value formatter at 180200),
4WFEVJDT / IGJY24TN (group consultation lists). These are client contracts,
not proof of server-side read/session preservation.

No publication, extraction, patient-group association or new schema. The only
scheduled caller is run_check's jobs-only run, which calls sync_clinical_metadata
when config clinical_metadata is true.
sync_metadata requires enabled=True and resolves project/karte from the ledger.
Names require separate owner opt-in; photos/contact fields are never retained.
CLI: project_metadata.py sync|view --database PATH --project-id ID --dataset NAME
     [--item-id ID] (sync additionally requires --read-only-get --token-cache PATH).
     care_team names need --retain-names (sync) and --show-names (view); both default off.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import time

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import _mcs_path  # noqa: F401

from mcs_contract import MCSError, SchemaError, valid_id as _valid_id
from project_metadata_contract import (collection_rows, ARTIFACT_KIND, CLINICAL_DATASETS, DATASETS, FETCH_ERROR_KINDS, JSON, MAX_ROWS, MAX_VALUE_ROWS, MetadataFetch, SCOPED_DATASETS, attempt_state, clinical_definition_fingerprint, metadata_target, normalize_rows)
from mcs_util import loads_dict

# datasets whose bound scope can move under a fetch (reason scope_changed)
# MCSError kinds an attempt keeps verbatim; any other kind becomes fetch_error
# a consultations walk stopped by these still stored complete pages
CLINICAL_PROGRESS_KIND = "clinical_metadata_progress_v1"
CLINICAL_REFRESH_S = 24 * 3600
CLINICAL_BACKOFF_S = 6 * 3600
CLINICAL_GET_CAP = 12












class _ClinicalHold(Exception):
    """A run boundary, not evidence that a clinical fetch failed."""


def _staged_resume(dataset: str, resume: dict, per_page: int,
                   item_id: int | None):
    """Validate a staged observation_values slice; any defect is a hold.

    Returns (items, start_page, total, total_pages) for resumption."""
    try:
        valid = (dataset == "observation_values" and isinstance(resume, dict)
                 and resume.get("sha256") == _stage_hash(resume)
                 and resume.get("contract") == "clinical-values-staging/1"
                 and resume.get("per_page") == per_page
                 and resume.get("capacity") == MAX_VALUE_ROWS
                 and type(resume.get("next_page")) is int
                 and 2 <= resume["next_page"] <= MAX_VALUE_ROWS // per_page + 1
                 and _valid_id(resume.get("timestamp"))
                 and type(resume.get("count")) is int
                 and resume["count"] == len(resume["rows"])
                 and 0 < resume["count"] <= MAX_VALUE_ROWS
                 and all(value is None or (type(value) is int and value >= 0)
                         for value in (resume.get("total"), resume.get("total_pages"))))
        if not valid or normalize_rows(dataset, resume["rows"]) != resume["rows"]:
            raise ValueError
        definition = resume.get("definition")
        if definition is not None and (
                normalize_rows("observation_items", [definition])[0] != definition
                or definition["lab_test_item"]["id"] != item_id):
            raise ValueError
        return (resume["rows"][:], resume["next_page"],
                resume.get("total"), resume.get("total_pages"))
    except (KeyError, TypeError, ValueError, MCSError):
        raise _ClinicalHold("staging_invalid") from None
















def fetch_metadata(get, dataset: str, entity_id: int, *, item_id: int | None = None,
                   max_pages: int = 5, per_page: int = 50,
                   retain_names: bool = False,
                   project_type: str | None = None,
                   _resume=None, _checkpoint=None) -> MetadataFetch:
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
    start_page = 1
    if _resume is not None:
        items, start_page, total, total_pages = _staged_resume(
            dataset, _resume, per_page, item_id)
        result.update(timestamp=_resume["timestamp"],
                      definition=_resume.get("definition"))
    if _checkpoint is not None and dataset != "observation_values":
        raise ValueError("only automatic observation values can stage")
    try:
        for page in range(start_page, start_page + max_pages):
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
            rows = collection_rows(raw.get(key), MAX_ROWS if dataset == "medication_periods" else per_page)
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
            if dataset == "observation_values" and (
                    (total is not None and total > MAX_VALUE_ROWS)
                    or len(items) + len(rows) > MAX_VALUE_ROWS):
                raise MCSError("capacity_limit")
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
            if _checkpoint is not None:
                _checkpoint({"next_page": page + 1, "timestamp": result["timestamp"],
                             "contract": "clinical-values-staging/1", "per_page": per_page,
                             "capacity": MAX_VALUE_ROWS,
                             "total": total, "total_pages": total_pages, "count": len(items),
                             "rows": items[:], "definition": result["definition"]})
        else:
            if _checkpoint is not None:
                raise _ClinicalHold("page_slice")
            result["reason"] = "page_limit"
    except MCSError as error:
        result["reason"] = error.kind if error.kind in FETCH_ERROR_KINDS or (
            error.kind == "scope_changed" and dataset in SCOPED_DATASETS) else "fetch_error"
        result["http_status"] = error.status
    if result["complete"]:
        result["rows"] = items
    else:
        result["definition"] = None
    return result




def sync_metadata(ledger, adapter, project_id: int, dataset: str, *,
                  enabled: bool = False, item_id: int | None = None,
                  retain_names: bool = False, max_pages: int = 5,
                  per_page: int = 20, now: float | None = None,
                  definition_source_artifact_id: int | None = None,
                  defer_deadline: bool = False,
                  _resume=None, _checkpoint=None) -> dict[str, JSON]:
    """Explicit storage-only sync; no publication, coverage, notifications or login.

    One artifact records each attempted fetch. Last complete data is not
    overwritten by failed/partial attempts; the view resolves freshness separately.
    """
    target = metadata_target(ledger.db, project_id, dataset)
    if type(enabled) is not bool:
        raise ValueError("enabled must be boolean")
    if type(defer_deadline) is not bool:
        raise ValueError("defer_deadline must be boolean")
    if now is not None and (type(now) not in (int, float) or not math.isfinite(now) or now < 0):
        raise ValueError("invalid metadata acquisition time")
    if not enabled:
        return {"state": "disabled", "artifact_id": None}
    if target is None:
        return {"state": "unknown", "reason": "association_unverified", "artifact_id": None}
    if not adapter._token:
        return {"state": "unknown", "reason": "cached_session_required", "artifact_id": None}
    definition = None
    if definition_source_artifact_id is not None:
        if dataset != "observation_values" or not _valid_id(definition_source_artifact_id):
            raise ValueError("invalid clinical definition pin")
        generation, items = _clinical_items(ledger.db, project_id, target["entity_id"])
        if generation != definition_source_artifact_id or item_id not in items:
            return {"state": "unknown", "reason": "definition_mapping_changed", "artifact_id": None}
        definition = items[item_id]
        definition_fingerprint = clinical_definition_fingerprint(target["entity_id"], items)
    get = adapter._get
    if dataset == "consultations" or defer_deadline:
        def get(path: str, params: dict[str, int | str], *,
                extend_session: bool) -> dict[str, JSON]:
            live = ledger.db.execute("SELECT is_archived FROM patients WHERE project_id=?",
                                     (project_id,)).fetchone()
            if (metadata_target(ledger.db, project_id, dataset) != target
                    or (defer_deadline and (live is None or live[0]))):
                raise MCSError("scope_changed")
            if definition_source_artifact_id is not None:
                _, current_items = _clinical_items(ledger.db, project_id, target["entity_id"])
                if clinical_definition_fingerprint(target["entity_id"], current_items) != definition_fingerprint:
                    raise _ClinicalHold("definition_changed")
            try:
                return adapter._get(path, params, extend_session=extend_session)
            except MCSError as error:
                if defer_deadline and error.kind == "rate_limited":
                    # The run refused this GET before transport, so there is no
                    # failed observation or clinical unit to advance.
                    raise _ClinicalHold("rate_limited") from None
                raise
    result = fetch_metadata(get, dataset, target["entity_id"], item_id=item_id,
                            retain_names=retain_names, max_pages=max_pages, per_page=per_page,
                            project_type="group" if dataset == "consultations" else None,
                            _resume=_resume, _checkpoint=_checkpoint)
    if defer_deadline and result["reason"] == "deadline_exceeded":
        raise _ClinicalHold("deadline")
    if definition_source_artifact_id is not None:
        _, current_items = _clinical_items(ledger.db, project_id, target["entity_id"])
        if clinical_definition_fingerprint(target["entity_id"], current_items) != definition_fingerprint:
            raise _ClinicalHold("definition_changed")
    live = ledger.db.execute("SELECT is_archived FROM patients WHERE project_id=?",
                             (project_id,)).fetchone() if defer_deadline else None
    if ((dataset == "consultations" or defer_deadline)
            and (metadata_target(ledger.db, project_id, dataset) != target
                 or (defer_deadline and (live is None or live[0])))):
        result.update(complete=False, rows=[], definition=None, reason="scope_changed")
    payload = {"contract": "project-metadata/1", "dataset": dataset, **target,
               "item_id": item_id, "attempted_at": time.time() if now is None else now,
               "names_retained": retain_names, **result}
    if definition_source_artifact_id is not None:
        payload["definition_source_artifact_id"] = definition_source_artifact_id
        payload["definition_fingerprint"] = definition_fingerprint     # computed from the same items above
        payload["definition_source"] = "observation_values" if result["definition"] else "observation_items"
        if result["complete"] and payload["definition"] is None:
            payload["definition"] = definition
    if dataset == "consultations":
        payload["project_type"] = "group"
    aid = ledger.artifact_add(ARTIFACT_KIND, json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                                       separators=(',', ':')),
                              project_id=project_id)
    return {"state": attempt_state(dataset, result["complete"], result["reason"]),
            "reason": result["reason"], "artifact_id": aid}


def _clinical_items(db, project_id, karte_id):
    """Only the newest complete, inventory-bound item set can define a value GET."""
    row = db.execute("""SELECT artifact_id,content FROM artifacts
        WHERE kind=? AND project_id=? AND CASE WHEN json_valid(content) THEN
          json_extract(content,'$.dataset')='observation_items'
          AND json_type(content,'$.entity_id')='integer'
          AND typeof(json_extract(content,'$.entity_id'))='integer'
          AND json_extract(content,'$.entity_id')=?
          AND json_extract(content,'$.item_id') IS NULL ELSE 0 END
        ORDER BY artifact_id DESC LIMIT 1""", (ARTIFACT_KIND, project_id, karte_id)).fetchone()
    if row is None:
        return None, {}
    try:
        payload = loads_dict(row["content"])
        if payload is None:
            return None, {}
        if (payload.get("contract") != "project-metadata/1" or payload.get("scope") != "karte"
                or payload.get("complete") is not True
                or payload.get("reason") is not None or payload.get("http_status") is not None):
            return None, {}
        rows = normalize_rows("observation_items", payload["rows"])
        items = {item["lab_test_item"]["id"]: item for item in rows}
        return (row["artifact_id"], items) if len(items) == len(rows) else (None, {})
    except (KeyError, TypeError, ValueError, MCSError):
        return None, {}




def _stage_hash(stage):
    return hashlib.sha256(json.dumps({key: value for key, value in stage.items()
                                     if key != "sha256"}, sort_keys=True,
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _clinical_progress(ledger, pid, state):
    """One mutable cursor artifact per patient; clinical attempt history stays intact."""
    with ledger.db:
        turn = ledger.db.execute("""SELECT COALESCE(MAX(CASE WHEN json_valid(meta)
            THEN json_extract(meta,'$.turn') END),0)+1 FROM artifacts WHERE kind=?""",
            (CLINICAL_PROGRESS_KIND,)).fetchone()[0]
        row = ledger.db.execute("SELECT artifact_id FROM artifacts WHERE kind=? AND project_id=? "
                                "ORDER BY artifact_id DESC LIMIT 1", (CLINICAL_PROGRESS_KIND, pid)).fetchone()
        raw = json.dumps(state, allow_nan=False)
        if row:
            ledger.db.execute("UPDATE artifacts SET content=?,meta=?,created_at=? WHERE artifact_id=?",
                              (raw, json.dumps({"turn": turn}), time.time(), row[0]))
        else:
            ledger.artifact_add_tx(CLINICAL_PROGRESS_KIND, raw, project_id=pid, meta={"turn": turn})


def sync_clinical_metadata(ledger, adapter, *, enabled=False, deadline):
    """Rotate registered medication and all observation items with a cached session and bounded GETs."""
    stats = {"mode": "off", "requests": 0, "complete": 0, "failed": 0, "limited": 0,
             "limits": {"value_rows": MAX_VALUE_ROWS, "pages_per_slice": 5}, "held": None}
    if enabled is not True:
        return stats
    stats["mode"] = "on"
    until = min(deadline - 60, time.monotonic() + 25)
    if time.monotonic() >= until:
        stats["held"] = "deadline"
        return stats
    client = copy.copy(adapter)
    client._token = adapter._read_cache()
    if not client._token:
        stats["held"] = "cached_session_required"
        return stats
    client.set_deadline(until)
    worker = client._worker

    def bounded_worker(request, **kwargs):
        if stats["requests"] >= CLINICAL_GET_CAP or time.monotonic() >= until:
            raise _ClinicalHold("budget" if stats["requests"] >= CLINICAL_GET_CAP else "deadline")
        if request.get("operation") != "api" or request.get("method") != "GET":
            raise _ClinicalHold("read_only_boundary")
        stats["requests"] += 1
        return worker(request, **kwargs)

    client._worker = bounded_worker
    while stats["requests"] < CLINICAL_GET_CAP and time.monotonic() < until:
        now = time.time()
        row = ledger.db.execute("""SELECT p.project_id,p.karte_id,s.content
            FROM patients p LEFT JOIN artifacts s ON s.artifact_id=(
              SELECT MAX(a.artifact_id) FROM artifacts a WHERE a.kind=? AND a.project_id=p.project_id)
            WHERE p.project_type='medical' AND COALESCE(p.is_archived,0)=0
              AND typeof(p.karte_id)='integer' AND p.karte_id>0
              AND COALESCE(CASE WHEN json_valid(s.content)
                   AND json_extract(s.content,'$.karte_id')=p.karte_id
                   AND json_type(s.content,'$.next_due_at') IN ('integer','real')
                   AND json_extract(s.content,'$.next_due_at') BETWEEN 0 AND ?
                   THEN json_extract(s.content,'$.next_due_at') END,0)<=?
            ORDER BY COALESCE(CASE WHEN json_valid(s.meta)
                   THEN json_extract(s.meta,'$.turn') END,0), p.project_id LIMIT 1""",
            (CLINICAL_PROGRESS_KIND, now + CLINICAL_REFRESH_S, now)).fetchone()
        if row is None:
            break
        pid, kid = row["project_id"], row["karte_id"]
        state = loads_dict(row["content"]) or {}
        if (not isinstance(state, dict) or state.get("karte_id") != kid
                or state.get("phase") not in CLINICAL_DATASETS
                or type(state.get("item_cursor", 0)) is not int or state.get("item_cursor", 0) < 0):
            state = {"karte_id": kid, "phase": "medication_periods", "item_cursor": 0}
        state.update(checked_at=now, next_due_at=0)
        phase, item_id, generation = state["phase"], None, None
        if phase == "observation_values":
            generation, items = _clinical_items(ledger.db, pid, kid)
            if generation is None:
                state.update(phase="observation_items", item_cursor=0)
                _clinical_progress(ledger, pid, state)
                continue
            fingerprint = clinical_definition_fingerprint(kid, items)
            if state.get("items_fingerprint") != fingerprint:
                state.update(items_fingerprint=fingerprint, item_cursor=0)
                state.pop("staging", None)
            state["items_generation"] = generation
            if state["item_cursor"] not in items:
                state["item_cursor"] = 0
            pending = sorted(item for item in items if item > state["item_cursor"])
            if not pending:
                state.update(phase="medication_periods", item_cursor=0,
                             next_due_at=now + (CLINICAL_BACKOFF_S if state.pop("cycle_failed", False)
                                                else CLINICAL_REFRESH_S))
                _clinical_progress(ledger, pid, state)
                continue
            item_id = pending[0]
        resume = state.get("staging")
        if resume is not None and (not isinstance(resume, dict)
                or resume.get("karte_id") != kid or resume.get("item_id") != item_id
                or resume.get("definition_fingerprint") != state.get("items_fingerprint")):
            state.pop("staging", None)
            resume = None

        def checkpoint(staged):
            staged.update(karte_id=kid, item_id=item_id,
                          definition_fingerprint=state["items_fingerprint"])
            staged["sha256"] = _stage_hash(staged)
            state["staging"] = staged
            _clinical_progress(ledger, pid, state)

        try:
            fetched = sync_metadata(ledger, client, pid, phase, enabled=True, item_id=item_id,
                                    max_pages=5, per_page=50, now=now,
                                    definition_source_artifact_id=generation, defer_deadline=True,
                                    _resume=resume,
                                    _checkpoint=checkpoint if phase == "observation_values" else None)
        except _ClinicalHold as hold:
            stats["held"] = str(hold)
            if str(hold) == "rate_limited":
                # Any pages fetched before the refusal are already checkpointed.
                # Keep that checkpoint (or the previous due state) unchanged.
                break
            if str(hold) in ("staging_invalid", "definition_changed"):
                state.pop("staging", None)
            _clinical_progress(ledger, pid, state)
            break
        state.pop("staging", None)
        if fetched["state"] != "complete":
            stats["failed"] += 1
            if fetched.get("reason") in ("page_limit", "capacity_limit"):
                stats["limited"] += 1
            # A failed value must not pause the remaining registered items.
            # Retry that failed cycle after backoff once all items were visited.
            pause = phase != "observation_values" or fetched.get("reason") == "session_expired"
            state.update(cycle_failed=True, next_due_at=now + CLINICAL_BACKOFF_S if pause else 0,
                         last_failure=fetched.get("reason"))
        else:
            stats["complete"] += 1
            state.pop("last_failure", None)
        state["last_attempt_id"] = fetched.get("artifact_id") or state.get("last_attempt_id", 0)
        if phase == "medication_periods":
            state["phase"] = "observation_items"
        elif phase == "observation_items":
            state.update(phase="observation_values", item_cursor=0)
        else:
            state["item_cursor"] = item_id
        _clinical_progress(ledger, pid, state)
        if fetched.get("reason") == "session_expired":
            stats["held"] = "session_expired"
            break
    if stats["held"] is None and (stats["requests"] == CLINICAL_GET_CAP or time.monotonic() >= until):
        stats["held"] = "budget" if stats["requests"] == CLINICAL_GET_CAP else "deadline"
    return stats


def main(argv: list[str] | None = None) -> int:
    """Executable opt-in sync or read-only local view; never print API responses."""
    from ledger import Ledger, LedgerReader
    from mcs_adapter import MCSAdapter
    from writer_lock import acquire_run_lock
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
        try:
            if store is not None:
                store.close()
        finally:
            if lock_fd is not None:
                os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
