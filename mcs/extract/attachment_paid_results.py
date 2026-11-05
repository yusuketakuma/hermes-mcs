"""Journal paid OCR inference callbacks before waiting for the shared database writer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

import bounded_http
import cloud_policy
import original_source_keys
from mcs_util import payload_hash, strict_json
from safe_io import atomic_write, fsync_dir, read_private_bytes

SCHEMA = 'attachment-paid-result/v1'
LIMIT = 6 * bounded_http.MAX_RESPONSE_BYTES + 65536


def _directory(root, *, create=False):
    path = Path(root) / 'attachment_paid_results'
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            fsync_dir(str(Path(root)))
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError('attachment_paid_directory_invalid')
    return path


def _identity(reservation, origin):
    if (not isinstance(reservation, dict) or not isinstance(origin, dict)
            or set(origin) != {'project_id', 'source_key', 'source_binding', 'contract', 'input_binding',
                               'selection', 'scope', 'purpose'}
            or type(origin['project_id']) is not int
            or any(not isinstance(origin[key], str) or re.fullmatch(r'[0-9a-f]{64}', origin[key]) is None
                   for key in ('source_binding', 'contract', 'input_binding', 'scope'))
            or not isinstance(origin['selection'], dict)
            or origin['purpose'] not in ('backlog', 'quality_repair')):
        raise ValueError('attachment_paid_identity_invalid')
    if original_source_keys.parse(origin['source_key'])[0] != 'attachment_ocr':
        raise ValueError('attachment_paid_source_invalid')
    if (reservation.get('project_id') != origin['project_id'] or reservation.get('message_id') is not None
            or reservation.get('route') != 'reextract'
            or reservation.get('unit_key') != origin['source_key'] + ':cloud'
            or reservation.get('input_binding') != origin['input_binding']
            or not isinstance(reservation.get('owner'), dict)
            or reservation['owner'].get('source_type') != 'attachment_ocr'
            or reservation['owner'].get('source_key') != origin['source_key']
            or type(reservation['owner'].get('local_artifact_id')) is not int):
        raise ValueError('attachment_paid_reservation_invalid')
    rid = reservation.get('reservation_id')
    if not isinstance(rid, str) or re.fullmatch(r'[0-9a-f]{32}', rid) is None:
        raise ValueError('attachment_paid_reservation_invalid')
    return rid


def preserve(root, origin, reservation, metadata, answer):
    """Durably retain only the exact response and its already-created reservation."""
    rid = _identity(reservation, origin)
    if not isinstance(metadata, dict):
        raise ValueError('attachment_paid_metadata_invalid')
    saved = None
    if answer is not None:
        saved = cloud_policy._saved_answer(answer, metadata)
    value = {'schema': SCHEMA, 'origin': origin, 'reservation': reservation,
             'metadata': metadata, 'answer': saved}
    value['hash'] = payload_hash(value)
    text = json.dumps(value, ensure_ascii=False)
    if len(text.encode('utf-8')) > LIMIT:
        raise ValueError('attachment_paid_result_too_large')
    path = _directory(root, create=True) / (rid + '.json')
    if os.path.lexists(path):
        prior = strict_json(read_private_bytes(path, LIMIT))
        if prior != value:
            raise ValueError('attachment_paid_result_conflict')
    else:
        atomic_write(str(path), lambda stream: stream.write(text), mode=0o600, makedirs=False)
    return path


def remove(path):
    """Acknowledge a journal only after its database settlement committed."""
    os.unlink(path)
    fsync_dir(str(Path(path).parent))


def pending(root):
    """Return a bounded private batch; a malformed directory never proves no work."""
    try:
        directory = _directory(root)
    except FileNotFoundError:
        return []
    result = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= 64:
                break
            if entry.name.startswith('.atomic.') and entry.name.endswith('.tmp'):
                continue
            if re.fullmatch(r'[0-9a-f]{32}\.json', entry.name) is None:
                raise ValueError('attachment_paid_filename_invalid')
            result.append(directory / entry.name)
    return result


def replay(ledger, paths, cfg):
    """Settle receipts under the caller's writer fence without adopting clinical results."""
    for path in paths:
        value = strict_json(read_private_bytes(path, LIMIT))
        if not isinstance(value, dict) or set(value) != {'schema', 'origin', 'reservation', 'metadata', 'answer', 'hash'}:
            raise ValueError('attachment_paid_result_invalid')
        body = {key: item for key, item in value.items() if key != 'hash'}
        if value['schema'] != SCHEMA or value['hash'] != payload_hash(body):
            raise ValueError('attachment_paid_result_invalid')
        reservation, origin = value['reservation'], value['origin']
        if path.name != _identity(reservation, origin) + '.json' or not isinstance(value['metadata'], dict):
            raise ValueError('attachment_paid_result_invalid')
        cloud_policy._stored_reservation(ledger, reservation, bound_only=True)
        local = ledger.db.execute("SELECT project_id,message_id,kind,CASE WHEN typeof(content)='text' "
                                  "AND length(content)<=4194304 THEN content END AS content FROM artifacts "
                                  "WHERE artifact_id=?", (reservation['owner']['local_artifact_id'],)).fetchone()
        document = strict_json(local['content']) if local is not None else None
        if (local is None or local['kind'] != 'attachment_extract_v1'
                or local['project_id'] != origin['project_id'] or not isinstance(document, dict)
                or document.get('stage') != 'local' or document.get('source_key') != origin['source_key']
                or document.get('source_binding') != origin['source_binding']
                or document.get('contract') != origin['contract']
                or document.get('result_hash') != payload_hash(document.get('document'))):
            raise ValueError('attachment_paid_source_binding_invalid')
        base = payload_hash(['attachment-inference/v1', origin['source_binding'],
                             reservation['owner']['local_artifact_id'], document['result_hash'], document['contract']])
        if (payload_hash([base, origin['scope']]) != origin['input_binding']
                or origin['selection'].get('model') != reservation['model']):
            raise ValueError('attachment_paid_input_binding_invalid')
        outcome = dict(value['metadata'])
        rows = cloud_policy._matching_bound_rows(ledger, 'cloud_usage_result', reservation['reservation_id'])
        if len(rows) == 1:
            recorded = cloud_policy._receipt_meta(rows[0]['meta'])
            if 'disposition' in recorded:
                outcome['disposition'] = recorded['disposition']
        cloud_policy.record_result(ledger, reservation, outcome, answer=value['answer'])
        obsolete = original_source_keys.binding(ledger.db, origin['project_id'], origin['source_key']) != origin['source_binding']
        if isinstance(cfg, dict) and cfg:
            settings, errors = cloud_policy.resolve_settings(cfg)
            chosen, profile_errors = cloud_policy.resolve_profile(cfg, route='reextract', purpose=origin['purpose'])
            if not errors and not profile_errors and chosen is not None:
                obsolete = (obsolete or cloud_policy.scope_generation({'cloud_llm': settings}) != origin['scope']
                            or dict(chosen) != origin['selection'])
        if obsolete:
            with ledger.db:
                ledger.db.execute('BEGIN IMMEDIATE')
                cloud_policy.record_disposition_tx(ledger, reservation, 'discarded_scope')
        remove(path)
