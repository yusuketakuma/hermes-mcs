"""Gated read-only cross-list artifacts with independent completeness and freshness."""
from __future__ import annotations

import json
import math
import time

from cross_lists import ARTIFACT_KIND, JSON, CapturedList, _options, validate_capture
from mcs_adapter import SchemaError, _valid_id


def _payload(content: str) -> CapturedList:
    """Validate persisted acquisition evidence; malformed data is never empty."""
    payload = json.loads(content)
    if (not isinstance(payload, dict) or payload.get("contract") != "cross-list/1"
            or payload.get("dataset") not in ("mentioned", "bookmarked")
            or type(payload.get("unread_only")) is not bool
            or type(payload.get("complete")) is not bool
            or type(payload.get("attempted_at")) not in (int, float)
            or not math.isfinite(payload["attempted_at"]) or payload["attempted_at"] < 0
            or type(payload.get("pages")) is not int or not 0 <= payload["pages"] <= 10
            or (payload.get("timestamp") is not None and not _valid_id(payload["timestamp"]))
            or (payload.get("http_status") is not None and (
                type(payload["http_status"]) is not int
                or not 100 <= payload["http_status"] <= 599))
            or payload.get("reason") not in {
                None, "schema_error", "snapshot_missing", "row_limit", "page_limit",
                "session_expired", "http_error", "network_error", "deadline_exceeded",
                "scope_mismatch", "response_too_large", "url_not_allowed"}):
        raise SchemaError("cross list view: contract invalid")
    _options(payload["dataset"], payload["unread_only"])
    payload["rows"] = validate_capture(payload.get("rows"))
    if (payload["complete"] and (payload["reason"] is not None
                                or payload["pages"] < 1 or payload["http_status"] is not None)
            or not payload["complete"] and (
                payload["reason"] is None or payload["rows"])):
        raise SchemaError("cross list view: state invalid")
    return payload


def get_cross_list(db, dataset: str, *, enabled: bool = False,
                   unread_only: bool = False, now: float | None = None,
                   max_age_s: float = 1800) -> dict[str, JSON]:
    """Read local/snapshot observations only after explicit operator publication.

    Failed attempts retain the last complete set as historical. Empty means a
    complete empty response, unknown means unacquired/invalid, and stale is an
    aged complete response. Freshness never proves real-API unread preservation,
    recipient identity, response, or request completion.
    """
    _options(dataset, unread_only)
    try:
        invalid = (type(enabled) is not bool or type(max_age_s) not in (int, float)
            or not math.isfinite(max_age_s) or max_age_s < 0
            or (now is not None and (
                type(now) not in (int, float) or not math.isfinite(now) or now < 0)))
    except OverflowError:
        invalid = True
    if invalid:
        raise ValueError("invalid cross list view options")
    result: dict[str, JSON] = {"state": "unknown", "reason": "not_fetched", "rows": [],
              "current_known": False, "historical": False, "stale": None,
              "last_complete_at": None, "attempted_at": None, "age_s": None,
              "http_status": None, "timestamp": None,
              "unread_preservation": "unverified", "session_preservation": "unverified"}
    if not enabled:
        result.update(state="disabled", reason="publication_disabled")
        return result
    base = ("SELECT content FROM artifacts WHERE kind=? "
            "AND project_id IS NULL AND message_id IS NULL "
            "AND CASE WHEN json_valid(content) THEN "
            "json_extract(content,'$.dataset')=? AND "
            "json_extract(content,'$.unread_only')=? ELSE 1 END ")
    params = (ARTIFACT_KIND, dataset, int(unread_only))
    latest = db.execute(base + "ORDER BY artifact_id DESC LIMIT 1", params).fetchone()
    complete = db.execute(base + "AND CASE WHEN json_valid(content) THEN "
                          "json_extract(content,'$.complete')=1 ELSE 0 END "
                          "ORDER BY artifact_id DESC LIMIT 1", params).fetchone()
    if latest is None:
        return result
    try:
        attempt = _payload(latest["content"])
        result.update(state="failed", reason=attempt["reason"],
                      attempted_at=attempt["attempted_at"], http_status=attempt["http_status"])
        if complete is not None:
            saved = _payload(complete["content"])
            # Never display rows against a contradicting current local identity.
            for row in saved["rows"]:
                stored = db.execute("SELECT project_id,parent_id FROM messages WHERE message_id=?",
                                    (row["message_id"],)).fetchone()
                if stored is not None and (stored["project_id"] != row["project_id"]
                                          or stored["parent_id"] != row["parent_id"]):
                    raise SchemaError("cross list view: scope mismatch")
            age = (time.time() if now is None else now) - saved["attempted_at"]
            fresh = 0 <= age <= max_age_s
            current = fresh and attempt["complete"]
            state = ("complete" if saved["rows"] else "empty") if fresh else "stale"
            result.update(rows=saved["rows"], last_complete_at=saved["attempted_at"],
                          age_s=max(0, age), timestamp=saved["timestamp"], stale=not fresh,
                          current_known=current, historical=not current,
                          state=state if attempt["complete"] else "failed")
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, SchemaError):
        result.update(state="unknown", reason="artifact_invalid", rows=[],
                      current_known=False, historical=False, stale=None,
                      last_complete_at=None, attempted_at=None, age_s=None,
                      http_status=None, timestamp=None)
    return result
