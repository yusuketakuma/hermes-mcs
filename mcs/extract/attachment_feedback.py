"""Bind human OCR correction requests to one current original and extraction."""
import json

import original_source_keys
from mcs_util import load_config, loads_dict, valid_text

KIND = 'attachment_extract_feedback_v1'


def revision(db, project_id, key, binding):
    """Return the last correction boundary for this exact OCR occurrence."""
    row = db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE kind=? AND project_id=? "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') END=? "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_binding') END=?",
        (KIND, project_id, key, binding)).fetchone()
    return row[0] or 0


def notes(db, source):
    """Use the actual correction request as existing extraction feedback."""
    boundary = source.get('_feedback_revision', 0)
    if not boundary:
        return None
    row = db.execute('SELECT CASE WHEN length(content)<=8192 THEN content END FROM artifacts '
        'WHERE artifact_id=? AND kind=? AND project_id=?',
        (boundary, KIND, source['project_id'])).fetchone()
    value = loads_dict(row[0]) if row else None
    if (not value or value.get('source_key') != source['source_key']
            or value.get('source_binding') != source['_source_binding']
            or not valid_text(value.get('field'), 120) or not valid_text(value.get('reason'), 2000)):
        raise ValueError('attachment_feedback_invalid')
    return [f"OCR correction report #{boundary} ({value['field']}): {value['reason']}"]


def apply_tx(db, req, now, *, cfg=None):
    """Append a source-pinned correction without editing results or paid evidence."""
    import notify_cards

    key, pid = req['source_key'], req['project_id']
    source = target(db, req, cfg=cfg)
    if source is None:
        return 'extraction_changed', {'message_id': req['message_id']}
    value = {key: req[key] for key in ('message_id', 'artifact_id', 'source_key', 'field',
                                     'reason', 'actor', 'command_id')}
    value.update(source_binding=source['_source_binding'], stage=source['extraction_stage'], at=now)
    db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) '
        'VALUES(?,?,?,?,?,?,?)', (KIND, pid, req['message_id'], json.dumps(value, ensure_ascii=False),
        'human', json.dumps({'source_key': key, 'source_binding': source['_source_binding'],
                            'source_artifact_id': req['artifact_id'], 'command_id': req['command_id']}), now))
    notify_cards.mark_snapshot_dirty(db)
    return None, {'message_id': req['message_id'], 'source_key': key, 'field': req['field']}


def valid_key(key):
    return original_source_keys.valid(key) and original_source_keys.parse(key)[0] == 'attachment_ocr'


def target(db, req, *, cfg=None):
    """Check the same current result at preview, confirmation and receipt application."""
    import attachment_extraction
    from measurement_reader import patient_scope_valid

    cfg = load_config() if cfg is None else cfg
    if not patient_scope_valid(db, req['project_id']):
        return None
    source = attachment_extraction.source(db, req['project_id'], req['source_key'], cfg=cfg)
    if (source is None or source['parent_message_id'] != req['message_id']
            or source['extraction_artifact_id'] != req['artifact_id']):
        return None
    return source
