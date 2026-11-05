"""Separate OCR cloud request identity from the local result that permits adoption."""
import original_source_keys
from mcs_util import payload_hash, positive, strict_json, valid_hash


def input_binding(source):
    """Bind cloud bytes to their raw OCR occurrence; request fingerprints bind the actual prompt."""
    return payload_hash(['attachment-cloud-input/v2', source['source_key'], source['_source_binding']])


def occurrence(db, meta):
    """Prove an OCR owner's current raw occurrence and its retained local admission evidence."""
    owner = meta.get('owner')
    key = owner.get('source_key') if isinstance(owner, dict) else None
    if (meta.get('route') != 'reextract' or meta.get('message_id') is not None
            or not positive(meta.get('project_id')) or not isinstance(owner, dict)
            or owner.get('source_type') != 'attachment_ocr' or not positive(owner.get('local_artifact_id'))
            or not original_source_keys.valid(key) or original_source_keys.parse(key)[0] != 'attachment_ocr'
            or meta.get('unit_key') != key + ':cloud'):
        return None
    source = original_source_keys.source(db, meta['project_id'], key)
    if source is None:
        return None
    row = db.execute("SELECT kind,project_id,message_id,CASE WHEN typeof(content)='text' "
        "AND length(content)<=4194304 THEN content END AS content FROM artifacts WHERE artifact_id=?",
        (owner['local_artifact_id'],)).fetchone()
    try:
        value = strict_json(row['content']) if row is not None else None
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None
    if (row is None or row['kind'] != 'attachment_extract_v1' or row['project_id'] != meta['project_id']
            or row['message_id'] != source['parent_message_id'] or not isinstance(value, dict)
            or value.get('schema') != 'attachment-inference/v1' or value.get('stage') != 'local'
            or value.get('source_key') != key or value.get('source_binding') != source['_source_binding']
            or not valid_hash(value.get('contract')) or not isinstance(value.get('document'), dict)
            or value.get('result_hash') != payload_hash(value['document'])
            or value.get('provider_evidence') != {'providers': ['local'], 'complete': True}):
        return None
    return source, value


def compatible_receipt(db, meta, source, scope):
    """Accept only a new stable binding or a fully reconstructed old binding under this exact scope."""
    bound = occurrence(db, meta)
    if bound is None or bound[0]['source_key'] != source['source_key'] or bound[0]['_source_binding'] != source['_source_binding']:
        return False
    old = bound[1]
    legacy = payload_hash(['attachment-inference/v1', source['_source_binding'],
                          meta['owner']['local_artifact_id'], old['result_hash'], old['contract']])
    return meta.get('input_binding') in (payload_hash([input_binding(source), scope]),
                                         payload_hash([legacy, scope]))


def recovery_rows(db, *, project_id, unit_key, binding, request_fp, owner, scope):
    """Find prior-format receipts for this current occurrence and exact HTTP envelope, without sending."""
    candidate = {'route': 'reextract', 'project_id': project_id, 'message_id': None,
                 'unit_key': unit_key, 'owner': owner}
    bound = occurrence(db, candidate)
    if bound is None or binding != payload_hash([input_binding(bound[0]), scope]):
        return []
    result = []
    for row in db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
            "FROM artifacts WHERE kind='cloud_usage_reserve' AND project_id=? AND message_id IS NULL "
            "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.route')='reextract' "
            "AND json_extract(meta,'$.unit_key')=? AND json_extract(meta,'$.request_fp')=? "
            "AND json_extract(meta,'$.input_binding')!=? END ORDER BY artifact_id",
            (project_id, unit_key, request_fp, binding)):
        try:
            meta = strict_json(row['meta'])
        except (ValueError, TypeError, RecursionError, OverflowError):
            continue
        if isinstance(meta, dict) and compatible_receipt(db, meta, bound[0], scope):
            result.append(row)
    return result
