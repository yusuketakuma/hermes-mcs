"""A writer interrupted during initialization releases its owned connection."""
import sqlite3

import pytest

import ledger
import ledger_schema


@pytest.mark.parametrize('error', [KeyboardInterrupt, SystemExit])
def test_writer_initialization_interruption_closes_connection(tmp_path, monkeypatch, error):
    path = tmp_path / 'synthetic.db'
    connection = sqlite3.connect(path)
    monkeypatch.setattr(ledger.sqlite3, 'connect', lambda *args, **kwargs: connection)

    def interrupted(db, *, schema_version):
        raise error('synthetic interruption')

    monkeypatch.setattr(ledger_schema, 'initialize', interrupted)
    try:
        with pytest.raises(error):
            ledger.Ledger(str(path))
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')
    finally:
        connection.close()
