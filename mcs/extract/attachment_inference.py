"""Infer clinical facts from retained OCR pages without changing their parent chat."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import attachment_ocr
import attachment_extraction
import attachment_inference_checkpoints
import cloud_client
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


def _stored(db, pid, key, cfg):
    return attachment_extraction.stored(db, pid, key, 'local', cfg, contract_for=_contract)


def _save(ledger, source, cfg, generation, document, meta, deadline=None):
    return attachment_extraction.save(ledger, source, cfg, document, meta,
        contract_for=_contract,
        write=lambda fn: _write(ledger, source, cfg, generation, fn, deadline=deadline))


def _local_checkpoints(ledger, source, cfg, generation, deadline=None):
    return attachment_inference_checkpoints.local_checkpoints(ledger, source, cfg,
        contract=_contract,
        write=lambda fn: _write(ledger, source, cfg, generation, fn, deadline=deadline))


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


_WRITE_LOCK_WAIT_S = 60


def _write(ledger, source, cfg, generation, fn, *, result=False, deadline=None):
    root = attachment_ocr._data_root(ledger)
    wait_s = _WRITE_LOCK_WAIT_S
    if deadline is not None:
        wait_s = min(wait_s, max(deadline - time.monotonic() - 1.0, 0.0))
    fd = writer_lock.wait_run_lock(str(root / 'run.lock'), wait_s=wait_s,
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


def run_pending(ledger, cfg, *, deadline=None, limit=1):
    """Infer bounded OCR pages locally after replaying any retained paid-response journals."""
    counts = {'done': 0, 'deferred': 0}
    if type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError('attachment_inference_args')
    if attachment_ocr.config_error(cfg) or not cfg.get('attachment_ocr', {}).get('enabled', False):
        return counts
    deadline = min(deadline or time.monotonic() + 120, time.monotonic() + 900)
    if ledger.db.in_transaction:
        return {**counts, 'deferred': 1}
    generation = writer_lock.updater_state()
    if generation[0] or not extract_llm._writer_current(full=True):
        return {**counts, 'deferred': 1}
    if not _recover_paid_results(ledger, generation, deadline=deadline):
        counts['paid_recovery_pending'] = True
    free = extract_llm.disk_free_mb(ledger.db)
    if free is None or free < extract_llm.disk_floor_mb():
        return {**counts, 'deferred': 1}
    if not extract_llm._context_memory_available():
        return {**counts, 'deferred': 1}
    root = attachment_ocr._data_root(ledger)
    fd = writer_lock.acquire_run_lock(str(root / 'attachment_inference_local.lock'))
    if fd is None:
        return {**counts, 'deferred': 1}
    try:
        # Alternate recent active patients with a keyset history lane.
        from safe_io import atomic_write, read_json_nofollow
        cursor_path = str(root / 'attachment_inference_local_cursor.json')
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
                if source is None or _stored(ledger.db, row['project_id'], key, cfg) is not None:
                    continue
                source['_feedback_revision'] = attachment_feedback.revision(
                    ledger.db, row['project_id'], key, source['_source_binding'])
                if used >= limit or time.monotonic() >= deadline:
                    finished = False
                    break
                used += 1
                after, after_page = position, page['page']
                try:
                    meta = {}
                    checkpoints = _local_checkpoints(ledger, source, cfg, generation, deadline=deadline)
                    with local_llm.background_scope(root), extract_llm.pin_target(local_llm.resolve(cfg)):
                        result = extract_llm.llm_extract(source['body_text'], deadline=deadline, meta_out=meta,
                                                       posted_at=None,
                                                       feedback=attachment_feedback.notes(ledger.db, source), **checkpoints)
                    if (not isinstance(result, dict) or meta.get('_quality_hold')
                            or not _save(ledger, source, cfg, generation, result, meta, deadline=deadline)):
                        counts['deferred'] += 1
                    else:
                        counts['done'] += 1
                except (OSError, ValueError, sqlite3.Error):
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
