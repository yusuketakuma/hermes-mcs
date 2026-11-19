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
import attachment_inference_deferral
import cloud_inference
import cloud_client
import cloud_policy
import extract_llm
import local_llm
import original_source_keys
import attachment_feedback
import patient_priority
import upgrade_scope
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


def _save(ledger, source, cfg, generation, stage, document, meta, unit=None, local=None, deadline=None):
    return attachment_extraction.save(ledger, source, cfg, stage, document, meta, unit, local,
        contract_for=_contract,
        write=lambda fn: _write(ledger, source, cfg, generation, fn, deadline=deadline))


def _local_checkpoints(ledger, source, cfg, generation, deadline=None):
    return attachment_inference_checkpoints.local_checkpoints(ledger, source, cfg,
        contract=_contract,
        write=lambda fn: _write(ledger, source, cfg, generation, fn, deadline=deadline))


class _PageDefer(ValueError):
    """Carry the proven gate code when a page-level write or guard defers."""

    def __init__(self, message, defer_reason):
        super().__init__(message)
        self.defer_reason = defer_reason


def _capture_writer_fence(fn):
    with writer_lock.capture_writer_refusal() as refusal:
        ok = fn()
    if refusal['writer_reason'] != 'writer_refusal_unclassified':
        writer_lock.record_writer_refusal(refusal['writer_reason'], refusal['writer_stage'])
    code = ('writer_refused' if refusal['writer_reason'] != 'writer_refusal_unclassified'
            else attachment_inference_deferral.UNCLASSIFIED)
    return ok, code


def _defer_result(counts, code):
    result = {**counts, 'deferred': 1}
    if code is not None:
        result['defer_reasons'] = {code: 1}
    return result


def _memory_defer_reason():
    import cloud_backlog
    import extract_llm_resident
    if extract_llm._context_memory_available is extract_llm_resident._context_memory_available:
        return attachment_inference_deferral.memory_reason(cloud_backlog.memory_headroom())
    return None if extract_llm._context_memory_available() else attachment_inference_deferral.UNCLASSIFIED


def _guard(ledger, source, cfg, generation):
    live = load_config()

    def _guard_fence():
        state = writer_lock.updater_state()
        if state != generation and not state[0]:
            writer_lock.record_writer_refusal('source_generation_changed', 'generation_fence')
        return state == generation and not generation[0] and extract_llm._writer_current(full=True)

    fence_ok, fence_code = _capture_writer_fence(_guard_fence)
    if not fence_ok:
        raise _PageDefer('attachment_inference_source_changed', fence_code)
    free = extract_llm.disk_free_mb(ledger.db)
    disk_code = attachment_inference_deferral.disk_reason(free, extract_llm.disk_floor_mb())
    if disk_code is not None:
        raise _PageDefer('attachment_inference_source_changed', disk_code)
    if (attachment_ocr.config_error(live)
            or not live.get('attachment_ocr', {}).get('enabled', False)
            or original_source_keys.binding(ledger.db, source['project_id'], source['source_key'])
            != source['_source_binding'] or _contract(cfg) != _contract(live)
            or attachment_feedback.revision(ledger.db, source['project_id'], source['source_key'],
                                            source['_source_binding']) != source.get('_feedback_revision', 0)):
        raise ValueError('attachment_inference_source_changed')


_WRITE_LOCK_WAIT_S = 60


def _write(ledger, source, cfg, generation, fn, *, result=False, deadline=None):
    root = attachment_ocr._data_root(ledger)
    wait_s = _WRITE_LOCK_WAIT_S
    if deadline is not None:
        wait_s = min(wait_s, max(deadline - time.monotonic() - 1.0, 0.0))
    fd = None

    def _lock():
        nonlocal fd
        fd = writer_lock.wait_run_lock(str(root / 'run.lock'), wait_s=wait_s,
            expected_code_stamp=generation[1], marker_path=str(root / writer_lock.UPDATE_MARKER_NAME))
        return fd is not None

    locked, lock_code = _capture_writer_fence(_lock)
    if not locked:
        raise _PageDefer('attachment_inference_writer_busy', lock_code)
    try:
        def _post_lock_fence():
            state = writer_lock.updater_state()
            if state != generation and not state[0]:
                writer_lock.record_writer_refusal('source_generation_changed', 'generation_fence')
            return state == generation and not generation[0]

        fence_ok, fence_code = _capture_writer_fence(_post_lock_fence)
        if not fence_ok:
            raise _PageDefer('attachment_inference_writer_changed', fence_code)
        if not result:
            _guard(ledger, source, cfg, generation)
        return fn()
    finally:
        os.close(fd)


def _dispatcher(ledger, source, cfg, generation, deadline=None):
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
        return call() if depth else _write(ledger, source, cfg, generation, call, result=result,
                                           deadline=deadline)
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


def _recover_paid_results(ledger, generation, deadline=None):
    """Replay bounded accounting journals before any new inference dispatch."""
    import attachment_paid_results as journal
    try:
        paths = journal.pending(attachment_ocr._data_root(ledger))
        if not paths:
            return True
        _write(ledger, None, None, generation, lambda: journal.replay(ledger, paths, load_config()),
               result=True, deadline=deadline)
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
        return _defer_result(counts, attachment_inference_deferral.UNCLASSIFIED if stage == 'local' else None)
    generation = None

    def _admit_writer():
        nonlocal generation
        generation = writer_lock.updater_state()
        return not generation[0] and extract_llm._writer_current(full=True)

    writer_ok, writer_code = _capture_writer_fence(_admit_writer)
    if not writer_ok:
        return _defer_result(counts, writer_code if stage == 'local' else None)
    if stage == 'local':
        with writer_lock.capture_writer_refusal():
            recovered = _recover_paid_results(ledger, generation, deadline=deadline)
    else:
        recovered = _recover_paid_results(ledger, generation, deadline=deadline)
    if not recovered:
        if stage == 'cloud':
            return {**counts, 'deferred': 1}
        counts['paid_recovery_pending'] = True
    free = extract_llm.disk_free_mb(ledger.db)
    disk_code = attachment_inference_deferral.disk_reason(free, extract_llm.disk_floor_mb())
    if disk_code is not None:
        return _defer_result(counts, disk_code if stage == 'local' else None)
    memory_code = _memory_defer_reason()
    if memory_code is not None:
        return _defer_result(counts, memory_code if stage == 'local' else None)
    root = attachment_ocr._data_root(ledger)
    scope = upgrade_scope.resolve(cfg)
    fd = writer_lock.acquire_run_lock(str(root / ('attachment_inference_' + stage + '.lock')))
    if fd is None:
        return _defer_result(counts, 'inference_lock_busy' if stage == 'local' else None)
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
        defer_reasons = {}
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
                if stage == 'local' and _stored(ledger.db, row['project_id'], key, 'cloud', cfg) is not None:
                    continue  # a current cloud result does not depend on the local model
                source['_feedback_revision'] = attachment_feedback.revision(
                    ledger.db, row['project_id'], key, source['_source_binding'])
                if not upgrade_scope.attachment_admitted(ledger.db, scope, row['project_id'], key,
                        source['_source_binding'], _contract(cfg), source['_feedback_revision']):
                    continue  # a closed upgrade scope keeps this earlier-contract page
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
                            dispatch=_dispatcher(ledger, source, cfg, generation, deadline=deadline),
                            input_binding=_input_binding(source, local),
                            owner={'source_type': 'attachment_ocr', 'source_key': key,
                                   'local_artifact_id': local['artifact_id']})
                        _journal_unit(unit, source, cfg)
                    meta = {}
                    checkpoints = (_local_checkpoints(ledger, source, cfg, generation, deadline=deadline)
                                   if stage == 'local' else {})
                    routing = local_llm.background_scope(root) if stage == 'local' else nullcontext()
                    with routing, extract_llm.pin_target(local_llm.resolve(cfg)):
                        result = extract_llm.llm_extract(source['body_text'], deadline=deadline, meta_out=meta,
                                                       posted_at=None, provider=unit,
                                                       feedback=attachment_feedback.notes(ledger.db, source), **checkpoints)
                    if (not isinstance(result, dict) or meta.get('_quality_hold')
                            or not _save(ledger, source, cfg, generation, stage, result, meta, unit, local,
                                     deadline=deadline)):
                        counts['deferred'] += 1
                        if stage == 'local':
                            defer_reasons[attachment_inference_deferral.UNCLASSIFIED] = (
                                defer_reasons.get(attachment_inference_deferral.UNCLASSIFIED, 0) + 1)
                    else:
                        counts['done'] += 1
                except (cloud_inference.CloudFallback, cloud_client.CloudNotSent, OSError, ValueError, sqlite3.Error) as err:
                    # Paid response uncertainty is retained by the shared unit; never local-fallback or discard.
                    counts['deferred'] += 1
                    if stage == 'local':
                        code = getattr(err, 'defer_reason', attachment_inference_deferral.UNCLASSIFIED)
                        defer_reasons[code] = defer_reasons.get(code, 0) + 1
            if finished:
                after, after_page = position + 1, 0
        if priority:
            cursor.update(offset=after, priority_page=after_page)
        else:
            cursor.update(after=after, page=after_page)
        cursor['turn'] += 1
        atomic_write(cursor_path, lambda stream: json.dump(cursor, stream), mode=0o600)
        if stage == 'local' and counts['deferred'] > 0:
            counts['defer_reasons'] = attachment_inference_deferral.normalize(
                defer_reasons, counts['deferred'])
        return counts
    finally:
        os.close(fd)
