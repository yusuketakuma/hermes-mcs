"""Recoverable system alerts wait out a verification window and are
suppressed when the condition already cleared before first send."""
import json
import time

import pytest

import alert_recovery
import collection_stages
from ledger import Ledger


@pytest.fixture
def led(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    yield db
    db.close()


def _outbox(led, kind, payload=None, created_ago=0.0):
    rid = led.outbox_add(kind, None, payload or {"run_id": 1, "detail": "x"})
    if created_ago:
        led.db.execute("UPDATE notify_outbox SET created_at=? WHERE event_id=?",
                       (time.time() - created_ago, rid))
        led.db.commit()
    return rid


def _ev(led, event_id):
    return led.db.execute(
        "SELECT event_id,kind,project_id,payload,attempts,progress,route,"
        "state,next_try FROM notify_outbox WHERE event_id=?",
        (event_id,)).fetchone()


def test_next_try_only_delays_recoverable_kinds():
    now = 1000.0
    assert alert_recovery.next_try_for("run_failed", now) == now + alert_recovery.VERIFY_S
    assert alert_recovery.next_try_for("health_write_failed", now) == now + alert_recovery.VERIFY_S
    assert alert_recovery.next_try_for("health_watch_stalled", now) == now + alert_recovery.VERIFY_S
    assert alert_recovery.next_try_for("session_expired", now) is None
    assert alert_recovery.next_try_for("update_notice", now) is None
    assert alert_recovery.next_try_for("new_messages", now) is None


def test_non_recoverable_kind_is_never_suppressed(led):
    rid = _outbox(led, "session_expired")
    assert alert_recovery.suppression_reason(led.db, _ev(led, rid)) is None


def test_health_write_failed_recovers_when_later_health_landed(led, tmp_path, monkeypatch):
    import health_report
    health = tmp_path / "health.json"
    health.write_text(json.dumps({"at": time.time() + 10}))
    monkeypatch.setattr(health_report, "HEALTH_FILE", str(health))
    rid = _outbox(led, "health_write_failed", created_ago=60.0)
    assert alert_recovery.suppression_reason(led.db, _ev(led, rid)) == "recovered_before_send"


def test_health_write_failed_still_broken_without_newer_health(led, tmp_path, monkeypatch):
    import health_report
    health = tmp_path / "health.json"
    health.write_text(json.dumps({"at": time.time() - 3600}))
    monkeypatch.setattr(health_report, "HEALTH_FILE", str(health))
    rid = _outbox(led, "health_write_failed", created_ago=60.0)
    assert alert_recovery.suppression_reason(led.db, _ev(led, rid)) is None


def test_health_write_failed_missing_health_file_is_not_recovered(led, tmp_path, monkeypatch):
    import health_report
    monkeypatch.setattr(health_report, "HEALTH_FILE", str(tmp_path / "absent.json"))
    rid = _outbox(led, "health_write_failed")
    assert alert_recovery.suppression_reason(led.db, _ev(led, rid)) is None


def _run(led, status, ago=0.0):
    now = time.time()
    led.db.execute(
        "INSERT INTO runs(run_id,started_at,finished_at,status,kind) "
        "VALUES(?,?,?,?,?)",
        (led.db.execute("SELECT COALESCE(MAX(run_id),0)+1 FROM runs").fetchone()[0],
         now - ago, now - ago, status, "tick"))
    led.db.commit()


def test_run_failed_recovers_on_later_ok_or_partial_run(led):
    rid = _outbox(led, "run_failed", {"run_id": 1, "detail": "crash: X"})
    led.db.execute("INSERT INTO runs(run_id,started_at,finished_at,status,kind) "
                   "VALUES(1,?,?,?,?)", (time.time() - 100, time.time() - 100, "failed", "tick"))
    led.db.commit()
    _run(led, "partial")
    assert alert_recovery.suppression_reason(led.db, _ev(led, rid)) == "recovered_before_send"


def test_run_failed_without_later_success_still_alerts(led):
    rid = _outbox(led, "run_failed", {"run_id": 1, "detail": "crash: X"})
    led.db.execute("INSERT INTO runs(run_id,started_at,finished_at,status,kind) "
                   "VALUES(1,?,?,?,?)", (time.time() - 100, time.time() - 100, "failed", "tick"))
    led.db.execute("INSERT INTO runs(run_id,started_at,finished_at,status,kind) "
                   "VALUES(2,?,?,?,?)", (time.time() - 50, time.time() - 50, "failed", "tick"))
    led.db.commit()
    assert alert_recovery.suppression_reason(led.db, _ev(led, rid)) is None


def test_queue_run_alert_delays_only_recoverable(led):
    assert collection_stages.queue_run_alert(led, "run_failed", 1, "x")
    row = led.db.execute(
        "SELECT next_try,created_at FROM notify_outbox WHERE kind='run_failed'").fetchone()
    assert row["next_try"] - row["created_at"] == pytest.approx(alert_recovery.VERIFY_S)

    assert collection_stages.queue_run_alert(led, "session_expired", 2, "x")
    row = led.db.execute(
        "SELECT next_try,created_at FROM notify_outbox WHERE kind='session_expired'").fetchone()
    assert row["next_try"] - row["created_at"] < 5


def test_suppressed_incident_does_not_hold_throttle(led):
    assert collection_stages.queue_run_alert(led, "run_failed", 1, "x")
    rid = led.db.execute(
        "SELECT event_id FROM notify_outbox WHERE kind='run_failed'").fetchone()["event_id"]
    led.outbox_suppress(rid)
    assert collection_stages.queue_run_alert(led, "run_failed", 2, "x")


def test_pending_incident_still_holds_throttle(led):
    assert collection_stages.queue_run_alert(led, "run_failed", 1, "x")
    assert not collection_stages.queue_run_alert(led, "run_failed", 2, "x")


@pytest.mark.parametrize("watched_ago,expected", [(10.0, "recovered_before_send"), (120.0, None)])
def test_health_watch_stalled_is_withheld_only_when_the_watcher_observed_again(
        led, tmp_path, watched_ago, expected):
    (tmp_path / "health_watch_status.json").write_text(
        json.dumps({"watched_at": time.time() - watched_ago}))
    rid = _outbox(led, "health_watch_stalled", created_ago=60.0)
    assert alert_recovery.suppression_reason(led.db, _ev(led, rid)) == expected


@pytest.mark.parametrize("content", [None, "{", "[]", '{"watched_at":"now"}', '{"watched_at":true}'])
def test_health_watch_stalled_stays_due_without_a_readable_newer_observation(led, tmp_path, content):
    if content is not None:
        (tmp_path / "health_watch_status.json").write_text(content)
    rid = _outbox(led, "health_watch_stalled", created_ago=60.0)
    assert alert_recovery.suppression_reason(led.db, _ev(led, rid)) is None
