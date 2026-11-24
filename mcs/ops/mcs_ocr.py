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

import attachment_inference_deferral  # noqa: E402
import attachment_ocr  # noqa: E402
from ledger import Ledger  # noqa: E402
from mcs_util import DB, ProviderSettingsUnreadable, load_config  # noqa: E402
import writer_lock  # noqa: E402
from safe_io import atomic_write  # noqa: E402
from writer_lock import acquire_run_lock  # noqa: E402

DATA = str(Path(DB).parent)


DRAIN_BUDGET_S = 900
LOCAL_CALL_S = 300
START_WAIT_S = 5
_RAW_COUNTS = frozenset({'complete', 'empty', 'failed', 'unsupported', 'deferred'})
STATUS_LIMIT = 4096


def _local_defer_reasons(local, local_diagnostic):
    """Return fixed deferral counts reported by the local stage, without re-probing."""
    total = local.get('deferred')
    if type(total) is not int or total <= 0:
        return {}
    reported = local.get('defer_reasons')
    if isinstance(reported, dict):
        return attachment_inference_deferral.normalize(reported, total)
    if local_diagnostic is not None and local_diagnostic['writer_reason'] != 'writer_refusal_unclassified':
        return {'writer_refused': total}
    return {attachment_inference_deferral.UNCLASSIFIED: total}


def _drain(ledger, cfg, generation, deadline, *, live_config, retry_failed_before=None):
    reasons, observed = {}, {}
    result = _drain_stages(ledger, cfg, generation, deadline, reasons, observed,
                           live_config=live_config, retry_failed_before=retry_failed_before)
    if result.get('local_deferred', 0) > 0:
        result['local_deferred_reasons'] = attachment_inference_deferral.normalize(
            reasons, result['local_deferred'])
        kept = attachment_inference_deferral.observed(observed)
        if kept:
            result['local_llm_defer_reasons'] = kept
    return result


def _drain_stages(ledger, cfg, generation, deadline, reasons, observed, *, live_config,
                  retry_failed_before=None):
    import attachment_inference

    result = {'local_done': 0, 'local_deferred': 0}
    active = {'local': True, 'ocr': True}
    local_blocked = False
    limited = False
    while any(active.values()) and time.monotonic() < deadline - 5:
        if writer_lock.restart_intent_active(DATA):
            # A live gateway-restart request ends the drain between items;
            # the item in flight already finished and settled.
            result['reason'] = 'restart_yield'
            return result
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
            stage_diagnostic = None
            try:
                if stage == 'local':
                    import extract_llm
                    notes = extract_llm._CALL_NOTES
                    previous_reason = getattr(notes, 'defer_reason', None)
                    reason_was_set = hasattr(notes, 'defer_reason')
                    notes.defer_reason = None
                    try:
                        with writer_lock.capture_writer_refusal() as local_diagnostic:
                            stage_diagnostic = local_diagnostic
                            local = attachment_inference.run_pending(ledger, cfg, stage='local',
                                deadline=min(deadline - 5, time.monotonic() + LOCAL_CALL_S), limit=1)
                    finally:
                        observed_reason = getattr(notes, 'defer_reason', None)
                        if reason_was_set:
                            notes.defer_reason = previous_reason
                        else:
                            try:
                                del notes.defer_reason
                            except AttributeError:
                                pass
                    result['local_done'] += local['done']
                    result['local_deferred'] += local['deferred']
                    if local.get('paid_recovery_pending') is True:
                        result['paid_recovery_pending'] = True
                    active['local'] = local['done'] > 0 and local['deferred'] == 0
                    local_blocked |= local['deferred'] > 0
                    if type(local['deferred']) is int and local['deferred'] > 0:
                        for code, count in _local_defer_reasons(local, local_diagnostic).items():
                            reasons[code] = reasons.get(code, 0) + count
                        observed_code = attachment_inference_deferral.llm_reason(observed_reason)
                        if observed_code is not None:
                            observed[observed_code] = observed.get(observed_code, 0) + local['deferred']
                else:
                    with writer_lock.capture_writer_refusal() as ocr_diagnostic:
                        stage_diagnostic = ocr_diagnostic
                        raw = attachment_ocr.run_pending(ledger, cfg, limit=1, deadline=deadline - 5,
                            **({"retry_failed_before": retry_failed_before} if retry_failed_before is not None else {}))
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
                    reasons['local_stage_error'] = reasons.get('local_stage_error', 0) + 1
            if stage_diagnostic is not None and stage_diagnostic['writer_reason'] != 'writer_refusal_unclassified':
                result.update(stage_diagnostic, writer_job_stage=stage)
    if limited or any(active.values()):
        result['queue_status'] = 'partial'
    return result


_NOT_STARTED = ("busy", "gateway_restart_pending", "writer_busy", "restart_yield")


def _status_text(result, at):
    """Serialize the status record; the optional deferral breakdowns yield to the reader's size limit."""
    data = json.dumps({"at": at, **result})
    shed = {"local_deferred_reasons", "local_llm_defer_reasons"}
    if len(data.encode()) > STATUS_LIMIT and shed & result.keys():
        data = json.dumps({"at": at, **{k: v for k, v in result.items() if k not in shed}})
    return data


def run(cfg=None, *, retry_errors=False):
    """Drain saved OCR and local pages independently under a single bounded job lock."""
    if type(retry_errors) is not bool:
        raise ValueError("ocr_retry_errors_invalid")
    retry_failed_before = time.time() if retry_errors else None
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
    if writer_lock.gateway_restart_pause(DATA):
        return {'reason': 'gateway_restart_pending'}
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
            remaining = min(work_deadline - time.monotonic() - 5, START_WAIT_S)
            fd = (writer_lock.wait_run_lock(os.path.join(DATA, 'run.lock'), wait_s=remaining,
                  expected_code_stamp=generation[1],
                  marker_path=os.path.join(DATA, writer_lock.UPDATE_MARKER_NAME)) if remaining > 0 else None)
        if fd is None:
            return {'reason': ('restart_yield' if writer_lock.restart_intent_active(DATA)
                               else 'writer_busy')}
        try:
            if writer_lock.updater_state() != generation:
                return {'reason': 'writer_blocked'}
            if time.monotonic() >= work_deadline - 5:
                return {'reason': 'writer_busy'}
            ledger = Ledger(DB)
        finally:
            os.close(fd)
        result = _drain(ledger, cfg, generation, work_deadline, live_config=live_config,
                        **({"retry_failed_before": retry_failed_before} if retry_failed_before is not None else {}))
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
                result['snapshot_failure'] = {'reason': ('publish_window_exhausted'
                                                         if remaining <= 0 else 'writer_lock_wait')}
            else:
                try:
                    if writer_lock.updater_state() != generation:
                        result['reason'] = 'writer_blocked'
                    else:
                        import maintenance
                        # "deferred"（公開ロック競合・窓不足）も失敗（None）と
                        # 同じ snapshot_deferred として扱う
                        if maintenance.refresh_snapshot(DB, force=True,
                                deadline=deadline) in (None, "deferred"):
                            result['reason'] = 'snapshot_deferred'
                            failure = snapshots.last_failure()
                            timing = snapshots.last_timing()
                            if not failure and timing.get("defer_reason") \
                                    == "publication_lock_busy":
                                failure = {"reason": "snapshot_lock_busy"}
                            if failure:
                                result['snapshot_failure'] = failure
                            if timing:
                                result['snapshot_timing'] = timing
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
    parser.add_argument("--retry-errors", action="store_true",
                        help="開始時点より古いOCR失敗を、この実行内で各添付1回だけ待機時間を省いて再処理する")
    args = parser.parse_args()
    try:
        result = run(None, retry_errors=True) if args.retry_errors else run(None)
    except (OSError, ValueError, sqlite3.Error):
        result = {"reason": "local_processing_failed"}
    # A run refused before the drain observed nothing: the last real aggregate stays and its
    # age still turns stale; the refusal is reported on stdout only.
    started = not (result.keys() == {"reason"} and result["reason"] in _NOT_STARTED)
    if started and Path(DATA).is_dir():
        try:
            atomic_write(os.path.join(DATA, "attachment_ocr_status.json"),
                         lambda stream: stream.write(_status_text(result, time.time())), mode=0o600)
        except (OSError, ValueError, ProviderSettingsUnreadable):
            result["reason"] = "status_unavailable"
    print(json.dumps({"attachment_ocr": result}, ensure_ascii=False))
    # Owner 2026-11-13: deferrals beside saved progress are backlog, not a stall; a run with no progress still fails.
    progressed = bool(result.get("complete", 0) or result.get("local_done", 0))
    deferred = result.get("deferred", 0) or result.get("local_deferred", 0)
    accepted = (None, "disabled", "writer_busy", "busy", "gateway_restart_pending", "restart_yield") + (("memory_deferred",) if progressed else ())
    return 0 if (not result.get("failed", 0) and (progressed or not deferred)
                 and result.get("reason") in accepted) else 1


if __name__ == "__main__":
    raise SystemExit(main())
