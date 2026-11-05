"""Read referral chronology only from current, completely analysed OCR documents."""
from __future__ import annotations

import time

import attachment_extraction
import attachment_ocr
import referral_document
from measurement_reader import patient_scope_valid
from mcs_util import load_config, payload_hash, positive
from snapshot_access import table_exists

MAX_ATTACHMENTS = 50
MAX_SOURCE_BYTES = 8 * 1024 * 1024
READ_SECONDS = 1.0


def _bound(item, sources):
    source = sources.get(item.get('page'))
    if source is None or item.get('field') not in referral_document.FIELD_LABELS:
        return None
    body, span = source['body_text'], item.get('evidence_span')
    if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
            or not 0 <= span[0] < span[1] <= len(body)
            or item.get('evidence') != body[span[0]:span[1]]
            or not isinstance(item.get('text'), str) or item['text'] not in item['evidence']):
        return None
    record = {**item, **{key: source[key] for key in ('source_key', 'project_id', 'message_id',
        'parent_message_id', 'attachment_id', 'page', 'source_artifact_id', 'extraction_artifact_id', 'content_hash')},
        'source': 'attachment_ocr', 'source_binding': source['_source_binding'],
        'posted_at': None, 'posted_at_ts': None, 'sender': None, 'profession': None,
        'quote': item['evidence'], 'subject': 'unspecified', 'details': [],
        'field_label': referral_document.FIELD_LABELS[item['field']]}
    record['id'] = payload_hash(['referral-document/v1', record['project_id'], record['source_key'],
                                 record['source_binding'], item['field'], span])
    return record


def _read(db, project_id, cfg, limit):
    coverage = {'state': 'unknown', 'attachments_examined': 0, 'documents': 0,
                'pending': 0, 'limited': False, 'complete': False}
    result = {'view': 'referral_timeline', 'project_id': project_id,
              'documents': [], 'events': [], 'coverage': coverage}
    if attachment_ocr.config_error(cfg):
        return result
    if not cfg.get('attachment_ocr', {}).get('enabled', False):
        coverage['state'] = 'disabled'
        return result
    if not patient_scope_valid(db, project_id) or not table_exists(db, 'attachments'):
        return result
    deadline, used = time.monotonic() + READ_SECONDS, 0
    rows = db.execute("SELECT a.attachment_id FROM attachments a JOIN messages m USING(message_id) "
        "WHERE m.project_id=? AND m.body_state IS NOT 'deleted' AND a.state IN ('downloaded','pruned') "
        "ORDER BY a.attachment_id DESC LIMIT ?", (project_id, limit + 1)).fetchall()
    coverage.update(state='observed', limited=len(rows) > limit)
    for row in rows[:limit]:
        if time.monotonic() >= deadline:
            coverage['limited'] = True
            break
        coverage['attachments_examined'] += 1
        document = attachment_ocr.current(db, row['attachment_id'], project_id=project_id)
        if document is None:
            coverage['pending'] += 1
            continue
        used += sum(len(page['text'].encode('utf-8')) for page in document['pages'])
        if used > MAX_SOURCE_BYTES:
            coverage['limited'] = True
            break
        projected = referral_document.project(document['pages'])
        if not projected['identified']:
            continue
        sources = {}
        for page in document['pages']:
            if not page['text'].strip():
                continue
            if time.monotonic() >= deadline:
                coverage['limited'] = True
                break
            key = f"attachment_ocr:{document['artifact_id']}:{page['page']}"
            source = attachment_extraction.source(db, project_id, key, cfg=cfg)
            if source is not None:
                sources[page['page']] = source
        if len(sources) != sum(bool(page['text'].strip()) for page in document['pages']):
            coverage['pending'] += 1
            continue
        fields = [_bound(item, sources) for item in projected['fields']]
        events = [_bound(item, sources) for item in projected['events']]
        if any(item is None for item in (*fields, *events)):
            coverage['pending'] += 1
            continue
        result['documents'].append({'attachment_id': row['attachment_id'],
            'parent_message_id': document['message_id'], 'source_artifact_id': document['artifact_id'],
            'fields': fields, 'limited': bool(projected.get('limited')),
            'unprocessed': projected.get('unprocessed', [])})
        result['events'].extend(events)
        coverage['documents'] += 1
        coverage['limited'] |= bool(projected.get('limited'))
    coverage['complete'] = not (coverage['pending'] or coverage['limited'])
    return result


def read(db, project_id, *, cfg=None, limit=MAX_ATTACHMENTS):
    """Borrow one read snapshot and expose source-bound document fields, never current treatment."""
    if not positive(project_id) or type(limit) is not int or not 1 <= limit <= MAX_ATTACHMENTS:
        raise ValueError('referral_scope_invalid')
    cfg = load_config() if cfg is None else cfg
    changes = db.total_changes
    db.execute('SAVEPOINT referral_timeline_read')
    try:
        result = _read(db, project_id, cfg, limit)
        if db.total_changes != changes:
            raise RuntimeError('referral_read_changed')
    except BaseException:
        db.execute('ROLLBACK TO referral_timeline_read')
        db.execute('RELEASE referral_timeline_read')
        raise
    db.execute('RELEASE referral_timeline_read')
    return result
