"""Bound stale GC's real SQLite waits and statements using synthetic databases."""
from contextlib import closing, nullcontext
import sqlite3
import time
from types import SimpleNamespace

import pytest
import extract_llm_selection as selection


def test_gc_writer_contention_respects_budget_and_restores_connection(tmp_path, monkeypatch):
    path = str(tmp_path / 'synthetic.sqlite3')
    with closing(sqlite3.connect(path)) as blocker, closing(sqlite3.connect(path)) as db:
        blocker.execute('CREATE TABLE synthetic(value)')
        blocker.commit()
        blocker.execute('BEGIN IMMEDIATE')
        db.execute('PRAGMA busy_timeout=1500')
        monkeypatch.setattr(selection, '_stale_gc_at', 0)
        monkeypatch.setattr(selection, '_STALE_GC_BUDGET_S', .03)
        monkeypatch.setattr(selection, '_writer_current', lambda: True)
        started = time.monotonic()
        selection._gc_stale_artifacts(SimpleNamespace(db=db),
            lambda *args, **kwargs: nullcontext(True), True)
        assert time.monotonic() - started < .75
        assert not db.in_transaction
        assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 1500
        blocker.rollback()
        with db:
            db.execute('INSERT INTO synthetic VALUES(1)')
        assert db.execute('SELECT value FROM synthetic').fetchone()[0] == 1


def test_gc_sql_interrupt_rolls_back_and_releases_writer(tmp_path):
    path = str(tmp_path / 'synthetic.sqlite3')
    with closing(sqlite3.connect(path)) as db, closing(sqlite3.connect(path)) as other:
        db.execute('CREATE TABLE synthetic(value)')
        db.execute('PRAGMA busy_timeout=987')
        with pytest.raises(selection._StaleGCDeadline) as caught:
            with selection._stale_gc_transaction(db, time.monotonic() + .03):
                db.execute('INSERT INTO synthetic VALUES(1)')
                db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL '
                           'SELECT x+1 FROM n) SELECT sum(x) FROM n').fetchone()
        assert caught.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        assert not db.in_transaction
        assert db.execute('SELECT COUNT(*) FROM synthetic').fetchone()[0] == 0
        assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 987
        # An expired progress handler would interrupt this later query.
        assert db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL '
                          'SELECT x+1 FROM n WHERE x<10000) SELECT max(x) FROM n').fetchone()[0] == 10000
        with other:
            other.execute('INSERT INTO synthetic VALUES(2)')


def test_gc_transaction_does_not_hide_sql_errors(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'synthetic.sqlite3')) as db:
        with pytest.raises(sqlite3.OperationalError, match='no such table'):
            with selection._stale_gc_transaction(db, time.monotonic() + 1):
                db.execute('INSERT INTO missing_table VALUES(1)')
        assert not db.in_transaction
