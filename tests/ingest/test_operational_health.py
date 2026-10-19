"""Additive health observations preserve collection state and durable writes."""
import health_report
import json
import os
import sqlite3

import pytest

import health_watch
import notify_cards
import operational_status
import run_check
from ingest_testkit import _ledger
from maintenance import local_backup_status

NOW = 200_000.0


@pytest.fixture
def stored(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    health = tmp_path / "data" / "health.json"
    health.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(health_report, 'HEALTH_FILE', str(health))
    monkeypatch.setattr(run_check.time, "time", lambda: NOW)
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10_000)
    monkeypatch.setattr(run_check.maintenance, "local_backup_status",
                        lambda **_: {"state": "verified", "newest_age_s": 50,
                                     "observed_at": NOW})
    yield db, health
    db.close()


def pending(db, *, kind="synthetic_notice", payload=None, due=NOW, progress=None):
    count = db.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    eid = db.outbox_add(kind, 1, payload or {"synthetic_id": count})
    db.db.execute(
        "UPDATE notify_outbox SET created_at=?,updated_at=?,next_try=?,progress=? "
        "WHERE event_id=?", (NOW - 20, NOW, due, progress, eid))
    db.db.commit()
    return eid


def test_producer_file_monitor_and_doctor_consume_the_same_safe_evidence(stored):
    db, path = stored
    eid = pending(db)
    notify_cards._reseat(db.db, eid, NOW, "intent_cards_unsettled")
    pending(db, kind="signal", payload={"digest": True}, due=NOW + 1000)
    db.db.execute(
        "INSERT INTO runs(started_at,finished_at,status,error) VALUES(?,?,'crashed',?)",
        (NOW - 500, NOW - 100, "synthetic-private-error"))
    db.db.commit()
    result = {"errors": [], "notify": {}}
    run_check._write_health(db, result, "ok", cfg={})
    record = json.loads(path.read_text())
    assert record["overall"] == "ok"
    waits = record["notify"]["pending_by_reason"]
    assert record["notify"]["pending"] == waits["total"] == 1
    assert sum(slot["count"] for slot in waits["by_reason"].values()) == 1
    assert waits["by_reason"]["intent_cards_unsettled"]["count"] == 1
    assert waits["observed_at"] == NOW
    assert record["backup"]["state"] == "disabled"
    assert record["backup"]["local"]["state"] == "verified"
    assert record["run"]["last_interrupt"]["count_24h"] == 1
    assert record["run"]["last_interrupt"]["reason"] == "unknown"
    assert "synthetic-private-error" not in path.read_text()

    monitor = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert monitor["status"] == "ok"
    assert monitor["notify_wait"]["reasons"] == {"intent_cards_unsettled": 1}
    assert monitor["backup_local"]["state"] == "verified"
    doctor = operational_status.stored_status(path.parent.parent, {}, now=NOW + 1)
    assert doctor["queues"]["notify"]["pending"] == 1
    assert doctor["last_interrupt"]["count_24h"] == 1
    assert doctor["backup"]["local"]["state"] == "verified"


@pytest.mark.parametrize("failure", ["raise", "deadline", "items", "large_row"])
def test_optional_failure_keeps_original_counts_verdict_and_connection(stored, monkeypatch, failure):
    db, _ = stored
    pending(db)
    pending(db)
    baseline = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok", cfg={})
    original_limit = db.db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    original_wait = db.db.execute("PRAGMA busy_timeout").fetchone()[0]
    if failure == "raise":
        def fail(*args, **kwargs):
            raise ValueError("synthetic-private-error")
        monkeypatch.setattr(notify_cards, "pending_breakdown", fail)
    elif failure == "deadline":
        monkeypatch.setattr(health_report, 'HEALTH_OBSERVATION_S', 0)
    elif failure == "items":
        monkeypatch.setattr(health_report, 'HEALTH_OBSERVATION_ITEMS', 2)
    else:
        db.db.execute("UPDATE notify_outbox SET progress=?",
                      ('{"large":"' + "x" * (2 * 1024 * 1024) + '"}',))
        db.db.commit()
    # An outer transaction's durable work must survive an interrupted diagnostic.
    db.db.execute("INSERT INTO runs(started_at,status,error) VALUES(?,'ok','')", (NOW,))
    assert db.db.in_transaction
    result = {"errors": [], "notify": {}}
    observed = health_report.build_health(db.db, result, "ok", cfg={})
    assert observed["notify"]["pending"] == baseline["notify"]["pending"] == 2
    assert observed["notify"]["pending_by_reason"]["status"] == "unavailable"
    assert observed["overall"] == baseline["overall"] == "ok"
    assert observed["state_reasons"] == baseline["state_reasons"]
    assert result["errors"] == []
    assert "synthetic-private-error" not in json.dumps(observed)
    assert db.db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_limit
    assert db.db.execute("PRAGMA busy_timeout").fetchone()[0] == original_wait
    assert db.db.in_transaction
    db.db.commit()
    assert db.db.execute("SELECT COUNT(*) FROM runs WHERE status='ok'").fetchone()[0] == 1
    assert db.db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 "
                         "FROM n WHERE x<2000) SELECT SUM(x) FROM n").fetchone()[0] == 2001000


def test_pending_success_has_one_population_read(stored):
    db, _ = stored
    pending(db)
    statements = []
    db.db.set_trace_callback(statements.append)
    try:
        report = health_report.pending_health(db.db, NOW)
    finally:
        db.db.set_trace_callback(None)
    queries = [sql for sql in statements if "FROM notify_outbox" in sql]
    assert len(queries) == 1
    assert report["pending"] == report["pending_by_reason"]["total"] == 1


def test_missing_real_count_still_fails_the_original_health_path(stored):
    db, _ = stored
    db.db.execute("DROP TABLE notify_outbox")
    with pytest.raises(sqlite3.Error):
        health_report.pending_health(db.db, NOW)


def test_local_observation_remains_available_without_loaded_config(stored):
    db, _ = stored
    result = health_report.build_health(db.db, {"errors": [], "notify": {}}, "failed",
                              cfg=health_report.CONFIG_NOT_LOADED)
    assert result["backup"]["state"] == "unknown"
    assert result["backup"]["local"]["state"] == "verified"
    assert result["overall"] == "failed"
    assert "run_failed" in result["state_reasons"]


def test_local_observation_failure_does_not_relabel_collection(stored, monkeypatch):
    db, _ = stored
    def fail(**_):
        raise OSError("synthetic-private-path")
    monkeypatch.setattr(run_check.maintenance, "local_backup_status", fail)
    result = health_report.build_health(db.db, {"errors": [], "notify": {}}, "failed", cfg={})
    assert result["backup"]["local"]["state"] == "unknown"
    assert result["overall"] == "failed"
    assert "synthetic-private-path" not in json.dumps(result)


@pytest.mark.parametrize("marker_kind,expected", [("regular", "verified"), ("fifo", "unverified")])
def test_real_local_backup_observation_reaches_monitor_and_doctor(stored, monkeypatch, marker_kind, expected):
    db, path = stored
    backups = path.parent / "backups"
    backups.mkdir()
    backup = backups / "ledger-20261013.db"
    backup.write_bytes(b"synthetic backup bytes")
    os.utime(backup, (NOW - 60, NOW - 60))
    if marker_kind == "regular":
        run_check.maintenance._write_ok_marker(str(backup))
    else:
        os.mkfifo(str(backup) + ".ok")
    monkeypatch.setattr(run_check.maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(run_check.maintenance, "local_backup_status", local_backup_status)
    run_check._write_health(db, {"errors": [], "notify": {}}, "ok", cfg={})
    record = json.loads(path.read_text())
    assert record["overall"] == "ok"
    assert record["backup"]["state"] == "disabled"
    assert record["backup"]["local"] == {"state": expected, "newest_age_s": 60, "observed_at": NOW}
    monitor = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert monitor["status"] == "ok"
    assert monitor["backup_local"]["state"] == expected
    doctor = operational_status.stored_status(path.parent.parent, {}, now=NOW + 1)
    assert doctor["backup"]["local"]["state"] == expected
