"""Validate external original-source keys and bind their retained source occurrences."""
import re

from mcs_util import loads_dict, payload_hash, positive


def parse(value):
    if not isinstance(value, str):
        raise ValueError('original_source_key_invalid')
    match = re.fullmatch(r'(karte|attachment_ocr):([1-9][0-9]{0,18})(?::([1-9][0-9]{0,18}))?', value)
    if match is None:
        raise ValueError('original_source_key_invalid')
    kind, artifact, page = match.groups()
    artifact, page = int(artifact), int(page) if page is not None else None
    if artifact >= 2**63 or (kind == 'karte') != (page is None) or page is not None and page > 30:
        raise ValueError('original_source_key_invalid')
    return kind, artifact, page


def valid(value):
    try:
        parse(value)
        return True
    except ValueError:
        return False


def source(db, pid, key):
    if not positive(pid):
        return None
    kind, artifact, page = parse(key)
    row = db.execute('SELECT artifact_id,project_id,message_id,meta,created_at,kind,model,'
                     'CASE WHEN length(content)<=4194304 THEN content END AS content '
                     'FROM artifacts WHERE artifact_id=? AND project_id=? AND kind=?',
                     (artifact, pid, 'karte_summary' if kind == 'karte' else 'attachment_ocr_v1')).fetchone()
    if row is None:
        return None
    if kind == 'karte':
        from mcs_queries import karte_source_record
        return karte_source_record(row, pid)
    from attachment_ocr import current
    raw = loads_dict(row['content']) or {}
    document = current(db, raw.get('attachment_id'), project_id=pid)
    if document is None or document['artifact_id'] != artifact or page > len(document['pages']):
        return None
    parent = db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=?',
                        (pid, document['message_id'])).fetchone()
    attachment = db.execute('SELECT * FROM attachments WHERE attachment_id=?',
                            (document['attachment_id'],)).fetchone()
    if parent is None or attachment is None:
        return None
    parent, attachment = dict(parent), dict(attachment)
    entry = document['pages'][page - 1]
    # Parent availability and ownership are guarded above; its chat edits are not OCR inputs.
    binding = payload_hash(['attachment-ocr-source/v2', pid, artifact, page, entry['text_hash'],
        document['runtime_contract'], document.get('text_hash'),
        [attachment.get(k) for k in ('attachment_id', 'message_id', 'sha256', 'bytes')]])
    return {'message_id': 0, 'source_key': key, 'parent_message_id': parent['message_id'],
            'parent_id': None, 'project_id': pid, 'source': 'attachment_ocr', 'source_kind': 'attachment_ocr',
            'source_artifact_id': artifact, 'artifact_id': artifact, 'attachment_id': document['attachment_id'],
            'page': page, 'text_hash': entry['text_hash'], 'content_hash': entry['text_hash'],
            'runtime_contract': document['runtime_contract'], 'body_text': entry['text'],
            'body_length': len(entry['text']), 'body_state': 'full', 'empty': not entry['text'].strip(),
            'sender_id': None, 'sender_name': None, 'posted_at': None, 'posted_at_ts': None,
            'first_seen': row['created_at'], '_source_binding': binding, 'historical': False,
            'original_available': document['original_available']}


def binding(db, pid, key):
    record = source(db, pid, key)
    if record is None:
        return None
    kind, artifact, _ = parse(key)
    if kind == 'karte':
        raw = db.execute('SELECT meta FROM artifacts WHERE artifact_id=? AND project_id=?', (artifact, pid)).fetchone()
        return payload_hash(['karte-source/v1', pid, artifact, record['content_hash'],
                             (loads_dict(raw['meta']) or {}).get('karte_id')])
    return record['_source_binding']
