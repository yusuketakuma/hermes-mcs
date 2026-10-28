"""Preserve bounded per-message intake intent while a job cannot be read."""
from __future__ import annotations

import json
import hashlib
import math
import os
import sqlite3
import time
import uuid

from inference_version import CURRENT_VERSION, LEGACY_VERSION, read_version
from mcs_util import canonical, strict_json, valid_hash

HOLD_KIND = 'semantic_intake_hold_v1'
RELEASED_KIND = 'semantic_intake_released_v1'
RELEASE_RECEIPT_KIND = 'semantic_intake_release_receipt_v1'
ARCHIVE_KIND = 'semantic_intake_archive_v1'
UNSTORED_KIND = 'semantic_intake_unstored_v1'
UNSTORED_EPOCH_KIND = 'semantic_intake_unstored_epoch_v1'
UNSTORED_ROOT_KIND = 'semantic_intake_unstored_root_v1'
_MAX_ORIGIN_BYTES = 4096
_MAX_FAILURE_MESSAGES = 512
_MAX_FAILURE_ROOTS = 128



def _unstored_unique_index(db):
    for index in db.execute('PRAGMA main.index_list(fetch_jobs)'):
        if not index[2] or index[4]:
            continue
        name = index[1].replace('"', '""')
        keys = [row for row in db.execute(f'PRAGMA main.index_xinfo("{name}")') if row[5]]
        if ([row[2] for row in keys] == ['kind', 'project_id', 'message_id']
                and all(row[4] == 'BINARY' for row in keys)):
            return name
    raise sqlite3.DatabaseError('semantic_intake_unstored_index_unverified')


def _failure_scope(messages, project_id):
    """Identify a bounded input scope, or return None if it cannot be proved."""
    if not isinstance(messages, (list, tuple)):
        return None
    pending = list(messages[:_MAX_FAILURE_MESSAGES + 1])
    if len(pending) > _MAX_FAILURE_MESSAGES:
        return None
    seen, roots, message_ids = 0, set(), set()
    try:
        while pending:
            message = pending.pop()
            seen += 1
            mid = message.message_id
            pid = message.project_id
            parent = message.parent_id
            if (seen > _MAX_FAILURE_MESSAGES or type(pid) is not int or pid != project_id
                    or type(mid) is not int or not 0 < mid < 2**63
                    or parent is not None and (type(parent) is not int or not 0 < parent < 2**63)):
                return None
            roots.add(parent if parent is not None else mid)
            message_ids.add(mid)
            if len(roots) > _MAX_FAILURE_ROOTS:
                return None
            replies = message.replies
            if not isinstance(replies, (list, tuple)) or len(pending) + len(replies) + seen > _MAX_FAILURE_MESSAGES:
                return None
            pending.extend(replies)
    except (AttributeError, TypeError):
        return None
    return (sorted(roots), sorted(message_ids)) if roots else None


def failure_roots(messages, project_id):
    scope = _failure_scope(messages, project_id)
    return scope[0] if scope else None


def record_unstored(ledger, project_id, *, roots=None, observed_ids=None):
    """Record that a rolled-back intake could not be preserved; never clear it automatically."""
    if type(project_id) is not int or not 0 < project_id < 2**63:
        raise ValueError('semantic_intake_project_unverified')
    if ledger.db.in_transaction:
        raise sqlite3.DatabaseError('semantic_intake_transaction_unverified')
    with ledger.db:
        _unstored_unique_index(ledger.db)
        for schema in ('main', 'temp'):
            if ledger.db.execute(
                    f"SELECT 1 FROM {schema}.sqlite_schema WHERE type='trigger' "
                    "AND lower(tbl_name) IN ('fetch_jobs','patients') LIMIT 1").fetchone():
                raise sqlite3.DatabaseError('semantic_intake_diagnostic_unverified')
        now = time.time()
        ledger.db.execute(
            "INSERT OR IGNORE INTO main.fetch_jobs(kind,project_id,message_id,payload,state,"
            "attempts,next_try,created_at,updated_at) VALUES(?,0,0,'{}','done',0,0,?,?)",
            (UNSTORED_EPOCH_KIND, now, now))
        updated = ledger.db.execute(
            "UPDATE main.fetch_jobs SET attempts=attempts+1,updated_at=MAX(updated_at,?) "
            "WHERE kind=? AND project_id=0 AND message_id=0 AND state='done' "
            "AND payload='{}' COLLATE BINARY AND reason_code IS NULL "
            "AND typeof(attempts)='integer' AND attempts>=0 AND attempts<?",
            (now, UNSTORED_EPOCH_KIND, 2**63 - 1))
        if updated.rowcount != 1:
            raise sqlite3.DatabaseError('semantic_intake_unstored_epoch_unverified')
        ledger.db.execute(
            "INSERT OR IGNORE INTO main.fetch_jobs(kind,project_id,message_id,payload,state,"
            "attempts,next_try,created_at,updated_at) VALUES(?,?,0,'{}','pending',0,0,?,?)",
            (UNSTORED_KIND, project_id, now, now))
        updated = ledger.db.execute(
            "UPDATE main.fetch_jobs SET attempts=attempts+1,updated_at=MAX(updated_at,?) "
            "WHERE kind=? AND project_id=? AND message_id=0 AND state='pending' "
            "AND payload='{}' COLLATE BINARY AND reason_code IS NULL "
            "AND typeof(attempts)='integer' AND attempts>=0 AND attempts<?",
            (now, UNSTORED_KIND, project_id, 2**63 - 1))
        if updated.rowcount != 1:
            raise sqlite3.DatabaseError('semantic_intake_diagnostic_unpersisted')
        if roots is not None and observed_ids is not None:
            if (not isinstance(observed_ids, list) or len(observed_ids) > _MAX_FAILURE_MESSAGES
                    or any(type(mid) is not int or not 0 < mid < 2**63 for mid in observed_ids)
                    or not _rowid_primary_key(ledger.db, 'messages', 'message_id')):
                roots = None
            else:
                existing = ledger.db.execute(
                    'SELECT message_id,project_id,parent_id FROM main.messages '
                    'WHERE message_id IN (SELECT value FROM json_each(?)) LIMIT ?',
                    (json.dumps(observed_ids), _MAX_FAILURE_MESSAGES + 1)).fetchall()
                expanded = set(roots)
                for row in existing:
                    parent = row['parent_id']
                    if (row['project_id'] != project_id or parent is not None
                            and (type(parent) is not int or not 0 < parent < 2**63)):
                        roots = None
                        break
                    expanded.add(parent if parent is not None else row['message_id'])
                if roots is not None:
                    roots = sorted(expanded) if len(expanded) <= _MAX_FAILURE_ROOTS else None
        if roots is None:
            roots = [0]  # the affected scope could not be proven
        elif (not isinstance(roots, list) or not roots or len(roots) > _MAX_FAILURE_ROOTS
              or any(type(root) is not int or not 0 < root < 2**63 for root in roots)):
            raise ValueError('semantic_intake_scope_unverified')
        for root in roots:
            ledger.db.execute(
                "INSERT OR IGNORE INTO main.fetch_jobs(kind,project_id,message_id,payload,state,"
                "attempts,next_try,created_at,updated_at) VALUES(?,?,?,'{}','pending',0,0,?,?)",
                (UNSTORED_ROOT_KIND, project_id, root, now, now))
            if not ledger.db.execute(
                    "SELECT 1 FROM main.fetch_jobs WHERE kind=? AND project_id=? AND message_id=? LIMIT 1",
                    (UNSTORED_ROOT_KIND, project_id, root)).fetchone():
                raise sqlite3.DatabaseError('semantic_intake_scope_unverified')
        ledger.db.execute(
            "UPDATE main.patients SET fetch_state='incomplete',fetch_reason=CASE "
            "WHEN fetch_reason IS NULL OR fetch_reason='' THEN 'semantic_intake_unstored' "
            "ELSE fetch_reason END "
            "WHERE project_id=? AND fetch_state IS NOT 'forbidden'", (project_id,))


def has_unstored(db, project_id):
    name = _unstored_unique_index(db)
    return db.execute(
        f'SELECT 1 FROM main.fetch_jobs INDEXED BY "{name}" '
        'WHERE kind=? AND project_id=? AND message_id=0 LIMIT 1',
        (UNSTORED_KIND, project_id)).fetchone() is not None


def has_unstored_root(db, project_id, root):
    """Hold a known affected root, or all roots when scope cannot be proven."""
    name = _unstored_unique_index(db)
    query = f'SELECT 1 FROM main.fetch_jobs INDEXED BY "{name}" '
    if db.execute(query + 'WHERE kind=? AND project_id=? AND message_id IN (0,?) LIMIT 1',
                  (UNSTORED_ROOT_KIND, project_id, root)).fetchone():
        return True
    if not unstored_project_epoch(db, project_id):
        return False
    return db.execute(query + 'WHERE kind=? AND project_id=? LIMIT 1',
                      (UNSTORED_ROOT_KIND, project_id)).fetchone() is None


def unstored_project_epoch(db, project_id):
    """Observe one patient's failure count for last-moment presentation guards."""
    name = _unstored_unique_index(db)
    row = db.execute(
        f"SELECT state,attempts,payload='{{}}' COLLATE BINARY AND reason_code IS NULL "
        f'FROM main.fetch_jobs INDEXED BY "{name}" '
        'WHERE kind=? AND project_id=? AND message_id=0 LIMIT 1',
        (UNSTORED_KIND, project_id)).fetchone()
    if row is None:
        return 0
    if row[0] != 'pending' or type(row[1]) is not int or not 0 < row[1] < 2**63 or row[2] != 1:
        raise sqlite3.DatabaseError('semantic_intake_unstored_unverified')
    return row[1]


def unstored_epoch(db):
    """Read the monotonic failure generation and validate the bounded marker index."""
    name = 'idx_semantic_intake_unstored_epoch'
    index = next((row for row in db.execute('PRAGMA main.index_list(fetch_jobs)')
                  if row[1] == name), None)
    keys = [row[2] for row in db.execute(f'PRAGMA main.index_xinfo({name})') if row[5]]
    ddl = db.execute("SELECT sql FROM main.sqlite_schema WHERE type='index' AND name=?",
                     (name,)).fetchone()
    normalized = ' '.join(ddl[0].lower().split()) if ddl and isinstance(ddl[0], str) else ''
    if (index is None or index[4] != 1 or keys != ['job_id']
            or normalized != "create index idx_semantic_intake_unstored_epoch on fetch_jobs(job_id) where kind='semantic_intake_unstored_v1'"):
        raise sqlite3.DatabaseError('semantic_intake_unstored_index_unverified')
    marker = db.execute(
        "SELECT job_id FROM main.fetch_jobs INDEXED BY idx_semantic_intake_unstored_epoch "
        "WHERE kind='semantic_intake_unstored_v1' ORDER BY job_id DESC LIMIT 1").fetchone()
    unique = _unstored_unique_index(db)
    row = db.execute(
        f"SELECT state,attempts,payload='{{}}' COLLATE BINARY AND reason_code IS NULL "
        f'FROM main.fetch_jobs INDEXED BY "{unique}" '
        'WHERE kind=? AND project_id=0 AND message_id=0 LIMIT 2',
        (UNSTORED_EPOCH_KIND,)).fetchall()
    if not row:
        if marker:
            raise sqlite3.DatabaseError('semantic_intake_unstored_epoch_unverified')
        return 0
    if (len(row) != 1 or row[0][0] != 'done' or row[0][2] != 1 or type(row[0][1]) is not int
            or not 0 < row[0][1] < 2**63):
        raise sqlite3.DatabaseError('semantic_intake_unstored_epoch_unverified')
    return row[0][1]


def report_write_failure(ledger, result, project_id, exc, ack_exclusions=None, job_id=None, messages=None):
    """Report a rolled-back hold write, leaving unclassified failures to the caller."""
    code = getattr(exc, 'sqlite_errorcode', None)
    storage_error = type(code) is int and code & 0xff in (
        sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_NOMEM,
        sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN)
    if not (getattr(exc, 'semantic_intake_hold_write_failed', False) or storage_error):
        return False
    if ack_exclusions is not None:
        ack_exclusions.add(project_id)
    incomplete = result.setdefault('incomplete', [])
    if project_id not in incomplete:
        incomplete.append(project_id)
    result['errors'].append(f'project {project_id}: semantic_intake_unstored')
    recorded = False
    try:
        scope = _failure_scope(messages, project_id)
        record_unstored(ledger, project_id, roots=scope[0] if scope else None,
                        observed_ids=scope[1] if scope else None)
        recorded = True
    except Exception:
        result['errors'].append(f'project {project_id}: semantic_intake_diagnostic_unpersisted')
    if job_id is not None and recorded:
        try:
            with ledger.db:
                ledger.db.execute('BEGIN IMMEDIATE')
                for schema in ('main', 'temp'):
                    if ledger.db.execute(
                            f"SELECT 1 FROM {schema}.sqlite_schema WHERE type='trigger' "
                            "AND lower(tbl_name)='fetch_jobs' LIMIT 1").fetchone():
                        raise sqlite3.DatabaseError('semantic_intake_retry_unverified')
                before = ledger.db.total_changes
                updated = ledger.db.execute(
                    "UPDATE main.fetch_jobs SET next_try=?,updated_at=? WHERE job_id=? AND state='pending'",
                    (time.time() + 300, time.time(), job_id))
                if updated.rowcount != 1 or ledger.db.total_changes != before + 1:
                    raise sqlite3.DatabaseError('semantic_intake_retry_unverified')
        except sqlite3.Error:
            result['errors'].append(f'project {project_id}: semantic_intake_retry_unpersisted')
    elif job_id is not None:
        result['errors'].append(f'project {project_id}: semantic_intake_retry_unpersisted')
    return True


def validate_indexes(db):
    """Require the scoped indexes before selecting or storing a hold."""
    expected = {
        'idx_semantic_intake_hold_root': (
            False, ['kind', 'project_id', 'model'],
            "create index idx_semantic_intake_hold_root on artifacts"
            "(kind, project_id, model) where kind='semantic_intake_hold_v1'"),
        'idx_semantic_intake_hold_epoch': (
            False, ['artifact_id'],
            "create index idx_semantic_intake_hold_epoch on artifacts"
            "(artifact_id) where kind='semantic_intake_hold_v1'"),
        'idx_semantic_intake_hold_project_epoch': (
            False, ['project_id', 'artifact_id'],
            "create index idx_semantic_intake_hold_project_epoch on artifacts"
            "(project_id, artifact_id) where kind='semantic_intake_hold_v1'"),
        'uq_semantic_intake_hold': (
            True, ['kind', 'project_id', 'model', 'meta'],
            "create unique index uq_semantic_intake_hold on artifacts"
            "(kind, project_id, model, meta) where kind='semantic_intake_hold_v1'"),
        'idx_semantic_intake_release_epoch': (
            False, ['artifact_id'],
            "create index idx_semantic_intake_release_epoch on artifacts"
            "(artifact_id) where kind in ('semantic_intake_released_v1','semantic_intake_release_receipt_v1')"),
        'idx_semantic_intake_release_project_epoch': (
            False, ['project_id', 'artifact_id'],
            "create index idx_semantic_intake_release_project_epoch on artifacts"
            "(project_id, artifact_id) where kind in ('semantic_intake_released_v1','semantic_intake_release_receipt_v1')"),
        'uq_semantic_intake_release_request': (
            True, ['kind', 'project_id', 'model'],
            "create unique index uq_semantic_intake_release_request on artifacts"
            "(kind, project_id, model) where kind='semantic_intake_release_receipt_v1'"),
    }
    indexes = {row[1]: row for row in db.execute('PRAGMA main.index_list(artifacts)')}
    for name, (unique, columns, ddl) in expected.items():
        index = indexes.get(name)
        rows = [row for row in db.execute(f'PRAGMA main.index_xinfo({name})') if row[5]]
        sql = db.execute("SELECT sql FROM main.sqlite_schema WHERE type='index' AND name=?",
                         (name,)).fetchone()
        normalized = ' '.join(sql[0].lower().split()) if sql and isinstance(sql[0], str) else ''
        if (index is None or bool(index[2]) != unique or index[4] != 1
                or [row[2] for row in rows] != columns
                or any(row[4] != 'BINARY' for row in rows) or normalized != ddl):
            raise sqlite3.DatabaseError('semantic_hold_index_unverified')


def hold_epoch(db, project_id=None):
    """Observe intake and release evidence without walking unrelated artifacts."""
    validate_indexes(db)
    if project_id is None:
        row = db.execute(
            "SELECT artifact_id FROM main.artifacts INDEXED BY idx_semantic_intake_hold_epoch "
            "WHERE kind='semantic_intake_hold_v1' ORDER BY artifact_id DESC LIMIT 1").fetchone()
    else:
        row = db.execute(
            "SELECT artifact_id FROM main.artifacts INDEXED BY idx_semantic_intake_hold_project_epoch "
            "WHERE kind='semantic_intake_hold_v1' AND project_id=? "
            "ORDER BY artifact_id DESC LIMIT 1", (project_id,)).fetchone()
    index = ('idx_semantic_intake_release_epoch' if project_id is None
             else 'idx_semantic_intake_release_project_epoch')
    released = db.execute(
        f'SELECT artifact_id FROM main.artifacts INDEXED BY {index} '
        "WHERE kind IN ('semantic_intake_released_v1','semantic_intake_release_receipt_v1') "
        + ('' if project_id is None else 'AND project_id=? ')
        + 'ORDER BY artifact_id DESC LIMIT 1', () if project_id is None else (project_id,)).fetchone()
    return max(row[0] if row else 0, released[0] if released else 0)

def _rowid_primary_key(db, table, key):
    if table not in ('fetch_jobs', 'artifacts', 'notify_outbox', 'messages'):
        return False
    fields = list(db.execute(f'PRAGMA main.table_xinfo({table})'))
    pk = [row for row in fields if row[5]]
    names = {row[1].lower() for row in fields}
    indexes = list(db.execute(f'PRAGMA main.index_list({table})'))
    info = next((row for row in db.execute(f'PRAGMA main.table_list({table})')
                 if row[0] == 'main' and row[1] == table and row[2] == 'table'), None)
    return (info is not None and info[4] == 0 and len(pk) == 1
            and pk[0][1].lower() == key and pk[0][2].upper() == 'INTEGER'
            and not any(row[3] == 'pk' for row in indexes)
            and not names.intersection({'rowid', '_rowid_', 'oid'}))


def _codec(db):
    encoding = db.execute('PRAGMA main.encoding').fetchone()[0]
    codec = {'utf-8': 'utf-8', 'utf-16le': 'utf-16-le', 'utf-16be': 'utf-16-be'}.get(
        encoding.lower() if isinstance(encoding, str) else '')
    if codec is None:
        raise sqlite3.DatabaseError('semantic_hold_encoding_unverified')
    return codec


def _payload(db, row, limit):
    if row['payload_type'] != 'text' or not _rowid_primary_key(db, 'fetch_jobs', 'job_id'):
        return None
    with db.blobopen('fetch_jobs', 'payload', row['job_id'], readonly=True) as blob:
        if len(blob) > limit:
            return None
        try:
            return blob.read().decode(_codec(db))
        except UnicodeError:
            return None


def supported_job(db, row, project_id, root):
    """Reject unknown or malformed job input without altering its stored bytes."""
    class Object(dict):
        def __init__(self, pairs):
            super().__init__(pairs)
            self.has_duplicates = len({key for key, _ in pairs}) != len(pairs)

    raw = _payload(db, row, 1024 * 1024)
    if raw is None:
        return False
    try:
        value = json.loads(raw, object_pairs_hook=Object)
        if (not isinstance(value, dict) or value.has_duplicates
                or read_version(value) not in (CURRENT_VERSION, LEGACY_VERSION)):
            return False
        canonical(value)
    except (TypeError, ValueError, RecursionError):
        return False
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > 128 or (type(node) is int and not -(2**63) <= node < 2**63):
            return False
        if isinstance(node, dict):
            if node.has_duplicates:
                return False
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
    if value.get('generation') is not None and not isinstance(value['generation'], str):
        return False
    notification_targets = value.get('intake_notification_targets')
    if notification_targets is not None and (not isinstance(notification_targets, dict)
            or any(not isinstance(mid, str) or not mid.isascii() or not mid.isdecimal()
                   or len(mid) > 19 or str(int(mid)) != mid or not 0 < int(mid) < 2**63 or not valid_hash(fp)
                   for mid, fp in notification_targets.items())):
        return False
    if 'targets' not in value:
        return True
    targets = value['targets']
    if (not isinstance(targets, list) or not targets
            or any(type(mid) is not int or not 0 < mid < 2**63 for mid in targets)):
        return False
    # A missing target is allowed by the legacy seed contract. A target that
    # exists under another thread is known to be out of scope and must hold.
    return db.execute(
        'SELECT 1 FROM json_each(?) target JOIN messages m ON m.message_id=target.value '
        'WHERE m.project_id IS NOT ? OR (m.message_id != ? AND COALESCE(m.parent_id,0) != ?) LIMIT 1',
        (json.dumps(targets), project_id, root, root)).fetchone() is None


def has_held_root(db, project_id, root):
    return has_unstored_root(db, project_id, root) or db.execute(
        "SELECT 1 FROM main.artifacts INDEXED BY idx_semantic_intake_hold_root "
        "WHERE kind='semantic_intake_hold_v1' AND project_id=? AND model=? LIMIT 1",
        (project_id, str(root))).fetchone() is not None


def quarantine_invalid_job(ledger, job):
    """Fail bounded malformed input without replacing its bytes or retry budget."""
    raw = job['payload']
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        return False
    try:
        if len(raw.encode('utf-8')) > 1024 * 1024:
            return False
        value = strict_json(raw)
    except UnicodeError:
        return False
    except (ValueError, TypeError, RecursionError):
        try:
            if isinstance(json.loads(raw), dict):
                # Duplicate keys and non-finite values cannot establish a
                # unique schema. Keep such objects held, including future data.
                return False
        except (ValueError, TypeError, RecursionError):
            pass
        value = None
    if isinstance(value, dict) and read_version(value) not in (CURRENT_VERSION, LEGACY_VERSION):
        return False
    if ledger.db.in_transaction:
        raise sqlite3.DatabaseError('semantic_intake_transaction_unverified')
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        if ledger.db.execute("SELECT 1 FROM temp.sqlite_schema WHERE type IN ('table','view') "
                             "AND lower(name) IN ('fetch_jobs','artifacts') LIMIT 1").fetchone():
            raise sqlite3.DatabaseError('semantic_intake_schema_unverified')
        for schema in ('main', 'temp'):
            if ledger.db.execute(f"SELECT 1 FROM {schema}.sqlite_schema WHERE type='trigger' "
                                 "AND lower(tbl_name)='fetch_jobs' LIMIT 1").fetchone():
                raise sqlite3.DatabaseError('semantic_intake_trigger_unverified')
        validate_indexes(ledger.db)
        row = ledger.db.execute(
            'SELECT job_id,typeof(payload) AS payload_type FROM main.fetch_jobs WHERE job_id=?',
            (job['job_id'],)).fetchone()
        if row is None or supported_job(ledger.db, row, job['project_id'], job['message_id']):
            return False
        changed = ledger.db.execute(
            "UPDATE main.fetch_jobs SET state='failed',reason_code='semantic_payload_invalid',updated_at=? "
            "WHERE job_id=? AND kind='semantic' AND project_id=? AND message_id=? "
            "AND state='pending' AND attempts IS ? AND payload IS ? AND updated_at IS ?",
            (time.time(), job['job_id'], job['project_id'], job['message_id'],
             job['attempts'], raw, job['updated_at']))
        return changed.rowcount == 1


def _read_intent(db, row, root):
    """Validate one bounded immutable intake identity."""
    with db.blobopen('artifacts', 'content', row['artifact_id'], readonly=True) as content:
        if len(content) > 16384:
            raise ValueError('semantic_hold_record_unverified')
        data = content.read().decode(_codec(db))
    with db.blobopen('artifacts', 'meta', row['artifact_id'], readonly=True) as meta:
        if len(meta) > 1024:
            raise ValueError('semantic_hold_record_unverified')
        metadata = strict_json(meta.read().decode(_codec(db)))
    value = strict_json(data)
    digest = hashlib.sha256(data.encode('utf-8')).hexdigest()
    if (not isinstance(metadata, dict) or metadata != {'intent_sha256': digest}
            or not isinstance(value, dict) or type(value.get('contract')) is not int
            or value['contract'] != 1 or type(value.get('root_id')) is not int
            or value['root_id'] != root or type(value.get('target_id')) is not int
            or not 0 < value['target_id'] < 2**63
            or not valid_hash(value.get('fingerprint'))
            or not valid_hash(value.get('source_generation'))):
        raise ValueError('semantic_hold_record_unverified')
    return value, digest


def preview_root(db, project_id, root, *, after=0, limit=16):
    """Read a bounded page of local hold identities; do not infer recovery eligibility."""
    if (type(project_id) is not int or not 0 < project_id < 2**63
            or type(root) is not int or not 0 < root < 2**63
            or type(after) is not int or not 0 <= after < 2**63
            or type(limit) is not int or not 1 <= limit <= 32):
        raise ValueError('semantic_hold_preview_args')
    if db.in_transaction:
        raise ValueError('semantic_hold_preview_transaction_active')
    with db:
        db.execute('BEGIN')
        if db.execute("SELECT 1 FROM temp.sqlite_schema WHERE type IN ('table','view') "
                      "AND lower(name) IN ('fetch_jobs','artifacts','notify_outbox','messages') LIMIT 1").fetchone():
            raise sqlite3.DatabaseError('semantic_hold_preview_schema_unverified')
        validate_indexes(db)
        _unstored_unique_index(db)
        if not _rowid_primary_key(db, 'artifacts', 'artifact_id'):
            raise sqlite3.DatabaseError('semantic_hold_artifact_identity_unverified')
        try:
            epoch = unstored_epoch(db)
            name = _unstored_unique_index(db)
            marker = db.execute(
                f'SELECT CASE WHEN state=\'pending\' THEN state END AS state,'
                "CASE WHEN typeof(attempts)='integer' THEN attempts END AS attempts,"
                "payload='{}' COLLATE BINARY AND reason_code IS NULL AS valid_payload,"
                "CASE WHEN typeof(created_at) IN ('integer','real') THEN created_at END AS first_at,"
                "CASE WHEN typeof(updated_at) IN ('integer','real') THEN updated_at END AS last_at "
                f'FROM main.fetch_jobs INDEXED BY "{name}" '
                "WHERE kind=? AND project_id=? AND message_id=0 LIMIT 1",
                (UNSTORED_KIND, project_id)).fetchone()
            unstored = 'present' if marker else 'none_observed'
            if marker and (marker['state'] != 'pending' or marker['valid_payload'] != 1
                           or type(marker['attempts']) is not int
                           or marker['attempts'] < 1 or not all(
                               type(marker[key]) in (int, float) and math.isfinite(marker[key])
                               and marker[key] >= 0 for key in ('first_at', 'last_at'))
                           or marker['last_at'] < marker['first_at']):
                raise sqlite3.DatabaseError('semantic_intake_unstored_unverified')
        except sqlite3.Error:
            epoch = None
            unstored = 'unverified'
            marker = None
        job = db.execute(
            "SELECT job_id,CASE WHEN state IN ('pending','failed','done') THEN state END AS state,"
            "CASE WHEN typeof(attempts)='integer' THEN attempts END AS attempts,"
            "typeof(payload) AS payload_type FROM main.fetch_jobs "
            "WHERE kind='semantic' AND project_id=? AND message_id=? LIMIT 1",
            (project_id, root)).fetchone()
        raw = _payload(db, job, 1024 * 1024) if job is not None else None
        job_info = {'status': 'missing' if job is None else 'unverified'}
        if raw is not None:
            job_info = {'status': 'supported' if supported_job(db, job, project_id, root) else 'held',
                        'payload_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                        'state': job['state'] if job['state'] in ('pending', 'failed', 'done') else 'unverified',
                        'attempts': job['attempts'] if type(job['attempts']) is int
                                    and 0 <= job['attempts'] < 2**63 else None}
        rows = db.execute(
            "SELECT artifact_id,typeof(content) AS content_type,typeof(meta) AS meta_type "
            "FROM main.artifacts INDEXED BY idx_semantic_intake_hold_root "
            "WHERE kind='semantic_intake_hold_v1' AND project_id=? AND model=? AND artifact_id>? "
            "ORDER BY artifact_id LIMIT ?", (project_id, str(root), after, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            entry = {'hold_id': row['artifact_id'], 'status': 'unverified'}
            if row['content_type'] == 'text' and row['meta_type'] == 'text':
                try:
                    value, digest = _read_intent(db, row, root)
                    event_id = value.get('event_id')
                    if event_id is not None and (type(event_id) is not int or not 0 < event_id < 2**63):
                        raise ValueError('semantic_hold_origin_unverified')
                    event_state = 'no_event_recorded' if event_id is None else 'unverified'
                    if type(event_id) is int and 0 < event_id < 2**63:
                        event = db.execute(
                            "SELECT CASE WHEN state IN ('pending','in_flight','accepted','failed','suppressed') "
                            "THEN state ELSE 'unverified' END AS state FROM main.notify_outbox "
                            "WHERE event_id=? AND kind='new_messages' AND project_id=?",
                            (event_id, project_id)).fetchone()
                        if event is not None and value['target_id'] in _arrival_targets(
                                db, project_id, event_id, strict=True):
                            event_state = event['state']
                    entry.update(status='observed', intent_sha256=digest,
                                 target_id=value['target_id'], event_id=event_id,
                                 event_state=event_state,
                                 recorded_fingerprint=value['fingerprint'],
                                 recorded_source_generation=value['source_generation'],
                                 source_status='unverified')
                except (ValueError, TypeError, RecursionError, UnicodeError, sqlite3.Error):
                    pass
            items.append(entry)
        return {'project_id': project_id, 'root_id': root, 'job': job_info,
                'holds': items, 'next_cursor': items[-1]['hold_id'] if len(rows) > limit else None,
                'eligibility': 'unverified', 'intake_unstored': unstored,
                'intake_hold_epoch': hold_epoch(db, project_id),
                'intake_unstored_epoch': epoch,
                'intake_unstored_observation': ({'failures': marker['attempts'],
                    'first_at': marker['first_at'], 'last_at': marker['last_at']} if marker else None)}


def _arrival_targets(db, project_id, event_id, *, strict=False):
    """Read only a bounded recorded event before deriving target eligibility."""
    row = db.execute(
        "SELECT event_id,typeof(payload) AS payload_type FROM notify_outbox "
        "WHERE event_id=? AND kind='new_messages' AND project_id=?",
        (event_id, project_id)).fetchone()
    if (row is None or row['payload_type'] not in ('text', 'blob')
            or not _rowid_primary_key(db, 'notify_outbox', 'event_id')):
        raise ValueError('semantic_hold_origin_unverified')
    with db.blobopen('notify_outbox', 'payload', event_id, readonly=True) as blob:
        if len(blob) > 1024 * 1024:
            raise ValueError('semantic_hold_origin_too_large')
        try:
            raw = blob.read()
            if row['payload_type'] == 'text':
                raw = raw.decode(_codec(db))
            value = strict_json(raw) if strict else json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            raise ValueError('semantic_hold_origin_unverified') from None
    ids = value.get('message_ids') if isinstance(value, dict) else None
    if (not isinstance(ids, list)
            or any(type(mid) is not int or not 0 < mid < 2**63 for mid in ids)):
        raise ValueError('semantic_hold_origin_unverified')
    return set(ids)


def hold_tx(ledger, project_id, root, message_ids, origin, *, eligible_ids=None,
            source_generation):
    """Append deduplicated target and source identities in the caller's transaction."""
    if not isinstance(origin, dict):
        raise ValueError('semantic_hold_origin_unverified')
    if not _rowid_primary_key(ledger.db, 'artifacts', 'artifact_id'):
        raise sqlite3.DatabaseError('semantic_hold_artifact_identity_unverified')
    origin_raw = canonical(origin)
    if len(origin_raw) > _MAX_ORIGIN_BYTES:
        raise ValueError('semantic_hold_origin_too_large')
    if not valid_hash(source_generation):
        raise ValueError('semantic_hold_source_unverified')
    event_id = origin.get('event_id')
    if event_id is not None:
        if type(event_id) is not int or not 0 < event_id < 2**63:
            raise ValueError('semantic_hold_origin_unverified')
        if eligible_ids is None:
            eligible_ids = _arrival_targets(ledger.db, project_id, event_id)
    else:
        eligible_ids = set()
    now = time.time()
    for mid in message_ids:
        fingerprint = ledger._semantic_message_fingerprint(project_id, mid)
        if fingerprint is None:
            raise ValueError('semantic_hold_source_unverified')
        incoming_event = event_id if mid in eligible_ids and not origin.get('notification_free') else None
        payload = {'contract': 1, 'root_id': root, 'target_id': mid,
                   'fingerprint': fingerprint, 'source_generation': source_generation,
                   'event_id': incoming_event, 'origin': origin}
        data = canonical(payload).decode('utf-8')
        digest = hashlib.sha256(data.encode('utf-8')).hexdigest()
        meta = json.dumps({'intent_sha256': digest}, sort_keys=True, separators=(',', ':'))
        changes = ledger.db.total_changes
        cur = ledger.db.execute(
            "INSERT OR IGNORE INTO main.artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "VALUES(?,?,NULL,?,?,?,?)", (HOLD_KIND, project_id, data, str(root), meta, now))
        if cur.rowcount not in (0, 1) or ledger.db.total_changes != changes + cur.rowcount:
            raise sqlite3.DatabaseError('semantic_hold_write_unverified')
        matches = ledger.db.execute(
            "SELECT artifact_id,typeof(content) AS content_type FROM main.artifacts "
            "INDEXED BY uq_semantic_intake_hold WHERE kind='semantic_intake_hold_v1' "
            "AND project_id=? AND model=? AND meta=? LIMIT 2",
            (project_id, str(root), meta)).fetchall()
        if len(matches) != 1 or matches[0]['content_type'] != 'text':
            raise ValueError('semantic_hold_record_unverified')
        with ledger.db.blobopen('artifacts', 'content', matches[0]['artifact_id'], readonly=True) as blob:
            if len(blob) > 16384:
                raise ValueError('semantic_hold_record_unverified')
            try:
                found = blob.read().decode(_codec(ledger.db))
            except UnicodeError:
                raise ValueError('semantic_hold_record_unverified') from None
        if found != data:
            raise ValueError('semantic_hold_record_unverified')


def _release_schema(db):
    """Reject shadow tables and write triggers other than the exact relation guards."""
    from ledger_schema import _GUARD_RELATIONS
    if db.execute("SELECT 1 FROM temp.sqlite_schema WHERE type IN ('table','view') "
                  "AND lower(name) IN ('fetch_jobs','artifacts','notify_outbox','messages',"
                  "'attachments','message_revisions','patients') LIMIT 1").fetchone():
        raise sqlite3.DatabaseError('semantic_release_schema_unverified')
    allowed = {}
    condition = _GUARD_RELATIONS[0][2].replace('a.', 'NEW.')
    for suffix, event in (('ins', 'INSERT'), ('upd', 'UPDATE OF message_id, project_id')):
        name = f'g1_artifacts_msg_{suffix}'
        ddl = f"""CREATE TRIGGER {name} BEFORE {event} ON artifacts WHEN {condition} BEGIN
          SELECT RAISE(ABORT,'g1: artifacts message relation')
          WHERE (SELECT mode FROM ledger_relation_guards WHERE relation='artifacts')='enforce';
          UPDATE ledger_relation_guards SET shadow_count=shadow_count+1
          WHERE relation='artifacts' AND mode='shadow'; END"""
        allowed[name] = ' '.join(ddl.split())
    for schema in ('main', 'temp'):
        for row in db.execute(f"SELECT name,sql FROM {schema}.sqlite_schema WHERE type='trigger' "
                              "AND lower(tbl_name) IN ('fetch_jobs','artifacts','notify_outbox')"):
            if schema != 'main' or ' '.join((row['sql'] or '').split()) != allowed.get(row['name']):
                raise sqlite3.DatabaseError('semantic_release_trigger_unverified')
    validate_indexes(db)
    _unstored_unique_index(db)
    if any(not _rowid_primary_key(db, table, key) for table, key in (
            ('fetch_jobs', 'job_id'), ('artifacts', 'artifact_id'),
            ('messages', 'message_id'), ('notify_outbox', 'event_id'))):
        raise sqlite3.DatabaseError('semantic_release_identity_unverified')


def _release_plan(ledger, project_id, root, job_id, expected_job_sha256=None):
    """Read a complete bounded root plan; authority is rechecked at apply."""
    from semantic_drain import _reservation_sched
    scheduler, long_defers = _reservation_sched(ledger)
    job = ledger.db.execute(
        "SELECT *,typeof(payload) AS payload_type FROM main.fetch_jobs WHERE job_id=? "
        "AND kind='semantic' AND project_id=? AND message_id=?",
        (job_id, project_id, root)).fetchone()
    if (job is None or not supported_job(ledger.db, job, project_id, root)
            or job['state'] not in ('pending', 'done', 'failed')
            or type(job['attempts']) is not int or not 0 <= job['attempts'] < 2**63
            or any(type(job[key]) not in (int, float) or not math.isfinite(job[key])
                   or not 0 <= job[key] < 2**63 for key in ('next_try', 'created_at', 'updated_at'))):
        raise ValueError('semantic_release_job_unverified')
    raw = _payload(ledger.db, job, 1024 * 1024)
    if expected_job_sha256 is not None and hashlib.sha256(raw.encode('utf-8')).hexdigest() != expected_job_sha256:
        raise ValueError('semantic_release_stale')
    payload = strict_json(raw)
    if (any(key in payload and type(payload[key]) is not bool
            for key in ('eligible', 'notification_free'))
            or 'origin' in payload and not isinstance(payload['origin'], dict)):
        raise ValueError('semantic_release_job_unverified')
    targets = set(payload.get('targets', [root]))
    if payload.get('eligible') and not payload.get('notification_free'):
        origin = payload.get('origin')
        event_id = origin.get('event_id') if isinstance(origin, dict) else None
        if (type(event_id) is not int or not 0 < event_id < 2**63
                or not targets.intersection(_arrival_targets(ledger.db, project_id, event_id, strict=True))):
            raise ValueError('semantic_release_original_origin_unverified')
    rows = ledger.db.execute(
        "SELECT artifact_id,typeof(content) AS content_type,typeof(meta) AS meta_type "
        "FROM main.artifacts INDEXED BY idx_semantic_intake_hold_root "
        "WHERE kind='semantic_intake_hold_v1' AND project_id=? AND model=? "
        "ORDER BY artifact_id LIMIT 129", (project_id, str(root))).fetchall()
    if not rows or len(rows) > 128:
        raise ValueError('semantic_release_capacity')
    thread = ledger.db.execute(
        'SELECT message_id FROM main.messages WHERE project_id=? '
        'AND (message_id=? OR parent_id=?) LIMIT 129', (project_id, root, root)).fetchall()
    if len(thread) > 128:
        raise ValueError('semantic_release_capacity')
    if not thread or not targets <= {r[0] for r in thread}:
        raise ValueError('semantic_release_source_unverified')
    allowed = set(targets) if not payload.get('notification_free') else set()
    if 'intake_notification_targets' in payload:
        allowed = {mid for mid in allowed if notification_allowed(ledger, payload, project_id, mid)}
    receipts, origins = [], []
    for item in rows:
        if item['content_type'] != 'text' or item['meta_type'] != 'text':
            raise ValueError('semantic_hold_record_unverified')
        intent, digest = _read_intent(ledger.db, item, root)
        mid = intent['target_id']
        if (mid not in {r[0] for r in thread}
                or ledger._semantic_message_fingerprint(project_id, mid) != intent['fingerprint']):
            raise ValueError('semantic_release_source_changed')
        event_id = intent.get('event_id')
        if event_id is not None:
            if type(event_id) is not int or not 0 < event_id < 2**63:
                raise ValueError('semantic_hold_origin_unverified')
            if mid not in _arrival_targets(ledger.db, project_id, event_id, strict=True):
                raise ValueError('semantic_hold_origin_unverified')
            event = ledger.db.execute('SELECT state FROM main.notify_outbox WHERE event_id=?',
                                      (event_id,)).fetchone()
            if event[0] not in ('pending', 'in_flight', 'accepted', 'failed', 'suppressed'):
                raise ValueError('semantic_hold_origin_unverified')
            if event[0] != 'suppressed':
                allowed.add(mid)
                origins.append({'source': 'intake_recovery', 'event_id': event_id})
        targets.add(mid)
        receipts.append({'hold_id': item['artifact_id'], 'intent_sha256': digest})
    if len(targets) > 128:
        raise ValueError('semantic_release_capacity')
    source_fields = ('body_text', 'sender_id', 'sender_name', 'sender_type', 'profession',
                     'organization', 'posted_at', 'content_hash', 'body_state', 'reply_count')
    size_sql = '+'.join(f'COALESCE(length(CAST({key} AS BLOB)),0)' for key in source_fields)
    size = ledger.db.execute(
        f'SELECT SUM({size_sql}) FROM main.messages '
        'WHERE project_id=? AND (message_id=? OR parent_id=?)', (project_id, root, root)).fetchone()[0]
    attachments = ledger.db.execute(
        'SELECT COALESCE(length(CAST(a.file_id AS BLOB)),0)+COALESCE(length(CAST(a.name AS BLOB)),0)'
        '+COALESCE(length(CAST(a.sha256 AS BLOB)),0)+COALESCE(length(CAST(a.state AS BLOB)),0) '
        'FROM main.attachments a JOIN main.messages m ON m.message_id=a.message_id '
        'WHERE m.project_id=? AND (m.message_id=? OR m.parent_id=?) LIMIT 513', (project_id, root, root)).fetchall()
    if ((size or 0) > 4 * 1024 * 1024 or len(attachments) > 512
            or sum(item[0] for item in attachments) > 1024 * 1024):
        raise ValueError('semantic_release_capacity')
    source = ledger._semantic_source_generation(project_id, root)
    event_rows = ledger.db.execute(
        "SELECT * FROM main.notify_outbox WHERE project_id=? AND kind IN ('new_messages','semantic_notice') "
        "ORDER BY event_id LIMIT 129", (project_id,))
    events = []
    event_bytes = 0
    for event in event_rows:
        if event['state'] not in ('pending', 'failed', 'in_flight', 'accepted', 'suppressed'):
            raise ValueError('semantic_hold_origin_unverified')
        if event['state'] == 'in_flight':
            raise ValueError('semantic_release_delivery_active')
        encoded = canonical(dict(event))
        event_bytes += len(encoded)
        if len(events) >= 128 or event_bytes > 8 * 1024 * 1024:
            raise ValueError('semantic_release_capacity')
        events.append(hashlib.sha256(encoded).hexdigest())
    snapshot = {'job': dict(job), 'holds': receipts, 'source_generation': source,
                'events': events, 'hold_epoch': hold_epoch(ledger.db, project_id),
                'unstored_epoch': unstored_project_epoch(ledger.db, project_id),
                'encoding': _codec(ledger.db), 'scheduler': scheduler}
    snapshot_hash = hashlib.sha256(canonical(snapshot)).hexdigest()
    return {'job': job, 'rows': rows, 'targets': targets, 'allowed': allowed, 'origins': origins,
            'source': source, 'receipts': receipts, 'snapshot_sha256': snapshot_hash,
            'scheduler': scheduler, 'long_defers': long_defers,
            'job_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
            'hold_epoch': snapshot['hold_epoch']}


def prepare_release(ledger, project_id, root):
    """Prepare a bounded snapshot token without changing intake or notification state."""
    if (type(project_id) is not int or not 0 < project_id < 2**63
            or type(root) is not int or not 0 < root < 2**63 or ledger.db.in_transaction):
        raise ValueError('semantic_release_args')
    old_limit = ledger.db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    ledger.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(old_limit, 1024 * 1024))
    deadline = time.monotonic() + 1.0
    ledger.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        with ledger.db:
            ledger.db.execute('BEGIN')
            _release_schema(ledger.db)
            if has_unstored_root(ledger.db, project_id, root):
                raise ValueError('semantic_release_unstored')
            row = ledger.db.execute("SELECT job_id FROM main.fetch_jobs WHERE kind='semantic' "
                                    'AND project_id=? AND message_id=?', (project_id, root)).fetchone()
            if row is None:
                raise ValueError('semantic_release_job_missing')
            plan = _release_plan(ledger, project_id, root, row['job_id'])
            return {'project_id': project_id, 'root_id': root, 'job_id': row['job_id'],
                    'job_sha256': plan['job_sha256'], 'hold_epoch': plan['hold_epoch'],
                    'snapshot_sha256': plan['snapshot_sha256'], 'holds': len(plan['rows'])}
    finally:
        ledger.db.set_progress_handler(None, 0)
        ledger.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, old_limit)


def _previous_release(db, project_id, root, snapshot, job_sha256, hold_epoch):
    row = db.execute("SELECT artifact_id FROM main.artifacts INDEXED BY uq_semantic_intake_release_request "
                     "WHERE kind='semantic_intake_release_receipt_v1' AND project_id=? AND model=?",
                     (project_id, snapshot)).fetchone()
    if row is None:
        return None
    with db.blobopen('artifacts', 'content', row[0], readonly=True) as blob:
        if len(blob) > 65536:
            raise ValueError('semantic_release_receipt_unverified')
        raw = blob.read().decode(_codec(db))
    with db.blobopen('artifacts', 'meta', row[0], readonly=True) as blob:
        if len(blob) > 1024:
            raise ValueError('semantic_release_receipt_unverified')
        meta = strict_json(blob.read().decode(_codec(db)))
    data = strict_json(raw)
    keys = {'contract', 'project_id', 'root_id', 'old_job_id', 'new_job_id', 'job_sha256',
            'job_record_sha256', 'hold_epoch', 'snapshot_sha256', 'source_generation',
            'holds', 'previous_long_defer'}
    if (not isinstance(data, dict) or set(data) != keys or type(data.get('contract')) is not int
            or data['contract'] != 1 or data.get('snapshot_sha256') != snapshot
            or data.get('root_id') != root or data.get('project_id') != project_id
            or any(type(data.get(key)) is not int or not 0 < data[key] < 2**63
                   for key in ('project_id', 'root_id', 'old_job_id', 'new_job_id', 'hold_epoch'))
            or data['old_job_id'] == data['new_job_id']
            or any(not valid_hash(data.get(key)) for key in
                   ('job_sha256', 'job_record_sha256', 'snapshot_sha256', 'source_generation'))
            or data['job_sha256'] != job_sha256 or data['hold_epoch'] != hold_epoch
            or not isinstance(data.get('holds'), list) or not 1 <= len(data['holds']) <= 128
            or any(not isinstance(item, dict) or set(item) != {'hold_id', 'intent_sha256'}
                   or type(item['hold_id']) is not int or not 0 < item['hold_id'] < 2**63
                   or not valid_hash(item['intent_sha256']) for item in data['holds'])
            or len({item['hold_id'] for item in data['holds']}) != len(data['holds'])
            or meta != {'receipt_sha256': hashlib.sha256(raw.encode()).hexdigest()}):
        raise ValueError('semantic_release_receipt_unverified')
    reservation = data['previous_long_defer']
    if reservation is not None and (not isinstance(reservation, dict) or set(reservation) != {'g', 'n'}
            or not isinstance(reservation['g'], str) or not reservation['g']
            or type(reservation['n']) is not int or not 0 <= reservation['n'] < 2**63 - 1):
        raise ValueError('semantic_release_receipt_unverified')
    return {'released': len(data['holds']), 'job_id': data['new_job_id'], 'previous_job_id': data['old_job_id']}


def release_root(ledger, project_id, root, *, expected_job_sha256,
                 expected_hold_epoch, expected_snapshot_sha256, cfg, run_lock_fd):
    """Replay verified held intent with a new job identity and retained evidence."""
    from mcs_queries import paused
    from semantic_policy import runtime_config
    from semantic_drain import _job_lock
    import fcntl
    db_path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    lock_path = os.path.join(os.path.dirname(db_path), 'run.lock')
    if type(run_lock_fd) is not int:
        raise ValueError('semantic_release_writer_lock')
    try:
        opened, current = os.fstat(run_lock_fd), os.stat(lock_path)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('semantic_release_writer_lock')
        fcntl.flock(run_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise ValueError('semantic_release_writer_lock') from None
    if (type(project_id) is not int or not 0 < project_id < 2**63
            or type(root) is not int or not 0 < root < 2**63
            or not valid_hash(expected_job_sha256) or not valid_hash(expected_snapshot_sha256)
            or type(expected_hold_epoch) is not int
            or not 0 < expected_hold_epoch < 2**63):
        raise ValueError('semantic_release_args')
    scfg, errors = runtime_config(cfg)
    if (errors or scfg['mode'] == 'off' or scfg['project_ids'] is not None
            and project_id not in scfg['project_ids']):
        raise ValueError('semantic_release_disabled')
    if ledger.db.in_transaction:
        raise ValueError('semantic_release_transaction_active')
    _release_schema(ledger.db)
    previous = _previous_release(ledger.db, project_id, root, expected_snapshot_sha256,
                                 expected_job_sha256, expected_hold_epoch)
    if previous is not None:
        return previous
    row = ledger.db.execute("SELECT job_id FROM main.fetch_jobs WHERE kind='semantic' "
                            "AND project_id=? AND message_id=?", (project_id, root)).fetchone()
    if row is None:
        raise ValueError('semantic_release_job_missing')
    with _job_lock(ledger, row['job_id']) as held:
        if not held:
            raise ValueError('semantic_release_worker_active')
        old_limit = ledger.db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
        ledger.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(old_limit, 1024 * 1024))
        deadline = time.monotonic() + 1.0
        ledger.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            with ledger.db:
                ledger.db.execute('BEGIN IMMEDIATE')
                _release_schema(ledger.db)
                if paused(ledger.db, project_id):
                    raise ValueError('semantic_release_paused')
                if has_unstored_root(ledger.db, project_id, root):
                    raise ValueError('semantic_release_unstored')
                if hold_epoch(ledger.db, project_id) != expected_hold_epoch:
                    raise ValueError('semantic_release_stale')
                plan = _release_plan(ledger, project_id, root, row['job_id'], expected_job_sha256)
                if plan['snapshot_sha256'] != expected_snapshot_sha256:
                    raise ValueError('semantic_release_stale')
                job, rows, targets = plan['job'], plan['rows'], plan['targets']
                allowed, origins, source = plan['allowed'], plan['origins'], plan['source']
                receipts = plan['receipts']
                # Keep the full old row and bytes. A new ID fences workers whose
                # old CAS predates intake guards, even when the input is unchanged.
                ledger.db.execute('UPDATE main.fetch_jobs SET kind=?,message_id=job_id WHERE job_id=?',
                                  (ARCHIVE_KIND, job['job_id']))
                columns = [r[1] for r in ledger.db.execute('PRAGMA main.table_info(fetch_jobs)')
                           if r[1] != 'job_id']
                copied = {key: job[key] for key in columns}
                copied['kind'], copied['message_id'] = 'semantic', root
                new = ledger.db.execute(
                    'INSERT INTO main.fetch_jobs(' + ','.join('"' + key.replace('"', '""') + '"' for key in columns) + ') VALUES('
                    + ','.join('?' for _ in columns) + ')', tuple(copied[key] for key in columns))
                ledger.db.executemany('UPDATE main.artifacts SET kind=? WHERE artifact_id=? AND kind=?',
                                      [(RELEASED_KIND, item['artifact_id'], HOLD_KIND) for item in rows])
                ledger.semantic_seed_tx(project_id, sorted(targets), {'source': 'intake_recovery'},
                                        _source_generation=source)
                for origin in origins:
                    ledger.semantic_seed_tx(project_id, sorted(targets), origin, _source_generation=source)
                current = ledger.db.execute('SELECT payload FROM main.fetch_jobs WHERE job_id=?',
                                            (new.lastrowid,)).fetchone()
                replay = strict_json(current['payload'])
                replay['generation'] = uuid.uuid4().hex
                previous_payload = strict_json(job['payload'])
                same_input = (set(previous_payload.get('targets', [root])) == targets
                              and previous_payload.get('source_generation') == source)
                replay['intake_notification_targets'] = {
                    str(mid): ledger._semantic_message_fingerprint(project_id, mid) for mid in sorted(allowed)}
                if allowed:
                    replay.pop('notification_free', None)
                else:
                    replay['notification_free'] = True
                ledger.db.execute('UPDATE main.fetch_jobs SET payload=? WHERE job_id=?',
                                  (json.dumps(replay, ensure_ascii=False, sort_keys=True), new.lastrowid))
                if same_input:
                    ledger.db.execute('UPDATE main.fetch_jobs SET next_try=MAX(next_try,?) WHERE job_id=?',
                                      (job['next_try'], new.lastrowid))
                from semantic_drain import _sched_write
                from semantic_runtime import payload_generation
                reservation = plan['long_defers'].get(str(job['job_id']))
                if reservation is not None:
                    entries = dict(plan['long_defers'])
                    entries.pop(str(job['job_id']))
                    if (reservation['g'] == payload_generation(previous_payload)
                            and same_input):
                        entries[str(new.lastrowid)] = {'g': replay['generation'], 'n': reservation['n']}
                    scheduler = dict(plan['scheduler'])
                    scheduler['long_defers'] = entries
                    _sched_write(ledger, scheduler, commit=False)
                receipt = {'contract': 1, 'project_id': project_id, 'root_id': root,
                           'old_job_id': job['job_id'], 'new_job_id': new.lastrowid,
                           'job_sha256': expected_job_sha256, 'hold_epoch': expected_hold_epoch,
                           'job_record_sha256': hashlib.sha256(canonical({key: job[key] for key in job.keys()
                                                                         if key != 'payload_type'})).hexdigest(),
                           'previous_long_defer': reservation,
                           'snapshot_sha256': expected_snapshot_sha256,
                           'source_generation': source, 'holds': receipts}
                data = canonical(receipt).decode('utf-8')
                ledger.db.execute('INSERT INTO main.artifacts(kind,project_id,model,content,meta,created_at) '
                                  'VALUES(?,?,?,?,?,?)', (RELEASE_RECEIPT_KIND, project_id, expected_snapshot_sha256, data,
                                  json.dumps({'receipt_sha256': hashlib.sha256(data.encode()).hexdigest()}), time.time()))
                return {'released': len(rows), 'job_id': new.lastrowid, 'previous_job_id': job['job_id']}
        finally:
            ledger.db.set_progress_handler(None, 0)
            ledger.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, old_limit)


def notification_allowed(ledger, payload, project_id, message_id):
    """Retain recovery's notification rights only for the verified target revision."""
    if 'intake_notification_targets' not in payload:
        return True
    targets = payload['intake_notification_targets']
    fingerprint = targets.get(str(message_id)) if isinstance(targets, dict) else None
    return (valid_hash(fingerprint)
            and ledger._semantic_message_fingerprint(project_id, message_id) == fingerprint)
