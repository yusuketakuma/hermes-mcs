"""Synthetic SQL witnesses retain exact observations without storing rows."""
import sqlite3

import pytest

import patient_readiness_witness as witness


class OwnedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.witness_authorizer_present = False
        self.witness_authorizer_epoch = 0
        self.read_witness = None

    def set_authorizer(self, callback):
        self.witness_authorizer_epoch += 1
        self.witness_authorizer_present = callback is not None
        return super().set_authorizer(callback)


@pytest.fixture
def dbs(tmp_path):
    path = tmp_path / 'synthetic.sqlite'
    setup = sqlite3.connect(path)
    setup.execute('PRAGMA journal_mode=WAL')
    setup.execute('CREATE TABLE observations(id INTEGER PRIMARY KEY, project_id INTEGER, value)')
    setup.execute('CREATE TABLE financial(id INTEGER PRIMARY KEY, project_id INTEGER, meta TEXT)')
    setup.executemany('INSERT INTO observations VALUES (?,?,?)', [(1, 1, 'a'), (2, 1, 'b'), (3, 2, 'c')])
    setup.commit()
    reader = sqlite3.connect(path, factory=OwnedConnection)
    writer = sqlite3.connect(path)
    reader.row_factory = sqlite3.Row
    writer.row_factory = sqlite3.Row
    reader.execute('PRAGMA query_only=ON')
    reader.execute('BEGIN')
    yield reader, writer
    reader.close()
    writer.close()
    setup.close()


def collect(recording, db, sql, params=(), *, count=None):
    cursor = db.execute(sql, params)
    handle = recording.start(sql, params, cursor.description)
    while count is None or count > 0:
        row = cursor.fetchone()
        if row is None:
            handle.eof()
            break
        handle.row(row)
        if count is not None:
            count -= 1
    else:
        handle.close()
    cursor.close()


def record(reader, sql, params=(), *, count=None):
    with witness.Recording(reader) as recording:
        collect(recording, reader, sql, params, count=count)
        result = recording.seal()
        assert result is not None
        return result


def matches(result, writer, *, clock=lambda: 0, deadline=100):
    writer.execute('BEGIN IMMEDIATE')
    try:
        return result.matches(writer, deadline, clock)
    finally:
        writer.rollback()


@pytest.mark.parametrize('mutation', ['update', 'delete', 'insert', 'other_patient'])
def test_read_witness_rechecks_exact_patient_and_empty_results(dbs, mutation):
    reader, writer = dbs
    result = record(reader, 'SELECT id,value FROM observations WHERE project_id=? ORDER BY id', (1,))
    if mutation == 'update':
        writer.execute("UPDATE observations SET value='changed' WHERE id=1")
    elif mutation == 'delete':
        writer.execute('DELETE FROM observations WHERE id=1')
    elif mutation == 'insert':
        writer.execute("INSERT INTO observations VALUES(4,1,'new')")
    else:
        writer.execute("UPDATE observations SET value='other' WHERE id=3")
    writer.commit()
    assert matches(result, writer) is (mutation == 'other_patient')


def test_absence_and_eof_are_not_a_prefix(dbs):
    reader, writer = dbs
    result = record(reader, 'SELECT id FROM observations WHERE project_id=? ORDER BY id', (9,))
    writer.execute("INSERT INTO observations VALUES(9,9,'new')")
    writer.commit()
    assert not matches(result, writer)


def test_global_malformed_absence_ignores_valid_other_patient_commit(dbs):
    reader, writer = dbs
    sql = ("SELECT 1 FROM financial WHERE CASE WHEN NOT json_valid(meta) THEN 1 "
           "WHEN json_type(meta)!='object' THEN 1 ELSE 0 END LIMIT 1")
    result = record(reader, sql)
    writer.execute('INSERT INTO financial VALUES(1,2,?)', ('{"valid":true}',))
    writer.commit()
    assert matches(result, writer)
    writer.execute('INSERT INTO financial VALUES(2,2,?)', ('{invalid',))
    writer.commit()
    assert not matches(result, writer)


@pytest.mark.parametrize('insert_id,expected', [(0, False), (5, True)])
def test_closed_prefix_matches_without_inventing_eof(dbs, insert_id, expected):
    reader, writer = dbs
    result = record(reader, 'SELECT id FROM observations WHERE project_id=? ORDER BY id', (1,), count=1)
    writer.execute('INSERT INTO observations VALUES(?,?,?)', (insert_id, 1, 'synthetic'))
    writer.commit()
    assert matches(result, writer) is expected


def test_duplicate_queries_agree_and_replay_once(dbs):
    reader, writer = dbs
    sql, params = 'SELECT id,value FROM observations WHERE project_id=? ORDER BY id', (1,)
    with witness.Recording(reader) as recording:
        collect(recording, reader, sql, params, count=1)
        collect(recording, reader, sql, params)
        result = recording.seal()
    assert result is not None
    queries = []
    writer.set_trace_callback(queries.append)
    assert matches(result, writer)
    writer.set_trace_callback(None)
    assert sum('SELECT id,value FROM observations' in q for q in queries) == 1


def test_inconsistent_duplicate_or_unclosed_cursor_cannot_seal(dbs):
    reader, _ = dbs
    sql = 'SELECT id FROM observations ORDER BY id'
    with witness.Recording(reader) as recording:
        collect(recording, reader, sql, count=1)
        cursor = reader.execute(sql)
        handle = recording.start(sql, (), cursor.description)
        other = reader.execute('SELECT id FROM observations WHERE id=2').fetchone()
        handle.row(other)
        handle.close()
        cursor.close()
        assert recording.seal() is None
    with witness.Recording(reader) as recording:
        cursor = reader.execute(sql)
        recording.start(sql, (), cursor.description)
        assert recording.seal() is None
        cursor.close()


@pytest.mark.parametrize('value,expected', [(1, True), (1.0, False), ('1', False), (b'1', False), (None, False)])
def test_row_fingerprint_keeps_sqlite_types(dbs, value, expected):
    reader, writer = dbs
    writer.execute('UPDATE observations SET value=1 WHERE id=1')
    writer.commit()
    result = record(reader, 'SELECT value FROM observations WHERE id=?', (1,))
    writer.execute('UPDATE observations SET value=? WHERE id=1', (value,))
    writer.commit()
    assert matches(result, writer) is expected


def test_bound_parameters_are_copied_and_float_bits_are_stable(dbs):
    reader, writer = dbs
    params = {'value': bytearray(b'synthetic')}
    result = record(reader, 'SELECT :value AS value', params)
    params['value'][:] = b'changed!!'
    assert matches(result, writer)
    assert witness._value(0.0) != witness._value(-0.0)


def test_freeze_parameters_rejects_adapters_and_copies_mutable_inputs():
    source = {'one': bytearray(b'abc'), 'two': memoryview(b'def'), 'three': None}
    frozen = witness.freeze_parameters(source)
    source['one'][:] = b'xyz'
    assert frozen == {'one': b'abc', 'two': b'def', 'three': None}
    assert witness.freeze_parameters([True, 1, 1.0, 'x', b'y']) == (True, 1, 1.0, 'x', b'y')
    for invalid in (None, object(), [object()], {'one': object()}, {1: 'x'}):
        with pytest.raises(ValueError):
            witness.freeze_parameters(invalid)


def test_unknown_udf_and_writes_are_denied_even_if_prepared_before_recording(dbs):
    reader, _ = dbs
    calls = []
    reader.create_function('unsafe_func', 0, lambda: calls.append('called') or 1)
    assert reader.execute('SELECT unsafe_func()').fetchone()[0] == 1
    calls.clear()
    with witness.Recording(reader) as recording:
        with pytest.raises(sqlite3.Error):
            reader.execute('SELECT unsafe_func()').fetchone()
        assert recording.seal() is None and calls == []
    with witness.Recording(reader) as recording:
        with pytest.raises(sqlite3.Error):
            reader.execute('DELETE FROM observations')
        assert recording.seal() is None


def test_known_deterministic_udf_replays(dbs):
    reader, writer = dbs
    for db in (reader, writer):
        db.create_function('mcs_thin_fact_count', 1, lambda value: len(value or ''), deterministic=True)
    result = record(reader, 'SELECT mcs_thin_fact_count(value) FROM observations WHERE id=?', (1,))
    assert matches(result, writer)


def test_failed_observation_no_transaction_and_budget_limits_fail_closed(dbs, monkeypatch):
    reader, writer = dbs
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'SELECT id FROM observations ORDER BY id')
        recording.fail()
        assert recording.seal() is None
    monkeypatch.setattr(witness, 'MAX_ROWS', 1)
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'SELECT id FROM observations ORDER BY id')
        assert recording.seal() is None
    with witness.Recording(writer) as recording:
        assert recording.seal() is None


def test_deadline_and_inactive_writer_fail_closed(dbs):
    reader, writer = dbs
    result = record(reader, 'SELECT id FROM observations ORDER BY id')
    assert not result.matches(writer, 100, lambda: 0)
    assert not matches(result, writer, clock=lambda: 100)
    def cancelled():
        raise RuntimeError('synthetic cancellation')
    assert not matches(result, writer, clock=cancelled)


def test_data_version_is_relative_and_not_replayed(dbs):
    reader, writer = dbs
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'PRAGMA data_version')
        collect(recording, reader, 'SELECT id FROM observations WHERE project_id=?', (1,))
        result = recording.seal()
    writer.execute("UPDATE observations SET value='irrelevant' WHERE project_id=2")
    writer.commit()
    assert result is not None and matches(result, writer)


def test_unrelated_commit_during_owned_read_keeps_snapshot_witness(dbs):
    reader, writer = dbs
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'SELECT value FROM observations WHERE project_id=? ORDER BY id', (1,))
        writer.execute("UPDATE observations SET value='other change' WHERE id=3")
        writer.commit()
        result = recording.seal()
    assert result is not None and matches(result, writer)


def test_nondeterministic_function_and_mutating_pragma_fail_closed(dbs):
    reader, _ = dbs
    for sql in ('SELECT CURRENT_TIMESTAMP', 'PRAGMA query_only=OFF'):
        with witness.Recording(reader) as recording:
            with pytest.raises(sqlite3.Error):
                reader.execute(sql).fetchone()
            assert recording.seal() is None


def test_sealed_observation_cannot_accept_later_sql(dbs):
    reader, writer = dbs
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'SELECT id FROM observations WHERE id=1')
        result = recording.seal()
        assert result is not None
        with pytest.raises(sqlite3.Error):
            reader.execute('SELECT value FROM observations WHERE id=2').fetchone()
    assert not matches(result, writer)


def test_postseal_cursor_hook_invalidates_even_if_query_was_cached(dbs):
    reader, writer = dbs
    sql = 'SELECT id FROM observations WHERE id=1'
    with witness.Recording(reader) as recording:
        collect(recording, reader, sql)
        result = recording.seal()
        assert result is not None
        try:
            cursor = reader.execute(sql)
            recording.start(sql, (), cursor.description).close()
            cursor.close()
        except sqlite3.Error:
            pass
    assert not matches(result, writer)


def test_existing_or_changed_authorizer_is_not_overwritten(dbs):
    reader, _ = dbs
    calls = []
    reader.set_authorizer(lambda *args: calls.append(args) or sqlite3.SQLITE_OK)
    with witness.Recording(reader) as recording:
        assert recording.seal() is None
    reader.execute('SELECT value FROM observations WHERE id=1').fetchone()
    assert calls
    reader.set_authorizer(None)
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'SELECT id FROM observations WHERE id=1')
        reader.set_authorizer(lambda *args: calls.append(args) or sqlite3.SQLITE_OK)
        assert recording.seal() is None
    calls.clear()
    reader.execute('SELECT value FROM observations WHERE id=2').fetchone()
    assert calls and reader.read_witness is None


def test_authorizer_changed_after_seal_invalidates_returned_witness(dbs):
    reader, writer = dbs
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'SELECT id FROM observations WHERE id=1')
        result = recording.seal()
        assert result is not None
        reader.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
    assert not matches(result, writer)


def test_witness_cannot_match_before_recording_context_closes(dbs):
    reader, writer = dbs
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'SELECT id FROM observations WHERE id=1')
        result = recording.seal()
        assert result is not None and not matches(result, writer)
    assert matches(result, writer)


def test_non_sqlite_rows_and_changed_factory_cannot_be_sealed(dbs):
    reader, writer = dbs
    with witness.Recording(reader) as recording:
        cursor = reader.execute('SELECT id FROM observations WHERE id=1')
        handle = recording.start('SELECT id FROM observations WHERE id=1', (), cursor.description)
        handle.row({'id': 1})
        handle.close()
        cursor.close()
        assert recording.seal() is None
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'SELECT id FROM observations WHERE id=1')
        reader.row_factory = None
        assert recording.seal() is None
    reader.row_factory = sqlite3.Row
    result = record(reader, 'SELECT id FROM observations WHERE id=1')
    writer.row_factory = None
    assert not matches(result, writer)
