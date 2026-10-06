"""A failed read-only ledger initialization releases its SQLite connection."""
import sqlite3

import pytest

import ledger


def test_reader_init_query_failure_closes_connection(monkeypatch):
    closed = []

    class BrokenConnection:
        def execute(self, sql):
            raise sqlite3.DatabaseError('synthetic_unreadable_database')

        def close(self):
            closed.append(True)

    def connect(database_uri, **kwargs):
        assert database_uri.endswith('?mode=ro') and kwargs['uri'] is True
        return BrokenConnection()

    monkeypatch.setattr(ledger.sqlite3, 'connect', connect)
    with pytest.raises(sqlite3.DatabaseError, match='synthetic_unreadable_database'):
        ledger.LedgerReader('/synthetic.db')
    assert closed == [True]
