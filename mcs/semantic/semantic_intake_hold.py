"""Preserve bounded per-message intake intent while a job cannot be read."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import time

from inference_version import CURRENT_VERSION, LEGACY_VERSION, VERSION_KEY, read_version
from mcs_util import canonical, valid_hash

HOLD_KIND = 'semantic_intake_hold_v1'
_MAX_ORIGIN_BYTES = 4096


def _rowid_primary_key(db, table, key):
    if table not in ('fetch_jobs', 'artifacts'):
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
            self.ambiguous_version = sum(key == VERSION_KEY for key, _ in pairs) > 1

    raw = _payload(db, row, 1024 * 1024)
    if raw is None:
        return False
    try:
        value = json.loads(raw, object_pairs_hook=Object)
        if (not isinstance(value, dict) or value.ambiguous_version
                or read_version(value) not in (CURRENT_VERSION, LEGACY_VERSION)):
            return False
        canonical(value)
    except (TypeError, ValueError, RecursionError):
        return False
    if 'targets' not in value:
        return True
    targets = value['targets']
    if (not isinstance(targets, list) or not targets
            or any(type(mid) is not int or not 0 < mid < 2**63 for mid in targets)):
        return False
    return db.execute(
        'SELECT 1 FROM json_each(?) target WHERE NOT EXISTS '
        '(SELECT 1 FROM messages WHERE project_id=? AND message_id=target.value '
        'AND (message_id=? OR parent_id=?)) LIMIT 1',
        (json.dumps(targets), project_id, root, root)).fetchone() is None


def has_held_root(db, project_id, root):
    return db.execute(
        'SELECT 1 FROM main.artifacts WHERE kind=? AND project_id=? AND model=? LIMIT 1',
        (HOLD_KIND, project_id, str(root))).fetchone() is not None


def _arrival_targets(db, project_id, event_id):
    """Read only a bounded recorded event before deriving target eligibility."""
    row = db.execute(
        "SELECT event_id,typeof(payload) AS payload_type FROM notify_outbox "
        "WHERE event_id=? AND kind='new_messages' AND project_id=?",
        (event_id, project_id)).fetchone()
    if row is None or row['payload_type'] != 'text':
        raise ValueError('semantic_hold_origin_unverified')
    with db.blobopen('notify_outbox', 'payload', event_id, readonly=True) as blob:
        if len(blob) > 1024 * 1024:
            raise ValueError('semantic_hold_origin_too_large')
        try:
            value = json.loads(blob.read().decode(_codec(db)))
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
            'SELECT artifact_id,typeof(content) AS content_type FROM main.artifacts '
            'WHERE kind=? AND project_id=? AND model=? AND meta=? LIMIT 2',
            (HOLD_KIND, project_id, str(root), meta)).fetchall()
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
