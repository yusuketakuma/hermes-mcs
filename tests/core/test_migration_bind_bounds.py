"""Old notification boundaries survive parameter batching and transaction failure."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

import ledger


def historical_database(tmp_path, *, abort_after=None):
    path = tmp_path / "synthetic-legacy.db"
    fixture = Path(__file__).resolve().parents[1] / "fixtures/schema_upgrade/shape-6.sql"
    ids = list(range(900000100, 900001200))
    with closing(sqlite3.connect(path)) as db:
        db.executescript(fixture.read_text())
        version = db.execute("PRAGMA user_version").fetchone()[0]
        db.execute("INSERT INTO patients(project_id) VALUES(900000001)")
        db.executemany("INSERT INTO messages(message_id,project_id,is_unread) VALUES(?,900000001,1)",
                       [(mid,) for mid in [*ids, 900001300]])
        db.execute("INSERT INTO notify_outbox(kind,project_id,payload) VALUES('new_messages',900000001,?)",
                   (json.dumps({"message_ids": ids}),))
        if abort_after is not None:
            db.execute("ALTER TABLE messages ADD COLUMN notified_at REAL")
            db.executescript(f"""
                CREATE TRIGGER synthetic_abort_notification_boundary BEFORE UPDATE OF notified_at ON messages
                WHEN (SELECT COUNT(*) FROM messages WHERE notified_at IS NOT NULL)>={abort_after}
                BEGIN SELECT RAISE(ABORT,'synthetic boundary failure'); END;
            """)
        db.commit()
    return path, version, ids


def limited_connections(monkeypatch):
    connect = sqlite3.connect

    def limited(*args, **kwargs):
        db = connect(*args, **kwargs)
        # Python 3.10 has the legacy default cap; setlimit is available from 3.11.
        if hasattr(db, "setlimit"):
            db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return db

    monkeypatch.setattr(ledger.sqlite3, "connect", limited)


def test_old_notification_backfill_binds_bounded_batches_with_one_timestamp(tmp_path, monkeypatch):
    path, version, ids = historical_database(tmp_path)
    limited_connections(monkeypatch)
    assert version < 7
    with closing(ledger.Ledger(str(path))) as db:
        observed = db.db.execute("SELECT message_id,notified_at FROM messages WHERE notified_at IS NOT NULL").fetchall()
        assert {row[0] for row in observed} == set(ids)
        stamp = observed[0][1]
        assert all(row[1] == stamp for row in observed)
        assert db.db.execute("SELECT notified_at FROM messages WHERE message_id=900001300").fetchone()[0] is None
        assert db.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
    with closing(ledger.Ledger(str(path))) as reopened:
        assert {row[0] for row in reopened.db.execute("SELECT notified_at FROM messages WHERE notified_at IS NOT NULL")} == {stamp}


def test_later_batch_failure_rolls_back_earlier_batch_and_version(tmp_path, monkeypatch):
    path, version, ids = historical_database(tmp_path, abort_after=900)
    limited_connections(monkeypatch)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic boundary failure"):
        ledger.Ledger(str(path))
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert db.execute("SELECT COUNT(*) FROM messages WHERE notified_at IS NOT NULL").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == len(ids) + 1
        db.execute("DROP TRIGGER synthetic_abort_notification_boundary")
        db.commit()
    with closing(ledger.Ledger(str(path))) as retried:
        assert retried.db.execute("SELECT COUNT(*) FROM messages WHERE notified_at IS NOT NULL").fetchone()[0] == len(ids)
