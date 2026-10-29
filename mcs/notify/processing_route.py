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
        return _projection_route(db, project_id, message_id, meta)
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


def _cloud_receipts(db, ids, project_id, root_id, model):
    import cloud_policy
    if (not isinstance(ids, list) or not ids or len(ids) > 128
            or any(not isinstance(rid, str) or len(rid) != 32 for rid in ids)):
        return False
    for rid in set(ids):
        rows = db.execute("SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve' "
                          "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.reservation_id') END=? "
                          "ORDER BY artifact_id LIMIT 2", (rid,)).fetchall()
        if len(rows) != 1:
            return False
        proof = cloud_policy.reservation_evidence(db, rows[0][0])
        reservation = proof["reservation"]
        if (reservation.get("project_id") != project_id or reservation.get("message_id") != root_id
                or reservation.get("model") != model or reservation.get("route") != "semantic_backlog"):
            return False
    return True


def _projection_route(db, project_id, mid, meta):
    # Rebuild only bounded, unrepaired current documents from the same chunks
    # the semantic worker uses. A mixed or incomplete lineage stays unknown.
    import semantic_extraction as extraction
    import semantic_facts
    import semantic_store
    import semantic_transport
    import semantic_v4
    from inference_version import is_current
    from mcs_util import loads_dict
    from semantic_policy import KIND_FACTS_V2

    msg = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages WHERE project_id=? AND message_id=?",
                     (project_id, mid)).fetchone()
    if msg is None:
        return None
    root = msg[0]
    extent = db.execute("SELECT COUNT(*),COALESCE(SUM(length(body_text)),0) FROM messages "
                        "WHERE project_id=? AND (message_id=? OR parent_id=?)",
                        (project_id, root, root)).fetchone()
    if extent[0] > 64 or extent[1] > 131072:
        return None
    for kind in (KIND_FACTS_V2, "semantic_facts_audit", "semantic_source_manifest", extraction.KIND_CHUNK_V2):
        count = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM artifacts WHERE kind=? AND project_id=? "
                           "AND message_id=? LIMIT 129)", (kind, project_id, mid)).fetchone()[0]
        if count > 128:
            return None
    fp = meta.get("fingerprint")
    local_model = semantic_transport.llm_model()
    bundle = semantic_store.thread_bundle(db, project_id, root, local_model=local_model)
    if bundle is None or bundle["source_fingerprint"] != fp:
        return None
    member = next((item for item in bundle["members"] if item["message_id"] == mid), None)
    if member is None:
        return None
    doc, problem = semantic_v4.reproject_doc(db, mid, meta)
    record = semantic_store.current_artifact(db, KIND_FACTS_V2, mid, fp)
    if problem or doc is None or record is None or record["meta"].get("repaired"):
        return None
    rebuilt, problem = extraction.rebuild_facts_v2_cached(member, db, fp, project_id, doc, record["meta"])
    if problem or rebuilt is None or semantic_facts.doc_hash(rebuilt) != meta.get("doc_hash"):
        return None
    generation = extraction.v2_generation()
    schema = extraction.SCHEMA_VERSION_V2 + ("/coverage-retry" if record["meta"].get("coverage_retry") else "")
    body_hash = semantic_facts.text_sha256(member["body_original"])
    routes, indices = set(), set()
    import cloud_policy
    for row in db.execute("SELECT model,meta FROM artifacts WHERE kind=? AND project_id=? AND message_id=? "
                          "ORDER BY artifact_id", (extraction.KIND_CHUNK_V2, project_id, mid)):
        item = loads_dict(row["meta"])
        if (not item or not is_current(item) or item.get("schema") != schema
                or item.get("generation") != generation or item.get("source_fingerprint") != fp
                or item.get("source_hash") != body_hash or item.get("revision") != member["revision"]
                or item.get("status") != "complete"):
            continue
        index, total, actual = item.get("chunk_index"), item.get("chunks_total"), item.get("actual_model")
        if type(index) is not int or type(total) is not int or not 0 <= index < total <= 64:
            return None
        indices.add(index)
        requests = item.get("inference_requests")
        if actual == row["model"] and actual == local_model and requests == []:
            routes.add("local")
        elif (actual in cloud_policy.PROVIDERS.values() and
              _cloud_receipts(db, requests, project_id, root, actual)):
            routes.add("cloud")
        else:
            return None
    if not indices or len(indices) != total or len(routes) != 1:
        return None
    return next(iter(routes))


def icon(db, project_id, message_ids):
    """Show a single route only when every visible post has the same proven route."""
    if not message_ids:
        return "💬"
    try:
        routes = {_route(db, project_id, mid) for mid in message_ids}
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return "💬"
    return {frozenset({"local"}): "💻", frozenset({"cloud"}): "☁️"}.get(frozenset(routes), "💬")
