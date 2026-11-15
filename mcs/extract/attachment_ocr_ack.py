"""Bind operational OCR alert acknowledgements to exact unresolved source evidence."""
from __future__ import annotations

import os
import sqlite3
import stat
import re
import time

import attachment_ocr
import inference_dependency_loss
from mcs_requests import payload_hash, valid_uuid
from mcs_util import strict_json, valid_hash, valid_text
from stats_math import finite_number

KIND = 'attachment_ocr_alert_ack_v1'
SCHEMA = 'attachment-ocr-alert-ack/v1'
ACTION = 'acknowledge_ocr_alert'
COMMAND = 'ops.attachment_ocr_alert_ack'
MAX_TARGETS = 20


def valid_targets(value):
    if (type(value) is not list or not 1 <= len(value) <= MAX_TARGETS
            or any(type(item) is not dict or set(item) != {'artifact_id', 'proof'}
                   or type(item.get('artifact_id')) is not int or item['artifact_id'] <= 0
                   or not valid_hash(item.get('proof')) for item in value)):
        return False
    ids = [item['artifact_id'] for item in value]
    return ids == sorted(set(ids))


def valid_plan(value):
    return (type(value) is dict and set(value) == {'version', 'action', 'database_identity', 'targets'}
            and type(value.get('version')) is int and value['version'] == 1
            and value.get('action') == ACTION and valid_hash(value.get('database_identity'))
            and valid_targets(value.get('targets')))


def valid_receipt(value):
    required = {'kind', 'command_id', 'payload_hash', 'outcome', 'actor', 'reason',
                'human_confirmed', 'processed_at', 'plan', 'targets', 'acknowledgement_ids',
                'backup', 'ocr_outcome', 'source_coverage', 'clinical_review', 'original_file_availability'}
    if (type(value) is not dict or set(value) != required or value.get('kind') != COMMAND
            or value.get('outcome') != 'applied' or not valid_uuid(value.get('command_id'))
            or not valid_hash(value.get('payload_hash')) or value.get('human_confirmed') is not True
            or not valid_text(value.get('actor'), 120) or not valid_text(value.get('reason'), 2000)
            or not finite_number(value.get('processed_at')) or value['processed_at'] < 0
            or not valid_plan(value.get('plan')) or not valid_targets(value.get('targets'))
            or value['targets'] != value['plan']['targets']
            or type(value.get('acknowledgement_ids')) is not list
            or len(value['acknowledgement_ids']) != len(value['targets'])
            or any(type(item) is not int or item <= 0 for item in value['acknowledgement_ids'])
            or len(set(value['acknowledgement_ids'])) != len(value['acknowledgement_ids'])
            or value.get('ocr_outcome') != 'failed' or value.get('source_coverage') != 'unrecovered'
            or value.get('clinical_review') != 'not_proven'
            or value.get('original_file_availability') != 'not_rechecked'
            or type(value.get('backup')) is not str
            or re.fullmatch(r'ocr_alert_acknowledgements/' + re.escape(value['command_id'])
                            + r'/before-[A-Za-z0-9_-]+\.db', value['backup']) is None):
        return False
    request = {key: value[key] for key in ('command_id', 'actor', 'reason', 'human_confirmed', 'plan')}
    try:
        return payload_hash(request) == value['payload_hash']
    except (ValueError, TypeError, RecursionError):
        return False


def database_identity(db):
    """Pin the current file; supported database restores replace its inode."""
    path = next((row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main'), None)
    if not path:
        raise ValueError('ocr_ack_database_unverified')
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('ocr_ack_database_unverified')
    return payload_hash([info.st_dev, info.st_ino])


def _failure_target(db, artifact_id):
    """Bind one latest structural failure and its exact retry stage."""
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
            or type(doc.get('attempts')) is not int or not 1 <= doc['attempts'] <= 3):
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
        attachment_ocr.RUNTIME_CONTRACT])}, doc['attempts']


def failure_target(db, artifact_id):
    """Observe exact failed-source evidence without granting an operational acknowledgement."""
    return _failure_target(db, artifact_id)[0]


def target(db, artifact_id):
    """Observe one latest exhausted failure on the caller's borrowed snapshot."""
    value, attempts = _failure_target(db, artifact_id)
    if attempts != 3:
        raise ValueError('ocr_ack_target_invalid')
    return value


def acknowledged(db, current, *, deadline=None):
    """Resolve only exact current proofs; valid older acknowledgements stay as history."""
    # shortcut: refuse more than 128 records for one failure; extend with a
    # bounded history reader if real operational review reaches this limit.
    rows = db.execute("SELECT CASE WHEN typeof(a.content)='text' AND length(a.content)<=65536 "
                      "THEN a.content END,CASE WHEN typeof(a.meta)='text' AND length(a.meta)<=4096 "
                      "THEN a.meta END,a.artifact_id FROM artifacts a "
                      "JOIN artifacts f ON f.artifact_id=? AND a.project_id IS f.project_id "
                      "AND a.message_id IS f.message_id WHERE a.kind=? AND CASE WHEN typeof(a.meta)='text' "
                      "AND length(a.meta)<=4096 THEN CASE WHEN json_valid(a.meta) "
                      "THEN json_extract(a.meta,'$.failure_artifact_id')=? "
                      "OR json_type(a.meta,'$.failure_artifact_id') IS NULL ELSE 1 END ELSE 1 END "
                      "ORDER BY a.artifact_id DESC LIMIT 129",
                      (current['artifact_id'], KIND, current['artifact_id'])).fetchall()
    if len(rows) > 128:
        raise ValueError('ocr_ack_receipt_unverified')
    found = 0
    for row in rows:
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError('ocr_ack_observation_incomplete')
        try:
            record, meta = strict_json(row[0]), strict_json(row[1])
            if (type(meta) is not dict or set(meta) != {'failure_artifact_id'}
                    or type(meta['failure_artifact_id']) is not int
                    or meta['failure_artifact_id'] != current['artifact_id']
                    or type(record) is not dict or set(record) != {'schema', 'target', 'command_id', 'request_hash'}
                    or record['schema'] != SCHEMA or not valid_targets([record['target']])
                    or record['target']['artifact_id'] != current['artifact_id']
                    or not valid_uuid(record['command_id']) or not valid_hash(record['request_hash'])):
                raise ValueError('ocr_ack_receipt_unverified')
            receipt_row = db.execute("SELECT CASE WHEN typeof(payload_hash)='text' AND length(payload_hash)=64 "
                "THEN payload_hash END,CASE WHEN typeof(receipt_json)='text' "
                "AND length(receipt_json)<=65536 THEN receipt_json END,outcome FROM command_receipts "
                'WHERE command_id=?', (record['command_id'],)).fetchone()
            if receipt_row is None or receipt_row[0] != record['request_hash'] or receipt_row[2] != 'applied':
                raise ValueError('ocr_ack_receipt_unverified')
            receipt = strict_json(receipt_row[1])
            if (not valid_receipt(receipt) or receipt['command_id'] != record['command_id']
                    or receipt['payload_hash'] != record['request_hash']
                    or record['target'] not in receipt['targets']
                    or receipt['acknowledgement_ids'][receipt['targets'].index(record['target'])] != row[2]):
                raise ValueError('ocr_ack_receipt_unverified')
            found += int(record['target'] == current)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError('ocr_ack_receipt_unverified') from None
    if found > 1:
        raise ValueError('ocr_ack_receipt_unverified')
    return found == 1


def current_acknowledged(db, artifact_id, *, deadline=None):
    """Refuse stale, incomplete or damaged evidence without accepting an old alert."""
    try:
        return acknowledged(db, target(db, artifact_id), deadline=deadline)
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


def failure_counts(db, *, cfg=None, deadline=None):
    """Observe one bounded failure cohort and verify its receipts on a borrowed snapshot."""
    if not db.in_transaction:
        raise ValueError('ocr_ack_snapshot_required')
    counts = {'pending_failed': 0, 'queue_status': 'partial',
              'failure_reason_counts': {}, 'exhausted_failed': 0,
              'acknowledged_exhausted': 0, 'unacknowledged_pending_failed': 0,
              'failure_cohort': payload_hash([])}
    policy = attachment_ocr.rejection_policy(cfg)
    if policy:
        counts.update(policy_binding=policy, policy_rejected_failed=0,
                      policy_rejected_exhausted=0, policy_rejected_unacknowledged=0)
    verified = True
    if deadline is not None and time.monotonic() >= deadline:
        return counts
    observed = db.execute("SELECT attachment_id FROM attachments WHERE state='downloaded' "
                          'ORDER BY attachment_id LIMIT 257').fetchall()
    checked, cohort = 0, []
    for item in observed[:256]:
        if deadline is not None and time.monotonic() >= deadline:
            break
        source = attachment_ocr._source(db, item[0])
        prior = attachment_ocr._latest(db, source) if source is not None else None
        if prior is not None and not attachment_ocr._matching(prior, source):
            verified = False
        if source is not None and attachment_ocr._matching(prior, source) and prior['state'] in ('failed', 'unavailable'):
            counts['pending_failed'] += 1
            reason = prior.get('reason')
            proof, accepted = None, False
            if type(reason) is str and reason in attachment_ocr.FAILURE_REASONS:
                reasons = counts['failure_reason_counts']
                reasons[reason] = reasons.get(reason, 0) + 1
            if type(prior.get('attempts')) is int and prior['attempts'] == 3:
                counts['exhausted_failed'] += 1
                if reason == 'output_schema_table_overlap':
                    try:
                        current = target(db, prior['artifact_id'])
                        proof = current['proof']
                        if acknowledged(db, current, deadline=deadline):
                            counts['acknowledged_exhausted'] += 1
                            accepted = True
                    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, RecursionError):
                        if policy:
                            verified = False
            item = [prior['artifact_id'], payload_hash(prior), proof, accepted]
            if policy:
                rejection = attachment_ocr.rejected_failure(db, source['attachment_id'], source['project_id'], cfg=cfg)
                if rejection is not None:
                    counts['policy_rejected_failed'] += 1
                    counts['policy_rejected_exhausted'] += int(prior.get('attempts') == 3)
                    counts['policy_rejected_unacknowledged'] += int(not accepted)
                elif reason == 'output_schema_table_overlap':
                    verified = False
                item.append(rejection)
            cohort.append(item)
        checked += 1
    counts['unacknowledged_pending_failed'] = counts['pending_failed'] - counts['acknowledged_exhausted']
    counts['failure_cohort'] = payload_hash(cohort)
    if verified and checked == len(observed) and (deadline is None or time.monotonic() < deadline):
        counts['queue_status'] = 'known'
    return counts
