"""Synthetic error and transaction ownership checks for rollup readers."""
import extract_llm_provider

import rollup_builder
import sqlite3

import pytest

import extract_llm
import inference_dependency_loss
import rollup


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE marker(value INTEGER)")
    yield connection
    connection.close()


@pytest.mark.parametrize("reader,inner", [
    (lambda db: rollup.build_rollup(db, 1), "_build_rollup"),
    (lambda db: rollup.current_cached_refs(db, 1, {}, None), "_current_cached_refs"),
])
def test_readers_keep_borrowed_transaction_after_success_and_failure(db, monkeypatch, reader, inner):
    db.execute("INSERT INTO marker VALUES (7)")
    monkeypatch.setattr(rollup, inner, lambda *_args, **_kwargs: {"synthetic": True})
    assert reader(db) == {"synthetic": True}
    error = ValueError("synthetic failure")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(rollup, inner, fail)
    with pytest.raises(ValueError) as caught:
        reader(db)
    assert caught.value is error
    assert db.in_transaction and db.execute("SELECT value FROM marker").fetchall() == [(7,)]
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0


@pytest.mark.parametrize("reader,inner,name", [
    (lambda db: rollup.build_rollup(db, 1), "_build_rollup", "rollup_build_read"),
    (lambda db: rollup.current_cached_refs(db, 1, {}, None), "_current_cached_refs", "rollup_cache_read"),
])
def test_success_does_not_hide_release_failure(db, monkeypatch, reader, inner, name):
    monkeypatch.setattr(rollup, inner, lambda *_args, **_kwargs: {"synthetic": True})
    db.set_authorizer(lambda action, arg1, arg2, *_: sqlite3.SQLITE_DENY
                      if action == sqlite3.SQLITE_SAVEPOINT and arg1 == "RELEASE"
                      and arg2 == name else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            reader(db)
    finally:
        db.set_authorizer(None)
        db.rollback()


@pytest.mark.parametrize("reader,inner", [
    (lambda db: rollup.build_rollup(db, 1), "_build_rollup"),
    (lambda db: rollup.current_cached_refs(db, 1, {}, None), "_current_cached_refs"),
])
@pytest.mark.parametrize("code", [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_FULL,
                                  sqlite3.SQLITE_IOERR | (3 << 8)])
def test_auto_rollback_preserves_original_error(db, monkeypatch, reader, inner, code):
    db.execute("INSERT INTO marker VALUES (7)")
    error = sqlite3.OperationalError("synthetic SQLite failure")
    error.sqlite_errorcode = code

    def fail(connection, *_args, **_kwargs):
        connection.rollback()  # Simulate SQLite discarding the entire transaction.
        raise error

    monkeypatch.setattr(rollup, inner, fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        reader(db)
    assert caught.value is error and caught.value.sqlite_errorcode == code
    assert not db.in_transaction and db.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0


@pytest.mark.parametrize("reader,inner", [
    (lambda db: rollup.build_rollup(db, 1), "_build_rollup"),
    (lambda db: rollup.current_cached_refs(db, 1, {}, None), "_current_cached_refs"),
])
def test_nested_source_binding_auto_rollback_propagates_to_caller(db, monkeypatch, reader, inner):
    db.execute("CREATE TABLE messages(message_id INTEGER)")
    db.execute("INSERT INTO marker VALUES (7)")
    error = sqlite3.DatabaseError("synthetic disk failure")
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR | (3 << 8)
    replay = []

    def fail(connection, _pairs):
        connection.rollback()
        raise error

    monkeypatch.setattr(inference_dependency_loss, "_source_bindings_page", fail)
    monkeypatch.setattr(inference_dependency_loss, "source_binding", lambda *_: replay.append(True))
    monkeypatch.setattr(rollup, inner, lambda connection, *_args, **_kwargs:
                        inference_dependency_loss.source_bindings(connection, [(1, 1)]))
    with pytest.raises(sqlite3.DatabaseError) as caught:
        reader(db)
    assert caught.value is error and not replay and not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_FULL,
                                  sqlite3.SQLITE_IOERR | (3 << 8)])
def test_resource_failure_never_replays_per_message(db, monkeypatch, code):
    error = sqlite3.DatabaseError("synthetic batch failure")
    if code is not None:
        error.sqlite_errorcode = code
    replay = []

    def fail(*_args):
        raise error

    monkeypatch.setattr(extract_llm, "resolved_model", lambda _cfg: "synthetic-model")
    monkeypatch.setattr(extract_llm_provider, 'resolved_model', extract_llm.resolved_model)
    monkeypatch.setattr(rollup_builder, "_measurement_source_page", fail)
    monkeypatch.setattr(rollup_builder, "_measurement_source_bound", lambda *_args, **_kwargs: replay.append(True))
    with pytest.raises(sqlite3.DatabaseError) as caught:
        rollup_builder._measurement_sources_bound(db, [{"message_id": 1}], ["extract_llm"], {}, {})
    assert caught.value is error and not replay


@pytest.mark.parametrize('kind', ['runtime', 'type', 'value', 'unknown_sql', 'unclassified_sql'])
def test_unexpected_measurement_batch_error_does_not_become_current(db, monkeypatch, kind):
    errors = {'runtime': RuntimeError('synthetic runtime failure'),
              'type': TypeError('synthetic type failure'),
              'value': ValueError('synthetic value failure'),
              'unknown_sql': sqlite3.OperationalError('synthetic unrecognized SQLite error'),
              'unclassified_sql': sqlite3.OperationalError('too many SQL variables')}
    errors['unknown_sql'].sqlite_errorcode = sqlite3.SQLITE_ERROR
    error = errors[kind]
    db.execute('INSERT INTO marker VALUES (7)')
    monkeypatch.setattr(rollup_builder, '_measurement_source_page', lambda *_: (_ for _ in ()).throw(error))
    monkeypatch.setattr(rollup_builder, '_measurement_source_bound', lambda *_args, **_kwargs: pytest.fail('unexpected single replay'))
    with pytest.raises(type(error)) as caught:
        rollup_builder._measurement_sources_bound(db, [{'message_id': 1}], ['extract_llm'], {}, {}, expected_model='synthetic-model')
    assert caught.value is error and db.in_transaction
    assert db.execute('SELECT value FROM marker').fetchall() == [(7,)]
    db.rollback()


@pytest.mark.parametrize('message', [
    'Expression tree is too large (maximum depth 1000)',
    'no such table: message_revisions',
])
def test_known_measurement_batch_sqlite_error_replays_single(db, monkeypatch, message):
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = sqlite3.SQLITE_ERROR
    visited = []
    db.execute('INSERT INTO marker VALUES (7)')
    monkeypatch.setattr(rollup_builder, '_measurement_source_page', lambda *_: (_ for _ in ()).throw(error))

    def single(_db, row, _kind, _cfg, **kwargs):
        visited.append((row['message_id'], kwargs['artifact_ref'], kwargs['expected_model']))
        return True

    monkeypatch.setattr(rollup_builder, '_measurement_source_bound', single)
    rows = [{'message_id': 1, 'body_state': 'full', 'body_text': '合成本文'}]
    result = rollup_builder._measurement_sources_bound(db, rows, ['extract_llm'], {(1, 'extract_llm'): 9}, {},
                                                expected_model='synthetic-model')
    assert result == [True] and visited == [(1, 9, 'synthetic-model')]
    assert db.in_transaction and db.execute('SELECT value FROM marker').fetchall() == [(7,)]
    db.rollback()
