"""Preserve bounded per-message intake intent while a job cannot be read."""
from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import time

from inference_version import CURRENT_VERSION, LEGACY_VERSION, read_version
from mcs_util import canonical, strict_json, valid_hash

HOLD_KIND = 'semantic_intake_hold_v1'
UNSTORED_KIND = 'semantic_intake_unstored_v1'
UNSTORED_EPOCH_KIND = 'semantic_intake_unstored_epoch_v1'
_MAX_ORIGIN_BYTES = 4096



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


def record_unstored(ledger, project_id):
    """Record that a rolled-back intake could not be preserved; never clear it automatically."""
    if type(project_id) is not int or not 0 < project_id < 2**63:
        raise ValueError('semantic_intake_project_unverified')
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
            "UPDATE main.fetch_jobs SET attempts=attempts+1,updated_at=? "
            "WHERE kind=? AND project_id=0 AND message_id=0 AND state='done' "
            "AND payload='{}' COLLATE BINARY AND reason_code IS NULL "
            "AND typeof(attempts)='integer' AND attempts>=0 AND attempts<?",
            (now, UNSTORED_EPOCH_KIND, 2**63 - 1))
        if updated.rowcount != 1:
            raise sqlite3.DatabaseError('semantic_intake_unstored_epoch_unverified')
        ledger.db.execute(
            "INSERT OR IGNORE INTO main.fetch_jobs(kind,project_id,message_id,payload,state,"
            "attempts,next_try,created_at,updated_at) VALUES(?,?,0,'{}','pending',0,0,?,?)",
            (UNSTORED_KIND, project_id, time.time(), time.time()))
        updated = ledger.db.execute(
            "UPDATE main.fetch_jobs SET attempts=attempts+1,updated_at=? "
            "WHERE kind=? AND project_id=? AND message_id=0 AND state='pending' "
            "AND payload='{}' COLLATE BINARY AND reason_code IS NULL "
            "AND typeof(attempts)='integer' AND attempts>=0 AND attempts<?",
            (now, UNSTORED_KIND, project_id, 2**63 - 1))
        if updated.rowcount != 1:
            raise sqlite3.DatabaseError('semantic_intake_diagnostic_unpersisted')
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


def report_write_failure(ledger, result, project_id, exc, ack_exclusions=None, job_id=None):
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
        record_unstored(ledger, project_id)
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
    """Observe the newest immutable hold ID without walking unrelated artifacts."""
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
    return row[0] if row else 0

def _rowid_primary_key(db, table, key):
    if table not in ('fetch_jobs', 'artifacts', 'notify_outbox'):
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
    return db.execute(
        "SELECT 1 FROM main.artifacts INDEXED BY idx_semantic_intake_hold_root "
        "WHERE kind='semantic_intake_hold_v1' AND project_id=? AND model=? LIMIT 1",
        (project_id, str(root))).fetchone() is not None


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
                            or not isinstance(value, dict) or value.get('contract') != 1
                            or value.get('root_id') != root or type(value.get('target_id')) is not int
                            or not 0 < value['target_id'] < 2**63
                            or not valid_hash(value.get('fingerprint'))
                            or not valid_hash(value.get('source_generation'))):
                        raise ValueError('semantic_hold_record_unverified')
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
