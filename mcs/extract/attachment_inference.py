"""Infer clinical facts from retained OCR pages without changing their parent chat."""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sqlite3
import time

import attachment_ocr
import attachment_extraction
import attachment_inference_checkpoints
import cloud_inference
import cloud_client
import cloud_policy
import extract_llm
import local_llm
import original_source_keys
import attachment_feedback
import patient_priority
import writer_lock
from mcs_util import load_config
from mcs_util import payload_hash as payload_hash

KIND = attachment_extraction.KIND
SCHEMA = attachment_extraction.SCHEMA
_CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _contract(cfg):
    return attachment_extraction.contract(cfg, code_digest=_CODE)


def _stored(db, pid, key, stage, cfg):
    return attachment_extraction.stored(db, pid, key, stage, cfg, contract_for=_contract)


def current(db, pid, source_key, *, cfg=None):
    """Compatibility entry for source-bound results; new readers use attachment_extraction."""
    return attachment_extraction.current(db, pid, source_key, cfg=load_config() if cfg is None else cfg)


_requirement = attachment_extraction.requirement
_validated = attachment_extraction.validated
_history_snapshot = attachment_extraction.history_snapshot
_input_binding = attachment_extraction.input_binding


def _save(ledger, source, cfg, generation, stage, document, meta, unit=None, local=None):
    return attachment_extraction.save(ledger, source, cfg, stage, document, meta, unit, local,
        contract_for=_contract, write=lambda fn: _write(ledger, source, cfg, generation, fn))


def _local_checkpoints(ledger, source, cfg, generation):
    return attachment_inference_checkpoints.local_checkpoints(ledger, source, cfg,
        contract=_contract, write=lambda fn: _write(ledger, source, cfg, generation, fn))


def _guard(ledger, source, cfg, generation):
    live = load_config()
    free = extract_llm.disk_free_mb(ledger.db)
    if (writer_lock.updater_state() != generation or generation[0]
            or not extract_llm._writer_current(full=True)
            or free is None or free < extract_llm.disk_floor_mb()
            or attachment_ocr.config_error(live)
            or not live.get('attachment_ocr', {}).get('enabled', False)
            or original_source_keys.binding(ledger.db, source['project_id'], source['source_key'])
            != source['_source_binding'] or _contract(cfg) != _contract(live)
            or attachment_feedback.revision(ledger.db, source['project_id'], source['source_key'],
                                            source['_source_binding']) != source.get('_feedback_revision', 0)):
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
    local = _stored(unit.ledger.db, source['project_id'], source['source_key'], 'local', cfg)
    if local is None or local['artifact_id'] != unit.owner['local_artifact_id']:
        raise ValueError('attachment_paid_source_binding_invalid')
    origin = {'project_id': source['project_id'], 'source_key': source['source_key'],
              'source_binding': source['_source_binding'], 'contract': local['contract'],
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


_CHECKPOINT_KIND = attachment_inference_checkpoints.KIND
_CHECKPOINT_SCHEMA = attachment_inference_checkpoints.SCHEMA


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
        if stage == 'cloud':
            return {**counts, 'deferred': 1}
        counts['paid_recovery_pending'] = True
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
                source['_feedback_revision'] = attachment_feedback.revision(
                    ledger.db, row['project_id'], key, source['_source_binding'])
                if used >= limit or time.monotonic() >= deadline:
                    finished = False
                    break
                local = _stored(ledger.db, row['project_id'], key, 'local', cfg)
                if stage == 'cloud' and local is None:
                    continue
                # A closed send route retires new sends, never the unit a prior
                # reservation created: owned pages still reach recovery or hold
                # unless the config itself declares the scope revoked.
                if stage == 'cloud' and _requirement(cfg, row['project_id'])[0] is not True and not (
                        cloud_policy.owns_unit_request(ledger.db, route='reextract',
                            project_id=row['project_id'], unit_key=key + ':cloud')
                        and not cloud_policy.scope_revoked(cfg, 'reextract', row['project_id'])):
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
                    routing = local_llm.background_scope(root) if stage == 'local' else nullcontext()
                    with routing, extract_llm.pin_target(local_llm.resolve(cfg)):
                        result = extract_llm.llm_extract(source['body_text'], deadline=deadline, meta_out=meta,
                                                       posted_at=None, provider=unit,
                                                       feedback=attachment_feedback.notes(ledger.db, source), **checkpoints)
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
