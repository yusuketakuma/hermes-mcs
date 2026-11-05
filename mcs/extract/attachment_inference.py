"""Infer clinical facts from retained OCR pages without changing their parent chat."""
from __future__ import annotations

import hashlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import time

import attachment_ocr
import cloud_inference
import cloud_client
import cloud_policy
import extract_llm
import extract_quality
import extract_validation
import inference_version
import local_llm
import original_source_keys
import patient_priority
import writer_lock
from mcs_util import load_config, loads_dict, payload_hash

KIND = 'attachment_extract_v1'
SCHEMA = 'attachment-inference/v1'
_CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _contract(cfg):
    return payload_hash([SCHEMA, _CODE, extract_llm._upgrade_contract(), local_llm.resolve(cfg)])


def _requirement(cfg, pid):
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


def _validated(document, body, quality):
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


def _stored(db, pid, key, stage, cfg):
    source = original_source_keys.source(db, pid, key)
    if source is None:
        return None
    row = db.execute("SELECT artifact_id,CASE WHEN length(content)<=4194304 THEN content END AS content "
        "FROM artifacts WHERE kind=? AND project_id=? "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') END=? "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.stage') END=? "
        "ORDER BY artifact_id DESC LIMIT 1", (KIND, pid, key, stage)).fetchone()
    if row is None or not isinstance(row['content'], str) or len(row['content']) > 4194304:
        return None
    value = loads_dict(row['content']) or {}
    proof = value.get('provider_evidence')
    if (value.get('schema') != SCHEMA or value.get('source_key') != key or value.get('stage') != stage
            or value.get('source_binding') != source['_source_binding']
            or value.get('contract') != _contract(cfg) or not isinstance(value.get('document'), dict)
            or value.get('result_hash') != payload_hash(value['document'])
            or not _validated(value['document'], source['body_text'], value.get('quality'))
            or not isinstance(proof, dict) or proof.get('complete') is not True):
        return None
    if stage == 'local':
        if proof.get('providers') != ['local']:
            return None
    else:
        needed, contract = _requirement(cfg, pid)
        local = _stored(db, pid, key, 'local', cfg)
        if (needed is not True or local is None or value.get('provider_contract') != contract
                or proof.get('providers') != [contract['backend']]
                or value.get('local_artifact_id') != local['artifact_id']):
            return None
        ids = value.get('reservation_artifact_ids')
        if (not isinstance(ids, list) or not 0 < len(ids) <= 512
                or any(type(item) is not int for item in ids)):
            return None
        try:
            evidence = cloud_policy.reservation_evidence_batch(db, ids)
            history = _history_snapshot(db, source)
            if history is None:
                return None
            expected_binding = payload_hash([_input_binding(source, local),
                cloud_policy.scope_generation({'cloud_llm': cloud_inference.resolve_cloud_settings(cfg)})])
            if any(item['reservation'].get('input_binding') != expected_binding
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
        original_source_keys.parse(source_key)
        local = _stored(db, pid, source_key, 'local', cfg)
        if local is None:
            return None
        requirement, _ = _requirement(cfg, pid)
        cloud = _stored(db, pid, source_key, 'cloud', cfg) if requirement is True else None
        return {**(cloud or local), 'require_cloud': requirement,
                'ready': requirement is False or cloud is not None}
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def _history_snapshot(db, source, *, require_ready=False):
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


def _input_binding(source, local):
    return payload_hash([SCHEMA, source['_source_binding'], local['artifact_id'], local['result_hash'],
                         local['contract']])


def _guard(ledger, source, cfg, generation):
    live = load_config()
    free = extract_llm.disk_free_mb(ledger.db)
    if (writer_lock.updater_state() != generation or generation[0]
            or free is None or free < extract_llm.disk_floor_mb()
            or attachment_ocr.config_error(live)
            or not live.get('attachment_ocr', {}).get('enabled', False)
            or original_source_keys.binding(ledger.db, source['project_id'], source['source_key'])
            != source['_source_binding'] or _contract(cfg) != _contract(live)):
        raise ValueError('attachment_inference_source_changed')


def _write(ledger, source, cfg, generation, fn, *, result=False):
    root = attachment_ocr._data_root(ledger)
    fd = writer_lock.wait_run_lock(str(root / 'run.lock'), wait_s=5,
        expected_code_stamp=generation[1], marker_path=str(root / writer_lock.UPDATE_MARKER_NAME))
    if fd is None:
        raise ValueError('attachment_inference_writer_busy')
    try:
        if writer_lock.updater_state() != generation or generation[0]:
            raise ValueError('attachment_inference_writer_changed')
        if not result:
            _guard(ledger, source, cfg, generation)
        return fn()
    finally:
        os.close(fd)


def _dispatcher(ledger, source, cfg, generation):
    # Shared response recovery recursively dispatches commit guards on this same owner thread.
    depth = 0

    def dispatch(fn, *, result=False):
        nonlocal depth
        def call():
            nonlocal depth
            if not result:
                _guard(ledger, source, cfg, generation)
            depth += 1
            try:
                return fn()
            finally:
                depth -= 1
        return call() if depth else _write(ledger, source, cfg, generation, call, result=result)
    return dispatch



def _journal_unit(unit, source, cfg):
    """Keep paid response bytes durable even when the accounting writer is busy."""
    import attachment_paid_results as journal
    root = attachment_ocr._data_root(unit.ledger)
    origin = {'project_id': source['project_id'], 'source_key': source['source_key'],
              'source_binding': source['_source_binding'], 'contract': _contract(cfg),
              'input_binding': unit._input_binding(), 'selection': dict(unit.selection),
              'scope': cloud_policy.scope_generation({'cloud_llm': cloud_inference.resolve_cloud_settings(cfg)}),
              'purpose': unit.purpose}
    record = unit.result

    def result(reservation, metadata, *, answer=None):
        path = journal.preserve(root, origin, reservation, metadata, answer)
        record(reservation, metadata, answer=answer)
        journal.remove(path)
    unit.result = result
    spawn = unit.spawn_quality_repair
    unit.spawn_quality_repair = lambda: _journal_unit(spawn(), source, cfg)
    return unit


def _recover_paid_results(ledger, generation):
    """Replay bounded accounting journals before any new inference dispatch."""
    import attachment_paid_results as journal
    try:
        paths = journal.pending(attachment_ocr._data_root(ledger))
        if not paths:
            return True
        _write(ledger, None, None, generation, lambda: journal.replay(ledger, paths, load_config()), result=True)
        return True
    except (cloud_client.CloudError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return False

def _save(ledger, source, cfg, generation, stage, document, meta, unit=None, local=None):
    proof = meta.get('_provider_evidence')
    quality = {key: meta.get(key) for key in ('completeness', 'completeness_chunks')}
    if (not isinstance(proof, dict) or proof.get('complete') is not True
            or not _validated(document, source['body_text'], quality)):
        return False
    provider = 'local' if unit is None else unit.contract['backend']
    if proof.get('providers') != [provider]:
        return False
    receipts = []
    if unit is not None:
        unit.commit_guard()
        if _history_snapshot(ledger.db, source) is None:
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
        'stage': stage, 'contract': _contract(cfg), 'document': document, 'result_hash': payload_hash(document),
        'quality': quality,
        'provider_evidence': proof, 'provider_contract': unit.contract if unit else None,
        'local_artifact_id': local['artifact_id'] if local else None, 'reservation_artifact_ids': receipts}

    def save():
        if unit is not None:
            unit.commit_guard()
        with ledger.db:
            if unit is not None and _history_snapshot(ledger.db, source) is None:
                return False
            if _stored(ledger.db, source['project_id'], source['source_key'], stage, cfg) is not None:
                return False
            ledger.artifact_add_tx(KIND, json.dumps(value, ensure_ascii=False), project_id=source['project_id'],
                message_id=source['parent_message_id'], model=unit.model if unit else local_llm.resolve(cfg)[1],
                meta={'source_key': source['source_key'], 'stage': stage})
            import notify_cards
            notify_cards.mark_snapshot_dirty(ledger.db)
        return True
    return (unit.dispatch(save) if unit is not None
            else _write(ledger, source, cfg, generation, save))


_CHECKPOINT_KIND = 'attachment_inference_checkpoint_v1'
_CHECKPOINT_SCHEMA = 'attachment-local-checkpoint/v1'


def _checkpoint_leaf(value, source, piece):
    """Validate retained local candidates and preserve pending quality as pending."""
    proof = extract_llm._leaf_evidence(value)
    if proof != {'providers': ['local'], 'complete': True}:
        return None
    candidate = {k: v for k, v in value.items() if k not in ('_quality_status', '_quality_result')}
    cleaned = extract_validation.validate(copy.deepcopy(candidate), source['body_text'])
    if cleaned is not None:
        for field in ('_items_dropped', '_evidence_dropped'):
            if type(candidate.get(field)) is int and candidate[field] >= 0:
                cleaned[field] = max(cleaned.get(field, 0), candidate[field])
    if (not extract_llm._response_has_required_fields(candidate) or cleaned is None
            or payload_hash(cleaned) != payload_hash(candidate)):
        return None
    state = value.get('_quality_status')
    quality = value.get('_quality_result')
    if state not in (None, 'pending', 'started'):
        return None
    if state is None:
        hints = extract_llm._rule_hints({'body_text': piece, 'posted_at': None})
        expected = extract_llm._hint_coverage(candidate, hints, piece)
        if not isinstance(quality, dict) or not extract_llm._quality_record_consistent(quality, expected):
            return None
    leaf = extract_llm._EvidenceLeaf(candidate, proof)
    if state is not None or quality.get('state') != 'hint_gaps_resolved':
        # A local interrupted repair can be retried; it has no paid-send uncertainty.
        leaf['_quality_status'] = 'pending'
    else:
        leaf['_quality_result'] = quality
    return leaf


def _local_checkpoints(ledger, source, cfg, generation):
    """Bind resumable local leaves and split paths to one exact OCR occurrence."""
    size = extract_llm._CHUNK_SIZE
    identity = {'schema': _CHECKPOINT_SCHEMA, 'source_key': source['source_key'],
        'source_binding': source['_source_binding'], 'contract': _contract(cfg),
        'plan': extract_llm.PLAN_VERSION, 'chunk_size': size}
    records = {}
    for row in ledger.db.execute("SELECT CASE WHEN typeof(content)='text' AND length(content)<=4194304 "
            "THEN content END FROM artifacts WHERE kind=? AND project_id=? "
            "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') END=? "
            "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.binding') END=? "
            "ORDER BY artifact_id", (_CHECKPOINT_KIND, source['project_id'], source['source_key'],
                                     payload_hash(identity))):
        raw = extract_llm._provider_strict_dict(row[0])
        if not isinstance(raw, dict) or any(raw.get(k) != v for k, v in identity.items()):
            records.clear()
            continue  # corrupt history cannot resurrect an earlier leaf
        key = raw.get('key')
        if not extract_llm._checkpoint_keys(key):
            records.clear()
            continue
        records[key] = raw
    splits, chunks = {}, {}
    for key in sorted(records, key=lambda k: (str(k).count(':'), str(k))):
        raw = records[key]
        piece = extract_llm._chunk_piece(source['body_text'], size, key, splits)
        if piece is None or raw.get('piece_hash') != attachment_ocr._hash(piece):
            continue
        if raw.get('kind') == 'split':
            smaller = raw.get('size')
            if type(smaller) is int and 1 <= smaller < len(piece):
                parts = extract_llm.plan_chunks(piece, smaller)
                if len(parts) >= 2 and ''.join(parts) == piece:
                    splits[key] = smaller
        elif raw.get('kind') == 'leaf' and isinstance(raw.get('value'), dict):
            if raw.get('value_hash') != payload_hash(raw['value']):
                continue
            leaf = _checkpoint_leaf(extract_llm._EvidenceLeaf(raw['value'], raw.get('evidence')), source, piece)
            if leaf is not None:
                chunks[key] = leaf

    def store(key, value=None, smaller=None):
        piece = extract_llm._chunk_piece(source['body_text'], size, key, splits)
        if piece is None:
            return False
        raw = {**identity, 'key': key, 'piece_hash': attachment_ocr._hash(piece)}
        if smaller is not None:
            if type(smaller) is not int or not 1 <= smaller < len(piece):
                return False
            raw.update(kind='split', size=smaller)
        else:
            leaf = _checkpoint_leaf(value, source, piece)
            if leaf is None:
                return False
            raw.update(kind='leaf', value=dict(leaf), evidence=leaf.provider_evidence,
                       value_hash=payload_hash(dict(leaf)))
        def save():
            with ledger.db:
                ledger.artifact_add_tx(_CHECKPOINT_KIND, json.dumps(raw, ensure_ascii=False),
                    project_id=source['project_id'], message_id=source['parent_message_id'],
                    model=local_llm.resolve(cfg)[1],
                    meta={'source_key': source['source_key'], 'binding': payload_hash(identity)})
            return True
        if not _write(ledger, source, cfg, generation, save):
            return False
        if smaller is not None:
            splits[key] = smaller
            chunks.pop(key, None)
        else:
            chunks[key] = leaf
        return True
    return {'chunk_size': size, 'chunks_in': chunks, 'splits_in': splits,
            'on_chunk': lambda key, value: store(key, value),
            'on_split': lambda key, smaller: store(key, smaller=int(smaller)),
            'on_chunk_replay': lambda *args: True}


def run_pending(ledger, cfg, *, stage='local', deadline=None, limit=1):
    """Process bounded OCR pages through the existing admission and paid-response recovery."""
    counts = {'done': 0, 'deferred': 0}
    if stage not in ('local', 'cloud') or type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError('attachment_inference_args')
    if attachment_ocr.config_error(cfg) or not cfg.get('attachment_ocr', {}).get('enabled', False):
        return counts
    deadline = min(deadline or time.monotonic() + 120, time.monotonic() + 900)
    if ledger.db.in_transaction:
        return {**counts, 'deferred': 1}
    generation = writer_lock.updater_state()
    if generation[0] or not extract_llm._writer_current(full=True):
        return {**counts, 'deferred': 1}
    if not _recover_paid_results(ledger, generation):
        return {**counts, 'deferred': 1}
    free = extract_llm.disk_free_mb(ledger.db)
    if free is None or free < extract_llm.disk_floor_mb():
        return {**counts, 'deferred': 1}
    if not extract_llm._context_memory_available():
        return {**counts, 'deferred': 1}
    root = attachment_ocr._data_root(ledger)
    fd = writer_lock.acquire_run_lock(str(root / ('attachment_inference_' + stage + '.lock')))
    if fd is None:
        return {**counts, 'deferred': 1}
    try:
        # Alternate recent active patients with a keyset history lane.
        from safe_io import atomic_write, read_json_nofollow
        cursor_path = str(root / ('attachment_inference_' + stage + '_cursor.json'))
        try:
            cursor = read_json_nofollow(cursor_path, 1024)
            cursor = {key: cursor.get(key, 0) for key in ('after', 'page', 'offset', 'priority_page', 'turn')}
            if any(type(item) is not int or item < 0 for item in cursor.values()):
                raise ValueError('cursor')
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            cursor = dict.fromkeys(('after', 'page', 'offset', 'priority_page', 'turn'), 0)
        priority = cursor['turn'] % 2 == 0
        after = cursor['offset'] if priority else cursor['after']
        after_page = cursor['priority_page'] if priority else cursor['page']
        if priority:
            rows = ledger.db.execute("SELECT a.artifact_id,a.project_id,CASE WHEN typeof(a.meta)='text' AND length(a.meta)<=32768 "
                "AND json_valid(a.meta) THEN json_extract(a.meta,'$.attachment_id') END AS attachment_id FROM artifacts a "
                'JOIN messages m ON m.message_id=a.message_id LEFT JOIN patients p ON p.project_id=m.project_id '
                'WHERE a.kind=? ORDER BY COALESCE(p.is_archived,0), '
                + patient_priority.activity_order_sql('m.project_id')
                + ', m.posted_at_ts DESC, a.artifact_id DESC LIMIT 64 OFFSET ?',
                (attachment_ocr.KIND, after)).fetchall()
        else:
            rows = ledger.db.execute("SELECT artifact_id,project_id,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 "
                "AND json_valid(meta) THEN json_extract(meta,'$.attachment_id') END AS attachment_id FROM artifacts WHERE kind=? "
                'AND artifact_id>=? ORDER BY artifact_id LIMIT 64', (attachment_ocr.KIND, after)).fetchall()
        if not rows:
            after, after_page = 0, 0
        used = 0
        for row in rows:
            position = after if priority else row['artifact_id']
            if time.monotonic() >= deadline or used >= limit:
                break
            doc = attachment_ocr.current(ledger.db, row['attachment_id'], project_id=row['project_id'])
            if doc is None or doc['artifact_id'] != row['artifact_id']:
                after, after_page = position + 1, 0
                continue
            finished = True
            for page in doc['pages']:
                if position == after and page['page'] <= after_page:
                    continue
                if not page['text'].strip():
                    continue
                key = f"attachment_ocr:{row['artifact_id']}:{page['page']}"
                source = original_source_keys.source(ledger.db, row['project_id'], key)
                if source is None or _stored(ledger.db, row['project_id'], key, stage, cfg) is not None:
                    continue
                if used >= limit or time.monotonic() >= deadline:
                    finished = False
                    break
                local = _stored(ledger.db, row['project_id'], key, 'local', cfg)
                if stage == 'cloud' and (local is None or _requirement(cfg, row['project_id'])[0] is not True):
                    continue
                history = _history_snapshot(ledger.db, source) if stage == 'cloud' else None
                if stage == 'cloud' and history is None:
                    counts['deferred'] += 1
                    continue
                used += 1
                after, after_page = position, page['page']
                unit = None
                try:
                    if stage == 'cloud':
                        unit = cloud_inference.CloudInferenceUnit(ledger, cfg, route='reextract', purpose='backlog',
                            project_id=row['project_id'], unit_key=key + ':cloud', deadline=deadline,
                            current=lambda: original_source_keys.binding(ledger.db, row['project_id'], key)
                                == source['_source_binding'],
                            dispatch_current=lambda: _history_snapshot(ledger.db, source, require_ready=True) == history,
                            dispatch=_dispatcher(ledger, source, cfg, generation),
                            input_binding=_input_binding(source, local),
                            owner={'source_type': 'attachment_ocr', 'source_key': key,
                                   'local_artifact_id': local['artifact_id']})
                        _journal_unit(unit, source, cfg)
                    meta = {}
                    checkpoints = (_local_checkpoints(ledger, source, cfg, generation)
                                   if stage == 'local' else {})
                    with extract_llm.pin_target(local_llm.resolve(cfg)):
                        result = extract_llm.llm_extract(source['body_text'], deadline=deadline, meta_out=meta,
                                                       posted_at=None, provider=unit, **checkpoints)
                    if (not isinstance(result, dict) or meta.get('_quality_hold')
                            or not _save(ledger, source, cfg, generation, stage, result, meta, unit, local)):
                        counts['deferred'] += 1
                    else:
                        counts['done'] += 1
                except (cloud_inference.CloudFallback, cloud_client.CloudNotSent, OSError, ValueError, sqlite3.Error):
                    # Paid response uncertainty is retained by the shared unit; never local-fallback or discard.
                    counts['deferred'] += 1
            if finished:
                after, after_page = position + 1, 0
        if priority:
            cursor.update(offset=after, priority_page=after_page)
        else:
            cursor.update(after=after, page=after_page)
        cursor['turn'] += 1
        atomic_write(cursor_path, lambda stream: json.dump(cursor, stream), mode=0o600)
        return counts
    finally:
        os.close(fd)
