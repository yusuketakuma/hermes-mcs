"""Erase every patient-context result and its cloud accounting under one receipted human approval.

The owner decided (2026-11-15) to discard all patient-context processing state, scans,
published contexts, quality results, generation slots and the patient_context route's
cloud accounting, then rebuild from scratch. Accounting of the other cloud routes stays,
because semantic and attachment results are proven against it. Spending already made is
carried over as totals per day, provider and settlement (cloud_usage_carryover_v1), split
at the period start so that budget keeps its count too; every spending limit keeps counting
it. Quality Jev usage of today keeps counting toward the Jev daily limit.

The operation runs only while patient-context generation is disabled in the
configuration, cloud sends are paused, nothing is in flight and the accounting is
readable. All writes happen in one SAVEPOINT inside the receipt transaction and are
rolled back when the reported spending would differ after the reset.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
from types import SimpleNamespace

import cloud_policy
from anthropic_client import AnthropicError
import patient_context_quota
import patient_context_refresh_state as state
from ledger_semantic_sources import bump_inference_analysis_epoch_tx
from mcs_util import load_config, payload_hash, until_epoch, valid_hash, valid_text
from patient_context_artifacts import quality_kind

COMMAND = "ops.patient_context_reset"
ROUTE = "patient_context"
# A send younger than this may still be on the wire (cloud_observation's 10-minute bucket).
IN_FLIGHT_S = 600
BACKUP_PREFIX = "patient-context-reset-"
# The CLI takes the backup just before the receipt transaction; an older copy is not this reset's.
BACKUP_MAX_AGE_S = 3600
BACKUP_CLOCK_SKEW_S = 300
# Accounting reads that fail as data, not as a programming error: the reset then refuses.
_UNVERIFIED = (ValueError, AnthropicError, InvalidOperation)
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
_RID = "CASE WHEN json_valid(meta) THEN json_extract(meta,'$.reservation_id') END"
_ROUTE = "CASE WHEN json_valid(meta) THEN json_extract(meta,'$.route') END"
_ROW = "artifact_id,kind,project_id,message_id,model,meta,created_at"
REFUSALS = frozenset({
    "patient_context_enabled", "cloud_not_paused", "quality_claim_active", "generation_active",
    "cloud_in_flight", "jev_in_flight", "accounting_unverified", "accounting_inconsistent",
    "spend_changed", "preview_changed", "backup_missing", "config_unreadable"})


def _placeholders(values):
    return ",".join("?" * len(values))


def _accounting(db):
    """This route's accounting rows, read once: reservations, results by reservation, row ids.

    Results and dispositions whose reservation is gone (pruned history) are erased with the
    rest, since the tallies count spending by reservation only; one that names another
    route's reservation is refused as inconsistent.
    """
    import ledger_schema
    reservations, results, ids, stray = {}, {}, [], set()
    rows = list(db.execute(f"SELECT {_ROW} FROM artifacts WHERE kind IN {ledger_schema.CLOUD_USAGE_KINDS_SQL} "
                           f"AND {_ROUTE}=? ORDER BY artifact_id", (ROUTE,)))
    for row in rows:
        if row["kind"] == "cloud_usage_reserve":
            meta = cloud_policy._accounting_meta(row["meta"])
            rid = meta.get("reservation_id")
            if not isinstance(rid, str) or rid in reservations:
                raise ValueError("accounting_inconsistent")
            reservations[rid] = (meta, row)
    for row in rows:
        ids.append(row["artifact_id"])
        if row["kind"] == "cloud_usage_reserve":
            continue
        meta = cloud_policy._accounting_meta(row["meta"])
        rid = meta.get("reservation_id")
        if rid not in reservations:
            stray.add(rid)
        elif row["kind"] == "cloud_usage_result":
            results.setdefault(rid, []).append((meta, row))
    named = [rid for rid in stray if isinstance(rid, str)]
    for start in range(0, len(named), 500):
        page = named[start:start + 500]
        # the partial reservation index answers this; any match belongs to another route
        if db.execute(f"SELECT 1 FROM artifacts WHERE kind IN {ledger_schema.CLOUD_USAGE_KINDS_SQL} "
                      f"AND kind='cloud_usage_reserve' AND {ledger_schema.CLOUD_USAGE_RESERVATION_SQL} "
                      f"IN ({_placeholders(page)}) LIMIT 1", page).fetchone():
            raise ValueError("accounting_inconsistent")
    return SimpleNamespace(reservations=reservations, results=results, ids=ids)


def _window_start(cfg):
    settings = cloud_policy._cfg(cfg)
    if settings["window_budget_usd"] is None or settings["window_start"] is None:
        return None
    return until_epoch(settings["window_start"])


def _carryover(accounting, window_start):
    """Totals by day, period side, provider and settlement for the reservations about to be removed.

    Each total is dated at its latest reservation, so its day and its side of the period start
    stay exact. Moving the period start later can only count more spending, never less.
    """
    groups = {}
    for rid, (meta, row) in accounting.reservations.items():
        maximum = cloud_policy._reserve_valid(meta, row)
        results = accounting.results.get(rid)
        cost, settled = cloud_policy._settled(meta, maximum, results) if results else (maximum, False)
        provider = cloud_policy._price_definition(meta["model"], meta["price_version"])["provider"]
        at = row["created_at"]
        key = (cloud_policy._periods(at)[0], window_start is not None and at >= window_start,
               provider, bool(settled))
        latest, requests, total = groups.get(key, (at, 0, Decimal(0)))
        groups[key] = (max(latest, at), requests + 1, total + cost)
    return [{"at": at, "provider": provider, "requests": requests, "cost_usd": str(total), "settled": settled}
            for (_day, _side, provider, settled), (at, requests, total) in sorted(groups.items())]


def _amounts(value):
    """Reported spending with every amount as a number, so equal sums compare equal."""
    if isinstance(value, dict):
        return {key: Decimal(item) if key.endswith("_usd") and isinstance(item, str) else _amounts(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_amounts(item) for item in value]
    return value


def _spend(db, cfg):
    """The spending every limit compares against, read inside the caller's transaction."""
    ledger = SimpleNamespace(db=db)
    out = {"day": cloud_policy.usage_report(ledger, period="day"),
           "month": cloud_policy.usage_report(ledger, period="month")}
    if out["day"]["pricing_unverified"] or out["month"]["pricing_unverified"]:
        raise ValueError("accounting_unverified")
    if cloud_policy._cfg(cfg)["window_budget_usd"] is not None:
        out["window"] = cloud_policy.window_spend(ledger, cfg)
    return _amounts(out)


def _quality_usage(db):
    """Jev usage rows of patient-context quality checks as (artifact_id, created_at, meta)."""
    return [(row["artifact_id"], row["created_at"], json.loads(row["meta"])) for row in db.execute(
        "SELECT artifact_id,created_at,meta FROM artifacts WHERE kind='semantic_usage' "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.operation') END IN (?,?)", QUALITY_OPERATIONS)]


def _work_refusal(db, cfg, now):
    """The first fixed reason, read without the accounting, that forbids the reset now, or None."""
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
    return None


def _accounting_refusal(db, now, accounting, quality):
    if cloud_policy.usage_report(SimpleNamespace(db=db))["pricing_unverified"]:
        return "accounting_unverified"
    # Count every accounting row: a partial count is not evidence that none is unreadable.
    if cloud_policy.evidence_hold_counts(db, now=now, scan_limit=1 << 62)["unattributable_accounting_rows"] != 0:
        return "accounting_unverified"
    if any(rid not in accounting.results and row["created_at"] > now - IN_FLIGHT_S
           for rid, (_meta, row) in accounting.reservations.items()):
        return "cloud_in_flight"
    if any("quality_result" not in meta and created_at > now - IN_FLIGHT_S for _id, created_at, meta in quality):
        return "jev_in_flight"
    return None


def _plan(db, cfg, now, stop):
    """(preview, accounting) in one read of each source; `stop` errors propagate unclassified."""
    accounting, carryover = None, []
    try:
        refusal = _work_refusal(db, cfg, now)
        quality = _quality_usage(db)
        accounting = _accounting(db)
        refusal = refusal or _accounting_refusal(db, now, accounting, quality)
        carryover = _carryover(accounting, _window_start(cfg))
    except stop:
        raise
    except _UNVERIFIED as error:
        refusal = str(error) if str(error) in REFUSALS else "accounting_unverified"
        accounting, quality = None, []
    kinds = {kind: db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (kind,)).fetchone()[0]
             for kind in ERASED_KINDS}
    from patient_context_refresh_readiness import lane_metrics
    try:
        outcomes = lane_metrics(db)
    except stop:
        raise
    except ValueError:
        outcomes = None
    body = {"refusal": refusal, "kinds": kinds, "quality_claims": db.execute(
                "SELECT COUNT(*) FROM fetch_jobs WHERE kind=?", (state.QUALITY_CLAIM,)).fetchone()[0],
            "accounting_rows": len(accounting.ids) if accounting else 0,
            "reservations": len(accounting.reservations) if accounting else 0,
            "unsettled_reservations": sum(entry["requests"] for entry in carryover if not entry["settled"]),
            "quality_usage_rows": len(quality), "carryover": carryover, "outcomes": outcomes}
    preview = {**body, "preview_hash": payload_hash({key: body[key] for key in body if key != "refusal"})}
    return preview, accounting, quality


def preview(db, *, cfg=None, now, stop=()):
    """Counts of what the reset removes and carries, without identifiers or content.

    `stop` are the caller's reader-limit errors: they propagate instead of reading as a refusal."""
    return _plan(db, load_config() if cfg is None else cfg, now, tuple(stop))[0]


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
            or not backup["name"].startswith(BACKUP_PREFIX) or not backup["name"].endswith(".db")
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
        # this reset's own copy: taken by the CLI just before, so modified within the last hour
        if (backup.is_symlink() or not backup.is_file() or os.path.getsize(backup) != req["backup"]["bytes"]
                or not now - BACKUP_MAX_AGE_S <= os.path.getmtime(backup) <= now + BACKUP_CLOCK_SKEW_S):
            return "backup_missing", {}
    except OSError:
        return "backup_missing", {}
    found, accounting, quality = _plan(db, cfg, now, ())
    if found["refusal"] is not None:
        return found["refusal"], {}
    if found["preview_hash"] != req["preview_hash"]:
        return "preview_changed", {}
    db.execute("SAVEPOINT patient_context_reset")
    try:
        before = _spend(db, cfg)
        for start in range(0, len(accounting.ids), 500):
            page = accounting.ids[start:start + 500]
            db.execute(f"DELETE FROM artifacts WHERE artifact_id IN ({_placeholders(page)})", page)
        if found["carryover"]:
            db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                       (cloud_policy.CARRYOVER_KIND, json.dumps({"schema": cloud_policy.CARRYOVER_KIND,
                        "entries": found["carryover"]}), "", json.dumps({"command_id": req["command_id"]}), now))
        today = cloud_policy._periods(now)[0]
        for artifact_id, created_at, meta in quality:
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
    except _UNVERIFIED as error:
        db.execute("ROLLBACK TO patient_context_reset")
        db.execute("RELEASE patient_context_reset")
        return (str(error) if str(error) in REFUSALS else "accounting_unverified"), {}
    db.execute("RELEASE patient_context_reset")
    return None, {"reason": req["reason"], "backup": req["backup"],
                  "erased": {"kinds": found["kinds"], "quality_claims": found["quality_claims"],
                             "accounting_rows": found["accounting_rows"],
                             "quality_usage_rows": found["quality_usage_rows"]},
                  "carryover": found["carryover"], "outcomes": found["outcomes"]}
