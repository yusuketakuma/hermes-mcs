"""添付の文字起こしを独立して処理し、チャット原文とは分けて保存する。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401

import attachment_ocr  # noqa: E402
from ledger import Ledger  # noqa: E402
from mcs_util import DB, ProviderSettingsUnreadable, load_config  # noqa: E402
import writer_lock  # noqa: E402
from safe_io import atomic_write  # noqa: E402
from writer_lock import acquire_run_lock  # noqa: E402

DATA = str(Path(DB).parent)


DRAIN_BUDGET_S = 900
LOCAL_CALL_S = 300
_RAW_COUNTS = frozenset({'complete', 'empty', 'failed', 'unsupported', 'deferred'})


def _drain(ledger, cfg, generation, deadline, *, live_config):
    import attachment_inference

    result = {'local_done': 0, 'local_deferred': 0}
    active = {'local': True, 'ocr': True}
    local_blocked = False
    limited = False
    while any(active.values()) and time.monotonic() < deadline - 5:
        for stage in ('local', 'local', 'local', 'ocr'):
            if not active[stage] or time.monotonic() >= deadline - 5:
                continue
            if writer_lock.updater_state() != generation:
                result['reason'] = 'writer_blocked'
                return result
            if live_config:
                try:
                    cfg = load_config()
                except (OSError, ValueError, ProviderSettingsUnreadable):
                    result['reason'] = 'config_invalid'
                    return result
                if attachment_ocr.config_error(cfg):
                    result['reason'] = 'config_invalid'
                    return result
                if not cfg.get('attachment_ocr', {}).get('enabled', False):
                    result['reason'] = 'disabled'
                    return result
            remaining = deadline - 5 - time.monotonic()
            raw_budget = cfg['attachment_ocr'].get('timeout_seconds', 120) + 5
            if stage == 'local' and active['ocr'] and raw_budget <= remaining < raw_budget + LOCAL_CALL_S:
                continue
            if stage == 'ocr' and remaining < raw_budget:
                active['ocr'], limited = False, True
                continue
            diagnostic = None
            try:
                if stage == 'local':
                    with writer_lock.capture_writer_refusal() as diagnostic:
                        local = attachment_inference.run_pending(ledger, cfg, stage='local',
                            deadline=min(deadline - 5, time.monotonic() + LOCAL_CALL_S), limit=1)
                    result['local_done'] += local['done']
                    result['local_deferred'] += local['deferred']
                    if local.get('paid_recovery_pending') is True:
                        result['paid_recovery_pending'] = True
                    active['local'] = local['done'] > 0 and local['deferred'] == 0
                    local_blocked |= local['deferred'] > 0
                else:
                    with writer_lock.capture_writer_refusal() as diagnostic:
                        raw = attachment_ocr.run_pending(ledger, cfg, limit=1, deadline=deadline - 5)
                    for key, value in raw.items():
                        if key == 'reason' and value is None and result.get('reason') is not None:
                            continue
                        result[key] = result.get(key, 0) + value if key in _RAW_COUNTS else value
                    active['ocr'] = (any(raw.get(key, 0) for key in ('complete', 'empty', 'failed', 'unsupported'))
                                     and not raw.get('deferred') and raw.get('reason') is None)
                    if raw.get('complete') and not local_blocked:
                        active['local'] = True
            except Exception:
                result['reason'] = 'local_processing_failed'
                key = 'local_deferred' if stage == 'local' else 'failed'
                result[key] = result.get(key, 0) + 1
                active[stage] = False
                if stage == 'local':
                    local_blocked = True
            if diagnostic is not None and diagnostic['writer_reason'] != 'writer_refusal_unclassified':
                result.update(diagnostic, writer_job_stage=stage)
    if limited or any(active.values()):
        result['queue_status'] = 'partial'
    return result


def run(cfg=None):
    """Drain saved OCR and local pages independently under a single bounded job lock."""
    live_config = cfg is None
    try:
        cfg = load_config() if live_config else cfg
    except (OSError, ValueError, ProviderSettingsUnreadable):
        return {'reason': 'config_invalid'}
    if attachment_ocr.config_error(cfg):
        return {'reason': 'config_invalid'}
    if not cfg.get('attachment_ocr', {}).get('enabled', False):
        return {'reason': 'disabled'}
    if not Path(DB).is_file():
        return {'reason': 'database_unavailable'}
    generation = writer_lock.updater_state()
    if generation[0]:
        return {'reason': 'writer_blocked'}
    job = acquire_run_lock(os.path.join(DATA, 'attachment_ocr_job.lock'))
    if job is None:
        return {'reason': 'busy'}
    ledger = None
    deadline = time.monotonic() + DRAIN_BUDGET_S
    try:
        import snapshots
        # Work yields the size-based verify budget plus the fixed tail so a
        # needed snapshot publish still fits inside the same absolute deadline.
        work_deadline = snapshots.snapshot_work_deadline(deadline, DB)
        fd = acquire_run_lock(os.path.join(DATA, 'run.lock'))
        if fd is None:
            remaining = work_deadline - time.monotonic() - 5
            fd = (writer_lock.wait_run_lock(os.path.join(DATA, 'run.lock'), wait_s=remaining,
                  expected_code_stamp=generation[1],
                  marker_path=os.path.join(DATA, writer_lock.UPDATE_MARKER_NAME)) if remaining > 0 else None)
        if fd is None:
            return {'reason': 'writer_busy'}
        try:
            if writer_lock.updater_state() != generation:
                return {'reason': 'writer_blocked'}
            if time.monotonic() >= work_deadline - 5:
                return {'reason': 'writer_busy'}
            ledger = Ledger(DB)
        finally:
            os.close(fd)
        result = _drain(ledger, cfg, generation, work_deadline, live_config=live_config)
        if result.get('reason') != 'config_invalid' and (
                result.get('complete', 0) or result.get('empty', 0) or result['local_done']):
            now = time.monotonic()
            remaining = min(5, deadline - now)
            if snapshots.snapshot_work_deadline(deadline, DB) <= now:
                remaining = 0
            fd = (writer_lock.wait_run_lock(os.path.join(DATA, 'run.lock'), wait_s=remaining,
                  expected_code_stamp=generation[1],
                  marker_path=os.path.join(DATA, writer_lock.UPDATE_MARKER_NAME)) if remaining > 0 else None)
            if fd is None:
                result['reason'] = 'snapshot_deferred'
            else:
                try:
                    if writer_lock.updater_state() != generation:
                        result['reason'] = 'writer_blocked'
                    else:
                        import maintenance
                        if maintenance.refresh_snapshot(DB, force=True,
                                deadline=deadline) is None:
                            result['reason'] = 'snapshot_deferred'
                finally:
                    os.close(fd)
        return result
    finally:
        if ledger is not None:
            ledger.close()
        os.close(job)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True,
                        help="設定で有効にした添付OCRと保存済みページの解析を期限内で進める")
    parser.parse_args()
    try:
        result = run(None)
    except (OSError, ValueError, sqlite3.Error):
        result = {"reason": "local_processing_failed"}
    if result != {"reason": "busy"} and Path(DATA).is_dir():
        try:
            atomic_write(os.path.join(DATA, "attachment_ocr_status.json"),
                         lambda stream: json.dump({"at": time.time(), **result}, stream), mode=0o600)
        except (OSError, ValueError, ProviderSettingsUnreadable):
            result["reason"] = "status_unavailable"
    print(json.dumps({"attachment_ocr": result}, ensure_ascii=False))
    return 0 if (not result.get("failed", 0) and not result.get("deferred", 0) and not result.get("local_deferred", 0)
                 and result.get("reason") in (None, "disabled", "writer_busy", "busy")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
