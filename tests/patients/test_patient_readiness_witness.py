"""Synthetic SQL witnesses retain exact observations without storing rows."""
import json
import sqlite3

import pytest

import patient_readiness_witness as witness
import drug_map


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
    setup.execute('CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT, content TEXT)')
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


@pytest.mark.parametrize('mutation,expected', [
    ('cursor', True), ('generation_id', False), ('generation_hash', False),
    ('generation_approval', False), ('generation_version', False),
    ('invalid_cursor', False), ('invalid_to_other', False), ('deleted', False),
    ('null_generation', False), ('later_valid', True), ('later_invalid', False),
])
def test_drug_generation_only_compares_validity_and_entire_generation(dbs, mutation, expected):
    reader, writer = dbs
    generation = ['synthetic', 'a' * 64, False, drug_map.RESOLVER_VERSION]
    original = {'dictionary': generation, 'cursor': 1}
    if mutation == 'invalid_to_other':
        original = {'dictionary': generation, 'cursor': -1}
    writer.execute('INSERT INTO artifacts VALUES(1,?,?)', (drug_map.PROGRESS_KIND, json.dumps(original)))
    writer.commit()
    with witness.Recording(reader, drug_generation_only=True) as recording:
        assert not recording.drug_progress_observed
        collect(recording, reader, drug_map.ANNOTATION_PROGRESS_SQL, (drug_map.PROGRESS_KIND,))
        assert recording.drug_progress_observed
        result = recording.seal()
    assert result is not None
    changed = {'dictionary': list(generation), 'cursor': 1}
    if mutation in ('cursor', 'later_valid'):
        changed['cursor'] = 2
    elif mutation == 'generation_id':
        changed['dictionary'][0] = 'another-synthetic'
    elif mutation == 'generation_hash':
        changed['dictionary'][1] = 'b' * 64
    elif mutation == 'generation_approval':
        changed['dictionary'][2] = True
    elif mutation == 'generation_version':
        changed['dictionary'][3] = 'wrong-version'
    elif mutation == 'invalid_cursor':
        changed['cursor'] = True
    elif mutation == 'invalid_to_other':
        changed['cursor'] = -2
    elif mutation == 'null_generation':
        changed['dictionary'] = None
    elif mutation == 'later_invalid':
        changed['cursor'] = -1
    if mutation == 'deleted':
        writer.execute('DELETE FROM artifacts WHERE artifact_id=1')
    elif mutation.startswith('later_'):
        writer.execute('INSERT INTO artifacts VALUES(2,?,?)', (drug_map.PROGRESS_KIND, json.dumps(changed)))
    else:
        writer.execute('UPDATE artifacts SET content=? WHERE artifact_id=1', (json.dumps(changed),))
    writer.commit()
    assert matches(result, writer) is expected


@pytest.mark.parametrize('opt_in,alter_sql', [(False, False), (True, True)])
def test_drug_progress_requires_opt_in_and_exact_query(dbs, opt_in, alter_sql):
    reader, writer = dbs
    progress = {'dictionary': None, 'cursor': 1}
    writer.execute('INSERT INTO artifacts VALUES(1,?,?)', (drug_map.PROGRESS_KIND, json.dumps(progress)))
    writer.commit()
    sql = drug_map.ANNOTATION_PROGRESS_SQL + (' ' if alter_sql else '')
    with witness.Recording(reader, drug_generation_only=opt_in) as recording:
        collect(recording, reader, sql, (drug_map.PROGRESS_KIND,))
        assert not recording.drug_progress_observed
        result = recording.seal()
    progress['cursor'] = 2
    writer.execute('UPDATE artifacts SET content=?', (json.dumps(progress),))
    writer.commit()
    assert not matches(result, writer)


def test_missing_drug_progress_is_not_valid_progress(dbs):
    reader, writer = dbs
    with witness.Recording(reader, drug_generation_only=True) as recording:
        collect(recording, reader, drug_map.ANNOTATION_PROGRESS_SQL, (drug_map.PROGRESS_KIND,))
        assert recording.drug_progress_observed
        result = recording.seal()
    writer.execute('INSERT INTO artifacts VALUES(1,?,?)',
                   (drug_map.PROGRESS_KIND, json.dumps({'dictionary': None, 'cursor': 1})))
    writer.commit()
    assert not matches(result, writer)


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


def test_unknown_udf_stays_readable_but_writes_are_denied(dbs):
    reader, _ = dbs
    calls = []
    reader.create_function('unsafe_func', 0, lambda: calls.append('called') or 1)
    assert reader.execute('SELECT unsafe_func()').fetchone()[0] == 1
    calls.clear()
    with witness.Recording(reader) as recording:
        assert reader.execute('SELECT unsafe_func()').fetchone()[0] == 1
        assert recording.seal() is None and calls == ['called']
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


def test_limit_disables_witness_without_changing_following_reads(dbs, monkeypatch):
    reader, _ = dbs
    monkeypatch.setattr(witness, 'MAX_ROWS', 1)
    with witness.Recording(reader) as recording:
        sql = 'SELECT id FROM observations ORDER BY id'
        cursor = reader.execute(sql)
        handle = recording.start(sql, (), cursor.description)
        values = []
        for row in cursor:
            values.append(row[0])
            handle.row(row)
        handle.eof()
        cursor.close()
        assert values == [1, 2, 3]
        assert reader.execute('SELECT count(*) FROM observations').fetchone()[0] == 3
        assert recording.seal() is None


def test_repeated_eof_is_harmless_but_a_later_row_invalidates(dbs):
    reader, writer = dbs
    sql = 'SELECT id FROM observations WHERE id=1'
    with witness.Recording(reader) as recording:
        cursor = reader.execute(sql)
        handle = recording.start(sql, (), cursor.description)
        row = cursor.fetchone()
        handle.row(row)
        assert cursor.fetchone() is None
        handle.eof()
        assert cursor.fetchone() is None
        handle.eof()
        cursor.close()
        result = recording.seal()
    assert result is not None and matches(result, writer)
    with witness.Recording(reader) as recording:
        cursor = reader.execute(sql)
        handle = recording.start(sql, (), cursor.description)
        row = cursor.fetchone()
        handle.row(row)
        assert cursor.fetchone() is None
        handle.eof()
        handle.row(row)
        cursor.close()
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
    with witness.Recording(reader) as recording:
        assert isinstance(reader.execute('SELECT CURRENT_TIMESTAMP').fetchone()[0], str)
        assert recording.seal() is None
    with witness.Recording(reader) as recording:
        with pytest.raises(sqlite3.Error):
            reader.execute('PRAGMA query_only=OFF').fetchone()
        assert recording.seal() is None


def test_sealed_observation_cannot_accept_later_sql(dbs):
    reader, writer = dbs
    with witness.Recording(reader) as recording:
        collect(recording, reader, 'SELECT id FROM observations WHERE id=1')
        result = recording.seal()
        assert result is not None
        assert reader.execute('SELECT value FROM observations WHERE id=2').fetchone()[0] == 'b'
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
