"""Verify receipted cloud history scope and fixed source occurrences."""
from __future__ import annotations
from contextlib import contextmanager
from functools import lru_cache
import json
import re
import time
from types import MappingProxyType

from anthropic_client import MODEL
from mcs_util import payload_hash, strict_json

def capture_history_tx(db, command_id, now, *, continuous=False):
    """Record the history scope within the human resume receipt transaction."""
    from inference_dependency_loss import source_binding
    if not db.in_transaction:
        raise RuntimeError("cloud_history_transaction_required")
    if continuous:
        if not isinstance(command_id, str) or not command_id or len(command_id) > 256:
            raise ValueError("cloud_history_command_invalid")
        db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
            ("cloud_history_scope", json.dumps({"version": 2, "command_id": command_id}), MODEL,
             json.dumps({"command_id": command_id}), now))
        return
    sources = {}
    cutoff = now - 600
    for count, row in enumerate(db.execute("SELECT project_id,message_id FROM messages m "
            "WHERE m.first_seen IS NOT NULL AND m.first_seen<=? "
            "AND (m.notified_at IS NULL OR m.notified_at<=?) "
            "AND NOT EXISTS (SELECT 1 FROM message_revisions r "
            "WHERE r.message_id=m.message_id AND r.observed_at>?) LIMIT 100001",
            (cutoff, cutoff, cutoff)), 1):
        if count > 100000:
            raise ValueError("cloud_history_scope_too_large")
        binding = source_binding(db, row[0], row[1])
        if binding is not None:
            sources[f"{row[0]}:{row[1]}"] = binding
    db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
        ("cloud_history_scope", json.dumps({"version": 1, "sources": sources}), MODEL,
         json.dumps({"command_id": command_id, "targets": len(sources)}), now))


@lru_cache(maxsize=2)
def _history_sources(raw):
    if not isinstance(raw, str) or len(raw) > 16 * 1024 * 1024:
        return {}
    try:
        doc = strict_json(raw)
        sources = doc.get("sources")
        if type(doc.get("version")) is not int or doc.get("version") != 1 or not isinstance(sources, dict):
            return {}
        if any(not isinstance(k, str) or re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", k) is None
               or not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
               for k, v in sources.items()):
            return {}
        return sources
    except (ValueError, TypeError, AttributeError, RecursionError):
        return {}


@lru_cache(maxsize=2)
def _history_document(raw):
    try:
        value = strict_json(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None


def _history_scope(db, scope):
    """Resolve a fixed scope or a continuous scope tied to its resume receipt."""
    if scope is None:
        return {}
    raw, meta = scope
    if not isinstance(raw, str) or len(raw) > 16 * 1024 * 1024:
        return {}
    try:
        doc = _history_document(raw)
        if isinstance(doc, dict) and type(doc.get("version")) is int and doc["version"] == 2:
            if not isinstance(meta, str) or len(meta) > 32768:
                return {}
            details = strict_json(meta)
            command_id = doc.get("command_id")
            if (set(doc) != {"version", "command_id"} or not isinstance(command_id, str)
                    or not command_id or len(command_id) > 256 or not isinstance(details, dict)
                    or details.get("command_id") != command_id):
                return {}
            control = db.execute("SELECT content,meta FROM artifacts WHERE kind='cloud_control' "
                "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.command_id') END=? "
                "ORDER BY artifact_id DESC LIMIT 1", (command_id,)).fetchone()
            if control is None or control[0] != "running":
                return {}
            control_meta = strict_json(control[1])
            if (not isinstance(control_meta, dict) or control_meta.get("command_id") != command_id
                    or not isinstance(control_meta.get('actor'), str) or not control_meta['actor'].strip()
                    or not isinstance(control_meta.get('reason'), str) or not control_meta['reason'].strip()):
                return {}
            receipt = db.execute("SELECT outcome,receipt_json FROM command_receipts WHERE command_id=?",
                                 (command_id,)).fetchone()
            if receipt is None or receipt[0] != 'applied':
                return {}
            recorded = strict_json(receipt[1])
            if (not isinstance(recorded, dict) or recorded.get('command_id') != command_id
                    or recorded.get('outcome') != 'applied' or recorded.get('control') != 'running'
                    or recorded.get('actor') != control_meta.get('actor')
                    or recorded.get('reason') != control_meta.get('reason')):
                return {}
            return None  # continuous scope, bounded by route and live source checks
    except (ValueError, TypeError, AttributeError, RecursionError):
        return {}
    return _history_sources(raw)


def history_target_ready(db, project_id, message_id):
    """Keep a newly arrived or edited processing target in the realtime lane."""
    cutoff = time.time() - 600
    return db.execute("SELECT 1 FROM messages m WHERE m.project_id=? AND m.message_id=? "
        "AND m.first_seen IS NOT NULL AND m.first_seen<=? "
        "AND (m.notified_at IS NULL OR m.notified_at<=?) AND NOT EXISTS "
        "(SELECT 1 FROM message_revisions r WHERE r.message_id=m.message_id AND r.observed_at>?)",
        (project_id, message_id, cutoff, cutoff, cutoff)).fetchone() is not None


def history_thread_allowed(db, project_id, message_id):
    """Require every prompt member to match the receipted history snapshot occurrence."""
    scope = db.execute("SELECT content,meta FROM artifacts WHERE kind='cloud_history_scope' "
                       "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    return _history_thread_allowed(db, project_id, message_id, _history_scope(db, scope))


def _history_thread_allowed(db, project_id, message_id, sources, bindings=None):
    from inference_dependency_loss import source_binding
    target = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages "
                        "WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
    if target is None:
        return False
    members = db.execute("SELECT message_id FROM messages WHERE project_id=? "
                         "AND (message_id=? OR parent_id=?)", (project_id, target[0], target[0])).fetchall()
    for row in members:
        binding = source_binding(db, project_id, row[0])
        if binding is None or sources is not None and sources.get(f"{project_id}:{row[0]}") != binding:
            return False
        if bindings is not None:
            bindings.append((row[0], binding))
    return bool(members)


def history_thread_snapshot(db, project_id, message_id):
    """Bind the active scope and every current thread occurrence to one read snapshot."""
    anchor = db.execute("SELECT artifact_id,content,meta FROM (SELECT artifact_id,content,meta FROM artifacts "
                        "WHERE kind='cloud_history_scope' ORDER BY artifact_id DESC LIMIT 1) "
                        "UNION ALL SELECT NULL,NULL,NULL UNION ALL SELECT NULL,NULL,NULL")
    try:
        scope = anchor.fetchone()
        if scope is None or scope[0] is None:
            return None
        sources = _history_scope(db, (scope[1], scope[2]))
        bindings = []
        if not _history_thread_allowed(db, project_id, message_id, sources, bindings):
            return None
        bindings.sort()
        return payload_hash({'scope_id': scope[0], 'project_id': project_id,
                             'target_id': message_id, 'members': bindings})
    finally:
        anchor.close()


def history_scope_target_sql(column):
    """Pre-filter candidates; the receipt and occurrence check remains authoritative."""
    if column not in ('m.message_id', 'j.message_id'):
        raise ValueError('cloud_history_column_invalid')
    scope = ("SELECT CASE WHEN json_valid(content) THEN content ELSE '{}' END "
             "FROM artifacts WHERE kind='cloud_history_scope' ORDER BY artifact_id DESC LIMIT 1")
    return (f"(json_extract(({scope}),'$.version')=2 OR {column} IN "
            f"(SELECT CAST(substr(s.key,instr(s.key,':')+1) AS INTEGER) "
            f"FROM json_each(({scope}), '$.sources') s))")


@contextmanager
def history_selection_check(db):
    """Bind one validated receipt and selection to the same SQLite read snapshot."""
    # Keep a statement open to retain the read snapshot without creating a
    # savepoint that an active cancellation handler could prevent releasing.
    anchor = db.execute("SELECT content,meta FROM (SELECT content,meta FROM artifacts "
                        "WHERE kind='cloud_history_scope' ORDER BY artifact_id DESC LIMIT 1) "
                        "UNION ALL SELECT NULL,NULL UNION ALL SELECT NULL,NULL")
    active = [True]
    try:
        scope = anchor.fetchone()
        resolved = _history_scope(db, scope)
        sources = None if resolved is None else MappingProxyType(dict(resolved))
        thread_results = {}

        def check(pid, mid):
            if not active[0]:
                return 0
            target = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages "
                                "WHERE project_id=? AND message_id=?", (pid, mid)).fetchone()
            if target is None:
                return 0
            key = (pid, target[0])
            if key not in thread_results:
                thread_results[key] = _history_thread_allowed(db, pid, mid, sources)
            return int(thread_results[key])

        yield check
    finally:
        active[0] = False
        anchor.close()
