"""Read-only local view of complete project metadata and independent fetch state."""
from __future__ import annotations

import json
from datetime import date
import math
import time

from mcs_adapter import SchemaError
from project_metadata import ARTIFACT_KIND, JSON, MAX_ROWS, metadata_target, normalize_rows

_CLINICAL = ("medication_periods", "observation_items", "observation_values")


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 600


def _chat_item(item, *, medication: bool, rule: bool) -> dict[str, JSON] | None:
    """Allowlist stored candidates; no master identity, unit conversion or authority."""
    from mcs_queries import item_unverified

    if not isinstance(item, dict) or not _text(item.get("name")):
        return None
    fields = (("dose", "action", "status", "subject", "route", "freq") if medication
              else ("unit", "flag"))
    if any(item.get(key) is not None and not _text(item[key]) for key in fields):
        return None
    if medication:
        if (any(item.get(key) is not None and type(item[key]) is not bool
                for key in ("prn", "negated"))
                or item.get("action") not in
                (None, "start", "stop", "change", "decrease", "increase", "none", "no_change")
                or item.get("status") not in (None, "current", "past", "planned")
                or item.get("subject") not in (None, "patient", "family", "other")):
            return None
        if item.get("subject") in ("family", "other"):
            return None  # other-person mentions cannot populate a patient comparison
        keys = ("name", *fields, "prn", "negated")
    else:
        value = item.get("value")
        try:
            numeric = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            numeric = False
        if not (numeric or _text(value)) or item.get("flag") not in (None, "high", "low"):
            return None
        keys = ("name", "value", *fields)
    row: dict[str, JSON] = {key: item.get(key) for key in keys}
    row.update(kind="medication" if medication else "lab", candidate=True,
               confirmation="unconfirmed", source_unverified=rule or item_unverified(item))
    if not medication:
        # The extraction's persisted date is not the message posting date.
        # Keep its label separate; do not run a new normalizer or infer units.
        normalized = item.get("normalized")
        sampled = normalized.get("measured_on") if isinstance(normalized, dict) else None
        if isinstance(sampled, str):
            try:
                if date.fromisoformat(sampled).isoformat() != sampled:
                    sampled = None
            except ValueError:
                sampled = None
        else:
            sampled = None
        row["measured_on"] = sampled
    return row


def _chat_candidates(db, project_id: int, dataset: str, now: float,
                     max_age_s: float) -> dict[str, JSON]:
    """Bounded same-snapshot candidates with existing current-generation selection."""
    from structured_view import fact_generations, latest_artifact, latest_fact_artifact

    messages = db.execute(
        "SELECT message_id,posted_at,posted_at_ts,content_hash FROM messages "
        "WHERE project_id=? ORDER BY posted_at_ts DESC,message_id DESC LIMIT ?",
        (project_id, MAX_ROWS + 1)).fetchall()
    truncated = len(messages) > MAX_ROWS
    messages = messages[:MAX_ROWS]
    generations = fact_generations(db, [message["message_id"] for message in messages])
    rows: list[JSON] = []
    sources: list[JSON] = []
    missing, excluded = 0, 0
    medication = dataset == "medication_periods"
    for message in messages:
        mid = message["message_id"]
        kinds = generations.get(mid, {})
        kind = next((key for key in ("semantic_facts_v4", "canonical_projection", "extract_llm")
                     if key in kinds), None)
        document = latest_fact_artifact(db, mid) if kind else None
        if kind is None and "extract_v1" in kinds:
            kind = "extract_v1"
            document = latest_artifact(db, kind, mid)
        # Empty selected generations never resurrect older extraction rows.
        if kind is None or document is None or document.get("_error"):
            missing += 1
            continue
        ts = message["posted_at_ts"]
        age = now - ts if type(ts) in (int, float) and math.isfinite(ts) else None
        provenance: dict[str, JSON] = {
            "source": "chat_extraction", "source_kind": kind,
            "source_artifact_id": kinds[kind], "message_id": mid,
            "content_hash": message["content_hash"], "generation_current": True,
            "posted_at": message["posted_at"], "posted_at_ts": ts,
            "age_s": max(0, age) if age is not None else None,
            "stale": not 0 <= age <= max_age_s if age is not None else None,
            "time_basis": "message_posted_at"}
        sources.append(provenance)
        key = "medications" if medication and kind == "extract_v1" else (
            "meds" if medication else "labs")
        items = document.get(key, [])
        if not isinstance(items, list):
            excluded += 1
            continue
        if len(items) > MAX_ROWS:
            truncated = True
        for item in items[:MAX_ROWS]:
            candidate = _chat_item(item, medication=medication, rule=kind == "extract_v1")
            if candidate is None:
                excluded += 1
                continue
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            rows.append({**candidate, "provenance": provenance})
    return {"source": "chat_extraction", "scope": "project",
            "field": "meds" if medication else "labs",
            "state": ("partial" if sources else "unknown") if (
                missing or excluded or truncated) else (
                "available" if rows else "empty" if sources else "unknown"),
            "rows": rows, "generations": sources, "messages_considered": len(messages),
            "unavailable_generations": missing, "excluded_items": excluded,
            "truncated": truncated, "limit": MAX_ROWS, "absence_confirmed": False,
            "item_association": "not_inferred"}


def get_project_metadata(db, project_id: int, dataset: str, *,
                         item_id: int | None = None, show_names: bool = False,
                         include_chat: bool = False,
                         now: float | None = None, max_age_s: float = 86400) -> dict[str, JSON]:
    """Show last complete data as historical after failures/expiry, never as current.

    Reads ledger or snapshot only. Empty is a complete empty acquisition; unknown
    is unacquired/unverified; failed is a fetch/schema failure; stale is an aged
    complete acquisition. Group consultations have patient_association='unknown'.
    include_chat adds separate unconfirmed candidates for clinical datasets only;
    omitted/False never inspects chat. No identity matching or authority is assigned.
    """
    from mcs_adapter import _valid_id

    try:
        invalid = (type(show_names) is not bool or type(include_chat) is not bool
            or (include_chat and dataset not in _CLINICAL)
            or type(max_age_s) not in (int, float)
            or not math.isfinite(max_age_s) or max_age_s < 0
            or (now is not None and (type(now) not in (int, float)
                                    or not math.isfinite(now) or now < 0))
            or (dataset == "observation_values" and not _valid_id(item_id))
            or (dataset != "observation_values" and item_id is not None))
    except OverflowError:
        invalid = True
    if invalid:
        raise ValueError("invalid metadata view options")
    target = metadata_target(db, project_id, dataset)
    result: dict[str, JSON] = {"state": "unknown", "reason": "not_fetched", "rows": [],
              "last_complete_at": None, "attempted_at": None, "age_s": None,
              "current_known": False, "historical": False, "stale": None,
              "definition": None, "http_status": None,
              "patient_association": "unknown" if dataset == "consultations" else "inventory",
              "source": "mcs_structured" if dataset.startswith(("medication", "observation")) else "mcs",
              "chat_comparison": "not_compared"}
    provenance: dict[str, JSON] | None = None
    if include_chat:
        result["chat_comparison"] = "side_by_side_unconfirmed"
        result["comparison"] = {"relationship": "unconfirmed", "matching": "not_performed",
                                "authority": "not_assigned", "absence_confirmed": False}
        provenance = {
            "source": "mcs_structured", "dataset": dataset,
            "entity_id": target["entity_id"] if target else None, "item_id": item_id,
            "attempt_artifact_id": None, "complete_artifact_id": None}
        result["structured_provenance"] = provenance
        result["chat_candidates"] = (_chat_candidates(
            db, project_id, dataset, time.time() if now is None else now, max_age_s)
            if target else {"source": "chat_extraction", "state": "unknown", "rows": [],
                            "reason": "association_unverified", "absence_confirmed": False})
    if target is None:
        result["reason"] = "association_unverified"
        return result
    result["scope"] = target["scope"]
    # Invalid JSON cannot be evidence for any dataset or target.
    base = ("SELECT artifact_id,content FROM artifacts WHERE kind=? AND project_id=? "
            "AND CASE WHEN json_valid(content) THEN "
            "json_extract(content,'$.dataset')=? "
            "AND json_extract(content,'$.entity_id')=? "
            "AND json_extract(content,'$.item_id') IS ? ELSE 0 END ")
    params = (ARTIFACT_KIND, project_id, dataset, target["entity_id"], item_id)
    latest = db.execute(base + "ORDER BY artifact_id DESC LIMIT 1", params).fetchone()
    complete = db.execute(base + "AND json_extract(content,'$.complete')=1 "
                          "ORDER BY artifact_id DESC LIMIT 1", params).fetchone()
    if latest is None:
        return result
    if provenance is not None:
        provenance["attempt_artifact_id"] = latest["artifact_id"]
    try:
        attempt = json.loads(latest["content"])
        if dataset == "consultations" and (
                attempt.get("scope") != "group"
                or not _valid_id(attempt.get("entity_id"))
                or ("project_type" in attempt and attempt["project_type"] != "group")
                or attempt.get("attempted_at", -1) < 0
                or (attempt.get("complete") is True and (
                    attempt.get("reason") is not None or attempt.get("http_status") is not None))):
            raise SchemaError("metadata view: group scope/state invalid")
        if (attempt.get("contract") != "project-metadata/1"
                or type(attempt.get("complete")) is not bool
                or type(attempt.get("attempted_at")) not in (int, float)
                or not math.isfinite(attempt["attempted_at"])
                or attempt.get("reason") not in {
                    None, "schema_error", "http_error", "forbidden", "session_expired",
                    "network_error", "deadline_exceeded", "fetch_error",
                    "snapshot_missing", "page_limit"} | (
                        {"scope_changed"} if dataset == "consultations" else set())
                or (attempt.get("http_status") is not None
                    and (type(attempt["http_status"]) is not int
                         or not 100 <= attempt["http_status"] <= 599))):
            raise SchemaError("metadata view: contract invalid")
        result["attempted_at"] = attempt["attempted_at"]
        result["reason"] = attempt["reason"]
        result["http_status"] = attempt.get("http_status")
        result["state"] = "failed"
        if dataset == "consultations" and attempt["reason"] in ("snapshot_missing", "page_limit"):
            result["state"] = "partial"
        if complete is not None:
            payload = json.loads(complete["content"])
            if dataset == "consultations" and (
                    payload.get("scope") != "group"
                    or not _valid_id(payload.get("entity_id"))
                    or ("project_type" in payload and payload["project_type"] != "group")
                    or payload.get("complete") is not True
                    or payload.get("attempted_at", -1) < 0
                    or payload.get("reason") is not None or payload.get("http_status") is not None):
                raise SchemaError("metadata view: complete group scope/state invalid")
            fetched_at = payload["attempted_at"]
            if (payload.get("contract") != "project-metadata/1"
                    or type(fetched_at) not in (int, float) or not math.isfinite(fetched_at)):
                raise SchemaError("metadata view: freshness invalid")
            rows = payload["rows"]
            if dataset == "care_team":
                rows = [{"id": row["id"], "type": row.get("type"),
                         "is_director": row.get("is_director"), "is_self": row.get("is_self"),
                         "station": None if row.get("facility") is None else {"name": row["facility"]},
                         "specialist_categories": None if row.get("professions") is None else
                         [{"name": name} for name in row["professions"]],
                         **{key: row.get(key) for key in ("last_name", "first_name")}}
                        for row in rows]
            result["rows"] = normalize_rows(dataset, rows,
                retain_names=show_names and payload.get("names_retained") is True)
            if dataset == "observation_values" and payload.get("definition") is not None:
                definition = normalize_rows("observation_items", [payload["definition"]])[0]
                lab = definition.get("lab_test_item")
                if not isinstance(lab, dict) or lab.get("id") != item_id:
                    raise SchemaError("metadata view: value item mismatch")
                result["definition"] = definition
            age = (time.time() if now is None else now) - fetched_at
            result.update(last_complete_at=fetched_at, age_s=max(0, age))
            fresh = 0 <= age <= max_age_s
            result["stale"] = not fresh
            if attempt.get("complete") is True:
                result["state"] = ("complete" if result["rows"] else "empty") if fresh else "stale"
            result["current_known"] = fresh and attempt.get("complete") is True
            result["historical"] = not result["current_known"]
            if provenance is not None:
                provenance["complete_artifact_id"] = complete["artifact_id"]
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError, OverflowError, SchemaError):
        result.update(state="unknown", reason="artifact_invalid", rows=[],
                      definition=None, http_status=None, attempted_at=None,
                      current_known=False, historical=False, stale=None)
    return result


def physician_viewed_status(db, project_id: int, message_id: int, *,
                            now: float, max_age_s: float = 86400) -> dict[str, JSON]:
    """Count care-team physicians whose 「見ました」 is not observed on one post.

    Known only when the care-team roster and the reactor set are both complete
    and current; otherwise unknown with a fixed reason. A roster ID counts as
    observed only when the viewed reactor's recorded profession is also 医師.
    Not observed is not "not read", and no names or IDs are returned.
    """
    from ledger import reaction_actor_summary

    out: dict[str, JSON] = {"state": "unknown", "reason": None, "physicians": None,
                            "viewed_observed": None, "not_observed": None}
    team = get_project_metadata(db, project_id, "care_team", now=now, max_age_s=max_age_s)
    if not team["current_known"]:
        out["reason"] = "care_team_not_current"
        return out
    if reaction_actor_summary(db, message_id, now=now)["state"] != "complete":
        out["reason"] = "reactors_not_current"
        return out
    rows = team["rows"]
    assert isinstance(rows, list)
    if any(not isinstance(row, dict) or row.get("professions") is None for row in rows):
        out["reason"] = "care_team_profession_unknown"   # unknown is not "not a physician"
        return out
    physicians = {row["id"] for row in rows if isinstance(row, dict)
                  and "医師" in (row.get("professions") or [])}
    viewed = {actor for actor, prof in db.execute(
        "SELECT actor_id,profession FROM message_reaction_actors "
        "WHERE message_id=? AND reaction_type='viewed'", (message_id,))
        if isinstance(prof, str) and "医師" in prof.split(", ")}
    seen = len(physicians & viewed)
    out.update(state="known", physicians=len(physicians), viewed_observed=seen,
               not_observed=len(physicians) - seen)
    return out
