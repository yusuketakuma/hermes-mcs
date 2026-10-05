"""本人宛と確認できる投稿の返信・UI操作の観測不足を公開snapshotから一覧する。"""
from __future__ import annotations

import base64
import json
import math
from typing import TypedDict

import mcs_signals
from mcs_requests import canonical, payload_hash, positive, valid_hash
from message_metadata import (
    REACTION_LABELS, _timestamp, get_message_metadata, mentions_self,
)
from read_model import _snapshot_meta, _table_exists

SCHEMA = "mcs-response-observation-list/1"


class _Counts(TypedDict):
    scanned: int
    self_target: int
    other_target: int
    unknown_target: int
    observed_pairs: int
    listed: int


class _Result(TypedDict):
    schema: str
    state: str
    reason: str | None
    items: list[dict[str, object]]
    next_cursor: str | None
    as_of: float | None
    counts: _Counts
    unknown_reasons: dict[str, int]
    clinical_completion: None
    nonresponse: None
    unread: None
    warnings: list[str]


def _result(state: str, reason: str | None = None) -> _Result:
    return {
        "schema": SCHEMA, "state": state, "reason": reason,
        "items": [], "next_cursor": None, "as_of": None,
        "counts": {"scanned": 0, "self_target": 0, "other_target": 0,
                   "unknown_target": 0, "observed_pairs": 0, "listed": 0},
        "unknown_reasons": {},
        "clinical_completion": None, "nonresponse": None, "unread": None,
        "warnings": ["observation_not_clinical_status", "unread_not_inferred",
                     "history_completeness_unverified"],
    }


def _field_unknown(metadata, field, row, as_of, max_age_s):
    if metadata[field + "_status"] != "observed":
        return field + "_unknown"
    if metadata["last_error"]:
        return "capture_incomplete"
    at = _timestamp(metadata[field + "_observed_at"])
    checked = _timestamp(metadata["checked_at"])
    body_at = _timestamp(row["updated_seen"])
    if not valid_hash(row["content_hash"]) or body_at is None:
        return "source_binding_unknown"
    if at is None or checked is None or not body_at <= at <= checked <= as_of:
        return "source_binding_unknown"
    if row["revision_hash"] is not None:
        revision_at = _timestamp(row["revision_at"])
        if (row["revision_hash"] != row["content_hash"] or revision_at is None
                or at <= revision_at):
            return "source_binding_unknown"
    if as_of - at > max_age_s or as_of - checked > max_age_s:
        return "observation_stale"
    return None


def get_response_observation_list(
        db, *, enabled=False, projects=None, limit=50, cursor=None, max_age_s=None):
    """公開offはSQLを実行しない。最大limit件を走査し、snapshot/scope束縛cursorを返す。"""
    if enabled is not True:
        return _result("disabled", "publication_disabled")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("bad_limit")
    if projects is not None and (not isinstance(projects, (list, tuple))
                                 or any(not positive(pid) for pid in projects)):
        raise ValueError("bad_project_scope")
    scope = None if projects is None else sorted(set(projects))
    meta = _snapshot_meta(db)
    as_of = _timestamp(meta["generated_at"])
    if not meta["published"] or not meta["generation_id"] or as_of is None:
        return _result("unavailable", "published_snapshot_required")
    result = _result("complete")
    result["as_of"] = as_of
    if max_age_s is None:
        result.update(state="unknown", reason="freshness_policy_unknown")
        return result
    try:
        valid_age = (isinstance(max_age_s, (int, float)) and not isinstance(max_age_s, bool)
                     and math.isfinite(max_age_s) and max_age_s >= 0)
    except OverflowError:
        valid_age = False
    if not valid_age:
        raise ValueError("bad_max_age_s")
    max_age_s = float(max_age_s)
    binding = payload_hash([meta["generation_id"], SCHEMA, scope, max_age_s])
    key = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not 0 < len(cursor) <= 2048:
                raise ValueError
            decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            keys = decoded["key"]
            if (decoded["binding"] != binding or not isinstance(keys, list)
                    or len(keys) != 1 or not positive(keys[0])):
                raise ValueError
            key = keys[0]
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("cursor_scope_or_generation_changed") from None
    if scope == []:
        return result
    self_id = mcs_signals.self_sender_id(db)
    if self_id is None:
        result.update(state="unknown", reason="self_identity_unknown")
        return result
    if not _table_exists(db, "message_revisions"):
        result.update(state="unknown", reason="source_history_unknown")
        return result
    sql = (
        "SELECT m.message_id,m.project_id,m.content_hash,m.updated_seen,m.body_state,"
        "p.fetch_state,r.content_hash revision_hash,r.observed_at revision_at "
        "FROM messages m JOIN patients p ON p.project_id=m.project_id "
        "LEFT JOIN message_revisions r ON r.message_id=m.message_id "
        "AND r.seq=(SELECT MAX(v.seq) FROM message_revisions v WHERE v.message_id=m.message_id) "
        "WHERE COALESCE(p.is_archived,0)=0 AND m.body_state IS NOT 'deleted'")
    params = []
    if scope is not None:
        sql += " AND m.project_id IN (" + ",".join("?" for _ in scope) + ")"
        params.extend(scope)
    if key is not None:
        sql += " AND m.message_id<?"
        params.append(key)
    rows = db.execute(sql + " ORDER BY m.message_id DESC LIMIT ?", (*params, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        result["counts"]["scanned"] += 1
        mid = row["message_id"]
        metadata = get_message_metadata(db, mid, as_of=as_of)
        reason = ("body_incomplete" if row["body_state"] != "full" else
                  _field_unknown(metadata, "mentions", row, as_of, max_age_s))
        target = mentions_self(metadata, self_id) if reason is None else None
        if target is None:
            result["counts"]["unknown_target"] += 1
            reason = reason or "mentions_unknown"
            result["unknown_reasons"][reason] = result["unknown_reasons"].get(reason, 0) + 1
            continue
        if target is False:
            result["counts"]["other_target"] += 1
            continue
        result["counts"]["self_target"] += 1
        observed = metadata["response_observation"]
        reply = dict(observed["reply"])
        unknown = []
        if observed["reply_time_context"] == "unknown":
            unknown.append("reply_time_context")
            if reply["state"] != "observed":
                reply["state"] = "unknown"
        if reply["state"] == "not_observed" and row["fetch_state"] != "complete":
            reply["state"] = "unknown"
        if reply["state"] == "unknown":
            unknown.append("reply")
        reaction = observed["self_reaction"]
        reaction_reason = _field_unknown(metadata, "reactions", row, as_of, max_age_s)
        if reaction_reason is None and any(r["self_reacted"] and r["count"] == 0
                                           for r in metadata["reactions"]):
            reaction_reason = "reaction_shape_unknown"
        if reaction_reason is None and any(r["type"] not in REACTION_LABELS
                                           for r in metadata["reactions"]):
            reaction_reason = "reaction_type_unknown"
        ui_state = ("unknown" if reaction_reason else
                    "observed" if reaction["types"] else "not_observed")
        if ui_state == "unknown":
            unknown.append("self_ui_action")
            assert reaction_reason is not None
            result["unknown_reasons"][reaction_reason] = (
                result["unknown_reasons"].get(reaction_reason, 0) + 1)
        missing = [name for name, state in (("reply", reply["state"]),
                                            ("self_ui_action", ui_state))
                   if state == "not_observed"]
        if not unknown and not missing:
            result["counts"]["observed_pairs"] += 1
            continue
        result["items"].append({
            "project_id": row["project_id"], "message_id": mid, "target_state": "self",
            "state": "unknown" if unknown else "not_observed",
            "unknown": unknown, "not_observed": missing, "reply": reply,
            "self_reaction": {
                "state": ui_state, "types": reaction["types"],
                "observed_at": reaction["observed_at"], "checked_at": reaction["checked_at"],
                "age_s": reaction["age_s"], "current_state": "unknown", "basis": "ui_operation_only"},
            "clinical_completion": None, "nonresponse": None, "unread": None,
        })
    result["counts"]["listed"] = len(result["items"])
    if result["counts"]["unknown_target"] or any(i["state"] == "unknown" for i in result["items"]):
        result["state"] = "unknown"
    if more:
        result["next_cursor"] = base64.urlsafe_b64encode(
            canonical({"binding": binding, "key": [rows[-1]["message_id"]]})).decode()
    return result
