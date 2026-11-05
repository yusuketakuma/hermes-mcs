"""Private synthetic OCR status is observed independently from collection health."""
import json
import os

import pytest

import health_report
import health_watch
from ledger import Ledger


CFG = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"},
       "backup": {"enabled": False}}


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
