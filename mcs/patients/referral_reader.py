"""Project referral chronology from a shared current, completely analysed OCR snapshot."""
import ocr_document_reader
import referral_document
from mcs_util import positive

MAX_ATTACHMENTS = ocr_document_reader.MAX_ATTACHMENTS


def _bound(item, sources):
    field = item.get('field')
    if field not in referral_document.FIELD_LABELS:
        return None
    return ocr_document_reader.bind(item, sources, schema=referral_document.SCHEMA,
                                    field_label=referral_document.FIELD_LABELS[field])


def project(snapshot):
    """Preserve referral fields and date-bound quotes without another database scan."""
    coverage = {**snapshot['coverage'], 'documents': 0}
    result = {'view': 'referral_timeline', 'project_id': snapshot['project_id'],
              'documents': [], 'events': [], 'coverage': coverage}
    for entry in snapshot['documents']:
        document, sources = entry['document'], entry['sources']
        projected = referral_document.project(document['pages'])
        if not projected['identified'] or not entry['ready']:
            continue
        fields = [_bound(item, sources) for item in projected['fields']]
        events = [_bound(item, sources) for item in projected['events']]
        if any(item is None for item in (*fields, *events)):
            coverage['pending'] += 1
            continue
        result['documents'].append({'attachment_id': document['attachment_id'],
            'parent_message_id': document['message_id'], 'source_artifact_id': document['artifact_id'],
            'fields': fields, 'limited': bool(projected.get('limited')),
            'unprocessed': projected.get('unprocessed', [])})
        result['events'].extend(events)
        coverage['documents'] += 1
        coverage['limited'] |= bool(projected.get('limited'))
    coverage['complete'] = coverage['state'] == 'observed' and not (coverage['pending'] or coverage['limited'])
    return result


def read(db, project_id, *, cfg=None, limit=MAX_ATTACHMENTS):
    """Read source-bound document fields, never current treatment."""
    if not positive(project_id) or type(limit) is not int or not 1 <= limit <= MAX_ATTACHMENTS:
        raise ValueError('referral_scope_invalid')
    return project(ocr_document_reader.read(db, project_id, cfg=cfg, limit=limit))
