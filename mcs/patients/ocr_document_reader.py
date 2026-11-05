"""Read one bounded snapshot of current OCR documents and source-bound clinical readiness."""
import time

import attachment_extraction
import attachment_ocr
from measurement_reader import patient_scope_valid
from mcs_util import load_config, payload_hash, positive
from snapshot_access import table_exists

MAX_ATTACHMENTS = 50
MAX_SOURCE_BYTES = 8 * 1024 * 1024
READ_SECONDS = 1.0


def bind(item, sources, *, schema, field_label):
    """Bind a literal page fragment to its current OCR and clinical source identities."""
    source = sources.get(item.get('page'))
    if source is None:
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
        'quote': item['evidence'], 'subject': 'unspecified', 'details': [], 'field_label': field_label}
    record['id'] = payload_hash([schema, record['project_id'], record['source_key'],
                                 record['source_binding'], item['field'], span])
    return record


def _read(db, project_id, cfg, limit):
    coverage = {'state': 'unknown', 'attachments_examined': 0, 'documents': 0, 'excluded': 0,
                'pending': 0, 'limited': False, 'complete': False}
    result = {'project_id': project_id, 'documents': [], 'coverage': coverage}
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
            state = attachment_ocr.view(db, row['attachment_id'], project_id=project_id)['state']
            coverage['excluded' if state in attachment_ocr.EXCLUDED_STATES else 'pending'] += 1
            continue
        used += sum(len(page['text'].encode('utf-8')) for page in document['pages'])
        if used > MAX_SOURCE_BYTES:
            coverage['limited'] = True
            break
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
        ready = len(sources) == sum(bool(page['text'].strip()) for page in document['pages'])
        if not ready:
            coverage['pending'] += 1
        result['documents'].append({'document': document, 'sources': sources, 'ready': ready})
        coverage['documents'] += 1
    coverage['complete'] = not (coverage['pending'] or coverage['limited'])
    return result


def read(db, project_id, *, cfg=None, limit=MAX_ATTACHMENTS):
    """Borrow one read snapshot for literal document projections, never current treatment."""
    if not positive(project_id) or type(limit) is not int or not 1 <= limit <= MAX_ATTACHMENTS:
        raise ValueError('ocr_document_scope_invalid')
    cfg = load_config() if cfg is None else cfg
    changes = db.total_changes
    db.execute('SAVEPOINT ocr_document_read')
    try:
        result = _read(db, project_id, cfg, limit)
        if db.total_changes != changes:
            raise RuntimeError('ocr_document_read_changed')
    except BaseException:
        db.execute('ROLLBACK TO ocr_document_read')
        db.execute('RELEASE ocr_document_read')
        raise
    db.execute('RELEASE ocr_document_read')
    return result
