"""Synthetic accounting order index: optional, exact, built only by a saved daily pass (H5)."""
import json
import sqlite3
import time
from contextlib import closing

import pytest

import cloud_policy as c
import ledger_schema
import maintenance
from ledger import Ledger
from test_cloud_policy import _reserve, _result, now

__all__ = ["now"]

DAY = 86400
INDEX = ledger_schema.CLOUD_USAGE_ORDER_INDEX


def _state(db):
    return ledger_schema.cloud_usage_order_index_state(db)


def _create(db):
    with db:
        db.execute(ledger_schema.CLOUD_USAGE_ORDER_DDL)


def _history(ledger, now):
    """Reservations and results whose created_at order and insertion order differ from artifact_id,
    with equal timestamps, a late result for an out-of-period reservation and a kind change."""
    reservations = [_reserve(ledger, unit=f"unit-{index}") for index in range(6)]
    for reservation in reservations[:4]:
        c.record_result(ledger, reservation, _result(reservation))
    ids = [row[0] for row in ledger.db.execute(
        "SELECT artifact_id FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') "
        "ORDER BY artifact_id")]
    with ledger.db:
        for position, artifact_id in enumerate(reversed(ids)):         # later ids get earlier times
            ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?",
                              (now - 600 + position // 2, artifact_id))  # pairs share a timestamp
        # a reservation from last period whose result arrives now (an orphan of the tally)
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (now - 70 * DAY, ids[0]))
        # a kind change in place: the row leaves the accounting history
        ledger.db.execute("UPDATE artifacts SET kind='cloud_usage_dryrun' WHERE artifact_id=?", (ids[-1],))
    return reservations


def _decisions(ledger, now):
    """Fresh tallies, the usage report and a refused reservation, as the budget checks see them."""
    ledger._cloud_usage_tally = None
    ledger._cloud_window_tally = None
    tally = c._tally(ledger, now)
    window = c._window_tally(ledger, now - DAY)
    return (str(tally.total), tally.last_id, bool(tally.invalid), sorted(tally.orphans), tally.prefix,
            str(window.total), window.prefix,
            c.usage_report(ledger, now=now), c.usage_report(ledger, now=now, period="day"))


def _traced(ledger, call):
    statements = []
    ledger.db.set_trace_callback(statements.append)
    try:
        call()
    finally:
        ledger.db.set_trace_callback(None)
    return statements


def test_canonical_ddl_is_what_sqlite_stores_and_matches_the_readers_predicate(tmp_path):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        assert _state(ledger.db) == "absent"
        _create(ledger.db)
        stored = ledger.db.execute("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?", (INDEX,)).fetchone()
        assert tuple(stored) == ("index", "artifacts", ledger_schema.CLOUD_USAGE_ORDER_DDL)
        assert _state(ledger.db) == "present"
        # the readers repeat the index predicate verbatim, so the partial index applies
        import inspect
        source = inspect.getsource(c)
        assert source.count(f"\"WHERE {ledger_schema.CLOUD_USAGE_ORDER_KINDS_SQL}") == 3
        # it is not one of the H4 guard objects
        assert INDEX not in ledger_schema._CLOUD_USAGE_DDL


@pytest.mark.parametrize("query", ["fingerprint", "update", "report"])
def test_present_index_removes_the_temporary_sort_from_each_reader(tmp_path, now, query):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _history(ledger, now)
        _create(ledger.db)
        tally = c._tally(ledger, now)
        source = c._accounting_order_source(ledger.db)
        assert source == f"artifacts INDEXED BY {INDEX}"
        base = (f"SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM {source} "
                f"WHERE {ledger_schema.CLOUD_USAGE_ORDER_KINDS_SQL} AND created_at>=?")
        sql, params = {"fingerprint": (base + " AND artifact_id<=? ORDER BY artifact_id", (tally.month, tally.last_id)),
                       "update": (base + " AND artifact_id>? ORDER BY artifact_id", (tally.month, 0)),
                       "report": (base + " ORDER BY artifact_id", (tally.month,))}[query]
        plan = " | ".join(row[3] for row in ledger.db.execute("EXPLAIN QUERY PLAN " + sql, params))
        assert INDEX in plan and "TEMP B-TREE" not in plan
        # the real readers go through the index too
        statements = _traced(ledger, lambda: _decisions(ledger, now))
        assert sum(f"INDEXED BY {INDEX}" in sql for sql in statements) >= 3


def test_answers_are_the_same_with_and_without_the_index(tmp_path, now):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _history(ledger, now)
        without = _decisions(ledger, now)
        _create(ledger.db)
        assert _decisions(ledger, now) == without
        # a reservation refused for a repeat input is refused the same way with the index
        with pytest.raises(Exception) as indexed:
            _reserve(ledger, unit="unit-0")
        with ledger.db:
            ledger.db.execute(f"DROP INDEX {INDEX}")
        with pytest.raises(Exception) as plain:
            _reserve(ledger, unit="unit-0")
        assert (type(indexed.value), indexed.value.args) == (type(plain.value), plain.value.args)


@pytest.mark.parametrize("other", [
    f"CREATE INDEX {INDEX} ON artifacts(artifact_id) WHERE kind IN ('cloud_usage_reserve')",
    f"CREATE INDEX {INDEX} ON artifacts(artifact_id, created_at) "
    f"WHERE {ledger_schema.CLOUD_USAGE_ORDER_KINDS_SQL}",
    f"CREATE TABLE {INDEX}(artifact_id INTEGER)",
    f"CREATE VIEW {INDEX} AS SELECT artifact_id FROM artifacts",
])
def test_a_different_object_with_the_name_keeps_the_original_reads_and_is_never_replaced(tmp_path, now, other):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _history(ledger, now)
        expected = _decisions(ledger, now)
        guard = ledger_schema.cloud_usage_guard_state(ledger.db)
        with ledger.db:
            ledger.db.execute(other)
        stored = ledger.db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (INDEX,)).fetchone()
        assert _state(ledger.db) == "schema_mismatch"
        assert c._accounting_order_source(ledger.db) == "artifacts"
        statements = _traced(ledger, lambda: _decisions(ledger, now))
        assert not any("INDEXED BY " + INDEX in sql for sql in statements)
        assert _decisions(ledger, now) == expected
        assert ledger_schema.install_cloud_usage_order_index(ledger.db, deadline=time.monotonic() + 30) \
            == "schema_mismatch"
        assert ledger.db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (INDEX,)).fetchone() == stored
        assert ledger_schema.cloud_usage_guard_state(ledger.db) == guard


def test_opening_the_ledger_never_builds_the_index(tmp_path):
    path = str(tmp_path / "synthetic.db")
    for _ in range(3):
        with closing(Ledger(path)) as ledger:
            assert _state(ledger.db) == "absent"


def test_install_rechecks_inside_its_transaction_and_rolls_back_past_the_deadline(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.db")
    Ledger(path).close()
    db = sqlite3.connect(path)
    try:
        assert ledger_schema.install_cloud_usage_order_index(db, deadline=time.monotonic() - 1) == "deadline"
        assert _state(db) == "absent" and not db.in_transaction
        # the DDL ran, but the deadline passed before commit: rolled back, nothing left
        clock = iter([0.0, 100.0])          # before BEGIN, then after the DDL
        monkeypatch.setattr(ledger_schema.time, "monotonic", lambda: next(clock))
        assert ledger_schema.install_cloud_usage_order_index(db, deadline=50.0) == "deadline"
        monkeypatch.undo()
        assert _state(db) == "absent" and not db.in_transaction
        assert ledger_schema.install_cloud_usage_order_index(db, deadline=time.monotonic() + 30) == "created"
        assert ledger_schema.install_cloud_usage_order_index(db, deadline=time.monotonic() + 30) == "present"
    finally:
        db.close()


def test_install_reports_present_when_another_writer_created_it_first(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.db")
    Ledger(path).close()
    db = sqlite3.connect(path)
    real = ledger_schema.cloud_usage_order_index_state
    calls = []

    def racing(connection):
        calls.append(None)
        if len(calls) == 2:           # after BEGIN IMMEDIATE: another writer already created it
            assert connection.in_transaction
            return "present"
        return real(connection)
    monkeypatch.setattr(ledger_schema, "cloud_usage_order_index_state", racing)
    try:
        assert ledger_schema.install_cloud_usage_order_index(db, deadline=time.monotonic() + 30) == "present"
        assert not db.in_transaction
    finally:
        db.close()


# ---------- the daily pass is the only builder ----------

def _maintained(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.db")
    Ledger(path).close()
    monkeypatch.setattr(maintenance, "DB_MAINT_STATE", str(tmp_path / "maint.json"))
    return path


def _index(path):
    with closing(sqlite3.connect(path)) as db:
        return _state(db)


def test_saved_daily_pass_builds_once_and_later_ticks_do_not_retry(tmp_path, monkeypatch):
    path = _maintained(tmp_path, monkeypatch)
    out = maintenance.db_maintenance(path, deadline=time.monotonic() + 300)
    assert out["cloud_usage_order_index"] == "created" and _index(path) == "present"
    assert maintenance.db_maintenance(path, deadline=time.monotonic() + 300) is None   # same day: nothing due
    state = json.loads((tmp_path / "maint.json").read_text())
    later = maintenance.db_maintenance(path, deadline=time.monotonic() + 300, now=state["daily_at"] + DAY + 1)
    assert later["cloud_usage_order_index"] == "present"


@pytest.mark.parametrize("deadline", [None, "short"])
def test_no_build_without_a_finite_deadline_with_enough_time(tmp_path, monkeypatch, deadline):
    path = _maintained(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(ledger_schema, "install_cloud_usage_order_index",
                        lambda *a, **k: calls.append(1) or "created")
    if deadline == "short":
        # the daily pass itself starts (30 s left) but nothing is left for the build afterwards
        clock = [1000.0]
        monkeypatch.setattr(maintenance, "monotonic", lambda: clock[0])
        real_prune = maintenance.prune_history

        def slow(*args, **kwargs):
            clock[0] += 20
            return real_prune(*args, **kwargs)
        monkeypatch.setattr(maintenance, "prune_history", slow)
        out = maintenance.db_maintenance(path, deadline=1000.0 + maintenance.DB_MAINT_MIN_S + 1)
    else:
        out = maintenance.db_maintenance(path)
    assert out["cloud_usage_order_index"] == "deadline" and calls == [] and _index(path) == "absent"
    assert json.loads((tmp_path / "maint.json").read_text())["daily_at"]


def test_unsaved_state_means_no_build_and_a_saved_retry_may_build(tmp_path, monkeypatch):
    path = _maintained(tmp_path, monkeypatch)
    real_write = maintenance.atomic_write

    def failing(*args, **kwargs):
        raise OSError("synthetic state write failure")
    monkeypatch.setattr(maintenance, "atomic_write", failing)
    out = maintenance.db_maintenance(path, deadline=time.monotonic() + 300)
    assert out["cloud_usage_order_index"] == "state_unpersisted" and _index(path) == "absent"
    monkeypatch.setattr(maintenance, "atomic_write", real_write)
    # nothing was saved, so the next tick is still a daily pass; once its state saves it builds
    out = maintenance.db_maintenance(path, deadline=time.monotonic() + 300)
    assert out["cloud_usage_order_index"] == "created"


def test_a_failed_daily_pass_never_builds(tmp_path, monkeypatch):
    path = _maintained(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(ledger_schema, "install_cloud_usage_order_index",
                        lambda *a, **k: calls.append(1) or "created")

    def broken(*args, **kwargs):
        raise maintenance.MaintenanceError("source_repair_retention_scope_invalid")
    monkeypatch.setattr(maintenance, "prune_history", broken)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.db_maintenance(path, deadline=time.monotonic() + 300)
    assert calls == [] and _index(path) == "absent"


def test_busy_build_keeps_the_saved_day_and_waits_for_the_next_daily_pass(tmp_path, monkeypatch):
    path = _maintained(tmp_path, monkeypatch)
    holder = sqlite3.connect(path)
    real = ledger_schema.install_cloud_usage_order_index

    def contended(db, *, deadline):
        holder.execute("BEGIN IMMEDIATE")
        try:
            return real(db, deadline=deadline)
        finally:
            holder.rollback()
    monkeypatch.setattr(ledger_schema, "install_cloud_usage_order_index", contended)
    started = time.monotonic()
    out = maintenance.db_maintenance(path, deadline=started + 300)
    assert out["cloud_usage_order_index"] == "busy" and time.monotonic() - started < 30
    holder.close()
    state = json.loads((tmp_path / "maint.json").read_text())
    assert state["daily_at"]                                               # the saved day is kept
    monkeypatch.setattr(ledger_schema, "install_cloud_usage_order_index", real)
    assert maintenance.db_maintenance(path, deadline=time.monotonic() + 300) is None   # no retry today
    retried = maintenance.db_maintenance(path, deadline=time.monotonic() + 300, now=state["daily_at"] + DAY + 1)
    assert retried["cloud_usage_order_index"] == "created"


def test_unexpected_database_errors_are_not_reported_as_a_missing_index(tmp_path, monkeypatch):
    path = _maintained(tmp_path, monkeypatch)

    def broken(db, *, deadline):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(ledger_schema, "install_cloud_usage_order_index", broken)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        maintenance.db_maintenance(path, deadline=time.monotonic() + 300)
    assert json.loads((tmp_path / "maint.json").read_text())["daily_at"]    # the daily pass stays done


def test_older_code_reads_the_same_answers_with_the_index_left_in_place(tmp_path, now, monkeypatch):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _history(ledger, now)
        _create(ledger.db)
        indexed = _decisions(ledger, now)
        monkeypatch.setattr(c, "_accounting_order_source", lambda db: "artifacts")   # a version without H5
        assert _decisions(ledger, now) == indexed
