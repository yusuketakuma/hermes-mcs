"""Synthetic: an access log the adapters could not write raises a health reason."""
import json

import pytest

import access_log
import health_report
import health_watch
from ledger import Ledger

CFG = {"backup": {"enabled": False}}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(health_report.time, "time", lambda: 1_790_000_900)
    monkeypatch.setattr(health_report, "HEALTH_FILE", str(tmp_path / "missing.json"))
    stored = Ledger(str(tmp_path / "ledger.db"))
    yield stored, tmp_path
    stored.close()


def _fail(root, at):
    folder = root / access_log.DIR_NAME
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o755)
    with pytest.raises(access_log.AccessLogError):
        access_log.record(root, via="slack", actor="slack:T1:U1", op="read", now=at)
    folder.chmod(0o700)


def test_an_unwritable_access_log_degrades_health_until_a_record_succeeds(ledger):
    stored, root = ledger
    report = health_report.build_health(stored.db, {"errors": []}, "ok", cfg=CFG)
    assert "access_log_unwritable" not in report["state_reasons"]
    assert report["access_log"] == {"state": "ok", "code": None, "since": None}
    _fail(root, 1_790_000_000)
    report = health_report.build_health(stored.db, {"errors": []}, "ok", cfg=CFG)
    assert report["overall"] == "degraded" and "access_log_unwritable" in report["state_reasons"]
    assert report["access_log"] == {"state": "failing", "code": "access_log_untrusted",
                                    "since": 1_790_000_000}
    access_log.record(root, via="slack", actor="slack:T1:U1", op="read")
    report = health_report.build_health(stored.db, {"errors": []}, "ok", cfg=CFG)
    assert "access_log_unwritable" not in report["state_reasons"]


def test_the_alert_says_answers_are_withheld_since_when_and_what_to_check(ledger):
    stored, root = ledger
    _fail(root, 1_790_000_000)
    health = root / "health.json"
    health.write_text(json.dumps(health_report.build_health(stored.db, {"errors": []}, "ok", cfg=CFG)))
    report = health_watch.classify_health(str(health), now=1_790_000_900, deadline_s=900)
    assert report["access_log_since"] == 1_790_000_000
    text = "\n".join(health_watch._alert_lines({**report, "alert": True, "disk_alert": False}))
    meaning, action = health_watch._REASON_JA["access_log_unwritable"]
    assert "閲覧・操作記録" in meaning and "返していません" in meaning
    assert "access_log" in action and "空き容量" in action
    assert meaning + "（" + health_watch._jst(1_790_000_000) + " から）" in text
    assert "閲覧・操作記録に遅れや不具合があります" in text and "原因未分類" not in text
    assert "MCS-HLT-ACCESS-LOG-UNWRITABLE" in text and "slack:T1:U1" not in text
