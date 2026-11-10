"""Expose current source-bound PCA candidates without promoting them to confirmed measurements."""
import ocr_document_reader
import pump_document


def project(snapshot):
    """Project a shared OCR snapshot as unverified PCA records with exact source quotes."""
    coverage = {**snapshot['coverage'], 'documents': 0}
    result = {'view': 'pump_timeline', 'project_id': snapshot['project_id'],
              'events': [], 'coverage': coverage}
    result['excluded_documents'] = snapshot.get('excluded_documents', [])
    for entry in snapshot['documents']:
        if not entry['ready']:
            continue
        projected = pump_document.project(entry['document']['pages'])
        if not projected['identified']:
            continue
        events = [ocr_document_reader.bind(item, entry['sources'], schema=pump_document.SCHEMA,
                                          field_label=item['field_label']) for item in projected['events']]
        if any(item is None for item in events):
            coverage['pending'] += 1
            continue
        result['events'].extend(events)
        coverage['documents'] += 1
        coverage['limited'] |= bool(projected.get('limited'))
    coverage['complete'] = coverage['state'] == 'observed' and not (coverage['pending'] or coverage['limited'])
    return result


def read(db, project_id, *, cfg=None, limit=ocr_document_reader.MAX_ATTACHMENTS):
    """Read current OCR sources once and keep device quantities as literal candidates."""
    return project(ocr_document_reader.read(db, project_id, cfg=cfg, limit=limit))
