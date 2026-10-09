"""Versioned machine-readable read model over one published snapshot.

T15 — the single provenance/validity contract shared by stats,
pharmacy views and exports. Replaces "markdown or [:4000]-sliced JSON"
as the machine surface: every read binds to the snapshot's
``snapshot_meta.generation_id`` and reports per-record state from a
fixed four-value enum instead of silently falling back to obsolete
projections.

Record states (derived rows, never source rows):
  current   a valid artifact bound to the message's CURRENT content_hash
            (and not invalidated) exists
  stale     artifacts exist but every valid one points at a superseded
            body hash or is invalidated — an edit/revision/policy
            rotation made the stored result non-current
  pending   no artifact row exists for the message at all — absence of
            a record is NOT evidence of absence of a clinical event
  unknown   artifact rows exist but none can be classified (malformed
            meta/content, or error-only rows)

Scopes:
  aggregate  ids/hashes/counts/states only — never body text, sender or
             patient names, statements, evidence quotes, or attachment
             file names (the contract an export/aggregate caller gets)
  detail     adds statements, evidence quotes, sender names and
             attachment names — pharmacist-local surface only

Missing/unmeasurable inputs stay null (unknown), never zero.
"""
from __future__ import annotations

import json
import math
from contextlib import suppress
from semantic_projection import PROJECTION_VERSION

from patient_context import context_items, extract_context, merged_context

CONTRACT = "mcs-read-model/1"
SCOPES = ("aggregate", "detail")
EXTRACTION_KINDS = ("extract_v1", "extract_llm", "canonical_projection",
                    "semantic_facts_v4")

# shared eligibility predicate — the same definition extract_llm uses
# to choose work, so coverage and records can never drift
_ELIGIBLE = ("m.body_text IS NOT NULL AND m.body_text != '' "
             "AND (m.body_state IS NULL OR m.body_state='full')")


def _table_exists(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _snapshot_meta(db) -> dict:
    """The generation this read binds to. A live ledger has no
    snapshot_meta — consumers must distinguish that, not guess."""
    if not _table_exists(db, "snapshot_meta"):
        return {"generation_id": None, "generated_at": None,
                "published": False}
    row = db.execute(
        "SELECT generation_id, generated_at FROM snapshot_meta "
        "WHERE singleton=1").fetchone()
    if (row is None or not isinstance(row[0], str) or not row[0].strip()
            or type(row[1]) not in (int, float)
            or not math.isfinite(row[1]) or row[1] < 0):
        return {"generation_id": None, "generated_at": None,
                "published": False}
    return {"generation_id": row[0], "generated_at": row[1],
            "published": True}


def _canonical_shape(content: dict) -> bool:
    for key, fields in (
            ("canonical_facts", ("fact_id", "kind", "validation_status",
                                 "workflow_status", "statement", "evidence_quote")),
            ("canonical_relations", ("left_fact_id", "right_fact_id", "type", "kind"))):
        items = content.get(key, [])
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict) or any(
                    item.get(field) is not None and not isinstance(item[field], str)
                    for field in fields):
                return False
            if key == "canonical_facts":
                ids = item.get("evidence_ids", [])
                if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                    return False
    return True


def _kind_state(rows, content_hash: str, *, engine_version=None,
                canonical=False) -> dict:
    """Classify one message's artifacts of one extraction kind.
    `rows` = artifact rows (newest-first) as mappings; the first valid
    hash-current non-invalidated row wins — an older row matching the
    current hash legitimately becomes current again on an A→B→A edit.
    Returns {"state", "artifact_id", "last_error"}."""
    saw_rows = saw_stale = saw_error = False
    current_id = None
    current_meta = None
    for r in rows:
        saw_rows = True
        try:
            meta = json.loads(r["meta"]) if isinstance(
                r["meta"], str) else r["meta"]
            content = json.loads(r["content"]) if isinstance(
                r["content"], str) else r["content"]
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(meta, dict) or not isinstance(content, dict):
            continue
        if canonical and not _canonical_shape(content):
            continue
        if (meta.get("error") not in (None, False, 0)
                or content.get("_error") not in (None, False, 0)):
            saw_error = True
            continue
        if engine_version is not None and meta.get("engine_version") != engine_version:
            continue
        if canonical and meta.get("projection_version") != PROJECTION_VERSION:
            saw_stale = True
            continue
        if content_hash is not None \
                and meta.get("hash") == content_hash \
                and meta.get("invalidated") in (None, False, 0):
            current_id = r["artifact_id"]
            current_meta = meta
            break
        saw_stale = True
    if current_id is not None:
        state = "current"
    elif saw_stale:
        state = "stale"
    elif not saw_rows:
        state = "pending"
    else:
        # rows exist but nothing classified — parse failures or
        # error-only results are reported as unknown, never as a
        # pretend-current read
        state = "unknown"
    return {"state": state, "artifact_id": current_id,
            "last_error": saw_error, "current_meta": current_meta}


def _message_records(db, project_id):
    """One provenance record per message — the derived-row view — as
    (record, by_kind, art_rows); facts/relations are filled by the
    caller only for the records it returns (a `limit` page)."""
    params = []
    where = ""
    if project_id is not None:
        where = "WHERE m.project_id=?"
        params.append(project_id)
    sql = f"""
      SELECT m.message_id, m.project_id, m.parent_id, m.posted_at_ts,
             m.body_state, m.content_hash,
             CASE WHEN {_ELIGIBLE} THEN 1 ELSE 0 END AS eligible
      FROM messages m {where} ORDER BY m.message_id
    """
    cursor = db.execute(sql, params)
    # Classify one batch at a time: historical artifact bodies must not
    # stay resident for every message merely to count whole-scope coverage.
    while rows := cursor.fetchmany(500):
        arts_by_mid = {}
        pid_by_mid = {m["message_id"]: m["project_id"] for m in rows}
        mids = list(pid_by_mid)
        ph = ",".join("?" * len(mids))
        for a in db.execute(
                "SELECT a.message_id, a.project_id, a.kind, a.artifact_id,"
                " a.content, a.meta FROM artifacts a WHERE a.kind IN "
                "('extract_v1','extract_llm','canonical_projection',"
                "'semantic_facts_v4') AND a.message_id IN (" + ph + ") "
                "ORDER BY a.artifact_id DESC", mids):
            # Legacy rows bind through the globally unique message ID.
            if a["project_id"] != pid_by_mid[a["message_id"]] and not (
                    a["project_id"] is None and a["kind"] in ("extract_v1", "extract_llm")):
                continue
            arts_by_mid.setdefault(a["message_id"], []).append(a)
        for msg in rows:
            mid = msg["message_id"]
            art_rows = arts_by_mid.get(mid, [])
            by_kind = _kind_map(art_rows, msg["content_hash"])
            if msg["body_state"] == "deleted":
                for entry in by_kind.values():
                    if entry["state"] == "current":
                        entry.update(state="stale", artifact_id=None)
            rec = {"project_id": msg["project_id"], "message_id": mid,
                   "parent_id": msg["parent_id"],
                   "posted_at_ts": msg["posted_at_ts"],
                   "body_state": msg["body_state"],
                   "content_hash": msg["content_hash"],
                   "extraction_eligible": bool(msg["eligible"]),
                   "state": _record_state(msg, by_kind),
                   "extraction": by_kind}
            yield rec, by_kind, art_rows


def _kind_map(art_rows, content_hash) -> dict:
    by_kind: dict = {}
    for kind in EXTRACTION_KINDS:
        kind_rows = [r for r in art_rows if r["kind"] == kind]
        st = _kind_state(kind_rows, content_hash,
                         engine_version=4 if kind == "semantic_facts_v4" else None,
                         canonical=kind in ("canonical_projection", "semantic_facts_v4"))
        entry = {"state": st["state"],
                 "artifact_id": st["artifact_id"]}
        if st["last_error"]:
            entry["last_error"] = True
        cm = st.get("current_meta") or {}
        if cm.get("engine_version") is not None:
            # T18: version-pinned readers can tell a v4 PASS row
            # from a legacy current without parsing content
            entry["engine_version"] = cm["engine_version"]
        by_kind[kind] = entry
    return by_kind


def _record_state(msg, by_kind: dict) -> str:
    kinds = (by_kind["semantic_facts_v4"], by_kind["canonical_projection"],
             by_kind["extract_llm"], by_kind["extract_v1"])
    if msg["body_state"] == "deleted":
        # the source is a tombstone — nothing derived from it may
        # read as current even if hashes still match
        return "stale"
    if any(k["state"] == "current" for k in kinds):
        return "current"
    if all(k["state"] == "pending" for k in kinds):
        return "pending"
    if any(k["state"] == "unknown" for k in kinds):
        return "unknown"
    return "stale"


def _fact_relations(by_kind: dict, art_rows, scope: str):
    """fact/relation ids from the CURRENT published extraction —
    v4 outranks the canonical projection (T18 precedence); evidence
    binding stays exact (ids + quote only in detail)."""
    facts, relations = [], []
    v4row = by_kind["semantic_facts_v4"]
    src = v4row if v4row["state"] == "current" \
        else by_kind["canonical_projection"]
    if not (src["state"] == "current" and src["artifact_id"]):
        return facts, relations
    row = next((r for r in art_rows
                if r["artifact_id"] == src["artifact_id"]), None)
    if row is None:
        return facts, relations
    try:
        doc = json.loads(row["content"])
    except (json.JSONDecodeError, TypeError):
        doc = None
    if not isinstance(doc, dict):
        return facts, relations
    for fact in doc.get("canonical_facts") or []:
        if not isinstance(fact, dict):
            continue
        f = {"fact_id": fact.get("fact_id"),
             "kind": fact.get("kind"),
             "validation_status": fact.get("validation_status"),
             "workflow_status": fact.get("workflow_status"),
             "evidence_ids": fact.get("evidence_ids") or []}
        if scope == "detail":
            f["statement"] = fact.get("statement")
            f["evidence_quote"] = fact.get("evidence_quote")
        facts.append(f)
    relations.extend({
        "left_fact_id": rel.get("left_fact_id"),
        "right_fact_id": rel.get("right_fact_id"),
        "kind": rel.get("type", rel.get("kind"))}
        for rel in doc.get("canonical_relations") or []
        if isinstance(rel, dict))
    return facts, relations


def message_patient_context(db, message, scope="detail", *, by_kind=None, art_rows=None):
    """Read source-bound context independently of canonical extraction precedence."""
    if message.get("body_state") not in (None, "full"):
        return []
    if art_rows is None:
        art_rows = db.execute(
            "SELECT artifact_id,kind,content,meta FROM artifacts "
            "WHERE (project_id=? OR (project_id IS NULL AND kind IN "
            "('extract_v1','extract_llm'))) AND message_id=? AND kind IN "
            "('extract_v1','extract_llm','canonical_projection','semantic_facts_v4') "
            "ORDER BY artifact_id DESC", (message["project_id"], message["message_id"])).fetchall()
    if by_kind is None:
        by_kind = _kind_map(art_rows, message["content_hash"])
    body = message.get("body_text")
    if body is None:
        row = db.execute("SELECT body_text FROM messages WHERE project_id=? AND message_id=?",
                         (message["project_id"], message["message_id"])).fetchone()
        body = row[0] if row else None
    if not isinstance(body, str) or not body:
        return []
    documents = []
    for kind in ("extract_v1", "extract_llm"):
        artifact_id = by_kind[kind]["artifact_id"]
        row = next((r for r in art_rows if r["artifact_id"] == artifact_id), None)
        documents.append(json.loads(row["content"]) if row is not None else None)
    items = merged_context(*documents, body)
    # A v4 worker fences legacy LLM writes; read its audited additive context.
    for kind in ("semantic_facts_v4", "canonical_projection"):
        entry = by_kind.get(kind, {})
        if entry.get("state") == "current":
            row = next((r for r in art_rows if r["artifact_id"] == entry["artifact_id"]), None)
            if row is not None:
                items = merged_context({"patient_context": items}, json.loads(row["content"]), body)
            break
    source = {key: message.get(key) for key in
              ("project_id", "message_id", "content_hash", "posted_at_ts")}
    return _context_projection(items, scope, source)


def _context_projection(items, scope, source):
    """Preserve grounded attribute values in detail and count only their keys in aggregate."""
    if scope == "detail":
        return [{**{key: item[key] for key in ("category", "text", "evidence", "subject")},
                 **({"origin": item["origin"]} if item.get("origin") == "explicit_heading" else {}),
                 **({"details": item["details"]} if item.get("details") else {}),
                 "source": dict(source)} for item in items]
    counts = {}
    attributes = {}
    for item in items:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
        for detail in item.get("details") or []:
            category = attributes.setdefault(item["category"], {})
            category[detail["key"]] = category.get(detail["key"], 0) + 1
    return [{"category": key, "count": value,
             **({"details": [{"key": name, "count": count} for name, count in sorted(attributes[key].items())]}
                if key in attributes else {})} for key, value in sorted(counts.items())]


def _attachments(db, scope: str, project_id=None):
    """The attachment manifest — ids/state always, file names only in
    detail scope (a name is user-authored content)."""
    if not _table_exists(db, "attachments"):
        return None
    out = []
    sql = ("SELECT a.attachment_id, a.message_id, a.name, a.bytes, "
           "a.sha256, a.state FROM attachments a")
    params = ()
    if project_id is not None:
        sql += " JOIN messages m ON m.message_id=a.message_id WHERE m.project_id=?"
        params = (project_id,)
    sql += " ORDER BY a.attachment_id"
    for r in db.execute(sql, params):
        item = {"attachment_id": r["attachment_id"],
                "message_id": r["message_id"], "bytes": r["bytes"],
                "sha256": r["sha256"], "state": r["state"]}
        if scope == "detail":
            item["name"] = r["name"]
        out.append(item)
    return out


def _project_metadata(db, scope, project_id, limit):
    """Expose fetched memo context only in detail, retaining explicit unknown/empty states."""
    if not _table_exists(db, "patients"):
        return None
    karte_column = "p.karte_id" if any(row[1] == "karte_id" for row in db.execute(
        "PRAGMA table_info(patients)")) else "NULL"
    where, params = (" WHERE p.project_id=?", [project_id]) if project_id is not None else ("", [])
    total = db.execute("SELECT COUNT(*) FROM patients p" + where, params).fetchone()[0]
    sql = (f"SELECT p.project_id,{karte_column} AS karte_id,a.artifact_id,a.content,a.meta "
           "FROM patients p LEFT JOIN artifacts a ON a.artifact_id=("
           "SELECT MAX(artifact_id) FROM artifacts WHERE kind='karte_summary' "
           "AND project_id=p.project_id)" + where + " ORDER BY p.project_id")
    if limit is not None:
        sql += " LIMIT ?"
        params = [*params, limit]
    records = []
    for row in db.execute(sql, params):
        record = {"project_id": row["project_id"], "state": "not_fetched", "patient_context": []}
        if row["artifact_id"] is not None:
            record["state"] = "unknown"
            try:
                content, meta = json.loads(row["content"]), json.loads(row["meta"])
            except (ValueError, TypeError, RecursionError):
                content = meta = None
            fetched_at = meta.get("fetched_at") if isinstance(meta, dict) else None
            try:
                valid_time = type(fetched_at) in (int, float) and math.isfinite(fetched_at) and fetched_at >= 0
            except OverflowError:
                valid_time = False
            if (isinstance(content, dict) and isinstance(meta, dict)
                    and type(meta.get("karte_id")) is int and meta["karte_id"] == row["karte_id"]
                    and isinstance(content.get("empty"), bool)
                    and valid_time
                    and (content.get("updated_at") is None or isinstance(content["updated_at"], str))
                    and (content["empty"] and content.get("comment") is None
                         or not content["empty"] and isinstance(content.get("comment"), str))):
                record["state"] = "fetched_empty" if content["empty"] else "reported"
                comment = content.get("comment") or ""
                items = context_items({"patient_context": extract_context(comment)}, comment)
                if scope == "detail":
                    record["comment"] = comment
                    record["unclassified_text"] = bool(comment) and not items
                source = {"artifact_id": row["artifact_id"], "project_id": row["project_id"],
                          "fetched_at": meta["fetched_at"], "updated_at": content.get("updated_at")}
                record["patient_context"] = _context_projection(items, scope, source)
        records.append(record)
    return {"records": records, "total": total, "truncated": limit is not None and total > limit}


def _coverage(db, records, attachments, project_id=None) -> dict:
    extraction = {k: {"current": 0, "stale": 0, "pending": 0,
                      "unknown": 0} for k in EXTRACTION_KINDS}
    collection = {"patients": None, "messages": 0,
                  "deleted": 0, "extraction_eligible": 0}
    for rec in records:
        collection["messages"] += 1
        collection["deleted"] += rec["body_state"] == "deleted"
        collection["extraction_eligible"] += bool(rec["extraction_eligible"])
        if not rec["extraction_eligible"]:
            continue
        for kind in EXTRACTION_KINDS:
            extraction[kind][rec["extraction"][kind]["state"]] += 1
    with suppress(Exception):
        if project_id is None:
            collection["patients"] = db.execute(
                "SELECT COUNT(*) FROM patients").fetchone()[0]
        else:
            collection["patients"] = db.execute(
                "SELECT COUNT(*) FROM patients WHERE project_id=?",
                (project_id,)).fetchone()[0]
    att_counts = None
    if attachments is not None:
        att_counts = {"total": len(attachments)}
        for a in attachments:
            att_counts[a["state"] or "unknown"] = \
                att_counts.get(a["state"] or "unknown", 0) + 1
    return {"collection": collection, "extraction": extraction,
            "attachments": att_counts}


def _field_counts(rows):
    """Count allowlisted registration fields without exporting their values."""
    counts = {}

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, path + "." + key if path else key)
        elif isinstance(value, list):
            for item in value:
                visit(item, path)
        elif value is not None:
            counts[path] = counts.get(path, 0) + 1

    for row in rows:
        visit(row, "")
    return [{"key": key, "count": count} for key, count in sorted(counts.items())]


def _registered_data(db, scope, project_id, limit):
    """Read optional registered clinical data separately from chat and memo reports."""
    if not _table_exists(db, "patients") or not {"project_type", "karte_id"} <= {
            row[1] for row in db.execute("PRAGMA table_info(patients)")}:
        return None
    from project_metadata_view import get_project_metadata

    params = [project_id] if project_id is not None else []
    sql = """WITH clinical AS (
        SELECT project_id,karte_id FROM patients WHERE project_type='medical'
        AND typeof(karte_id)='integer' AND karte_id>0""" + (
        " AND project_id=?" if project_id is not None else "") + """
        ), targets AS (
        SELECT project_id,'medication_periods' AS dataset,NULL AS item_id FROM clinical
        UNION ALL SELECT project_id,'observation_items',NULL FROM clinical
        UNION ALL SELECT DISTINCT p.project_id,'observation_values',json_extract(a.content,'$.item_id')
        FROM clinical p JOIN artifacts a ON a.project_id=p.project_id
        WHERE a.kind='project_metadata_v1' AND CASE WHEN json_valid(a.content) THEN
          json_extract(a.content,'$.dataset')='observation_values'
          AND json_extract(a.content,'$.entity_id')=p.karte_id
          AND json_type(a.content,'$.item_id')='integer'
          AND typeof(json_extract(a.content,'$.item_id'))='integer'
          AND json_extract(a.content,'$.item_id')>0 ELSE 0 END
        ) """
    total = db.execute(sql + "SELECT COUNT(*) FROM targets", params).fetchone()[0]
    query = sql + "SELECT * FROM targets ORDER BY project_id,dataset,item_id"
    if limit is not None:
        query += " LIMIT ?"
        params = [*params, limit]
    now = _snapshot_meta(db)["generated_at"]
    records = []
    for target in db.execute(query, params):
        result = get_project_metadata(db, target["project_id"], target["dataset"],
                                      item_id=target["item_id"], now=now)
        record = {"project_id": target["project_id"], "dataset": target["dataset"],
                  "source": "mcs_structured", "state": result["state"],
                  "rows_total": len(result["rows"]),
                  "rows_truncated": limit is not None and len(result["rows"]) > limit}
        if scope == "detail":
            record.update({key: result[key] for key in (
                "reason", "scope", "last_complete_at", "attempted_at", "age_s", "current_known",
                "historical", "stale", "definition", "http_status", "chat_comparison",
                "definition_binding", "definition_source_artifact_id", "definition_source",
                "attempt_reason") if key in result})
            record["item_id"] = target["item_id"]
            record["rows"] = result["rows"] if limit is None else result["rows"][:limit]
        else:
            record["fields"] = _field_counts(result["rows"])
            record["definition_fields"] = _field_counts([result["definition"]] if result["definition"] else [])
        records.append(record)
    return {"records": records, "total": total, "truncated": limit is not None and total > limit,
            "absence_confirmed": False, "clinical_state": "not_inferred"}


def read_model(db, scope: str = "aggregate", project_id=None,
               limit=None) -> dict:
    """The complete machine read for one opened connection.

    `db` is the caller's already-validated snapshot connection (the
    View's single-generation rule applies — this function never opens
    or re-opens a database itself). `limit` bounds the records list
    honestly (``truncated`` + ``total``), never slicing JSON."""
    if scope not in SCOPES:
        raise ValueError("read_model_scope_invalid")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("bad_limit")
    # coverage describes the whole scope like `total` does — never just
    # the page `limit` cut
    records = []

    def counted_records():
        for rec, by_kind, art_rows in _message_records(db, project_id):
            if limit is None or len(records) < limit:
                rec["facts"], rec["relations"] = _fact_relations(
                    by_kind, art_rows, scope)
                rec["patient_context"] = message_patient_context(
                    db, rec, scope, by_kind=by_kind, art_rows=art_rows)
                records.append(rec)
            yield rec

    attachments = _attachments(db, scope, project_id)
    coverage = _coverage(db, counted_records(), attachments, project_id)
    total = coverage["collection"]["messages"]
    return {
        "contract": CONTRACT,
        "snapshot": _snapshot_meta(db),
        "scope": scope,
        "coverage": coverage,
        "attachments": attachments,
        "project_metadata": _project_metadata(db, scope, project_id, limit),
        "registered_data": _registered_data(db, scope, project_id, limit),
        "records": records,
        "total": total,
        "truncated": limit is not None and total > limit,
    }
