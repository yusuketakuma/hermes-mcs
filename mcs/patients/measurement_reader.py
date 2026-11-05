"""Read a bounded local measurement candidate set on one borrowed snapshot."""
import copy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
import sqlite3
import time

import measurement_sources
import measurement_timeline
import semantic_policy
from mcs_queries import current_extract_pred, current_fact_pred
from mcs_util import html_to_text
from patient_read_scope import patient_scope_valid as patient_scope_valid


_MAX_ROWS = 5000
_MAX_BYTES = 32 * 1024 * 1024
_MAX_FIELD_BYTES = 1000000
_MAX_BODY_BYTES = 20000
_MAX_HTML_BYTES = 80000
_MAX_MESSAGES = 200
_UNSUPPORTED_KINDS = ("canonical_projection", "semantic_facts_v4", "semantic_facts_v2",
                      "extract_llm_chunk", "extract_feedback_v1")


def boundary_fingerprint(db, project_id, message_id):
    """Bind a page boundary to the bounded raw source and revision occurrences."""
    columns = {row[1]: row for row in db.execute("PRAGMA table_info(message_revisions)")}
    if (not {"message_id", "seq"} <= columns.keys()
            or any(str(columns[name][2]).upper() != "INTEGER" or columns[name][3] != 1
                   or columns[name][5] != position
                   for name, position in (("message_id", 1), ("seq", 2)))):
        raise ValueError("measurement_cursor_revision_unavailable")
    row = db.execute(
        "SELECT CASE WHEN typeof(body_state)='text' AND octet_length(body_state)<=16 "
        "THEN CASE WHEN body_state IN ('full','snippet','deleted') THEN body_state ELSE 'unknown' END "
        "ELSE 'unknown' END,"
        "typeof(content_hash), CASE WHEN octet_length(content_hash)<=64 THEN CAST(content_hash AS BLOB) END,"
        "CASE WHEN typeof(posted_at_ts) IN ('integer','real') THEN posted_at_ts END,"
        "CASE WHEN octet_length(body_text)<=20000 THEN CAST(body_text AS BLOB) END,"
        "CASE WHEN octet_length(body_html)<=80000 THEN CAST(body_html AS BLOB) END "
        "FROM messages WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
    if row is None:
        raise ValueError("measurement_cursor_changed")
    state, digest_type, digest, posted, body, html = row
    if digest is None and digest_type != "null":
        raise ValueError("measurement_cursor_hash_unavailable")
    invalid = (digest_type != "text" or not isinstance(digest, bytes)
               or re.fullmatch(rb"[0-9a-f]{64}", digest) is None)
    revisions = []
    for seq_type, seq_value, raw in db.execute(
            "SELECT typeof(seq), CASE WHEN typeof(seq)='integer' THEN seq END, "
            "CASE WHEN octet_length(seq)<=64 THEN CAST(seq AS BLOB) END "
            "FROM message_revisions WHERE message_id=? ORDER BY seq LIMIT 5001", (message_id,)):
        if len(revisions) >= 5000 or raw is None:
            raise ValueError("measurement_cursor_revision_unavailable")
        invalid |= seq_type != "integer" or type(seq_value) is not int or not 0 < seq_value < 2**63
        revisions.append([seq_type, hashlib.sha256(raw).hexdigest()])
    hashes = [hashlib.sha256(value).hexdigest() if isinstance(value, bytes) else None for value in (body, html)]
    identity = [message_id, state, digest_type, hashlib.sha256(digest).hexdigest() if digest is not None else None,
                posted, revisions, hashes]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":"), allow_nan=False).encode()).hexdigest(), invalid


def _unknown(reason):
    return {"records": [], "coverage": {"source_complete": False,
            "local_checks_complete": None, "scope_state": "unknown", "reader_state": reason}}


def read_live_page(db, project_id, *, cfg, cursor=None, checkpoint=None, now=None):
    """Read one source-bound page on a caller-authorized, borrowed read snapshot.

    The cursor is a position, not authorization. The caller rechecks actor and
    patient each click; other pages and latest/correction relations are unknown.
    """
    if not db.in_transaction:
        raise ValueError("measurement_snapshot_required")
    if type(project_id) is not int or not 0 < project_id < 2**63:
        raise ValueError("project_id_invalid")
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("measurement_time_invalid")
    if not patient_scope_valid(db, project_id):
        return {"project_id": project_id, "outcome": "unknown", "reason": "measurement_unavailable",
                "items": [], "next_cursor": None, "source_complete": False, "relations": "not_assessed"}
    mode = None
    if cursor is not None:
        if not isinstance(cursor, dict) or cursor.get("mode") not in ("after", "within"):
            raise ValueError("measurement_cursor_invalid")
        mode = cursor["mode"]
        expected = {"mode", "message_id", "source_fp"} | ({"offset", "records_fp"} if mode == "within" else set())
        if (set(cursor) != expected or type(cursor["message_id"]) is not int
                or not 0 < cursor["message_id"] < 2**63
                or not isinstance(cursor["source_fp"], str)
                or re.fullmatch(r"[0-9a-f]{64}", cursor["source_fp"]) is None
                or mode == "within" and (type(cursor["offset"]) is not int or not 1 <= cursor["offset"] < 400
                    or not isinstance(cursor["records_fp"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", cursor["records_fp"]) is None)):
            raise ValueError("measurement_cursor_invalid")
        if boundary_fingerprint(db, project_id, cursor["message_id"])[0] != cursor["source_fp"]:
            raise ValueError("measurement_cursor_changed")
    if mode == "within":
        rows = db.execute("SELECT message_id FROM messages WHERE project_id=? AND message_id=?",
                          (project_id, cursor["message_id"])).fetchall()
    else:
        rows = db.execute("SELECT message_id FROM messages WHERE project_id=? "
                          + ("AND message_id<? " if mode == "after" else "")
                          + "ORDER BY message_id DESC LIMIT 2",
                          (project_id, cursor["message_id"]) if mode == "after" else (project_id,)).fetchall()
    base = {"project_id": project_id, "outcome": "unknown", "reason": "observations_unavailable",
            "items": [], "next_cursor": None, "source_complete": False, "relations": "not_assessed"}
    if not rows:
        return base
    mid = rows[0][0]
    if type(mid) is not int or not 0 < mid < 2**63:
        return {**base, "reason": "source_unavailable"}
    fingerprint, invalid = boundary_fingerprint(db, project_id, mid)
    after = {"mode": "after", "message_id": mid, "source_fp": fingerprint}
    if mode == "within":
        more = db.execute("SELECT 1 FROM messages WHERE project_id=? AND message_id<? LIMIT 1",
                          (project_id, mid)).fetchone() is not None
    else:
        more = len(rows) > 1
    next_cursor = after if more else None
    if invalid:
        return {**base, "reason": "source_unverified", "next_cursor": next_cursor if mode != "within" else None}
    got = read_measurements(db, project_id, cfg=cfg, message_id=mid, checkpoint=checkpoint)
    coverage = got["coverage"]
    if coverage.get("reader_state") != "bounded" or coverage.get("scope_state") != "observed":
        reason = coverage.get("reader_state")
        if reason in ("unsupported_dependency", "resource_limit", "source_unverified", "source_unavailable"):
            return {**base, "reason": reason, "next_cursor": next_cursor if mode != "within" else None}
        return {**base, "reason": "measurement_unavailable"}
    if (coverage.get("deleted") or coverage.get("incomplete") or coverage.get("truncated")
            or coverage.get("correction_candidates") or coverage.get("invalid_items")
            or got["projection_coverage"].get("invalid_count")):
        return {**base, "reason": "source_relation_unconfirmed", "next_cursor": next_cursor if mode != "within" else None}
    records = []
    for item in got["records"]:
        if (item.get("state") not in ("observed", "history") or item.get("subject") != "patient"
                or item.get("verification") != "quote_supported"):
            continue
        try:
            posted = datetime.fromisoformat(item["posted_at"].replace("Z", "+00:00"))
            measured = date.fromisoformat(item["measured_on"])
            if (posted.tzinfo is None or posted.utcoffset() is None or posted.timestamp() > now
                    or measured > datetime.fromtimestamp(now, timezone(timedelta(hours=9))).date()):
                continue
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        records.append(item)
    records_fp = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
    if mode == "within" and (records_fp != cursor["records_fp"] or cursor["offset"] >= len(records)):
        raise ValueError("measurement_cursor_changed")
    offset = cursor["offset"] if mode == "within" else 0
    displayed = records[offset:offset + 8]
    if offset + len(displayed) < len(records):
        next_cursor = {"mode": "within", "message_id": mid, "source_fp": fingerprint,
                       "offset": offset + len(displayed), "records_fp": records_fp}
    return {**base, "outcome": "observed" if displayed else "unknown", "items": displayed,
            "reason": None if displayed else "observations_unavailable", "next_cursor": next_cursor}


def _columns(db, table):
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def _preflight(db, table, scope, params, columns, *, row_limit):
    """Bound every column before any reader can materialize a matching row."""
    count = db.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} WHERE ({scope}) LIMIT ?)",
                       (*params, row_limit + 1)).fetchone()[0]
    if count > row_limit:
        return None
    terms = []
    unsafe = []
    for name in columns:
        field = '"' + name.replace('"', '""') + '"'
        limit = (_MAX_BODY_BYTES if table == "messages" and name == "body_text" else
                 _MAX_HTML_BYTES if table == "messages" and name == "body_html" else _MAX_FIELD_BYTES)
        terms.append(f"COALESCE(octet_length({field}),0)")
        unsafe.append(f"CASE WHEN typeof({field}) NOT IN ('text','blob') THEN 0 "
                      f"WHEN octet_length({field})>{limit} THEN 1 "
                      f"WHEN instr(CAST({field} AS BLOB),x'00')>0 THEN 1 ELSE 0 END")
    size = db.execute(f"SELECT COALESCE(SUM({' + '.join(terms)}),0) FROM {table} WHERE ({scope})",
                      params).fetchone()[0]
    if size > _MAX_BYTES or db.execute(
            f"SELECT 1 FROM {table} WHERE ({scope}) AND ({' OR '.join(unsafe)}) LIMIT 1", params).fetchone():
        return None
    return size


def read_measurements(db, project_id, *, cfg=None, checkpoint=None, message_id=None):
    """Return source-bound candidates or unknown; no caller or production data is modified.

    The caller must authorize the actor for this patient; a project ID is not authorization.
    Row and byte limits bound materialization, not SQL scan time or an earlier caller's allocations.
    Canonical, chunk and human-feedback dependencies are held until their resource bounds are proved.
    Source bodies and evidence are transient, never logged or returned. Upstream source completeness is not asserted.
    """
    if checkpoint is not None and not callable(checkpoint):
        raise ValueError("checkpoint_invalid")
    if checkpoint:
        checkpoint()
    if type(project_id) is not int or not 0 < project_id < 2**63:
        raise ValueError("project_id_invalid")
    if message_id is not None and (type(message_id) is not int or not 0 < message_id < 2**63):
        raise ValueError("message_id_invalid")
    if not isinstance(cfg, dict):
        return _unknown("configuration_unavailable")
    cfg = copy.deepcopy(cfg)
    if isinstance(cfg.get("semantic"), dict) and cfg["semantic"].get("fact_source") == "canonical":
        return _unknown("canonical_unavailable")
    try:
        policy, errors = semantic_policy.semantic_config(cfg, key_present=False)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return _unknown("configuration_unavailable")
    if errors or policy.get("fact_source") != "legacy":
        return _unknown("configuration_unavailable")
    db.execute("SAVEPOINT measurement_reader")
    try:
        result = _read_snapshot(db, project_id, cfg, checkpoint=checkpoint, message_id=message_id)
    except BaseException as error:
        try:
            db.execute("RELEASE measurement_reader")
        except sqlite3.Error:
            pass
        if (isinstance(error, UnicodeError) or isinstance(error, sqlite3.OperationalError)
                and str(error).startswith("Could not decode to UTF-8")):
            return _unknown("source_unverified")
        raise
    else:
        db.execute("RELEASE measurement_reader")
        return result


def _read_snapshot(db, project_id, cfg, *, checkpoint=None, message_id=None):
    if checkpoint:
        checkpoint()
    required = {
        "patients": {"project_id", "patient_name", "fetch_state", "fetch_reason", "history_floor", "initial_import", "is_archived"},
        "messages": set(measurement_sources._MESSAGE_FIELDS) | {"body_html"},
        "artifacts": {"artifact_id", "project_id", "message_id", "kind", "content", "meta", "model"},
        "message_revisions": {"message_id", "seq", "content_hash"},
        "fetch_jobs": {"project_id", "kind", "state", "payload"},
        "attachments": {"message_id"},
    }
    columns = {table: _columns(db, table) for table in required}
    if any(not fields.issubset(columns[table]) for table, fields in required.items()):
        return _unknown("schema_unavailable")
    try:
        db.execute("SELECT octet_length('x')").fetchone()
    except sqlite3.OperationalError as error:
        if "no such function: octet_length" in str(error):
            return _unknown("byte_length_unavailable")
        raise
    mid_scope = "SELECT message_id FROM messages WHERE project_id=?" + (" AND message_id=?" if message_id else "")
    mid_params = (project_id, message_id) if message_id else (project_id,)
    patient_size = _preflight(db, "patients", "project_id=?", (project_id,), columns["patients"], row_limit=1)
    if patient_size is None:
        return _unknown("resource_limit")
    patient = db.execute("SELECT fetch_state,fetch_reason,is_archived FROM patients WHERE project_id=?",
                         (project_id,)).fetchone()
    if patient is None or patient[2] != 0 or patient[0] == "forbidden" or patient[1] in (
            "forbidden", "permission_denied", "access_revoked"):
        return _unknown("scope_unavailable")
    scopes = [
        ("messages", "project_id=?" + (" AND message_id=?" if message_id else ""), mid_params, _MAX_MESSAGES),
        ("artifacts", (f"message_id IN ({mid_scope})" if message_id else f"project_id=? OR message_id IN ({mid_scope})"),
         (mid_params if message_id else (project_id, project_id)), _MAX_ROWS),
        # dependency_loss.read materializes only previous revisions, not the
        # latest revision of every unrelated message in this patient.
        ("message_revisions", "message_id IN (SELECT message_id FROM messages WHERE project_id=?) "
         "AND seq<(SELECT MAX(n.seq) FROM message_revisions n "
         "WHERE n.message_id=message_revisions.message_id)",
         (project_id,), _MAX_ROWS),
        ("artifacts", "project_id=? AND kind IN ('inference_dependency_loss','karte_summary')",
         (project_id,), _MAX_ROWS),
        ("fetch_jobs", "project_id=?", (project_id,), _MAX_ROWS),
        ("attachments", f"message_id IN ({mid_scope})", mid_params, _MAX_ROWS),
    ]
    if message_id is not None:
        source = db.execute("SELECT typeof(parent_id),CASE WHEN typeof(parent_id)='integer' THEN parent_id END,"
                            "typeof(posted_at_ts),CASE WHEN typeof(posted_at_ts) IN ('integer','real') "
                            "THEN posted_at_ts END FROM messages WHERE project_id=? AND message_id=?",
                            (project_id, message_id)).fetchone()
        if source is None:
            return _unknown("source_unavailable")
        if source[0] not in ('null', 'integer') or source[2] not in ('null', 'integer', 'real'):
            return _unknown("source_unverified")
        root, posted = source[1] or message_id, source[3] or 0
        # _thread_context materializes these four bodies, even when this page contains one post.
        scopes.append(("messages", "project_id=? AND message_id IN (SELECT message_id FROM messages "
            "WHERE project_id=? AND ((message_id=? AND message_id!=?) OR (parent_id=? "
            "AND (posted_at_ts<? OR (posted_at_ts=? AND message_id<?)))) "
            "AND body_text IS NOT NULL AND body_text!='' AND (body_state IS NULL OR body_state='full') "
            "ORDER BY (message_id=?) DESC,COALESCE(posted_at_ts,0) DESC,message_id DESC LIMIT 4)",
            (project_id, project_id, root, message_id, root, posted, posted, message_id, root), 4))
    total = patient_size
    for table, scope, params, limit in scopes:
        if checkpoint:
            checkpoint()
        size = _preflight(db, table, scope, params, columns[table], row_limit=limit)
        if size is None:
            return _unknown("resource_limit")
        total += size
        if total > _MAX_BYTES:
            return _unknown("resource_limit")
    marks = ",".join("?" for _ in _UNSUPPORTED_KINDS)
    if db.execute(f"SELECT 1 FROM artifacts WHERE message_id IN ({mid_scope}) "
                  f"AND kind IN ({marks}) LIMIT 1", (*mid_params, *_UNSUPPORTED_KINDS)).fetchone():
        return _unknown("unsupported_dependency")
    messages = [dict(row) for row in db.execute(
        "SELECT " + ",".join(measurement_sources._MESSAGE_FIELDS) + ",body_html FROM messages "
        "WHERE project_id=?" + (" AND message_id=?" if message_id else "") +
        " ORDER BY message_id DESC", mid_params)]
    if message_id is not None and (len(messages) != 1 or messages[0]["message_id"] != message_id):
        return _unknown("source_unavailable")
    for message in messages:
        if checkpoint:
            checkpoint()
        body, html, digest = message["body_text"], message.pop("body_html"), message["content_hash"]
        if message["body_state"] == "full" and (
                not isinstance(body, str) or not isinstance(html, str) or not isinstance(digest, str)
                or hashlib.sha256(html.encode()).hexdigest() != digest or html_to_text(html) != body):
            return _unknown("source_unverified")
    arts, refs = {}, {}
    rows = db.execute("SELECT a.artifact_id,a.message_id,a.kind,a.content FROM artifacts a "
        "JOIN messages m ON m.message_id=a.message_id WHERE m.project_id=? "
        + ("AND m.message_id=? " if message_id else "") +
        "AND a.kind IN ('extract_v1','extract_llm') " + current_extract_pred()
        + " AND (a.kind='extract_v1' OR (1=1 " + current_fact_pred() + ")) ORDER BY a.artifact_id",
        mid_params)
    for row in rows:
        if checkpoint:
            checkpoint()
        mid, kind = row["message_id"], row["kind"]
        arts.setdefault(mid, {})[kind] = row["content"]
        refs[(mid, kind)] = row["artifact_id"]
    try:
        if checkpoint:
            checkpoint()
        result = measurement_sources.collect_measurements(db, project_id, messages, arts, refs, cfg=cfg)
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        return _unknown("source_unverified")
    if result["coverage"].get("truncated") or (message_id is None and result["coverage"].get("ids_match") is not True):
        return _unknown("source_unverified")
    if checkpoint:
        checkpoint()
    def identity(record):
        ref = record.get("source_ref")
        return ((record.get("message_id"), record.get("kind"), record.get("key"),
                 ref.get("artifact_id"), ref.get("field"), ref.get("index"))
                if isinstance(ref, dict) else None)

    names = {identity(record): record.get("name") for record in result["records"]
             if record.get("kind") == "lab" and identity(record) is not None}
    projected = measurement_timeline.project_measurements(result["records"], source_complete=False)
    for item in projected["items"]:
        if item.get("state") in ("observed", "history") and item.get("verification") == "quote_supported":
            name = names.get(identity(item))
            if isinstance(name, str):
                item["name"] = name
    if checkpoint:
        checkpoint()
    result["records"] = projected["items"]
    result["projection_coverage"] = projected["coverage"]
    result["coverage"]["reader_state"] = "bounded"
    return result
