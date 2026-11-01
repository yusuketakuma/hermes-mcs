"""Mandatory notification observations use the existing bounded tick connection."""
import json
import sqlite3

import pytest

import health_report
import health_watch
import notify_recovery
import notify_scope
import status_snapshot
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
            "by_plan": {"current": 0, "superseded": 0},
            "oldest_age_s": None, "observed_at": now}


def _assert_unknown(parts, reason):
    assert parts == {"total": None, "cards": None, "by_state": dict.fromkeys(STATES),
                     "by_plan": dict.fromkeys(("current", "superseded")),
                     "oldest_age_s": None, "observed_at": NOW,
                     "status": "unknown", "reason": reason}


@pytest.mark.parametrize("reader", ["cards", "parts", "holds"])
@pytest.mark.parametrize("elapsed,recorded", [(0.5, True), (2.1, False)])
def test_notification_readers_use_separate_time_budget(stored, monkeypatch, reader, elapsed, recorded):
    stored.execute("PRAGMA busy_timeout=7000")
    original_limit = stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    stored.execute("INSERT INTO runs(started_at,status,error) VALUES(?,'ok','')", (NOW,))
    before = stored.total_changes
    clock = [100.0]
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])

    def read(db, *args):
        assert db is stored
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
        assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == min(
            original_limit, health_report.HEALTH_OBSERVATION_ROW_BYTES)
        clock[0] += elapsed
        if reader == "cards":
            return {"attempts_unsettled": 0, "oldest_unsettled_age_s": None}
        if reader == "parts":
            return _empty()
        return {"parts": {}}

    if reader == "cards":
        monkeypatch.setattr(notify_scope, "health_cards", read)
        report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg={})["cards"]
        if recorded:
            assert report["attempts_unsettled"] == 0
        else:
            assert report["status"] == "unknown" and report["reason"] == "observation_limit"
            assert report["attempts_unsettled"] is None
    elif reader == "parts":
        monkeypatch.setattr(notify_scope, "incomplete_parts_health", read)
        report = health_report.incomplete_parts_health(stored, NOW, {})
        if recorded:
            assert report == {**_empty(), "status": "recorded"}
        else:
            _assert_unknown(report, "observation_limit")
    else:
        monkeypatch.setattr(notify_recovery, "part_hold_report", read)
        report = health_report.part_holds_health(stored, NOW)
        if recorded:
            assert report == {"parts": {}, "status": "recorded", "observed_at": NOW}
        else:
            assert report == {"status": "unknown", "observed_at": NOW,
                              "reason": "observation_limit"}

    assert stored.in_transaction and stored.total_changes == before
    assert stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_limit
    assert stored.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    assert stored.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 "
                          "FROM n WHERE x<2000) SELECT SUM(x) FROM n").fetchone()[0] == 2001000
    stored.commit()


def test_other_health_readers_keep_default_time_budget(stored, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])

    def read(db, now, checkpoint):
        clock[0] += 0.5
        checkpoint()
        pytest.fail("pending diagnostics must keep the original time budget")

    monkeypatch.setattr(notify_scope, "pending_breakdown", read)
    report = health_report.pending_health(stored, NOW)
    assert report["pending"] == 0
    assert report["pending_by_reason"]["status"] == "unavailable"


def test_build_health_bounds_part_read_and_keeps_observed_zero_healthy(stored, monkeypatch):
    original_limit = stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    original_wait = stored.execute("PRAGMA busy_timeout").fetchone()[0]

    def read(db, now, cfg=None):
        assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == min(
            original_limit, health_report.HEALTH_OBSERVATION_ROW_BYTES)
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == min(
            original_wait, int(health_report.NOTIFICATION_OBSERVATION_S * 1000))
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
        clock[0] += health_report.NOTIFICATION_OBSERVATION_S + 0.1
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


def test_card_observation_preserves_observed_zero(stored):
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg={})
    assert report["cards"]["attempts_unsettled"] == 0
    assert report["cards"]["layout_inventory"] == []
    assert report["attention"]["delivery_unsettled"]["attempts"] == 0
    assert "notification_cards_unavailable" not in report["state_reasons"]


@pytest.mark.parametrize("failure", ["sql_limit", "unavailable"])
@pytest.mark.parametrize("run_status", ["ok", "failed"])
def test_failed_card_read_is_unknown_and_reaches_monitor(stored, tmp_path, monkeypatch, failure, run_status):
    stored.execute("PRAGMA busy_timeout=7000")
    original_limit = stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    stored.execute("INSERT INTO runs(started_at,status,error) VALUES(?,'ok','')", (NOW,))
    clock = [100.0]
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])

    def read(db, cfg=None):
        if failure == "unavailable":
            raise ValueError("SYNTHETIC_PRIVATE_ERROR")
        clock[0] += health_report.NOTIFICATION_OBSERVATION_S + 0.1
        db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 "
                   "FROM n WHERE x<2000) SELECT SUM(x) FROM n").fetchone()
        pytest.fail("card SQL must be interrupted at the observation deadline")

    monkeypatch.setattr(notify_scope, "health_cards", read)
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, run_status, cfg={})
    cards = report["cards"]
    assert cards["status"] == "unknown"
    assert cards["reason"] == ("observation_limit" if failure == "sql_limit" else "observation_unavailable")
    assert all(cards[name] is None for name in ("pending", "attempts_unsettled", "layout_inventory",
                                               "unsettled", "unknown_parts", "oldest_unsettled_age_s"))
    assert "SYNTHETIC_PRIVATE_ERROR" not in json.dumps(report)
    assert report["attention"]["delivery_unsettled"] == {"attempts": None, "oldest_age_s": None}
    assert report["overall"] == ("failed" if run_status == "failed" else "degraded")
    assert report["notify"]["state"] == "incomplete"
    assert "notification_cards_unavailable" in report["state_reasons"]
    assert stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_limit
    assert stored.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    assert stored.in_transaction
    assert stored.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 "
                          "FROM n WHERE x<2000) SELECT SUM(x) FROM n").fetchone()[0] == 2001000
    stored.commit()
    assert stored.execute("SELECT COUNT(*) FROM runs WHERE started_at=?", (NOW,)).fetchone()[0] == 1
    path = tmp_path / "health.json"
    path.write_text(json.dumps(report))
    watched = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert watched["status"] == report["overall"]
    assert "notification_cards_unavailable" in watched["state_reasons"]
    spec = {"sources": {"health": {"kind": "json", "path": "health.json"}}, "fields": [
        {"name": "delivery_unsettled_attempts", "source": "health",
         "path": "attention.delivery_unsettled.attempts", "type": "int"}]}
    snap = status_snapshot.snapshot(str(tmp_path), spec, now=NOW)
    assert snap["unavailable"] == ["delivery_unsettled_attempts"]


@pytest.mark.parametrize("overall,expected", [("ok", "degraded"), ("degraded", "degraded"),
                                             ("failed", "failed")])
def test_monitor_rechecks_unknown_cards_even_if_producer_reasons_are_missing(tmp_path, overall, expected):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"at": NOW, "overall": overall, "state_reasons": [],
                                "cards": {"status": "unknown"}}))
    watched = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert watched["status"] == expected
    assert watched["state_reasons"] == ["notification_cards_unavailable"]
    stale = health_watch.classify_health(str(path), NOW + 1801, 1800)
    assert stale["status"] == "stale" and stale["state_reasons"] is None


@pytest.mark.parametrize("unknown", ["cards", "parts", "both", "unread", "intake"])
def test_unknown_blocks_never_hide_unreadable_reasons_or_delay_alert(tmp_path, unknown):
    health = {"at": NOW, "overall": "degraded", "state_reasons": ["notification_pending:3"]}
    if unknown in ("cards", "both"):
        health["cards"] = {"status": "unknown"}
    if unknown in ("parts", "both"):
        health["notify"] = {"incomplete_parts": {"status": "unknown"}}
    if unknown == "unread":
        health["unread_at"] = None
    if unknown == "intake":
        health["semantic_intake_unstored"] = "unverified"
    path = tmp_path / "data" / "health.json"
    path.parent.mkdir()
    path.write_text(json.dumps(health))
    watched = health_watch.evaluate(str(tmp_path), NOW + 1, cfg={})
    assert watched["state_reasons"] is None
    assert watched["alert"] and not watched["alert_held"]
