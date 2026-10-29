"""Identify the route of the fact artifact selected for a visible chat post."""
from __future__ import annotations

import sqlite3

from mcs_queries import FACT_KINDS_SQL, current_fact_pred
from mcs_util import strict_json


def _route(db, project_id, message_id):
    row = db.execute(
        "SELECT a.kind,a.model,a.meta,a.content FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id AND m.project_id=a.project_id WHERE a.message_id=? AND m.project_id=? "
        f"AND a.kind IN ({FACT_KINDS_SQL}) AND m.body_state IS NOT 'deleted' "
        "AND CASE WHEN json_valid(a.content) THEN json_type(a.content)='object' ELSE 0 END "
        + current_fact_pred('a', 'm') + " ORDER BY a.artifact_id DESC LIMIT 1",
        (message_id, project_id)).fetchone()
    if row is None:
        return None
    try:
        meta = strict_json(row["meta"])
        content = strict_json(row["content"])
    except (TypeError, ValueError, RecursionError):
        return None
    if (not isinstance(meta, dict) or not isinstance(content, dict) or not content
            or not isinstance(row["model"], str) or not row["model"]):
        return None
    if row["kind"] != "extract_llm":
        # Derived rows can reuse mixed stages; their model column is not provenance.
        return None
    if meta.get("actual_model") != row["model"]:
        return None
    backend = meta.get("backend")
    import cloud_policy
    if backend is None:
        return None if row["model"] in cloud_policy.PROVIDERS.values() else "local"
    if backend not in cloud_policy.PROVIDERS or cloud_policy.PROVIDERS[backend] != row["model"]:
        return None
    import extract_llm
    contract = extract_llm._cloud_contract({"cloud_llm": {"mode": "off", "provider": backend}})
    return "cloud" if all(meta.get(key) == value for key, value in contract.items()) else None


def icon(db, project_id, message_ids):
    """Show a single route only when every visible post has the same proven route."""
    if not message_ids:
        return "💬"
    try:
        routes = {_route(db, project_id, mid) for mid in message_ids}
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return "💬"
    return {frozenset({"local"}): "💻", frozenset({"cloud"}): "☁️"}.get(frozenset(routes), "💬")
