"""Only the migration commit moves the schema version fence (synthetic ledgers only)."""
import sqlite3
from contextlib import closing

import ledger
import ledger_schema


def _version(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def test_new_and_reopened_ledgers_carry_the_current_version(tmp_path):
    path = str(tmp_path / "ledger.db")
    ledger.Ledger(path).close()
    assert _version(path) == ledger.SCHEMA_VERSION
    ledger.Ledger(path).close()
    assert _version(path) == ledger.SCHEMA_VERSION


def test_initialize_itself_never_moves_the_version(tmp_path, monkeypatch):
    # the migration runs for real, then its fence is undone; nothing after it may re-advance
    real = ledger_schema._migrate

    def migrate_then_undo_fence(db, **kwargs):
        real(db, **kwargs)
        db.execute("PRAGMA user_version=0")
    monkeypatch.setattr(ledger_schema, "_migrate", migrate_then_undo_fence)
    path = str(tmp_path / "ledger.db")
    ledger.Ledger(path).close()
    assert _version(path) == 0
