"""Shared snapshot statistics scope, SQL windows and aggregate result contracts.

Pure values and transformations only. Callers borrow one validated snapshot
connection; this module never connects, commits, closes, schedules or reads files.
"""
from __future__ import annotations

from datetime import datetime

from mcs_queries import JST

DEFINITION_VERSION = "2026-10-17"
DETAIL_LIMIT = 100


def ratio(num: int, den: int, unit: str) -> dict:
    """Ratio with explicit denominator — 0 denominator is null, not 0%."""
    return {"numerator": num, "denominator": den, "unit": unit,
            "value": (num / den) if den else None,
            "reason": None if den else "denominator_zero"}



def parse_timestamp(text: str) -> int:
    """YYYY-MM-DD -> JST midnight; datetime requires an explicit offset.
    A bare `until` date is that day's 0:00 — to include a whole day the
    caller passes the next date (A-6: since <= t < until)."""
    if not isinstance(text, str):
        raise ValueError("bad_time_arg")
    try:
        if "T" not in text and " " not in text:
            d = datetime.strptime(text, "%Y-%m-%d")
            return int(d.replace(tzinfo=JST).timestamp())
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        if dt.tzinfo is None:
            raise ValueError("bad_time_arg")
        return int(dt.timestamp())
    except (ValueError, OverflowError):
        raise ValueError("bad_time_arg") from None



def resolve_scope(args: dict, snapshot_ts: int) -> dict:
    """Resolve since/until/as_of once. Half-open [since, until) —
    including a whole day needs the next date as `until`. as_of defaults
    to the snapshot's generated_at and may not exceed it (A-6)."""
    since = parse_timestamp(args["since"]) if args.get("since") else None
    until = parse_timestamp(args["until"]) if args.get("until") else None
    if since is not None and until is not None and until <= since:
        raise ValueError("bad_period")
    as_of = parse_timestamp(args["as_of"]) if args.get("as_of") else snapshot_ts
    if as_of > snapshot_ts:
        raise ValueError("as_of_after_snapshot")
    if until is not None and until > as_of:
        until = as_of
        # clamping may invert the range — surface it, don't silently
        # return an empty success
        if since is not None and until <= since:
            raise ValueError("bad_period")
    try:
        limit = min(max(int(args.get("limit") or 20), 1), DETAIL_LIMIT)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("bad_limit") from None
    return {"since": since, "until": until, "as_of": as_of,
            "project_id": args.get("project"), "limit": limit}



def message_scope_sql(scope: dict, col: str = "m.posted_at_ts") -> tuple[str, list]:
    sql, params = "", []
    if scope["since"] is not None:
        sql += f" AND {col} >= ?"
        params.append(scope["since"])
    if scope["until"] is not None:
        sql += f" AND {col} < ?"
        params.append(scope["until"])
    elif scope.get("as_of") is not None:
        # as_of freezes the window even without --until (refstats pins
        # it): nothing posted after it counts; undated rows keep their
        # pre-existing treatment
        sql += f" AND ({col} IS NULL OR {col} <= ?)"
        params.append(scope["as_of"])
    if scope["project_id"] is not None:
        sql += " AND m.project_id = ?"
        params.append(scope["project_id"])
    return sql, params



def limited_items(rows: list, limit: int) -> dict:
    return {"total": len(rows), "returned": min(len(rows), limit),
            "truncated": len(rows) > limit, "items": rows[:limit]}



def stat_result(status: str, scope: dict, data: dict, reason=None) -> dict:
    out = {"status": status, "definition_version": DEFINITION_VERSION,
           "scope": {"since": scope["since"], "until": scope["until"],
                     "as_of": scope["as_of"],
                     "project_id": scope["project_id"]}}
    if reason:
        out["reason"] = reason
    out.update(data)
    return out
