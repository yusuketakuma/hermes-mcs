"""Scheduled jobs yield the single cron slot only while a gateway restart request is pending."""
import json
import sys

import pytest

import health_watch
import mcs_ocr
import run_check
import writer_lock
from run_check_stage_testkit import _point_run_check_at

NOW = 10_000.0


def _receipt(data, row):
    data.mkdir(exist_ok=True)
    (data / "gateway_restart.json").write_text(
        row if isinstance(row, str) else json.dumps(row), encoding="utf-8")


@pytest.mark.parametrize("row, paused", [
    ({"status": "requested", "at": NOW - 5}, True),
    ({"status": "requested", "at": NOW - writer_lock.GATEWAY_RESTART_PAUSE_S}, True),
    ({"status": "requested", "at": NOW - writer_lock.GATEWAY_RESTART_PAUSE_S - 10}, False),
    ({"status": "requested", "at": NOW + 60}, False),
    ({"status": "requested", "at": "soon"}, False),
    ({"status": "supervisor_restart_verified", "at": NOW - 5}, False),
    ({"status": "failed", "at": NOW - 5, "error": "gateway_restart_busy_timeout"}, False),
    ({"status": "unknown", "at": NOW - 5}, False),
    ("not json", False),
    ("[]", False),
    (None, False),
])
def test_only_a_fresh_requested_receipt_pauses(tmp_path, row, paused):
    data = tmp_path / "data"
    if row is not None:
        _receipt(data, row)
    pause = writer_lock.gateway_restart_pause(str(data), NOW)
    if paused:
        assert pause == {"reason": "gateway_restart_pending", "requested_at": row["at"],
                         "until": row["at"] + writer_lock.GATEWAY_RESTART_PAUSE_S}
    else:
        assert pause is None


def test_scheduled_tick_yields_before_lock_wait_or_fetch(tmp_path, monkeypatch, capsys):
    data = _point_run_check_at(tmp_path, monkeypatch)
    _receipt(data, {"status": "requested", "at": run_check.time.time()})
    monkeypatch.setattr(run_check, "acquire_run_lock",
                        lambda path: pytest.fail("a pending restart must not take or wait for run.lock"))
    for argv in (["run_check", "--json", "--mark-read"], ["run_check", "--json", "--jobs-only"]):
        monkeypatch.setattr(sys, "argv", argv)
        assert run_check.main() == 3
        assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "gateway_restart_pending"}


def test_command_worker_is_not_paused(tmp_path, monkeypatch, capsys):
    data = _point_run_check_at(tmp_path, monkeypatch)
    _receipt(data, {"status": "requested", "at": run_check.time.time()})
    taken = []
    monkeypatch.setattr(run_check, "acquire_run_lock", lambda path: taken.append(path))
    monkeypatch.setattr(run_check.writer_lock, "wait_run_lock", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["run_check", "--commands-only"])
    assert run_check.main() == 3            # synthetic lock refusal, after the pause check
    assert taken and "gateway_restart_pending" not in capsys.readouterr().out


def test_ocr_yields_without_overwriting_its_status(tmp_path, monkeypatch, capsys):
    database = tmp_path / "synthetic.db"
    database.touch()
    status = tmp_path / "attachment_ocr_status.json"
    status.write_text('{"at": 1}')
    _receipt(tmp_path, {"status": "requested", "at": mcs_ocr.time.time()})
    monkeypatch.setattr(mcs_ocr, "DB", str(database))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr, "load_config", lambda: {
        "attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"}})
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, "generation"))
    monkeypatch.setattr(mcs_ocr, "acquire_run_lock",
                        lambda path: pytest.fail("a pending restart must not start OCR work"))
    monkeypatch.setattr(mcs_ocr.sys, "argv", ["mcs_ocr", "--run"])
    assert mcs_ocr.main() == 0
    assert json.loads(capsys.readouterr().out) == {"attachment_ocr": {"reason": "gateway_restart_pending"}}
    assert status.read_text() == '{"at": 1}'


def test_health_watch_shows_the_pause_without_alerting_on_it(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "health.json").write_text(json.dumps({"at": NOW - 30, "overall": "ok"}))
    _receipt(data, {"status": "requested", "at": NOW - 30})
    report = health_watch.evaluate(home=str(tmp_path), now=NOW, cfg={})
    assert report["gateway_restart_pause"]["reason"] == "gateway_restart_pending"
    assert report["status"] == "ok" and report["alert"] is False
    saved = json.loads((data / "health_watch_status.json").read_text())
    assert saved["gateway_restart_pause"] == report["gateway_restart_pause"]

    _receipt(data, {"status": "supervisor_restart_verified", "at": NOW - 1})
    assert "gateway_restart_pause" not in health_watch.evaluate(home=str(tmp_path), now=NOW, cfg={})
