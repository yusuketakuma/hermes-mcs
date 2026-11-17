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


def _run(led, status, ago=0.0, kind="tick"):
    now = time.time()
    led.db.execute(
        "INSERT INTO runs(run_id,started_at,finished_at,status,kind) "
        "VALUES(?,?,?,?,?)",
        (led.db.execute("SELECT COALESCE(MAX(run_id),0)+1 FROM runs").fetchone()[0],
         now - ago, now - ago, status, kind))
    led.db.commit()


def test_run_failed_recovers_on_later_complete_run(led):
    rid = _outbox(led, "run_failed", {"run_id": 1, "detail": "crash: X"})
    led.db.execute("INSERT INTO runs(run_id,started_at,finished_at,status,kind) "
                   "VALUES(1,?,?,?,?)", (time.time() - 100, time.time() - 100, "failed", "tick"))
    led.db.commit()
    _run(led, "ok")
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


@pytest.mark.parametrize("failed_kind", ["tick", "deep"])
@pytest.mark.parametrize("later_kind", ["tick", "deep"])
@pytest.mark.parametrize("status", ["ok", "partial", "failed", "running"])
def test_run_recovery_requires_complete_success_of_same_kind(led, failed_kind, later_kind, status):
    _run(led, "failed", ago=100, kind=failed_kind)
    event_id = _outbox(led, "run_failed", {"run_id": 1}, created_ago=60)
    _run(led, status, kind=later_kind)
    expected = "recovered_before_send" if status == "ok" and failed_kind == later_kind else None
    assert alert_recovery.suppression_reason(led.db, _ev(led, event_id)) == expected


@pytest.mark.parametrize("payload", [{"detail": "legacy"}, {"run_id": 99}, {"run_id": True},
                                     {"run_id": "1"}, []])
def test_time_fallback_does_not_guess_missing_failure_kind(led, payload):
    event_id = _outbox(led, "run_failed", {"detail": "legacy"}, created_ago=60)
    led.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?", (json.dumps(payload), event_id))
    _run(led, "ok")
    _run(led, "ok", kind="deep")
    assert alert_recovery.suppression_reason(led.db, _ev(led, event_id)) is None


def test_old_success_before_failed_run_cannot_recover_even_with_newer_timestamp(led):
    _run(led, "ok", ago=-100)
    _run(led, "failed")
    event_id = _outbox(led, "run_failed", {"run_id": 2}, created_ago=60)
    assert alert_recovery.suppression_reason(led.db, _ev(led, event_id)) is None


@pytest.mark.parametrize("last_status", ["failed", "partial"])
def test_later_incomplete_run_invalidates_intermediate_recovery(led, last_status):
    _run(led, "failed", ago=100)
    event_id = _outbox(led, "run_failed", {"run_id": 1}, created_ago=60)
    _run(led, "ok", ago=40)
    _run(led, last_status)
    assert alert_recovery.suppression_reason(led.db, _ev(led, event_id)) is None


def test_unknown_original_kind_cannot_be_recovered(led):
    _run(led, "failed", ago=100, kind="unknown")
    event_id = _outbox(led, "run_failed", {"run_id": 1}, created_ago=60)
    _run(led, "ok", kind="unknown")
    assert alert_recovery.suppression_reason(led.db, _ev(led, event_id)) is None


def test_unfinished_ok_row_cannot_prove_recovery(led):
    _run(led, "failed", ago=100)
    event_id = _outbox(led, "run_failed", {"run_id": 1}, created_ago=60)
    _run(led, "ok")
    led.db.execute("UPDATE runs SET finished_at=NULL WHERE run_id=2")
    assert alert_recovery.suppression_reason(led.db, _ev(led, event_id)) is None


def test_flushing_run_does_not_hide_a_completed_recovery(led):
    failed_id = led.begin_run(None, kind="tick")
    led.finish_run(failed_id, "failed", "synthetic")
    event_id = _outbox(led, "run_failed", {"run_id": failed_id})
    recovered_id = led.begin_run(None, kind="tick")
    led.finish_run(recovered_id, "ok")
    flushing_id = led.begin_run(None, kind="tick")
    assert led.db.execute("SELECT finished_at FROM runs WHERE run_id=?", (flushing_id,)).fetchone()[0] is None
    assert alert_recovery.suppression_reason(led.db, _ev(led, event_id)) == "recovered_before_send"
    led.finish_run(flushing_id, "failed", "synthetic")
    assert alert_recovery.suppression_reason(led.db, _ev(led, event_id)) is None
