"""Observe retained cloud reservations without exposing their payloads."""
from __future__ import annotations

import math
import sqlite3
import time

import cloud_policy as policy
from mcs_util import strict_json

# Bound retained evidence independently of the reader's time and output budgets.
MAX_ROWS = 100000
_VALID_META = ("CASE WHEN typeof(meta)='text' AND length(CAST(meta AS BLOB))<=32768 "
               "AND json_valid(meta) THEN CASE WHEN json_type(meta)='object' THEN meta END END")


def _unknown(reason):
    return {"state": "unknown", "reason": reason, "scope": "retained_records",
            "unresolved_requests": None, "missing_results": None, "unsettled_results": None,
            "settled_requests": None, "oldest_unresolved_age_s": None, "unresolved_over_10m": None}


def _record(meta, project_id, message_id, model, created, now):
    if (not isinstance(meta, str) or len(meta.encode("utf-8")) > 32768
            or type(created) not in (int, float) or not math.isfinite(created)
            or not 0 <= created <= now):
        raise ValueError("record_invalid")
    value = strict_json(meta)
    if not isinstance(value, dict):
        raise ValueError("metadata_invalid")
    return value, {"project_id": project_id, "message_id": message_id,
                   "model": model, "created_at": created}


def observe(db, *, now=None):
    """Validate bounded retained reservations and return aggregate counts only."""
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("observation_time_invalid")
    if not db.in_transaction:
        raise sqlite3.DatabaseError("observation_snapshot_required")

    def classify(reserve_count, result_count, *values):
        try:
            if reserve_count != 1 or result_count > 1:
                return "invalid"
            meta, row = _record(*values[:5], now)
            maximum = policy._reserve_valid(meta, row)
            if not result_count:
                return "missing"
            result, result_row = _record(*values[5:], now)
            if result_row["created_at"] < row["created_at"]:
                return "invalid"
            _, settled = policy._settled(meta, maximum, [(result, result_row)])
            return "settled" if settled else "unsettled"
        except (policy.AnthropicError, ValueError, TypeError, ArithmeticError, RecursionError):
            return "invalid"

    # Group small identities, then fetch each bounded proof pair by primary key.
    # Raw proof payloads never cross the reader boundary or accumulate in Python.
    query = f"""
        WITH identities AS MATERIALIZED (
            SELECT artifact_id,kind,json_extract({_VALID_META},'$.reservation_id') AS rid
            FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')
            LIMIT ?
        ), pairs AS (
            SELECT rid,COUNT(*) AS records,
                   SUM(kind='cloud_usage_reserve') AS nr,
                   SUM(kind='cloud_usage_result') AS nt,
                   MIN(CASE WHEN kind='cloud_usage_reserve' THEN artifact_id END) AS reserve_id,
                   MIN(CASE WHEN kind='cloud_usage_result' THEN artifact_id END) AS result_id
            FROM identities GROUP BY rid
        ), classified AS MATERIALIZED (
            SELECT pairs.records,r.created_at,
                   mcs_cloud_observe(nr,nt,
                       CASE WHEN length(CAST(r.meta AS BLOB))<=32768 THEN r.meta END,
                       r.project_id,r.message_id,r.model,r.created_at,
                       CASE WHEN length(CAST(t.meta AS BLOB))<=32768 THEN t.meta END,
                       t.project_id,t.message_id,t.model,t.created_at) AS state
            FROM pairs LEFT JOIN artifacts r ON r.artifact_id=reserve_id
                       LEFT JOIN artifacts t ON t.artifact_id=result_id
        )
        SELECT COALESCE(SUM(records),0) AS records_scanned,
               COALESCE(SUM(state='invalid'),0) AS invalid_groups,
               COALESCE(SUM(state='missing'),0) AS missing_results,
               COALESCE(SUM(state='unsettled'),0) AS unsettled_results,
               COALESCE(SUM(state='settled'),0) AS settled_requests,
               MAX(CASE WHEN state IN ('missing','unsettled') THEN ?-created_at END) AS oldest_age,
               COALESCE(SUM(state IN ('missing','unsettled') AND ?-created_at>=600),0) AS aged
        FROM classified
    """
    db.create_function("mcs_cloud_observe", 12, classify)
    try:
        row = db.execute(query, (MAX_ROWS + 1, now, now)).fetchone()
        if row["records_scanned"] > MAX_ROWS:
            return _unknown("observation_limit")
        if row["invalid_groups"]:
            return {**_unknown("invalid_or_unmatched_records"), "state": "partial",
                    "records_scanned": row["records_scanned"],
                    "invalid_groups_observed": row["invalid_groups"]}
        return {"state": "complete", "reason": None, "scope": "retained_records",
                "records_scanned": row["records_scanned"],
                "unresolved_requests": row["missing_results"] + row["unsettled_results"],
                "missing_results": row["missing_results"], "unsettled_results": row["unsettled_results"],
                "settled_requests": row["settled_requests"],
                "oldest_unresolved_age_s": row["oldest_age"], "unresolved_over_10m": row["aged"]}
    except (ValueError, sqlite3.Error):
        return _unknown("observation_limit_or_read_failure")
    finally:
        db.create_function("mcs_cloud_observe", 12, None)
