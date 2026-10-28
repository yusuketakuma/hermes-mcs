"""Quarantine certain malformed semantic jobs while preserving their evidence."""
from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import time

from inference_version import CURRENT_VERSION, LEGACY_VERSION, VERSION_KEY, read_version
from mcs_util import canonical, strict_json

CURSOR_KIND = 'semantic_invalid_scan_v1'
EVIDENCE_KIND = 'semantic_invalid_evidence_v1'
PAGE_SIZE = 16
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_REASON_BYTES = 256
MAX_EVIDENCE_PER_JOB = 128


class _Deadline(Exception):
    pass


def _safe_schema(db):
    if db.execute("SELECT 1 FROM temp.sqlite_schema WHERE type IN ('table','view') "
                  "AND lower(name) IN ('fetch_jobs','artifacts') LIMIT 1").fetchone():
        return False
    fields = list(db.execute('PRAGMA main.table_xinfo(fetch_jobs)'))
    primary = [row for row in fields if row[5]]
    names = {row[1].lower() for row in fields}
    table = next((row for row in db.execute('PRAGMA main.table_list(fetch_jobs)')
                  if row[0] == 'main' and row[1] == 'fetch_jobs' and row[2] == 'table'), None)
    indexes = list(db.execute('PRAGMA main.index_list(fetch_jobs)'))
    if (table is None or table[4] != 0 or len(primary) != 1
            or primary[0][1].lower() != 'job_id' or primary[0][2].upper() != 'INTEGER'
            or any(row[3] == 'pk' for row in indexes)
            or names.intersection({'rowid', '_rowid_', 'oid'})
            or db.execute('PRAGMA encoding').fetchone()[0].upper() != 'UTF-8'):
        return False
    unique_scope = False
    for index in indexes:
        if not index[2] or index[4]:
            continue
        name = index[1].replace('"', '""')
        keys = [row for row in db.execute(f'PRAGMA main.index_xinfo("{name}")') if row[5]]
        if ([row[2] for row in keys] == ['kind', 'project_id', 'message_id']
                and all(row[4] == 'BINARY' for row in keys)):
            unique_scope = True
            break
    if not unique_scope:
        return False
    for schema in ('main', 'temp'):
        if db.execute(f"SELECT 1 FROM {schema}.sqlite_schema WHERE type='trigger' "
                      "AND tbl_name='fetch_jobs' COLLATE NOCASE LIMIT 1").fetchone():
            return False
    return True


def _blob(db, column, rowid, limit):
    with db.blobopen('fetch_jobs', column, rowid, readonly=True) as blob:
        if len(blob) > limit:
            return None
        try:
            return blob.read().decode('utf-8')
        except UnicodeError:
            return None


def _certain_invalid(raw):
    """Only classify recognized object versions with an invalid explicit target list."""
    class Object(dict):
        def __init__(self, pairs):
            super().__init__(pairs)
            self.ambiguous_version = sum(key == VERSION_KEY for key, _ in pairs) > 1
            self.ambiguous_targets = sum(key == 'targets' for key, _ in pairs) > 1

    try:
        payload = json.loads(raw, object_pairs_hook=Object)
        if (not isinstance(payload, dict) or payload.ambiguous_version or payload.ambiguous_targets
                or read_version(payload) not in (CURRENT_VERSION, LEGACY_VERSION)):
            return False
        canonical(payload)
    except (TypeError, ValueError, RecursionError):
        return False
    return 'targets' in payload and (payload['targets'] is None or payload['targets'] == [])


def _cursor(db):
    rows = db.execute(
        "SELECT job_id,state,attempts,typeof(payload) AS payload_type FROM main.fetch_jobs "
        "WHERE kind=? AND project_id=0 AND message_id=0 LIMIT 2", (CURSOR_KIND,)).fetchall()
    if not rows:
        return None, 0, 0
    if (len(rows) != 1 or rows[0]['payload_type'] != 'text'
            or rows[0]['state'] != 'done' or rows[0]['attempts'] != 0):
        raise ValueError('semantic_quarantine_cursor_unverified')
    raw = _blob(db, 'payload', rows[0]['job_id'], 256)
    try:
        value = strict_json(raw)
    except (TypeError, ValueError, RecursionError):
        raise ValueError('semantic_quarantine_cursor_unverified') from None
    if not isinstance(value, dict) or set(value) != {'cursor', 'upper'}:
        raise ValueError('semantic_quarantine_cursor_unverified')
    cursor, upper = value['cursor'], value['upper']
    if (type(cursor) is not int or type(upper) is not int
            or not 0 <= cursor <= upper < 2**63):
        raise ValueError('semantic_quarantine_cursor_unverified')
    return rows[0]['job_id'], cursor, upper


def _store_cursor(db, cursor_job, value, upper):
    raw = json.dumps({'cursor': value, 'upper': upper}, separators=(',', ':'))
    before = db.total_changes
    if cursor_job is None:
        now = time.time()
        db.execute("INSERT INTO main.fetch_jobs(kind,project_id,message_id,payload,state,next_try,created_at,updated_at) "
                   "VALUES(?,0,0,?,'done',0,?,?)", (CURSOR_KIND, raw, now, now))
    else:
        db.execute('UPDATE main.fetch_jobs SET payload=?,updated_at=? WHERE job_id=? AND kind=?',
                   (raw, time.time(), cursor_job, CURSOR_KIND))
    if db.total_changes != before + 1:
        raise sqlite3.DatabaseError('semantic_quarantine_cursor_write_unverified')


def _quarantine_one(ledger, job_id, deferred, deadline, diagnostics):
    if str(job_id) in deferred:
        return False
    db = ledger.db
    row = db.execute(
        "SELECT job_id,project_id,message_id,attempts,typeof(payload) AS payload_type,"
        "typeof(reason_code) AS reason_type,typeof(updated_at) AS updated_type,"
        "CASE WHEN typeof(updated_at) IN ('null','integer','real') THEN updated_at END AS updated_at "
        ",typeof(next_try) AS next_type,"
        "CASE WHEN typeof(next_try) IN ('null','integer','real') THEN next_try END AS next_try "
        "FROM main.fetch_jobs "
        "WHERE job_id=? AND kind='semantic' AND state='pending'", (job_id,)).fetchone()
    if (row is None or row['payload_type'] != 'text'
            or type(row['project_id']) is not int or row['project_id'] <= 0
            or type(row['message_id']) is not int or row['message_id'] <= 0
            or type(row['attempts']) is not int or row['attempts'] < 0):
        return False
    raw = _blob(db, 'payload', job_id, MAX_PAYLOAD_BYTES)
    if raw is None or not _certain_invalid(raw):
        return False
    old_updated = row['updated_at']
    if (row['updated_type'] not in ('null', 'integer', 'real')
            or old_updated is not None and (type(old_updated) not in (int, float)
                                            or not math.isfinite(old_updated))):
        return False
    old_next = row['next_try']
    if (row['next_type'] not in ('null', 'integer', 'real')
            or old_next is not None and (type(old_next) not in (int, float)
                                         or not math.isfinite(old_next))):
        return False
    if row['reason_type'] == 'null':
        old_reason = None
    elif row['reason_type'] == 'text':
        old_reason = _blob(db, 'reason_code', job_id, MAX_REASON_BYTES)
        if old_reason is None:
            return False
    else:
        return False
    model = str(job_id)
    evidence = db.execute(
        'SELECT artifact_id FROM main.artifacts INDEXED BY idx_semantic_invalid_evidence '
        'WHERE kind=? AND project_id=? AND model=? LIMIT ?',
        (EVIDENCE_KIND, row['project_id'], model, MAX_EVIDENCE_PER_JOB + 1)).fetchall()
    if len(evidence) >= MAX_EVIDENCE_PER_JOB:
        diagnostics['evidence_full_observed'] = diagnostics.get('evidence_full_observed', 0) + 1
        return False
    if time.monotonic() >= deadline:
        raise _Deadline
    content = canonical({'job_id': job_id, 'root_id': row['message_id'],
                         'payload_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                         'attempts': row['attempts'], 'old_next_try': old_next,
                         'old_reason': old_reason, 'old_updated_at': old_updated}).decode('utf-8')
    changes = db.total_changes
    ledger.artifact_add_tx(EVIDENCE_KIND, content, project_id=row['project_id'], model=model)
    if db.total_changes != changes + 1:
        raise sqlite3.DatabaseError('semantic_quarantine_evidence_write_unverified')
    changes = db.total_changes
    cur = db.execute("UPDATE main.fetch_jobs SET state='failed',reason_code='invalid_payload',updated_at=? "
                     "WHERE job_id=? AND kind='semantic' AND state='pending'", (time.time(), job_id))
    if (cur.rowcount != 1 or db.total_changes != changes + 1 or db.execute(
            "SELECT 1 FROM main.fetch_jobs WHERE job_id=? AND state='failed' AND attempts=? "
            "AND reason_code='invalid_payload' AND payload=? COLLATE BINARY",
            (job_id, row['attempts'], raw)).fetchone() is None):
        raise sqlite3.DatabaseError('semantic_quarantine_job_write_unverified')
    from ledger import bump_semantic_job_state_epoch_tx
    changes = db.total_changes
    bump_semantic_job_state_epoch_tx(db)
    if db.total_changes > changes + 1:
        raise sqlite3.DatabaseError('semantic_quarantine_epoch_write_unverified')
    return True


def scan(ledger, deadline, scheduler_reader, job_lock, *, diagnostics=None):
    """Inspect one fixed page and persist progress with any quarantine changes."""
    db = ledger.db
    diagnostics = {} if diagnostics is None else diagnostics
    if db.in_transaction:
        raise ValueError('semantic_quarantine_transaction_active')
    if time.monotonic() >= deadline:
        return 0
    prior_timeout = db.execute('PRAGMA busy_timeout').fetchone()[0]
    timeout_ms = max(0, min(50, int((deadline - time.monotonic()) * 1000)))
    db.execute(f'PRAGMA busy_timeout={timeout_ms}')
    try:
        if not _safe_schema(db):
            diagnostics['schema_unverified'] = True
            return 0
        if time.monotonic() >= deadline:
            return 0
        db.execute('BEGIN IMMEDIATE')
        with db:
            if not _safe_schema(db):
                diagnostics['schema_unverified'] = True
                return 0
            if time.monotonic() >= deadline:
                return 0
            sched, deferred = scheduler_reader(ledger)
            if len(deferred) > 500:
                raise ValueError('semantic_quarantine_deferred_unverified')
            epoch = sched.get('job_state_epoch', 0)
            if type(epoch) is not int or not 0 <= epoch < 2**63 - PAGE_SIZE - 1:
                raise ValueError('semantic_quarantine_epoch_unverified')
            cursor_job, cursor, upper = _cursor(db)
            if not upper:
                row = db.execute(
                    "SELECT job_id FROM main.fetch_jobs INDEXED BY idx_semantic_invalid_page "
                    "WHERE kind='semantic' AND state='pending' ORDER BY job_id DESC LIMIT 1").fetchone()
                if row is None or row['job_id'] <= 0:
                    return 0
                upper = row['job_id']
            rows = db.execute(
                "SELECT job_id FROM main.fetch_jobs INDEXED BY idx_semantic_invalid_page "
                "WHERE kind='semantic' AND state='pending' AND job_id>? AND job_id<=? "
                "ORDER BY job_id LIMIT ?", (cursor, upper, PAGE_SIZE)).fetchall()
            if not rows:
                _store_cursor(db, cursor_job, 0, 0)
                return 0
            moved = 0
            last = cursor
            for row in rows:
                if time.monotonic() >= deadline:
                    break
                try:
                    with job_lock(ledger, row['job_id']) as held:
                        if held:
                            moved += int(_quarantine_one(ledger, row['job_id'], deferred, deadline, diagnostics))
                except _Deadline:
                    break
                last = row['job_id']
            if last != cursor:
                _store_cursor(db, cursor_job, last, upper)
            return moved
    finally:
        db.execute(f'PRAGMA busy_timeout={int(prior_timeout)}')
