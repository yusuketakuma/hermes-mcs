"""Bind operational OCR alert acknowledgements to exact unresolved source evidence."""
from __future__ import annotations

import os
import sqlite3
import stat

import attachment_ocr
import inference_dependency_loss
from mcs_requests import payload_hash, valid_uuid
from mcs_util import strict_json, valid_hash

KIND = 'attachment_ocr_alert_ack_v1'
SCHEMA = 'attachment-ocr-alert-ack/v1'
ACTION = 'acknowledge_ocr_alert'
COMMAND = 'ops.attachment_ocr_alert_ack'
MAX_TARGETS = 20


def database_identity(db):
    """Pin the current file; supported database restores replace its inode."""
    path = next((row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main'), None)
    if not path:
        raise ValueError('ocr_ack_database_unverified')
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('ocr_ack_database_unverified')
    return payload_hash([info.st_dev, info.st_ino])


def target(db, artifact_id):
    """Observe one latest exhausted failure on the caller's borrowed snapshot."""
    if not db.in_transaction:
        raise ValueError('ocr_ack_snapshot_required')
    if type(artifact_id) is not int or artifact_id <= 0:
        raise ValueError('ocr_ack_target_invalid')
    row = db.execute("SELECT artifact_id,kind,project_id,message_id,model,created_at,"
                     "CASE WHEN typeof(content)='text' AND length(content)<=4194304 THEN content END AS content,"
                     "CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END AS meta "
                     "FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    if row is None or row['kind'] != attachment_ocr.KIND:
        raise ValueError('ocr_ack_target_invalid')
    try:
        doc, meta = strict_json(row['content']), strict_json(row['meta'])
    except (ValueError, TypeError, RecursionError):
        raise ValueError('ocr_ack_target_invalid') from None
    if not isinstance(doc, dict) or not isinstance(meta, dict):
        raise ValueError('ocr_ack_target_invalid')
    source = attachment_ocr._source(db, doc.get('attachment_id'), row['project_id'])
    if (source is None or source['state'] != 'downloaded' or source['body_state'] != 'full'
            or source['message_id'] != row['message_id']
            or meta.get('attachment_id') != source['attachment_id']
            or not attachment_ocr._matching(doc, source)
            or doc.get('state') != 'failed' or doc.get('reason') != 'output_schema_table_overlap'
            or type(doc.get('attempts')) is not int or doc['attempts'] != 3):
        raise ValueError('ocr_ack_target_invalid')
    latest = attachment_ocr._latest(db, source)
    if latest is None or latest['artifact_id'] != artifact_id:
        raise ValueError('ocr_ack_target_changed')
    occurrence = inference_dependency_loss.source_binding(db, source['project_id'], source['message_id'])
    if not valid_hash(occurrence):
        raise ValueError('ocr_ack_source_unverified')
    # Parent revision and the attachment record (including acquisition time)
    # distinguish observed ABA; hashes alone cannot prove an occurrence.
    return {'artifact_id': artifact_id, 'proof': payload_hash([
        SCHEMA, database_identity(db), dict(row), source, occurrence,
        attachment_ocr.RUNTIME_CONTRACT])}


def acknowledged(db, current):
    """Require both the bound acknowledgement and the same atomic command receipt."""
    rows = db.execute("SELECT a.content,a.meta,a.artifact_id FROM artifacts a "
                      "JOIN artifacts f ON f.artifact_id=? AND a.project_id IS f.project_id "
                      "AND a.message_id IS f.message_id WHERE a.kind=? AND CASE WHEN json_valid(a.meta) "
                      "THEN json_extract(a.meta,'$.failure_artifact_id') END=? "
                      "ORDER BY a.artifact_id DESC LIMIT 2",
                      (current['artifact_id'], KIND, current['artifact_id'])).fetchall()
    if len(rows) != 1:
        return False
    try:
        ack = strict_json(rows[0][0])
        meta = strict_json(rows[0][1])
        if (type(meta) is not dict or set(meta) != {'failure_artifact_id'}
                or type(meta['failure_artifact_id']) is not int
                or meta['failure_artifact_id'] != current['artifact_id']):
            return False
        if (type(ack) is not dict or set(ack) != {'schema', 'target', 'command_id', 'request_hash'}
                or ack['schema'] != SCHEMA or ack['target'] != current
                or not valid_uuid(ack['command_id']) or not valid_hash(ack['request_hash'])):
            return False
        row = db.execute('SELECT payload_hash,receipt_json FROM command_receipts WHERE command_id=?',
                         (ack['command_id'],)).fetchone()
        if row is None or row[0] != ack['request_hash']:
            return False
        receipt = strict_json(row[1])
        return (type(receipt) is dict and receipt.get('kind') == COMMAND
                and receipt.get('outcome') == 'applied'
                and receipt.get('command_id') == ack['command_id']
                and receipt.get('payload_hash') == ack['request_hash']
                and type(receipt.get('targets')) is list
                and 1 <= len(receipt['targets']) <= MAX_TARGETS
                and receipt['targets'].count(current) == 1
                and type(receipt.get('acknowledgement_ids')) is list
                and len(receipt['acknowledgement_ids']) == len(receipt['targets'])
                and receipt['acknowledgement_ids'][receipt['targets'].index(current)] == rows[0][2]
                and receipt.get('ocr_outcome') == 'failed'
                and receipt.get('source_coverage') == 'unrecovered')
    except (ValueError, TypeError, KeyError, RecursionError):
        return False


def current_acknowledged(db, artifact_id):
    """Refuse stale, incomplete or damaged evidence without accepting an old alert."""
    try:
        return acknowledged(db, target(db, artifact_id))
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, RecursionError):
        return False


def build_plan(db, artifact_ids):
    """Plan at most twenty exact current failures without altering the caller's transaction."""
    if (type(artifact_ids) is not list or not 1 <= len(artifact_ids) <= MAX_TARGETS
            or any(type(value) is not int or value <= 0 for value in artifact_ids)
            or len(set(artifact_ids)) != len(artifact_ids)):
        raise ValueError('ocr_ack_targets_invalid')
    targets = [target(db, value) for value in sorted(artifact_ids)]
    if any(acknowledged(db, item) for item in targets):
        raise ValueError('ocr_ack_already_acknowledged')
    return {'version': 1, 'action': ACTION, 'database_identity': database_identity(db), 'targets': targets}


def failure_counts(db, *, deadline=None):
    """Observe one bounded failure cohort and verify its receipts on a borrowed snapshot."""
    import time
    if not db.in_transaction:
        raise ValueError('ocr_ack_snapshot_required')
    counts = {'pending_failed': 0, 'queue_status': 'partial',
              'failure_reason_counts': {}, 'exhausted_failed': 0,
              'acknowledged_exhausted': 0, 'unacknowledged_pending_failed': 0}
    if deadline is not None and time.monotonic() >= deadline:
        return counts
    observed = db.execute("SELECT attachment_id FROM attachments WHERE state='downloaded' "
                          'ORDER BY attachment_id LIMIT 257').fetchall()
    checked = 0
    for item in observed[:256]:
        if deadline is not None and time.monotonic() >= deadline:
            break
        source = attachment_ocr._source(db, item[0])
        prior = attachment_ocr._latest(db, source) if source is not None else None
        if source is not None and attachment_ocr._matching(prior, source) and prior['state'] in ('failed', 'unavailable'):
            counts['pending_failed'] += 1
            reason = prior.get('reason')
            if type(reason) is str and reason in attachment_ocr.FAILURE_REASONS:
                reasons = counts['failure_reason_counts']
                reasons[reason] = reasons.get(reason, 0) + 1
            if type(prior.get('attempts')) is int and prior['attempts'] == 3:
                counts['exhausted_failed'] += 1
                if reason == 'output_schema_table_overlap' and current_acknowledged(db, prior['artifact_id']):
                    counts['acknowledged_exhausted'] += 1
        checked += 1
    counts['unacknowledged_pending_failed'] = counts['pending_failed'] - counts['acknowledged_exhausted']
    if checked == len(observed):
        counts['queue_status'] = 'known'
    return counts
