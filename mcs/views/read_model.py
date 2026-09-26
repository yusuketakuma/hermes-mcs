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
    if row is None:
        return {"generation_id": None, "generated_at": None,
                "published": False}
    return {"generation_id": row[0], "generated_at": row[1],
            "published": True}


def _kind_state(rows, content_hash: str) -> dict:
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
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(meta, dict) or not isinstance(content, dict):
            continue
        if meta.get("error") in (True, 1) or content.get("_error"):
            saw_error = True
            continue
        if content_hash is not None \
                and meta.get("hash") == content_hash \
                and not meta.get("invalidated"):
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


def _message_records(db, scope: str, project_id, limit):
    """One provenance record per message — the derived-row view."""
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
    rows = db.execute(sql, params).fetchall()
    total = len(rows)
    truncated = False
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
        truncated = True
    records = []
    for msg in rows:
        mid = msg["message_id"]
        art_rows = db.execute(
            "SELECT kind, artifact_id, content, meta FROM artifacts "
            "WHERE message_id=? AND kind IN "
            "('extract_v1','extract_llm','canonical_projection',"
            "'semantic_facts_v4') "
            "ORDER BY artifact_id DESC",
            (mid,)).fetchall()
        by_kind: dict = {}
        for kind in EXTRACTION_KINDS:
            kind_rows = [r for r in art_rows if r["kind"] == kind]
            st = _kind_state(kind_rows, msg["content_hash"])
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
        proj = by_kind["canonical_projection"]
        extraction = by_kind["extract_llm"]
        v1 = by_kind["extract_v1"]
        v4row = by_kind["semantic_facts_v4"]
        if msg["body_state"] == "deleted":
            # the source is a tombstone — nothing derived from it may
            # read as current even if hashes still match
            state = "stale"
        elif any(k["state"] == "current"
                 for k in (v4row, proj, extraction, v1)):
            state = "current"
        elif all(k["state"] == "pending"
                 for k in (v4row, proj, extraction, v1)):
            state = "pending"
        elif any(k["state"] == "unknown"
                 for k in (v4row, proj, extraction, v1)):
            state = "unknown"
        else:
            state = "stale"
        rec = {"project_id": msg["project_id"], "message_id": mid,
               "parent_id": msg["parent_id"],
               "posted_at_ts": msg["posted_at_ts"],
               "body_state": msg["body_state"],
               "content_hash": msg["content_hash"],
               "extraction_eligible": bool(msg["eligible"]),
               "state": state,
               "extraction": by_kind}
        # fact/relation ids from the CURRENT published extraction —
        # v4 outranks the canonical projection (T18 precedence);
        # evidence binding stays exact (ids + quote only in detail)
        facts, relations = [], []
        src = v4row if v4row["state"] == "current" else proj
        if src["state"] == "current" and src["artifact_id"]:
            row = next((r for r in art_rows
                        if r["artifact_id"] == src["artifact_id"]), None)
            if row is not None:
                try:
                    doc = json.loads(row["content"])
                except (json.JSONDecodeError, TypeError):
                    doc = None
                if isinstance(doc, dict):
                    for fact in doc.get("canonical_facts") or []:
                        if not isinstance(fact, dict):
                            continue
                        f = {"fact_id": fact.get("fact_id"),
                             "kind": fact.get("kind"),
                             "validation_status":
                                 fact.get("validation_status"),
                             "workflow_status":
                                 fact.get("workflow_status"),
                             "evidence_ids":
                                 fact.get("evidence_ids") or []}
                        if scope == "detail":
                            f["statement"] = fact.get("statement")
                            f["evidence_quote"] = fact.get("evidence_quote")
                        facts.append(f)
                    for rel in doc.get("canonical_relations") or []:
                        if isinstance(rel, dict):
                            relations.append({
                                "left_fact_id": rel.get("left_fact_id"),
                                "right_fact_id": rel.get("right_fact_id"),
                                "kind": rel.get("kind")})
        rec["facts"] = facts
        rec["relations"] = relations
        records.append(rec)
    return records, total, truncated


def _attachments(db, scope: str):
    """The attachment manifest — ids/state always, file names only in
    detail scope (a name is user-authored content)."""
    if not _table_exists(db, "attachments"):
        return None
    out = []
    for r in db.execute(
            "SELECT attachment_id, message_id, name, bytes, sha256, "
            "state FROM attachments ORDER BY attachment_id"):
        item = {"attachment_id": r["attachment_id"],
                "message_id": r["message_id"], "bytes": r["bytes"],
                "sha256": r["sha256"], "state": r["state"]}
        if scope == "detail":
            item["name"] = r["name"]
        out.append(item)
    return out


def _coverage(db, records) -> dict:
    extraction = {k: {"current": 0, "stale": 0, "pending": 0,
                      "unknown": 0} for k in EXTRACTION_KINDS}
    for rec in records:
        if not rec["extraction_eligible"]:
            continue
        for kind in EXTRACTION_KINDS:
            extraction[kind][rec["extraction"][kind]["state"]] += 1
    collection = {"patients": None, "messages": len(records),
                  "deleted": None, "extraction_eligible": None}
    try:
        collection["patients"] = db.execute(
            "SELECT COUNT(*) FROM patients").fetchone()[0]
    except Exception:
        pass
    collection["deleted"] = sum(
        1 for r in records if r["body_state"] == "deleted")
    collection["extraction_eligible"] = sum(
        1 for r in records if r["extraction_eligible"])
    att = _attachments(db, "aggregate")
    att_counts = None
    if att is not None:
        att_counts = {"total": len(att)}
        for a in att:
            att_counts[a["state"] or "unknown"] = \
                att_counts.get(a["state"] or "unknown", 0) + 1
    return {"collection": collection, "extraction": extraction,
            "attachments": att_counts}


def read_model(db, scope: str = "aggregate", project_id=None,
               limit=None) -> dict:
    """The complete machine read for one opened connection.

    `db` is the caller's already-validated snapshot connection (the
    View's single-generation rule applies — this function never opens
    or re-opens a database itself). `limit` bounds the records list
    honestly (``truncated`` + ``total``), never slicing JSON."""
    if scope not in SCOPES:
        raise ValueError("read_model_scope_invalid")
    records, total, truncated = _message_records(
        db, scope, project_id, limit)
    return {
        "contract": CONTRACT,
        "snapshot": _snapshot_meta(db),
        "scope": scope,
        "coverage": _coverage(db, records),
        "attachments": _attachments(db, scope),
        "records": records,
        "total": total,
        "truncated": truncated,
    }
