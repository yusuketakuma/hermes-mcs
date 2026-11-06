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

    def read(db, *args, **kwargs):
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
    assert report["cards"]["observation"]["phase"] == "result_assembly"
    assert report["cards"]["observation"]["stop"] == "complete"
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

    def read(db, cfg=None, **kwargs):
        if failure == "unavailable":
            raise ValueError("SYNTHETIC_PRIVATE_ERROR")
        clock[0] += health_report.NOTIFICATION_OBSERVATION_S + 0.1
        db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 "
                   "FROM n WHERE x<2000) SELECT SUM(x) FROM n").fetchone()
        pytest.fail("card SQL must be interrupted at the observation deadline")

    monkeypatch.setattr(notify_scope, "health_cards", read)
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, run_status, cfg={})
    cards = report["cards"]
    assert cards["completion"]["status"] == "unknown"
    assert cards["completion"]["observed_at"] == NOW
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


def test_exhausted_sql_budget_reports_unknowns_never_fake_counts(stored, monkeypatch):
    """When the shared observation window is spent, every health SQL read
    stops at its bound and reports honest 'unknown' — a slow table can no
    longer stall the tick, and nothing reads as a healthy zero."""
    clock = iter(range(100, 10000))
    monkeypatch.setattr(health_report.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(health_report, "HEALTH_SQL_BUDGET_S", 0.001)
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg={})
    notify = report["notify"]
    assert notify["held_reasons"]["status"] == "unknown"
    assert notify["held"] is None and notify["retired_held"] is None
    assert notify["state"] == "incomplete"
    assert report["semantic_jobs"]["pending"] is None
    assert report["extract_qc_jobs"]["status"] == "unknown"
    assert report["extract_v2_coverage"]["status"] == "unknown"
    assert report["extract_v2_coverage"]["current"] is None
    assert report["attention"]["status"] == "unknown"
    assert report["semantic_intake_unstored"] == "unverified"
    assert report["overall"] == "degraded"
    # the tick's connection survived every bound: no leaked savepoint or handler
    assert stored.execute("SELECT 1").fetchone()[0] == 1


def test_pending_count_exhaustion_is_unknown_not_a_fake_zero(stored, monkeypatch):
    def fail_breakdown(*args, **kwargs):
        raise RuntimeError("synthetic diagnostic failure")
    monkeypatch.setattr(notify_scope, "pending_breakdown", fail_breakdown)
    clock = iter(range(100, 10000))
    monkeypatch.setattr(health_report.time, "monotonic", lambda: next(clock))
    report = health_report.pending_health(stored, NOW, seconds=0.001)
    assert report["pending"] is None
    assert report["pending_by_reason"]["status"] == "unavailable"
    assert report["pending_by_reason"]["reason"] == "observation_limit"


def test_pending_count_failure_still_propagates_real_errors(stored, monkeypatch):
    def fail_breakdown(*args, **kwargs):
        raise RuntimeError("synthetic diagnostic failure")
    monkeypatch.setattr(notify_scope, "pending_breakdown", fail_breakdown)
    monkeypatch.setattr(health_report.time, "monotonic", lambda: 100.0)
    stored.execute("DROP TABLE notify_outbox")
    with pytest.raises(sqlite3.Error):
        health_report.pending_health(stored, NOW)


def test_depleted_budget_never_starts_the_read(stored):
    """seconds<=0 is a depleted window: no SQL, no callback, no savepoint —
    the 'stop at the deadline' contract holds before any work begins."""
    calls = []
    with pytest.raises(RuntimeError, match="observation_limit"):
        health_report.bounded_health_read(
            stored, lambda check: calls.append(check), seconds=0)
    with pytest.raises(RuntimeError, match="observation_limit"):
        health_report.bounded_health_read(
            stored, lambda check: calls.append(check), seconds=-1)
    assert calls == []
    # nothing touched the connection: no busy_timeout change, no savepoint
    assert stored.execute("SELECT 1").fetchone()[0] == 1


def test_unobserved_pending_count_is_incomplete_never_ok(stored, tmp_path, monkeypatch):
    """Breakdown stops at its bound and the fallback count exhausts too:
    an unmeasured pending queue is incomplete work, never a healthy zero —
    producer reasons and the monitor's own recheck must both show it."""
    monkeypatch.setattr(
        health_report, "pending_health",
        lambda *a, **k: {"pending": None, "oldest_age_s": None,
                         "pending_by_reason": {"status": "unavailable", "observed_at": NOW,
                                               "reason": "observation_limit"}})
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg={})
    assert report["notify"]["pending"] is None
    assert report["notify"]["state"] == "incomplete"
    assert report["overall"] == "degraded"
    assert "notification_pending_unavailable" in report["state_reasons"]
    assert "notification_pending" not in report["state_reasons"]
    path = tmp_path / "health.json"
    path.write_text(json.dumps(report))
    watched = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert watched["status"] == "degraded"
    assert watched["notify_pending"] is None
    assert "notification_pending_unavailable" in watched["state_reasons"]


@pytest.mark.parametrize("overall,expected", [("ok", "degraded"), ("degraded", "degraded"),
                                             ("failed", "failed")])
def test_monitor_rechecks_unknown_pending_even_if_producer_reasons_are_missing(
        tmp_path, overall, expected):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"at": NOW, "overall": overall, "state_reasons": [],
                                "notify": {"pending": None}}))
    watched = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert watched["status"] == expected
    assert watched["state_reasons"] == ["notification_pending_unavailable"]
    stale = health_watch.classify_health(str(path), NOW + 1801, 1800)
    assert stale["status"] == "stale" and stale["state_reasons"] is None


def test_observed_pending_zero_stays_healthy(stored, tmp_path):
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg={})
    assert report["notify"]["pending"] == 0
    assert report["notify"]["state"] == report["overall"] == "ok"
    assert "notification_pending_unavailable" not in report["state_reasons"]
    path = tmp_path / "health.json"
    path.write_text(json.dumps(report))
    watched = health_watch.classify_health(str(path), NOW + 1, 1800)
    assert watched["status"] == "ok" and watched["notify_pending"] == 0


def test_coverage_joins_count_distinct_messages(stored, monkeypatch):
    """The join-driven coverage COUNTs must keep EXISTS semantics: a message
    with several matching artifacts still counts once."""
    stored.execute(
        "INSERT INTO messages(message_id,project_id,body_text,body_state,"
        "content_hash,first_seen) VALUES(1,1,'body','full','h'*64,1)")
    for _ in range(2):  # two poison artifacts on ONE message
        stored.execute(
            "INSERT INTO artifacts(kind,message_id,project_id,content,meta,created_at) "
            "VALUES('extract_llm',1,1,'{}','not-json',1)")
    stored.commit()
    monkeypatch.setattr(health_report, "prev_health", lambda: {})
    report = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg={})
    coverage = report["extract_v2_coverage"]
    assert coverage["poison_gated"] == 1 and coverage["eligible"] == 1
    assert coverage["current"] == 0 and coverage["status"] == "recorded"


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


def test_card_phase_markers_add_no_sql_or_delivery_changes(stored):
    statements = []
    stored.set_trace_callback(statements.append)
    try:
        original = notify_scope.health_cards(stored, {})
        original_sql = list(statements)
        statements.clear()
        observation = {}
        observed = notify_scope.health_cards(stored, {}, observation=observation)
    finally:
        stored.set_trace_callback(None)
    assert observed == original
    assert statements == original_sql
    assert observation == {"phase": "result_assembly"}


@pytest.mark.parametrize("helper,phase", [("layout_migration_counts", "layout_inventory"),
                                         ("_delivery_latency", "delivery_latency")])
def test_failed_card_phase_is_fixed_and_preserves_unknown(stored, monkeypatch, helper, phase):
    clock = [100.0]
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])

    def unavailable(*args):
        clock[0] += 0.125
        raise ValueError("SYNTHETIC_PRIVATE_ERROR")

    monkeypatch.setattr(notify_scope, helper, unavailable)
    cards = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg={})["cards"]
    assert cards["observation"] == {"phase": phase, "budget_s": 2.0,
                                    "elapsed_s": 0.125, "remaining_s": 1.875,
                                    "stop": "read_unavailable"}
    assert cards["status"] == cards["completion"]["status"] == "unknown"
    assert cards["pending"] is None and cards["completion"]["reason"] == "observation_unavailable"
    assert "SYNTHETIC_PRIVATE_ERROR" not in json.dumps(cards)


@pytest.mark.parametrize("failure,stop,reason", [
    ("sql_time", "time_limit", "observation_limit"),
    ("length", "row_size_limit", "observation_unavailable"),
    ("busy", "storage_busy", "observation_unavailable"),
    ("interrupt", "read_unavailable", "observation_limit"),
])
def test_card_stop_classification_uses_causal_fixed_codes(stored, monkeypatch, failure, stop, reason):
    clock = [100.0]
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])
    stored.execute("PRAGMA busy_timeout=7000")
    original_limit = stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    stored.execute("INSERT INTO runs(started_at,status,error) VALUES(?,'ok','')", (NOW,))
    before = stored.total_changes

    def read(db, cfg=None, *, observation=None):
        observation["phase"] = "completion_counts"
        if failure == "sql_time":
            clock[0] += 2.1
            db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 "
                       "FROM n WHERE x<2000) SELECT SUM(x) FROM n").fetchone()
        elif failure == "length":
            db.execute("SELECT zeroblob(?)", (health_report.HEALTH_OBSERVATION_ROW_BYTES + 1,)).fetchone()
        elif failure == "busy":
            error = sqlite3.OperationalError("SYNTHETIC_PRIVATE_ERROR")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        else:
            # An interrupt without this observer's expired progress handler is
            # insufficient evidence to label the cause as its time limit.
            error = sqlite3.OperationalError("interrupted")
            error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
            raise error
        pytest.fail("the synthetic observation should stop")

    monkeypatch.setattr(notify_scope, "health_cards", read)
    cards = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok", cfg={})["cards"]
    assert cards["observation"]["phase"] == "completion_counts"
    assert cards["observation"]["stop"] == stop
    assert cards["observation"]["budget_s"] == 2.0
    assert cards["observation"]["remaining_s"] >= 0
    assert cards["reason"] == reason and cards["pending"] is None
    assert "SYNTHETIC_PRIVATE_ERROR" not in json.dumps(cards)
    assert stored.total_changes == before and stored.in_transaction
    assert stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_limit
    assert stored.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    assert stored.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 "
                          "FROM n WHERE x<2000) SELECT SUM(x) FROM n").fetchone()[0] == 2001000


def test_card_phases_share_one_window_and_caller_deadline(stored, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])

    def read(db, cfg=None, *, observation=None):
        observation["phase"] = "card_counts"
        clock[0] += 0.3
        observation["phase"] = "delivery_latency"
        # Changing phases never starts a new window.
        clock[0] += 0.3
        return {"pending": 0}

    monkeypatch.setattr(notify_scope, "health_cards", read)
    cards = health_report.build_health(stored, {"errors": [], "notify": {}}, "ok",
                                      cfg={}, deadline=100.5)["cards"]
    assert cards["observation"] == {"phase": "delivery_latency", "budget_s": 0.5,
                                    "elapsed_s": 0.6, "remaining_s": 0.0, "stop": "time_limit"}
    assert cards["status"] == "unknown" and cards["pending"] is None


def test_depleted_card_budget_records_no_read(stored, monkeypatch):
    monkeypatch.setattr(health_report.time, "monotonic", lambda: 100.0)
    calls = []
    statements = []
    observation = {}
    stored.set_trace_callback(statements.append)
    try:
        with pytest.raises(RuntimeError, match="observation_limit"):
            health_report.bounded_health_read(stored, lambda check: calls.append(check),
                                              seconds=0, observation=observation)
    finally:
        stored.set_trace_callback(None)
    assert calls == statements == []
    assert observation == {"phase": "connection_setup", "budget_s": 0.0,
                           "elapsed_s": 0.0, "remaining_s": 0.0, "stop": "budget_exhausted"}


def test_item_limit_diagnostic_keeps_existing_boundary(stored, monkeypatch):
    monkeypatch.setattr(health_report.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(health_report, "HEALTH_OBSERVATION_ITEMS", 2)
    observation = {}
    checked = []

    def read(check):
        for _ in range(3):
            check()
            checked.append(True)

    with pytest.raises(RuntimeError, match="observation_limit"):
        health_report.bounded_health_read(stored, read, seconds=2, observation=observation)
    assert checked == [True, True]
    assert observation["stop"] == "item_limit"


def test_card_failure_diagnostics_cannot_replace_read_failure(stored, monkeypatch):
    clock_calls = [0]

    def clock():
        clock_calls[0] += 1
        if clock_calls[0] > 2:
            raise ValueError("SYNTHETIC_CLOCK_FAILURE")
        return 100.0

    monkeypatch.setattr(health_report.time, "monotonic", clock)
    original_limit = stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    stored.execute("PRAGMA busy_timeout=7000")

    def read(check):
        raise RuntimeError("SYNTHETIC_READ_FAILURE")

    with pytest.raises(RuntimeError, match="SYNTHETIC_READ_FAILURE"):
        health_report.bounded_health_read(stored, read, seconds=2, observation={})
    assert stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_limit
    assert stored.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    assert stored.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["release", "restore", "both"])
def test_card_cleanup_failure_is_unknown_and_attributed_to_cleanup(stored, monkeypatch, failure):
    monkeypatch.setattr(health_report.time, "monotonic", lambda: 100.0)
    stored.execute("PRAGMA busy_timeout=7000")
    original_limit = stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    calls = []
    errors = {"release": sqlite3.OperationalError("SYNTHETIC_PRIVATE_RELEASE"),
              "restore": sqlite3.OperationalError("SYNTHETIC_PRIVATE_RESTORE")}

    class CleanupFailure:
        armed = True

        def __getattr__(self, name):
            return getattr(stored, name)

        def set_progress_handler(self, callback, frequency):
            if self.armed and callback is None:
                calls.append("handler_clear")
            return stored.set_progress_handler(callback, frequency)

        def setlimit(self, category, limit):
            if self.armed and limit == original_limit:
                calls.append("limit_restore")
            return stored.setlimit(category, limit)

        def execute(self, sql, *args):
            result = stored.execute(sql, *args)
            if self.armed and sql == "RELEASE mcs_health_observation":
                calls.append("release")
                if failure in ("release", "both"):
                    raise errors["release"]
            if self.armed and sql == "PRAGMA busy_timeout=7000":
                calls.append("wait_restore")
                self.armed = False
                if failure in ("restore", "both"):
                    raise errors["restore"]
            return result

    proxy = CleanupFailure()
    # Check the original cleanup exception object and precedence directly;
    # the outer producer must then report the same technical failure as unknown.
    observation = {}
    with pytest.raises(sqlite3.OperationalError) as raised:
        health_report.bounded_health_read(proxy, lambda check: {}, seconds=2,
                                          observation=observation)
    assert raised.value is errors["restore" if failure in ("restore", "both") else "release"]
    assert calls == ["handler_clear", "limit_restore", "release", "wait_restore"]
    assert observation["phase"] == "connection_cleanup"
    assert observation["stop"] == "read_unavailable"
    calls.clear()
    proxy.armed = True
    cards = health_report.build_health(proxy, {"errors": [], "notify": {}}, "ok", cfg={})["cards"]
    assert cards["status"] == cards["completion"]["status"] == "unknown"
    assert cards["pending"] is None and cards["reason"] == "observation_unavailable"
    assert cards["observation"]["phase"] == "connection_cleanup"
    assert cards["observation"]["stop"] == "read_unavailable"
    assert "SYNTHETIC_PRIVATE" not in json.dumps(cards)
    assert calls == ["handler_clear", "limit_restore", "release", "wait_restore"]
    assert stored.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_limit
    assert stored.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    assert not stored.in_transaction
