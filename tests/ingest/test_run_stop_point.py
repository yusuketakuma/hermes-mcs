"""A failed run records where it stopped and shows the fixed cause and next step."""
import json
import time
from types import SimpleNamespace

import pytest

import collection_diagnostics as diagnostics
import collection_stages
import health_report
import health_watch
import mcs_setup
import notify_flush
import run_check
from ingest_testkit import _ledger

PRIVATE = "synthetic_patient_123_secret"
OCR_DISABLED_CFG = {"attachment_ocr": {"enabled": False}}


def _stopped(*stages, error=None):
    error = error or RuntimeError(PRIVATE)
    for stage in stages:
        diagnostics.mark_stage(error, stage)
    return error


def test_innermost_stage_wins_and_unknown_stays_unknown():
    assert diagnostics.stop_point(_stopped("discovery", "jobs"), "network_error") == {
        "stage": "discovery", "category": "network"}
    assert diagnostics.stop_point(_stopped("not_a_stage"), f"crash: {PRIVATE}") == {
        "stage": "unknown", "category": "runtime"}
    assert diagnostics.stop_point(RuntimeError(), "crash: AttributeError")["category"] == "runtime"


def test_run_stage_and_relogin_step_tag_the_escaping_error():
    ledger = SimpleNamespace(db=SimpleNamespace(in_transaction=False))

    def failing():
        raise RuntimeError(PRIVATE)

    result = {"errors": []}
    with pytest.raises(RuntimeError) as raised:
        run_check._run_stage(result, "jobs", time.monotonic() + 60,
                             collection_stages.with_relogin, None, ledger, result,
                             "reply_jobs", failing)
    assert diagnostics.stop_point(raised.value, "x")["stage"] == "reply_jobs"
    with pytest.raises(RuntimeError) as raised:
        run_check._run_stage(result, "deliver", time.monotonic() + 60, failing)
    assert diagnostics.stop_point(raised.value, "x")["stage"] == "deliver"


@pytest.mark.parametrize("value", [
    None, "deliver", {"stage": "deliver"}, {"stage": PRIVATE, "category": "network"},
    {"stage": "deliver", "category": PRIVATE}, {"stage": "deliver", "category": "network", "x": 1},
])
def test_malformed_stop_points_are_never_explained(value):
    assert diagnostics.valid_stop(value) is None
    assert diagnostics.stop_explanation(value) is None


def test_failed_run_records_stop_in_health_and_alert(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, "prev_health", lambda: {})
    monkeypatch.setattr(health_report, "attachment_free_mb", lambda path=None: 10000)
    alerts = []
    monkeypatch.setattr(collection_stages, "queue_run_alert",
                        lambda *a, **k: alerts.append(k["stop"]))
    result = {"errors": [], "notify": {}}
    error = _stopped("deliver")
    try:
        run_check._fail_run(db, SimpleNamespace(no_notify=True), result, db.begin_run(None),
                            "failed", error, "network_error", time.monotonic() + 60,
                            alert="run_failed")
        health = health_report.build_health(db.db, result, "failed", cfg=OCR_DISABLED_CFG)
        ok = health_report.build_health(db.db, result, "ok", cfg=OCR_DISABLED_CFG)
    finally:
        db.close()
    stop = {"stage": "deliver", "category": "network"}
    assert alerts == [stop] and health["run"]["stop"] == stop
    assert "stop" not in ok["run"]
    assert PRIVATE not in json.dumps(health)


def _health(tmp_path, run_status, stop):
    path = tmp_path / "data" / "health.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"at": 1000, "overall": "failed", "run_status": run_status,
                                "state_reasons": ["run_failed"], "run": {"stop": stop}}))
    return str(path)


def test_alert_names_the_stopped_stage_and_the_cause_specific_step(tmp_path):
    stop = {"stage": "unread", "category": "network"}
    report = health_watch.classify_health(_health(tmp_path, "failed", stop), 1000, 600)
    assert report["run_stop"] == stop
    text = "\n".join(health_watch._alert_lines({**report, "alert": True, "disk_alert": False}))
    assert "・収集の実行が失敗しました（止まった処理: 未読投稿の取得）" in text
    assert "　・通信: MCSとの通信に失敗しました" in text
    assert "　　→ 次回の収集で再試行されます。続く場合はネットワーク接続を確認" in text
    assert "data/mcs.log の末尾を確認" not in text


def test_stop_from_a_successful_record_is_ignored(tmp_path):
    report = health_watch.classify_health(
        _health(tmp_path, "ok", {"stage": "unread", "category": "network"}), 1000, 600)
    assert report["run_stop"] is None
    text = "\n".join(health_watch._alert_lines({**report, "alert": True, "disk_alert": False}))
    assert "止まった処理" not in text and "data/mcs.log の末尾を確認" in text


def test_immediate_notice_shows_stage_cause_and_step_without_raw_detail():
    stop = {"stage": "attachments", "category": "storage"}
    text = notify_flush._system_event_text("run_failed", {"run_id": 3, "detail": PRIVATE, "stop": stop})
    assert text.splitlines()[:4] == [
        "[MCSLOG] 🔴 収集処理の失敗", "影響: 新しい患者情報の更新が遅れる可能性があります。",
        "止まった処理: 添付の取得", "原因: 取得した内容をこのMacに保存できませんでした"]
    assert "次の操作: ディスクの空き容量と data フォルダの書込み権限を確認" in text
    assert PRIVATE not in text
    session = notify_flush._system_event_text(
        "session_expired", {"stop": {"stage": "unread", "category": "login"}})
    assert "止まった処理: 未読投稿の取得" in session
    assert "次の操作: MCSへ手動で再ログインしてください。" in session
    legacy = notify_flush._system_event_text("run_failed", {"stop": {"stage": PRIVATE}})
    assert "止まった処理" not in legacy and "mcs_setup.py check" in legacy


def test_doctor_reports_the_last_stop(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup.time, "time", lambda: 1000)
    _health(tmp_path, "failed", {"stage": "derive", "category": "postprocess"})
    result = mcs_setup._doctor_collection({}, probe=True)
    assert result["stop"] == {"stage": "derive", "category": "postprocess"}


def test_the_user_guide_lists_every_stage():
    from pathlib import Path
    guide = (Path(__file__).resolve().parents[2] / "docs/guides/COLLECTION_DIAGNOSTICS.md").read_text(encoding="utf-8")
    for code, label in diagnostics.STAGES.items():
        assert f"| {label} | `{code}` |" in guide
