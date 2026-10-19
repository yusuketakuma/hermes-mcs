"""A failed savepoint release still restores the tick connection's settings. Synthetic DB only."""
import health_report
import sqlite3

import pytest



class _ReleaseFails:
    """Delegate to a real connection, failing only the observation RELEASE."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, *args):
        if sql.startswith("RELEASE mcs_health_observation"):
            raise sqlite3.OperationalError("synthetic release failure")
        return self.db.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.db, name)


def test_busy_timeout_and_limits_are_restored_when_release_fails(tmp_path):
    db = sqlite3.connect(tmp_path / "ledger.db")
    try:
        db.execute("PRAGMA busy_timeout=7000")
        limit = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
        with pytest.raises(sqlite3.OperationalError, match="synthetic release failure"):
            health_report.bounded_health_read(_ReleaseFails(db), lambda check: 1)
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
        assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == limit
        # The restored connection still writes with its own settings.
        db.execute("ROLLBACK TO mcs_health_observation")
        db.execute("RELEASE mcs_health_observation")
        db.execute("CREATE TABLE t(x)")
        db.execute("INSERT INTO t VALUES(1)")
        db.commit()
        assert db.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        db.close()


def test_a_successful_read_still_returns_and_restores(tmp_path):
    db = sqlite3.connect(tmp_path / "ledger.db")
    try:
        db.execute("PRAGMA busy_timeout=7000")
        assert health_report.bounded_health_read(db, lambda check: "ok") == "ok"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
        assert not db.in_transaction
    finally:
        db.close()
