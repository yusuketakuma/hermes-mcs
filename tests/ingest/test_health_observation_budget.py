"""Mandatory notification observations use the existing bounded tick connection."""
import json
import sqlite3

import pytest

import health_report
import health_watch
import notify_scope
from ingest_testkit import _ledger


NOW = 200_000.0
STATES = ("pending", "unknown", "not_sent", "held")


@pytest.fixture
def stored(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(health_report.time, "time", lambda: NOW)
    monkeypatch.setattr(health_report, "HEALTH_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(health_report, "attachment_free_mb", lambda path=None: 10_000)
    monkeypatch.setattr(health_report.maintenance, "local_backup_status",
                        lambda **_: {"state": "verified", "newest_age_s": 50, "observed_at": NOW})
    try:
        yield ledger.db
    finally:
        ledger.close()


def _empty(now=NOW):
    return {"total": 0, "cards": 0, "by_state": dict.fromkeys(STATES, 0),
            "oldest_age_s": None, "observed_at": now}


def _assert_unknown(parts, reason):
    assert parts == {"total": None, "cards": None, "by_state": dict.fromkeys(STATES),
                     "oldest_age_s": None, "observed_at": NOW,
                     "status": "unknown", "reason": reason}


def test_build_health_bounds_part_read_and_keeps_observed_zero_healthy(stored, monkeypatch):
    original_limit = stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    original_wait = stored.execute("PRAGMA busy_timeout").fetchone()[0]

    def read(db, now, cfg=None):
        assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == min(
            original_limit, health_report.HEALTH_OBSERVATION_ROW_BYTES)
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == min(
            original_wait, int(health_report.HEALTH_OBSERVATION_S * 1000))
        return _empty(now)

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", read)
    report = health_report.build_health(stored, {"errors": ["code_changed"], "notify": {}},
                                        "partial", cfg={})
    assert report["notify"]["incomplete_parts"] == {**_empty(), "status": "recorded"}
    assert report["overall"] == report["notify"]["state"] == "ok"
    assert report["state_reasons"] == ["code_changed"]
    assert stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_limit
    assert stored.execute("PRAGMA busy_timeout").fetchone()[0] == original_wait


@pytest.mark.parametrize("failure", ["unavailable", "large_row"])
def test_failed_part_observation_is_unknown_without_losing_outer_transaction(stored, monkeypatch, failure):
    stored.execute("PRAGMA busy_timeout=7000")
    original_limit = stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    stored.execute("INSERT INTO runs(started_at,status,error) VALUES(?,'ok','')", (NOW,))
    before = stored.total_changes
    def read(db, now, cfg=None):
        if failure == "unavailable":
            raise ValueError("SYNTHETIC_PRIVATE_ERROR")
        db.execute("SELECT zeroblob(?)", (health_report.HEALTH_OBSERVATION_ROW_BYTES + 1,)).fetchone()
        pytest.fail("part read should stop at its existing observation bound")

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", read)
    parts = health_report.incomplete_parts_health(stored, NOW, {})
    _assert_unknown(parts, "observation_unavailable")
    assert "SYNTHETIC_PRIVATE_ERROR" not in json.dumps(parts)
    assert stored.total_changes == before and stored.in_transaction
    assert stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_limit
    assert stored.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    stored.commit()
    assert stored.execute("SELECT COUNT(*) FROM runs WHERE started_at=?", (NOW,)).fetchone()[0] == 1


@pytest.mark.parametrize("outer_transaction", [False, True])
def test_sql_deadline_clears_handler_and_preserves_connection(stored, monkeypatch, outer_transaction):
    stored.execute("PRAGMA busy_timeout=7000")
    original_limit = stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    if outer_transaction:
        stored.execute("INSERT INTO runs(started_at,status,error) VALUES(?,'ok','')", (NOW,))
    clock = [100.0]
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])

    def read(db, now, cfg=None):
        clock[0] += 1
        # Exercise the real SQLite progress handler, not a raised stub timeout.
        db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 "
                   "FROM n WHERE x<2000) SELECT SUM(x) FROM n").fetchone()
        pytest.fail("SQL must be interrupted at the existing time budget")

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", read)
    _assert_unknown(health_report.incomplete_parts_health(stored, NOW), "observation_limit")
    assert stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_limit
    assert stored.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    assert stored.in_transaction is outer_transaction
    # The clock stays past the old deadline: this succeeds only after cleanup.
    assert stored.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 "
                          "FROM n WHERE x<2000) SELECT SUM(x) FROM n").fetchone()[0] == 2001000
    stored.commit()
    assert stored.execute("SELECT COUNT(*) FROM runs WHERE started_at=?", (NOW,)).fetchone()[0] == int(outer_transaction)


@pytest.mark.parametrize("run_status", ["ok", "failed"])
def test_unknown_parts_reach_producer_and_monitor_without_zero(stored, tmp_path, monkeypatch, run_status):
    def fail(*args, **kwargs):
        raise RuntimeError("observation_limit")

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", fail)
    result = {"errors": ["code_changed"], "notify": {}}
    report = health_report.build_health(stored, result, run_status, cfg={})
    _assert_unknown(report["notify"]["incomplete_parts"], "observation_limit")
    assert report["overall"] == ("failed" if run_status == "failed" else "degraded")
    assert report["notify"]["state"] == "incomplete"
    assert "notification_parts_incomplete" in report["state_reasons"]
    assert result["errors"] == ["code_changed"]
    path = tmp_path / "health.json"
    path.write_text(json.dumps(report))
    watched = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert watched["notify_incomplete_parts"] is None and watched["status"] == report["overall"]
