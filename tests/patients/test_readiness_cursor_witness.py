"""The real owned cursor records complete SQLite read observations."""
import sqlite3
import time
from types import SimpleNamespace

import pytest

import patient_context_refresh as refresh
import patient_readiness_witness as witness


@pytest.fixture
def owned(tmp_path):
    path = tmp_path / 'synthetic.sqlite'
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY, pid INTEGER, value TEXT)')
    connection.executemany('INSERT INTO sample VALUES(?,?,?)', [(1, 1, 'a'), (2, 1, 'b'), (3, 2, 'c')])
    connection.commit()
    yield SimpleNamespace(db=connection)
    connection.close()


def reader(owned):
    return refresh._candidate_reader(owned, time.monotonic() + 60)


def test_real_cursor_capture_replay_and_reuse(owned):
    with reader(owned) as source:
        db = source.db
        assert db.execute('PRAGMA query_only').fetchone()[0] == 1
        with witness.Recording(db) as recording:
            cursor = db.cursor()
            cursor.execute('SELECT id,value FROM sample WHERE pid=? ORDER BY id', [1])
            assert cursor.fetchone()['value'] == 'a'
            assert [row['value'] for row in cursor.fetchmany(2)] == ['b']
            assert cursor.fetchone() is None and cursor.fetchone() is None
            cursor.execute('SELECT id FROM sample WHERE pid=? ORDER BY id', (2,))
            assert [row['id'] for row in cursor] == [3]
            cursor.close()
            result = recording.seal()
            assert result is not None
        with reader(owned) as check:
            assert result.matches(check.db, time.monotonic() + 30, time.monotonic)
        owned.db.execute("UPDATE sample SET value='changed' WHERE id=1")
        owned.db.commit()
        with reader(owned) as check:
            assert not result.matches(check.db, time.monotonic() + 30, time.monotonic)


@pytest.mark.parametrize('method', ['fetchone', 'fetchmany', 'description'])
def test_cursor_started_before_recording_cannot_certify_later_reads(owned, method):
    with reader(owned) as source:
        db = source.db
        before = db.execute('SELECT id FROM sample ORDER BY id')
        with witness.Recording(db) as recording:
            if method == 'description':
                assert before.description[0][0] == 'id'
            else:
                assert getattr(before, method)()
            before.close()
            assert db.execute('SELECT 1').fetchone()[0] == 1
            assert recording.seal() is None


@pytest.mark.parametrize('method', ['execute', 'fetchone', 'fetchmany'])
def test_cancelled_cursor_cannot_seal_even_if_error_is_caught(owned, method):
    with reader(owned) as source:
        db = source.db
        with witness.Recording(db) as recording:
            cursor = db.execute('SELECT id FROM sample ORDER BY id')
            db.scan_until = time.monotonic() - 1
            try:
                with pytest.raises(refresh._ScanInterrupted):
                    cursor.execute('SELECT 1') if method == 'execute' else getattr(cursor, method)()
            finally:
                db.scan_until = time.monotonic() + 30
                cursor.close()
            assert db.execute('SELECT 1').fetchone()[0] == 1
            assert recording.seal() is None


def test_cursor_reuse_freezes_parameters_and_repeated_eof(owned):
    with reader(owned) as source:
        db = source.db
        with witness.Recording(db) as recording:
            cursor = db.cursor()
            values = [bytearray(b'a')]
            cursor.execute('SELECT ? AS value', values)
            values[0][:] = b'z'
            assert cursor.fetchone()['value'] == b'a'
            assert cursor.fetchone() is None and cursor.fetchone() is None
            cursor.execute('SELECT id FROM sample ORDER BY id')
            assert cursor.fetchmany(1)[0]['id'] == 1
            assert [row['id'] for row in cursor.fetchall()] == [2, 3]
            cursor.close()
            result = recording.seal()
        with reader(owned) as check:
            assert result is not None and result.matches(check.db, time.monotonic() + 30, time.monotonic)


def test_inline_fetchone_is_finalized_and_replayed(owned):
    with reader(owned) as source:
        with witness.Recording(source.db) as recording:
            assert source.db.execute('SELECT value FROM sample WHERE id=?', (1,)).fetchone()['value'] == 'a'
            result = recording.seal()
        with reader(owned) as check:
            assert result is not None and result.matches(check.db, time.monotonic() + 30, time.monotonic)


def test_capture_limit_and_unknown_udf_keep_normal_reading(owned, monkeypatch):
    with reader(owned) as source:
        db = source.db
        db.create_function('pure_unknown', 0, lambda: 42, deterministic=True)
        with witness.Recording(db) as recording:
            assert db.execute('SELECT pure_unknown()').fetchone()[0] == 42
            assert db.execute('SELECT count(*) FROM sample').fetchone()[0] == 3
            assert recording.seal() is None
        monkeypatch.setattr(witness, 'MAX_ROWS', 1)
        with witness.Recording(db) as recording:
            assert [row[0] for row in db.execute('SELECT id FROM sample ORDER BY id').fetchall()] == [1, 2, 3]
            assert db.execute('SELECT count(*) FROM sample').fetchone()[0] == 3
            assert recording.seal() is None


def test_caught_sqlite_fetch_error_invalidates_recording(owned):
    with reader(owned) as source:
        db = source.db
        calls = []

        def failing(value):
            calls.append(value)
            if len(calls) > 1:
                raise ValueError('synthetic failure')
            return 1

        db.create_function('mcs_thin_fact_count', 1, failing, deterministic=True)
        with witness.Recording(db) as recording:
            cursor = db.execute('SELECT mcs_thin_fact_count(value) FROM sample ORDER BY id')
            with pytest.raises(sqlite3.Error):
                cursor.fetchone()
            cursor.close()
            assert db.execute('SELECT 1').fetchone()[0] == 1
            assert recording.seal() is None


@pytest.mark.parametrize('method', ['executemany', 'executescript'])
def test_unsupported_cursor_methods_invalidate_before_delegating(owned, method):
    with reader(owned) as source:
        db = source.db
        with witness.Recording(db) as recording:
            cursor = db.cursor()
            with pytest.raises(sqlite3.Error):
                if method == 'executemany':
                    cursor.executemany('SELECT ?', [(1,)])
                else:
                    cursor.executescript('SELECT 1;')
            cursor.close()
            assert recording.seal() is None


@pytest.mark.parametrize('kind', ['factory', 'row_factory', 'cursor_factory', 'function', 'progress'])
def test_unsupported_connection_changes_keep_original_read_and_disable_seal(owned, kind):
    with reader(owned) as source:
        db = source.db
        with witness.Recording(db) as recording:
            if kind == 'factory':
                db.row_factory = None
            elif kind == 'row_factory':
                cursor = db.cursor()
                cursor.row_factory = None
                cursor.close()
            elif kind == 'cursor_factory':
                assert db.cursor(factory=sqlite3.Cursor).execute('SELECT 1').fetchone()[0] == 1
            elif kind == 'function':
                db.create_function('pure_synthetic', 0, lambda: 42)
                assert db.execute('SELECT pure_synthetic()').fetchone()[0] == 42
            else:
                db.set_progress_handler(lambda: 0, 1000)
            assert db.execute('SELECT 1').fetchone()[0] == 1
            assert recording.seal() is None


def test_existing_authorizer_is_not_replaced(owned):
    with reader(owned) as source:
        db, called = source.db, []
        db.set_authorizer(lambda *args: called.append(True) or sqlite3.SQLITE_OK)
        with witness.Recording(db) as recording:
            assert db.execute('SELECT 1').fetchone()[0] == 1
            assert recording.seal() is None
        called.clear()
        assert db.execute('SELECT 2').fetchone()[0] == 2
        assert called


def test_authorizer_change_during_capture_is_preserved_and_invalidates(owned):
    with reader(owned) as source:
        db, called = source.db, []
        with witness.Recording(db) as recording:
            assert db.execute('SELECT id FROM sample WHERE id=1').fetchone()[0] == 1
            db.set_authorizer(lambda *args: called.append(True) or sqlite3.SQLITE_OK)
            assert recording.seal() is None
        called.clear()
        assert db.execute('SELECT 2').fetchone()[0] == 2
        assert called


@pytest.mark.parametrize('size', [0, 1, 5, -1])
def test_fetchmany_matches_sqlite_api_without_recording(owned, size):
    with reader(owned) as source:
        cursors = [source.db.execute('SELECT id FROM sample ORDER BY id'),
                   owned.db.execute('SELECT id FROM sample ORDER BY id')]
        results = []
        for cursor in cursors:
            try:
                results.append(('ok', [row[0] for row in cursor.fetchmany(size)]))
            except Exception as error:
                results.append(('error', type(error)))
            finally:
                cursor.close()
        assert results[0] == results[1]


@pytest.mark.parametrize('args,kwargs', [((), {}), ((None,), {}), ((), {'size': 1}), ((1, 2), {})])
def test_fetchmany_argument_contract_matches_sqlite(owned, args, kwargs):
    with reader(owned) as source:
        cursors = [source.db.execute('SELECT id FROM sample ORDER BY id'),
                   owned.db.execute('SELECT id FROM sample ORDER BY id')]
        results = []
        for cursor in cursors:
            try:
                results.append(('ok', [row[0] for row in cursor.fetchmany(*args, **kwargs)]))
            except Exception as error:
                results.append(('error', type(error), str(error)))
            finally:
                cursor.close()
        assert results[0] == results[1]
