"""Validate, read and persist source-bound attachment extraction results."""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import attachment_ocr
import attachment_cloud_identity
import attachment_feedback
import attachment_inference_contract
import cloud_inference
import cloud_policy
import extract_llm
import extract_quality
import extract_validation
import inference_version
import local_llm
import original_source_keys
from mcs_util import load_config, loads_dict, payload_hash, positive

KIND = 'attachment_extract_v1'
SCHEMA = 'attachment-inference/v1'
_FACADE_CODE = hashlib.sha256(Path(__file__).with_name('attachment_inference.py').read_bytes()).hexdigest()


def contract(cfg, *, code_digest=None):
    return attachment_inference_contract.contract(cfg, code_digest=code_digest or _FACADE_CODE,
        source_digests=extract_llm._LOADED_SOURCE_DIGESTS,
        inference_version=inference_version.CURRENT_VERSION, extract_version=extract_llm.EXTRACT_VERSION,
        context_version=extract_llm.PATIENT_CONTEXT_VERSION)


def requirement(cfg, pid):
    if not cloud_policy.send_route('reextract'):
        # A closed send route can never require a new send — and a broken
        # cloud config must not block an unowned page's local completion.
        return False, None
    if not isinstance(cfg, dict) or not cfg:
        return None, None
    settings, errors = cloud_policy.resolve_settings(cfg)
    if errors:
        return None, None
    if settings['mode'] != 'on' or not cloud_policy.eligible(settings, 'reextract', pid):
        return False, None
    try:
        return True, cloud_inference.cloud_provider_contract(cfg)
    except cloud_inference.CloudFallback:
        return None, None


def _cloud_requirement(db, cfg, pid, key):
    """requirement() extended for recovery: a closed send route retires new sends,
    never the unit a prior reservation created — an owned page still needs its
    paid answer read or held, not silently replaced by the local result.
    Ownership is decided before settings: an unreadable cloud config cannot
    release an owned unit to the local path, nor block an unowned one."""
    if not cloud_policy.owns_unit_request(db, route='reextract',
            project_id=pid, unit_key=key + ':cloud'):
        return requirement(cfg, pid)
    if cloud_policy.scope_revoked(cfg, 'reextract', pid):
        # a proven revocation — declared off/dry_run or a verifiable
        # route/project exclusion — leaves the orphaned unit out of scope.
        return False, None
    # Owned and not revoked: a computed 'off' (mode omitted, key unreadable)
    # or a broken config keeps the unit held — never released to local.
    try:
        return True, cloud_inference.cloud_provider_contract(cfg)
    except cloud_inference.CloudFallback:
        return True, None


def validated(document, body, quality):
    if not extract_llm._response_has_required_fields(document) or not extract_quality.completeness_resolved({
            'inference_engine_version': inference_version.CURRENT_VERSION, 'integrity': quality}):
        return False
    cleaned = extract_validation.validate(copy.deepcopy(document), body)
    if '_chunks_total' in document and (type(document['_chunks_total']) is not int
            or document['_chunks_total'] < 1 or document['_chunks_total'] != quality['completeness_chunks']):
        return False
    counters = ('_items_dropped', '_evidence_dropped')
    comparable = {key: value for key, value in document.items() if key not in (*counters, '_chunks_total')}
    return (cleaned is not None and payload_hash(cleaned) == payload_hash(comparable)
            and not any(document.get(field) for field in ('_items_dropped', '_evidence_dropped')))


def stored(db, pid, key, stage, cfg, *, contract_for=None):
    contract_for = contract_for or contract
    source = original_source_keys.source(db, pid, key)
    if source is None:
        return None
    row = db.execute("SELECT artifact_id,CASE WHEN length(content)<=4194304 THEN content END AS content "
        "FROM artifacts WHERE kind=? AND project_id=? "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') END=? "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.stage') END=? "
        "ORDER BY artifact_id DESC LIMIT 1", (KIND, pid, key, stage)).fetchone()
    if (row is None or not isinstance(row['content'], str) or len(row['content']) > 4194304
            or row['artifact_id'] <= attachment_feedback.revision(db, pid, key, source['_source_binding'])):
        return None
    value = loads_dict(row['content']) or {}
    proof = value.get('provider_evidence')
    if (value.get('schema') != SCHEMA or value.get('source_key') != key or value.get('stage') != stage
            or value.get('source_binding') != source['_source_binding']
            or value.get('contract') != contract_for(cfg) or not isinstance(value.get('document'), dict)
            or value.get('result_hash') != payload_hash(value['document'])
            or not validated(value['document'], source['body_text'], value.get('quality'))
            or not isinstance(proof, dict) or proof.get('complete') is not True):
        return None
    if stage == 'local':
        if proof.get('providers') != ['local']:
            return None
    else:
        needed, provider_contract = _cloud_requirement(db, cfg, pid, key)
        local = stored(db, pid, key, 'local', cfg, contract_for=contract_for)
        if (needed is not True or local is None or provider_contract is None
                or value.get('provider_contract') != provider_contract
                or proof.get('providers') != [provider_contract['backend']]
                or value.get('local_artifact_id') != local['artifact_id']):
            return None
        ids = value.get('reservation_artifact_ids')
        if (not isinstance(ids, list) or not 0 < len(ids) <= 512
                or any(type(item) is not int for item in ids)):
            return None
        try:
            evidence = cloud_policy.reservation_evidence_batch(db, ids)
            history = history_snapshot(db, source)
            if history is None:
                return None
            scope = cloud_policy.scope_generation({'cloud_llm': cloud_inference.resolve_cloud_settings(cfg)})
            if any(not attachment_cloud_identity.compatible_receipt(db, item['reservation'], source, scope)
                   or item['reservation'].get('project_id') != pid
                   or item['reservation'].get('message_id') is not None
                   or item['reservation'].get('unit_key') != key + ':cloud'
                   or item['reservation'].get('route') != 'reextract'
                   for item in evidence.values()):
                return None
        except (ValueError, TypeError, KeyError):
            return None
    return {**value, 'artifact_id': row['artifact_id']}


def current(db, pid, source_key, *, cfg=None):
    """Return a source-bound result, keeping mandatory cloud follow-up visibly pending."""
    cfg = load_config() if cfg is None else cfg
    try:
        with closing(db.execute('SELECT artifact_id FROM (SELECT artifact_id FROM artifacts LIMIT 1) '
                                'UNION ALL SELECT NULL UNION ALL SELECT NULL')) as anchor:
            anchor.fetchone()
            original_source_keys.parse(source_key)
            local = stored(db, pid, source_key, 'local', cfg)
            if local is None:
                return None
            needed, _ = _cloud_requirement(db, cfg, pid, source_key)
            cloud = stored(db, pid, source_key, 'cloud', cfg) if needed is True else None
            return {**(cloud or local), 'require_cloud': needed,
                    'ready': needed is False or cloud is not None}
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def source(db, pid, key, *, cfg=None):
    """Read one ready clinical document bound to its currently valid OCR original."""
    cfg = load_config() if cfg is None else cfg
    if (not positive(pid) or attachment_ocr.config_error(cfg)
            or not cfg.get('attachment_ocr', {}).get('enabled', False)
            or not original_source_keys.valid(key)
            or original_source_keys.parse(key)[0] != 'attachment_ocr'):
        return None
    record = original_source_keys.source(db, pid, key)
    if record is None or not record['body_text'].strip():
        return None
    result = current(db, pid, key, cfg=cfg)
    if (result is None or not result['ready']
            or record['_source_binding'] != result['source_binding']):
        return None
    return {**record, 'document': result['document'], 'ready': True,
            'extraction_artifact_id': result['artifact_id'], 'result_hash': result['result_hash'],
            'extraction_stage': result['stage']}


def iter_sources(db, pid, *, mid=None, cfg=None):
    """Yield ready clinical documents with their independent, currently valid OCR originals."""
    cfg = load_config() if cfg is None else cfg
    if (not positive(pid) or mid is not None and not positive(mid)
            or attachment_ocr.config_error(cfg) or not cfg.get('attachment_ocr', {}).get('enabled', False)):
        return
    params = (pid, mid) if mid is not None else (pid,)
    for row in db.execute('SELECT a.attachment_id FROM attachments a JOIN messages m USING(message_id) '
            "WHERE m.project_id=? AND m.body_state IS NOT 'deleted' AND a.state IN ('downloaded','pruned') "
            + ('AND m.message_id=? ' if mid is not None else '') + 'ORDER BY a.attachment_id', params):
        document = attachment_ocr.current(db, row['attachment_id'], project_id=pid)
        if document is None:
            continue
        for page in document['pages']:
            if not page['text'].strip():
                continue
            key = f"attachment_ocr:{document['artifact_id']}:{page['page']}"
            record = source(db, pid, key, cfg=cfg)
            if record is not None:
                yield record


def history_snapshot(db, source, *, require_ready=False):
    """Bind cloud admission to the parent's receipted history scope, without granting a new scope."""
    try:
        pid, parent = source['project_id'], source['parent_message_id']
        if (type(pid) is not int or pid <= 0 or type(parent) is not int or parent <= 0
                or require_ready and not cloud_policy.history_target_ready(db, pid, parent)):
            return None
        # This snapshot checks history_thread_allowed and its committed resume receipt.
        return cloud_policy.history_thread_snapshot(db, pid, parent)
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None


def input_binding(source, local):
    return attachment_cloud_identity.input_binding(source)


def save(ledger, source, cfg, stage, document, meta, unit=None, local=None, *, contract_for, write):
    proof = meta.get('_provider_evidence')
    quality = {key: meta.get(key) for key in ('completeness', 'completeness_chunks')}
    if (not isinstance(proof, dict) or proof.get('complete') is not True
            or not validated(document, source['body_text'], quality)):
        return False
    provider = 'local' if unit is None else unit.contract['backend']
    if proof.get('providers') != [provider]:
        return False
    receipts = []
    if unit is not None:
        unit.commit_guard()
        if history_snapshot(ledger.db, source) is None:
            return False
        pending = [unit]
        while pending:
            member = pending.pop()
            pending.extend(member._children)
            for receipt in member.completed_reservations:
                rows = ledger.db.execute("SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve' "
                    "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.reservation_id') END=?",
                    (receipt['reservation_id'],)).fetchall()
                if len(rows) != 1:
                    return False
                receipts.append(rows[0][0])
        if not receipts:
            return False
        cloud_policy.reservation_evidence_batch(ledger.db, receipts)
    value = {'schema': SCHEMA, 'source_key': source['source_key'], 'source_binding': source['_source_binding'],
        'stage': stage, 'contract': contract_for(cfg), 'document': document, 'result_hash': payload_hash(document),
        'quality': quality,
        'provider_evidence': proof, 'provider_contract': unit.contract if unit else None,
        'local_artifact_id': local['artifact_id'] if local else None, 'reservation_artifact_ids': receipts}

    def save():
        if unit is not None:
            unit.commit_guard()
        with ledger.db:
            if unit is not None and history_snapshot(ledger.db, source) is None:
                return False
            if stored(ledger.db, source['project_id'], source['source_key'], stage, cfg, contract_for=contract_for) is not None:
                return False
            ledger.artifact_add_tx(KIND, json.dumps(value, ensure_ascii=False), project_id=source['project_id'],
                message_id=source['parent_message_id'], model=unit.model if unit else local_llm.resolve(cfg)[1],
                meta={'source_key': source['source_key'], 'stage': stage})
            import notify_cards
            notify_cards.mark_snapshot_dirty(ledger.db)
        return True
    return (unit.dispatch(save) if unit is not None
            else write(save))
