"""Private synthetic OCR status is observed independently from collection health."""
import json
import os

import pytest

import health_report
import health_watch
from ledger import Ledger


CFG = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"},
       "backup": {"enabled": False}}
WRITER_DIAGNOSTIC = {'writer_reason': 'run_lock_not_acquired', 'writer_stage': 'write_lock',
                     'writer_job_stage': 'ocr'}


def _status(data, **updates):
    data.mkdir(parents=True, exist_ok=True)
    value = {"at": 1000, "complete": 0, "empty": 0, "failed": 0, "unsupported": 0, "deferred": 0, "pending_failed": 0, "queue_status": "known",
             **updates}
    path = data / "attachment_ocr_status.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def _health(home, *, at=1000, overall="ok", reasons=()):
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "health.json").write_text(json.dumps({
        "at": at, "overall": overall, "state_reasons": list(reasons)}))


def test_disabled_never_reads_status_and_unknown_config_is_not_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(health_report.safe_io, "read_private_bytes", lambda *args, **kwargs:
                        pytest.fail("disabled OCR read status"))
    assert health_report.attachment_ocr_health(str(tmp_path), {}, 1000) == {"state": "disabled"}
    assert health_report.attachment_ocr_health(str(tmp_path), None, 1000)["state"] == "unknown"


@pytest.mark.parametrize("kind,reason", [
    ("missing", "status_missing"), ("invalid", "status_corrupt"),
    ("oversized", "status_corrupt"), ("public", "status_corrupt"),
    ("symlink", "status_corrupt"), ("fifo", "status_corrupt"),
    ("future", "status_corrupt"), ("bad_count", "status_corrupt"),
    ("incomplete", "status_corrupt"), ("private_reason", "status_corrupt"),
    ("stale", "status_stale"), ("failed", "processing_failed"),
    ("deferred", "processing_deferred"), ("runtime", "processing_failed"),
])
def test_invalid_or_unhealthy_evidence_never_becomes_healthy_zero(tmp_path, kind, reason):
    data = tmp_path / "data"
    path = _status(data)
    if kind == "missing":
        path.unlink()
    elif kind == "invalid":
        path.write_text("not json synthetic private bytes")
    elif kind == "oversized":
        path.write_bytes(b" " * 4097)
    elif kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        path.unlink()
        path.symlink_to(data / "synthetic-target")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    else:
        updates = {"future": {"at": 1001}, "bad_count": {"failed": True},
                   "private_reason": {"reason": "synthetic-private-path"},
                   "stale": {"at": 0}, "failed": {"failed": 1},
                   "deferred": {"deferred": 1}, "runtime": {"reason": "runtime_unavailable"}}
        if kind == "incomplete":
            path.write_text(json.dumps({"at": 1000}))
        else:
            _status(data, **updates[kind])
    report = health_report.attachment_ocr_health(str(data), CFG, 3000 if kind == "stale" else 1000)
    assert report["state"] not in ("ok", "disabled")
    assert report["reason"] == "attachment_ocr_" + reason
    assert "synthetic-private" not in json.dumps(report)


def test_health_producer_includes_ocr_failure_and_degrades(tmp_path, monkeypatch):
    _status(tmp_path, failed=1)
    monkeypatch.setattr(health_report.time, "time", lambda: 1000)
    monkeypatch.setattr(health_report, "HEALTH_FILE", str(tmp_path / "missing.json"))
    ledger = Ledger(str(tmp_path / "ledger.db"))
    try:
        report = health_report.build_health(ledger.db, {"errors": []}, "ok", cfg=CFG)
        assert report["overall"] == "degraded"
        assert report["attachment_ocr"]["state"] == "failed"
        assert "attachment_ocr_processing_failed" in report["state_reasons"]
    finally:
        ledger.close()


def test_live_watcher_detects_missing_then_failure_then_recovery_with_dedup(tmp_path):
    _health(tmp_path)
    missing = health_watch.evaluate(str(tmp_path), 1000, cfg=CFG)
    assert missing["status"] == "degraded" and missing["alert"]
    assert missing["attachment_ocr"]["state"] == "unknown"
    assert not health_watch.evaluate(str(tmp_path), 1001, cfg=CFG)["alert"]
    _status(tmp_path / "data", failed=1)
    failed = health_watch.evaluate(str(tmp_path), 1002, cfg=CFG)
    assert failed["alert"] and "attachment_ocr_processing_failed" in failed["state_reasons"]
    assert "添付の文字起こし" in "\n".join(health_watch._reason_rows(failed, failed["state_reasons"]))
    _status(tmp_path / "data", at=1003)
    recovered = health_watch.evaluate(str(tmp_path), 1003, cfg=CFG)
    assert recovered["status"] == "ok" and not recovered["alert"]


def test_live_healthy_or_disabled_replaces_recorded_ocr_failure(tmp_path):
    _health(tmp_path, overall="degraded", reasons=["attachment_ocr_processing_failed"])
    _status(tmp_path / "data")
    for cfg in (CFG, {}):
        report = health_watch.evaluate(str(tmp_path), 1000, cfg=cfg)
        assert report["status"] == "ok" and report["state_reasons"] == []


def test_live_ocr_change_alerts_even_when_collection_health_is_stale(tmp_path):
    _health(tmp_path, at=0)
    _status(tmp_path / "data", at=10000)
    first = health_watch.evaluate(str(tmp_path), 10000, cfg=CFG)
    assert first["status"] == "stale"
    _status(tmp_path / "data", at=10001, failed=1)
    next_report = health_watch.evaluate(str(tmp_path), 10001, cfg=CFG)
    assert next_report["status"] == "stale" and next_report["alert"]
    assert "attachment_ocr_processing_failed" in next_report["state_reasons"]


def test_unobserved_queue_is_unknown_and_backoff_failure_remains_failed(tmp_path):
    for updates, state, reason in (
            ({"queue_status": "partial"}, "unknown", "attachment_ocr_queue_unverified"),
            ({"pending_failed": 1}, "failed", "attachment_ocr_processing_failed")):
        _status(tmp_path, **updates)
        report = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
        assert report["state"] == state and report["reason"] == reason


def test_failure_diagnostics_preserve_unknown_reasons_and_partial_observation(tmp_path):
    diagnostic = {"failure_reason_counts": {"output_schema_table_overlap": 2, "timeout": 1},
                  "exhausted_failed": 2}
    _status(tmp_path, pending_failed=4, queue_status="partial")
    prior = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert prior["state"] == "failed"
    assert "failure_reason_counts" not in prior and "exhausted_failed" not in prior
    _status(tmp_path, pending_failed=4, queue_status="partial", **diagnostic)
    assert health_report.attachment_ocr_health(str(tmp_path), CFG, 1000) == {**prior, **diagnostic}


@pytest.mark.parametrize("updates,now", [
    ({}, 1000), ({"queue_status": "partial"}, 1000),
    ({"paid_recovery_pending": True, "local_done": 1}, 1000),
    ({"reason": "memory_deferred", "deferred": 1}, 1000),
    ({"local_deferred": 1}, 1000), ({"at": 0}, 3000),
])
def test_empty_failure_diagnostics_do_not_override_health_outcome(tmp_path, updates, now):
    _status(tmp_path, **updates)
    prior = health_report.attachment_ocr_health(str(tmp_path), CFG, now)
    diagnostic = {"failure_reason_counts": {}, "exhausted_failed": 0}
    _status(tmp_path, **updates, **diagnostic)
    assert health_report.attachment_ocr_health(str(tmp_path), CFG, now) == {**prior, **diagnostic}


@pytest.mark.parametrize("diagnostic", [
    {"failure_reason_counts": {}}, {"exhausted_failed": 0},
    {"failure_reason_counts": None, "exhausted_failed": 0},
    {"failure_reason_counts": [], "exhausted_failed": 0},
    {"failure_reason_counts": {"synthetic_private_reason": 1}, "exhausted_failed": 0},
    {"failure_reason_counts": {"timeout": True}, "exhausted_failed": 0},
    {"failure_reason_counts": {"timeout": 1.0}, "exhausted_failed": 0},
    {"failure_reason_counts": {"timeout": -1}, "exhausted_failed": 0},
    {"failure_reason_counts": {"timeout": 2, "engine_failed": 1}, "exhausted_failed": 0},
    {"failure_reason_counts": {}, "exhausted_failed": True},
    {"failure_reason_counts": {}, "exhausted_failed": 1.0},
    {"failure_reason_counts": {}, "exhausted_failed": -1},
    {"failure_reason_counts": {}, "exhausted_failed": 3},
])
def test_malformed_failure_diagnostics_are_corrupt_and_never_exposed(tmp_path, diagnostic):
    _status(tmp_path, pending_failed=2, **diagnostic)
    assert health_report.attachment_ocr_health(str(tmp_path), CFG, 1000) == {
        "state": "unknown", "reason": "attachment_ocr_status_corrupt"}


def test_failure_diagnostics_require_observed_pending_count(tmp_path):
    path = _status(tmp_path, failure_reason_counts={}, exhausted_failed=0)
    value = json.loads(path.read_text())
    del value["pending_failed"]
    path.write_text(json.dumps(value))
    assert health_report.attachment_ocr_health(str(tmp_path), CFG, 1000) == {
        "state": "unknown", "reason": "attachment_ocr_status_corrupt"}


@pytest.mark.parametrize("reason_count,exhausted", [(257, 0), (0, 257)])
def test_failure_diagnostics_cannot_exceed_queue_observation_limit(tmp_path, reason_count, exhausted):
    _status(tmp_path, pending_failed=257, failure_reason_counts={"timeout": reason_count},
            exhausted_failed=exhausted)
    assert health_report.attachment_ocr_health(str(tmp_path), CFG, 1000) == {
        "state": "unknown", "reason": "attachment_ocr_status_corrupt"}


def test_failure_diagnostics_accept_the_full_observation_limit(tmp_path):
    diagnostic = {"failure_reason_counts": {"timeout": 256}, "exhausted_failed": 256}
    _status(tmp_path, pending_failed=256, queue_status="partial", **diagnostic)
    report = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert report["state"] == "failed" and report["pending_failed"] == 256
    assert {key: report[key] for key in diagnostic} == diagnostic


def test_drain_keeps_last_failure_snapshot_without_summing_it(tmp_path, monkeypatch):
    import attachment_inference
    import mcs_ocr
    import writer_lock

    generation, calls = (False, 1), []
    diagnostic = {"failure_reason_counts": {"output_schema_table_overlap": 1},
                  "exhausted_failed": 1}

    def raw(*args, **kwargs):
        calls.append(1)
        return {"complete": 0, "empty": 0, "failed": 1 if len(calls) == 1 else 0,
                "unsupported": 0, "deferred": 0, "pending_failed": 1,
                "queue_status": "partial", **diagnostic}

    monkeypatch.setattr(writer_lock, "updater_state", lambda: generation)
    monkeypatch.setattr(mcs_ocr.time, "monotonic", lambda: 1000)
    monkeypatch.setattr(attachment_inference, "run_pending",
                        lambda *args, **kwargs: {"done": 0, "deferred": 0})
    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending", raw)
    result = mcs_ocr._drain(None, CFG, generation, 1600, live_config=False)
    assert len(calls) == 2 and result["failed"] == 1 and result["queue_status"] == "partial"
    assert {key: result[key] for key in diagnostic} == diagnostic
    _status(tmp_path, **result)
    report = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert report["state"] == "failed" and report["pending_failed"] == 1
    assert {key: report[key] for key in diagnostic} == diagnostic


@pytest.mark.parametrize("local_done,local_deferred,state", [(1, 0, "ok"), (0, 1, "deferred")])
def test_local_inference_counts_are_valid_and_not_ocr_failure(tmp_path, local_done, local_deferred, state):
    _status(tmp_path, local_done=local_done, local_deferred=local_deferred)
    report = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert report["state"] == state and report["failed"] == 0
    assert report["local_done"] == local_done and report["local_deferred"] == local_deferred
    if local_deferred:
        assert report["reason"] == "attachment_ocr_local_inference_deferred"
        text = "\n".join(health_watch._reason_rows(report, [report["reason"]]))
        assert "ローカル解析" in text


def test_cloud_accounting_hold_is_visible_while_local_progresses(tmp_path):
    _status(tmp_path, local_done=2, local_deferred=0, paid_recovery_pending=True)
    report = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert report['state'] == 'deferred' and report['local_done'] == 2
    assert report['reason'] == 'attachment_ocr_accounting_deferred'
    text = '\n'.join(health_watch._reason_rows(report, [report['reason']]))
    assert '会計' in text and 'ローカル解析は継続' in text and '再送しません' in text


@pytest.mark.parametrize('invalid', [1, 'true', None, []])
def test_paid_recovery_marker_requires_explicit_boolean(tmp_path, invalid):
    _status(tmp_path, paid_recovery_pending=invalid)
    report = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert report['reason'] == 'attachment_ocr_status_corrupt'


def test_memory_admission_hold_is_not_a_processing_failure(tmp_path):
    _status(tmp_path, reason='memory_deferred', deferred=1)
    report = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert report['state'] == 'deferred' and report['failed'] == 0
    assert report['reason'] == 'attachment_ocr_memory_deferred'


@pytest.mark.parametrize('job_stage', ['local', 'ocr'])
def test_drain_writer_refusal_remains_valid_after_success(tmp_path, monkeypatch, job_stage):
    import attachment_inference
    import mcs_ocr
    import writer_lock

    generation = (False, 1)
    calls = {'local': 0, 'ocr': 0}

    def completed(stage):
        calls[stage] += 1
        if stage == job_stage and calls[stage] == 1:
            writer_lock.record_writer_refusal('run_lock_not_acquired', 'write_lock')
            return 1
        return 0

    monkeypatch.setattr(writer_lock, 'updater_state', lambda: generation)
    monkeypatch.setattr(mcs_ocr.time, 'monotonic', lambda: 1000)
    monkeypatch.setattr(attachment_inference, 'run_pending',
                        lambda *args, **kwargs: {'done': completed('local'), 'deferred': 0})
    monkeypatch.setattr(mcs_ocr.attachment_ocr, 'run_pending', lambda *args, **kwargs: {
        'complete': completed('ocr'), 'empty': 0, 'failed': 0, 'unsupported': 0, 'deferred': 0})
    result = mcs_ocr._drain(None, CFG, generation, 1600, live_config=False)
    diagnostic = {'writer_reason': 'run_lock_not_acquired', 'writer_stage': 'write_lock',
                  'writer_job_stage': job_stage}
    assert calls[job_stage] == 2
    assert {key: result[key] for key in diagnostic} == diagnostic
    assert result['local_done'] == (job_stage == 'local')
    assert result['complete'] == (job_stage == 'ocr')
    _status(tmp_path, **result)
    report = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert report['state'] == 'ok'
    assert {key: report[key] for key in diagnostic} == diagnostic
    assert report['local_done'] == result['local_done']
    assert report['complete'] == result['complete']
    assert report['failed'] == report['deferred'] == report['local_deferred'] == 0


@pytest.mark.parametrize('fields', [
    ('writer_reason',), ('writer_stage',), ('writer_job_stage',),
    ('writer_reason', 'writer_stage'), ('writer_reason', 'writer_job_stage'),
    ('writer_stage', 'writer_job_stage'),
])
def test_partial_writer_diagnostic_is_corrupt(tmp_path, fields):
    _status(tmp_path, **{key: WRITER_DIAGNOSTIC[key] for key in fields})
    assert health_report.attachment_ocr_health(str(tmp_path), CFG, 1000) == {
        'state': 'unknown', 'reason': 'attachment_ocr_status_corrupt'}


@pytest.mark.parametrize('field', tuple(WRITER_DIAGNOSTIC))
@pytest.mark.parametrize('invalid', [None, 1, []])
def test_writer_diagnostic_requires_strings(tmp_path, field, invalid):
    _status(tmp_path, **{**WRITER_DIAGNOSTIC, field: invalid})
    assert health_report.attachment_ocr_health(str(tmp_path), CFG, 1000) == {
        'state': 'unknown', 'reason': 'attachment_ocr_status_corrupt'}


@pytest.mark.parametrize('updates', [
    {'writer_reason': 'synthetic-private-reason'},
    {'writer_stage': 'synthetic-private-stage'},
    {'writer_job_stage': 'synthetic-private-job'},
    {'writer_stage': 'generation_fence'},
    {'writer_reason': 'source_generation_changed'},
    {'writer_extra': 'synthetic-private-value'},
])
def test_unknown_or_mismatched_writer_diagnostic_is_not_exposed(tmp_path, updates):
    _status(tmp_path, **{**WRITER_DIAGNOSTIC, **updates})
    assert health_report.attachment_ocr_health(str(tmp_path), CFG, 1000) == {
        'state': 'unknown', 'reason': 'attachment_ocr_status_corrupt'}


@pytest.mark.parametrize('reason,stage', [
    ('run_lock_not_acquired', 'ledger_lock'),
    ('update_marker_present', 'update_restore_fence'),
    ('source_generation_changed', 'generation_fence'),
])
def test_writer_diagnostic_keeps_canonical_reason_stage_pair(tmp_path, reason, stage):
    diagnostic = {**WRITER_DIAGNOSTIC, 'writer_reason': reason, 'writer_stage': stage}
    _status(tmp_path, **diagnostic)
    report = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert report['state'] == 'ok'
    assert {key: report[key] for key in diagnostic} == diagnostic


@pytest.mark.parametrize('updates,now', [
    ({'failed': 1, 'paid_recovery_pending': True}, 1000),
    ({'pending_failed': 1}, 1000),
    ({'paid_recovery_pending': True, 'local_done': 1}, 1000),
    ({'reason': 'memory_deferred', 'deferred': 1}, 1000),
    ({'reason': 'snapshot_deferred', 'complete': 1}, 1000),
    ({'queue_status': 'partial', 'local_deferred': 1}, 1000),
    ({'local_deferred': 1}, 1000),
    ({'at': 0, 'failed': 1}, 3000),
])
def test_writer_diagnostic_does_not_override_health_outcome(tmp_path, updates, now):
    _status(tmp_path, **updates)
    prior = health_report.attachment_ocr_health(str(tmp_path), CFG, now)
    assert prior['state'] != 'ok'
    _status(tmp_path, **updates, **WRITER_DIAGNOSTIC)
    assert health_report.attachment_ocr_health(str(tmp_path), CFG, now) == {
        **prior, **WRITER_DIAGNOSTIC}


def test_validated_partial_failure_snapshot_reaches_cause_specific_alert(tmp_path):
    _status(tmp_path, pending_failed=2, queue_status='partial',
            failure_reason_counts={'output_schema_table_overlap': 1}, exhausted_failed=1)
    ocr = health_report.attachment_ocr_health(str(tmp_path), CFG, 1000)
    assert ocr['state'] == 'failed' and ocr['pending_failed'] == 2
    lines = health_watch._reason_rows({'attachment_ocr': ocr}, ['attachment_ocr_processing_failed'])
    text = '\n'.join(lines)
    assert '表の行列対応を確定できず未採用' in text
    assert '観測範囲' in text and '1件' in text and '自動再試行が上限' in text
    assert 'doctor --share' in text and '元の添付' in text
    assert 'OCRの設定・実行環境' not in text
    assert '全件' not in text and 'すべて' not in text
