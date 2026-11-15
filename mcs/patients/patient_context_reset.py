"""Erase every patient-context result and its cloud accounting under one receipted human approval.

The owner decided (2026-11-15) to discard all patient-context processing state, scans,
published contexts, quality results, generation slots and the patient_context route's
cloud accounting, then rebuild from scratch. Accounting of the other cloud routes stays,
because semantic and attachment results are proven against it. Spending already made is
carried over as per-day totals (cloud_usage_carryover_v1), so every spending limit keeps
counting it; quality Jev usage of today keeps counting toward the Jev daily limit.

The operation runs only while patient-context generation is disabled in the
configuration, cloud sends are paused, nothing is in flight and the accounting is
readable. All writes happen in one SAVEPOINT inside the receipt transaction and are
rolled back when the reported spending would differ after the reset.
"""
from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace

import cloud_policy
import patient_context_quota
import patient_context_refresh_state as state
from ledger_semantic_sources import bump_inference_analysis_epoch_tx
from mcs_util import load_config, payload_hash, valid_hash, valid_text
from patient_context_artifacts import quality_kind

COMMAND = "ops.patient_context_reset"
ROUTE = "patient_context"
# A send younger than this may still be on the wire (cloud_observation's 10-minute bucket).
IN_FLIGHT_S = 600
QUALITY_OPERATIONS = ("patient_summary_quality", "patient_source_quality")
CARRIED_JEV_OPERATION = "patient_context_reset_carryover"
_SOURCE_QUALITY = tuple(quality_kind(kind, {"purpose": "source"}) for kind in (
    state.QUALITY, state.QUALITY_FAIL, state.QUALITY_PASS, state.QUALITY_RETRY, state.QUALITY_SKIP))
# Results the reset discards; their absence is the starting point of the rebuild.
ERASED_KINDS = (
    state.KIND, state.LEGACY_KIND, state.SCAN, state.CHUNK, state.PROGRESS, patient_context_quota.KIND,
    state.QUALITY, state.QUALITY_FAIL, state.QUALITY_PASS, state.QUALITY_REPAIR,
    state.QUALITY_REPAIR_CANDIDATE, state.QUALITY_RETRY, state.QUALITY_SKIP,
    state.SOURCE_REPAIR, state.SOURCE_REPAIR_CANDIDATE, *_SOURCE_QUALITY,
)
# Records the reset keeps: viewers' "since" positions, sealed notification renders and
# dependency-loss evidence that rollups and other views also read.
KEPT_KINDS = ("patient_context_view_checkpoint_v1", "patient_context_progress_snapshot_v1",
              "inference_dependency_loss")
_USAGE_KINDS = ("cloud_usage_reserve", "cloud_usage_result", "cloud_usage_disposition")
_RID = "CASE WHEN json_valid(meta) THEN json_extract(meta,'$.reservation_id') END"
_ROUTE = "CASE WHEN json_valid(meta) THEN json_extract(meta,'$.route') END"
REFUSALS = frozenset({
    "patient_context_enabled", "cloud_not_paused", "quality_claim_active", "generation_active",
    "cloud_in_flight", "jev_in_flight", "accounting_unverified", "accounting_inconsistent",
    "spend_changed", "preview_changed", "backup_missing", "config_unreadable"})


def _placeholders(values):
    return ",".join("?" * len(values))


def _patient_context_reservations(db):
    """route=patient_context reservations as {reservation_id: (meta, row)}; refuses ambiguous rows."""
    reservations = {}
    for row in db.execute(f"SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
                          f"WHERE kind='cloud_usage_reserve' AND {_ROUTE}=?", (ROUTE,)):
        meta = cloud_policy._accounting_meta(row["meta"])
        rid = meta.get("reservation_id")
        if not isinstance(rid, str) or rid in reservations:
            raise ValueError("accounting_inconsistent")
        reservations[rid] = (meta, row)
    stray = db.execute(f"SELECT COUNT(*) FROM artifacts WHERE kind IN ('cloud_usage_result','cloud_usage_disposition') "
                       f"AND {_ROUTE}=? AND ({_RID} IS NULL OR {_RID} NOT IN "
                       f"(SELECT {_RID} FROM artifacts WHERE kind='cloud_usage_reserve' AND {_ROUTE}=?))",
                       (ROUTE, ROUTE)).fetchone()[0]
    if stray:
        raise ValueError("accounting_inconsistent")
    return reservations


def _carryover(db, reservations):
    """Per-day totals by provider and settlement for the reservations about to be removed."""
    groups = {}
    for rid, (meta, row) in reservations.items():
        maximum = cloud_policy._reserve_valid(meta, row)
        results = [(cloud_policy._accounting_meta(r["meta"]), r) for r in db.execute(
            f"SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
            f"WHERE kind='cloud_usage_result' AND {_RID}=?", (rid,))]
        cost, settled = cloud_policy._settled(meta, maximum, results) if results else (maximum, False)
        provider = cloud_policy._price_definition(meta["model"], meta["price_version"])["provider"]
        key = (cloud_policy._periods(row["created_at"])[0], provider, bool(settled))
        at, requests, total = groups.get(key, (row["created_at"], 0, Decimal(0)))
        groups[key] = (max(at, row["created_at"]), requests + 1, total + cost)
    return [{"at": at, "provider": provider, "requests": requests, "cost_usd": str(total), "settled": settled}
            for (_day, provider, settled), (at, requests, total) in sorted(groups.items())]


def _spend(db, cfg):
    """The spending every limit compares against, read inside the caller's transaction."""
    ledger = SimpleNamespace(db=db)
    out = {"day": cloud_policy.usage_report(ledger, period="day"),
           "month": cloud_policy.usage_report(ledger, period="month")}
    settings, errors = cloud_policy.resolve_settings(cfg)
    if not errors and settings["window_budget_usd"] is not None:
        out["window"] = str(cloud_policy.window_spend(ledger, cfg))
    return out


def _quality_usage(db):
    """Jev usage rows of patient-context quality checks as (artifact_id, created_at, meta)."""
    rows = []
    for row in db.execute("SELECT artifact_id,created_at,meta FROM artifacts WHERE kind='semantic_usage' "
                          "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.operation') END IN (?,?)",
                          QUALITY_OPERATIONS):
        meta = json.loads(row["meta"])
        rows.append((row["artifact_id"], row["created_at"], meta))
    return rows


def _refusal(db, cfg, now):
    """The first fixed reason that forbids the reset now, or None."""
    if state.options(cfg)["enabled"]:
        return "patient_context_enabled"
    if cloud_policy.global_block_reason(SimpleNamespace(db=db)) != "paused":
        return "cloud_not_paused"
    if db.execute("SELECT 1 FROM fetch_jobs WHERE kind=? AND next_try>? LIMIT 1",
                  (state.QUALITY_CLAIM, now)).fetchone():
        return "quality_claim_active"
    for (content,) in db.execute("SELECT content FROM artifacts WHERE kind=?", (patient_context_quota.KIND,)):
        doc = json.loads(content) if isinstance(content, str) else None
        if (isinstance(doc, dict) and doc.get("state") in ("reserved", "started")
                and isinstance(doc.get("lease_until"), (int, float)) and doc["lease_until"] > now):
            return "generation_active"
    report = cloud_policy.usage_report(SimpleNamespace(db=db))
    holds = cloud_policy.evidence_hold_counts(db, now=now)
    if report["pricing_unverified"] or holds["unattributable_accounting_rows"] != 0:
        return "accounting_unverified"
    reservations = _patient_context_reservations(db)
    answered = {row[0] for row in db.execute(
        f"SELECT {_RID} FROM artifacts WHERE kind='cloud_usage_result' AND {_ROUTE}=?", (ROUTE,))}
    if any(rid not in answered and row["created_at"] > now - IN_FLIGHT_S
           for rid, (_meta, row) in reservations.items()):
        return "cloud_in_flight"
    if any("quality_result" not in meta and created_at > now - IN_FLIGHT_S
           for _id, created_at, meta in _quality_usage(db)):
        return "jev_in_flight"
    return None


def preview(db, *, cfg=None, now):
    """Counts of what the reset removes and carries, without identifiers or content."""
    cfg = load_config() if cfg is None else cfg
    try:
        refusal = _refusal(db, cfg, now)
        reservations = _patient_context_reservations(db)
        carryover = _carryover(db, reservations)
    except ValueError as error:
        refusal = str(error) if str(error) in REFUSALS else "accounting_unverified"
        reservations, carryover = {}, []
    kinds = {kind: db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (kind,)).fetchone()[0]
             for kind in ERASED_KINDS}
    rids = list(reservations)
    accounting = 0
    for start in range(0, len(rids), 500):
        page = rids[start:start + 500]
        accounting += db.execute(f"SELECT COUNT(*) FROM artifacts WHERE kind IN (?,?,?) AND {_RID} IN "
                                 f"({_placeholders(page)})", (*_USAGE_KINDS, *page)).fetchone()[0]
    from patient_context_refresh_readiness import lane_metrics
    try:
        outcomes = lane_metrics(db)
    except ValueError:
        outcomes = None
    body = {"refusal": refusal, "kinds": kinds, "quality_claims": db.execute(
                "SELECT COUNT(*) FROM fetch_jobs WHERE kind=?", (state.QUALITY_CLAIM,)).fetchone()[0],
            "accounting_rows": accounting, "reservations": len(reservations),
            "unsettled_reservations": sum(entry["requests"] for entry in carryover if not entry["settled"]),
            "quality_usage_rows": len(_quality_usage(db)), "carryover": carryover, "outcomes": outcomes}
    return {**body, "preview_hash": payload_hash({key: body[key] for key in body if key != "refusal"})}


def validate_operation(req, base):
    """Operation rules for the shared human-confirmed envelope (projectless)."""
    if req.get("project_id") is not None:
        return "bad_project_id"
    if req.keys() - (base | {"reason", "preview_hash", "backup"}):
        return "unknown_field"
    if not valid_text(req.get("reason"), 2000):
        return "bad_reason"
    if not valid_hash(req.get("preview_hash")):
        return "bad_preview_hash"
    backup = req.get("backup")
    if (not isinstance(backup, dict) or set(backup) != {"name", "bytes", "sha256"}
            or not valid_text(backup["name"], 255) or "/" in backup["name"]
            or type(backup["bytes"]) is not int or backup["bytes"] <= 0 or not valid_hash(backup["sha256"])):
        return "bad_backup"
    return None


def apply_tx(db, req, now, *, cfg=None, backup_dir=None):
    """Erase patient-context results inside the caller's receipt transaction; return (error, extra)."""
    import maintenance
    try:
        cfg = load_config() if cfg is None else cfg
    except (OSError, ValueError):
        return "config_unreadable", {}
    backup = Path(backup_dir or maintenance.BACKUP_DIR) / req["backup"]["name"]
    try:
        if backup.is_symlink() or not backup.is_file() or os.path.getsize(backup) != req["backup"]["bytes"]:
            return "backup_missing", {}
    except OSError:
        return "backup_missing", {}
    found = preview(db, cfg=cfg, now=now)
    if found["refusal"] is not None:
        return found["refusal"], {}
    if found["preview_hash"] != req["preview_hash"]:
        return "preview_changed", {}
    before = _spend(db, cfg)
    reservations = _patient_context_reservations(db)
    db.execute("SAVEPOINT patient_context_reset")
    try:
        rids = list(reservations)
        for start in range(0, len(rids), 500):
            page = rids[start:start + 500]
            db.execute(f"DELETE FROM artifacts WHERE kind IN (?,?,?) AND {_RID} IN ({_placeholders(page)})",
                       (*_USAGE_KINDS, *page))
        if found["carryover"]:
            db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                       (cloud_policy.CARRYOVER_KIND, json.dumps({"schema": cloud_policy.CARRYOVER_KIND,
                        "entries": found["carryover"]}), "", json.dumps({"command_id": req["command_id"]}), now))
        today = cloud_policy._periods(now)[0]
        for artifact_id, created_at, meta in _quality_usage(db):
            if created_at >= today:
                carried = {"operation": CARRIED_JEV_OPERATION, "jev_requests": meta.get("jev_requests", 0)}
                db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(carried), artifact_id))
            else:
                db.execute("DELETE FROM artifacts WHERE artifact_id=?", (artifact_id,))
        db.execute(f"DELETE FROM artifacts WHERE kind IN ({_placeholders(ERASED_KINDS)})", ERASED_KINDS)
        db.execute("DELETE FROM fetch_jobs WHERE kind=?", (state.QUALITY_CLAIM,))
        bump_inference_analysis_epoch_tx(db)
        if _spend(db, cfg) != before:
            raise ValueError("spend_changed")
    except ValueError as error:
        db.execute("ROLLBACK TO patient_context_reset")
        db.execute("RELEASE patient_context_reset")
        return (str(error) if str(error) in REFUSALS else "accounting_unverified"), {}
    db.execute("RELEASE patient_context_reset")
    return None, {"reason": req["reason"], "backup": req["backup"],
                  "erased": {"kinds": found["kinds"], "quality_claims": found["quality_claims"],
                             "accounting_rows": found["accounting_rows"],
                             "quality_usage_rows": found["quality_usage_rows"]},
                  "carryover": found["carryover"], "outcomes": found["outcomes"]}
