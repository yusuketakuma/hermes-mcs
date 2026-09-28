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
from contextlib import suppress

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


def _kind_state(rows, content_hash: str, *, engine_version=None) -> dict:
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
        if (meta.get("error") not in (None, False, 0)
                or content.get("_error") not in (None, False, 0)):
            saw_error = True
            continue
        if engine_version is not None and meta.get("engine_version") != engine_version:
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
    # One batched artifacts scan per ~500 messages replaces the
    # per-message query (N+1). Chunking stays under SQLite's host-variable
    # ceiling; kind remains the leading term so idx_artifacts_kind_msg
    # serves each chunk. Rows arrive globally artifact_id-DESC, which
    # preserves the newest-first order _kind_map relies on.
    arts_by_mid: dict = {}
    pid_by_mid = {m["message_id"]: m["project_id"] for m in rows}
    mids = list(pid_by_mid)
    for i in range(0, len(mids), 500):
        chunk = mids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for a in db.execute(
                "SELECT a.message_id, a.project_id, a.kind, a.artifact_id,"
                " a.content, a.meta FROM artifacts a WHERE a.kind IN "
                "('extract_v1','extract_llm','canonical_projection',"
                "'semantic_facts_v4') AND a.message_id IN (" + ph + ") "
                "ORDER BY a.artifact_id DESC", chunk):
            if a["project_id"] != pid_by_mid[a["message_id"]]:
                continue  # same guard as the per-message project_id=? clause
            arts_by_mid.setdefault(a["message_id"], []).append(a)
    records = []
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
        facts, relations = _fact_relations(by_kind, art_rows, scope)
        rec["facts"] = facts
        rec["relations"] = relations
        records.append(rec)
    return records, total, truncated


def _kind_map(art_rows, content_hash) -> dict:
    by_kind: dict = {}
    for kind in EXTRACTION_KINDS:
        kind_rows = [r for r in art_rows if r["kind"] == kind]
        st = _kind_state(kind_rows, content_hash,
                         engine_version=4 if kind == "semantic_facts_v4" else None)
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


def _coverage(db, records, attachments, project_id=None) -> dict:
    extraction = {k: {"current": 0, "stale": 0, "pending": 0,
                      "unknown": 0} for k in EXTRACTION_KINDS}
    for rec in records:
        if not rec["extraction_eligible"]:
            continue
        for kind in EXTRACTION_KINDS:
            extraction[kind][rec["extraction"][kind]["state"]] += 1
    collection = {"patients": None, "messages": len(records),
                  "deleted": None, "extraction_eligible": None}
    with suppress(Exception):
        if project_id is None:
            collection["patients"] = db.execute(
                "SELECT COUNT(*) FROM patients").fetchone()[0]
        else:
            collection["patients"] = db.execute(
                "SELECT COUNT(*) FROM patients WHERE project_id=?",
                (project_id,)).fetchone()[0]
    collection["deleted"] = sum(
        1 for r in records if r["body_state"] == "deleted")
    collection["extraction_eligible"] = sum(
        1 for r in records if r["extraction_eligible"])
    att_counts = None
    if attachments is not None:
        att_counts = {"total": len(attachments)}
        for a in attachments:
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
    attachments = _attachments(db, scope, project_id)
    return {
        "contract": CONTRACT,
        "snapshot": _snapshot_meta(db),
        "scope": scope,
        "coverage": _coverage(db, records, attachments, project_id),
        "attachments": attachments,
        "records": records,
        "total": total,
        "truncated": truncated,
    }
