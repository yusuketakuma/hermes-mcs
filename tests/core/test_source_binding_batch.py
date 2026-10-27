"""Synthetic equivalence for ordered, snapshot-bound occurrence fences."""
from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

import inference_dependency_loss as loss
from common_testkit import make_ledger
from extract_testkit import _message


@pytest.fixture
def ledger(tmp_path):
    store = make_ledger(tmp_path)
    store.ensure_patient(1)
    store.ensure_patient(2)
    for mid, pid in ((1, 1), (2, 1), (3, 2)):
        store.save_messages([_message(mid=mid, project_id=pid, body=f'完全合成の投稿{mid}です。')])
    yield store
    store.close()


def compare(db, pairs):
    db.execute('SAVEPOINT source_test_snapshot')
    try:
        expected = [loss.source_binding(db, pid, mid) for pid, mid in pairs]
        assert loss.source_bindings(db, pairs) == expected
        return expected
    finally:
        db.execute('RELEASE source_test_snapshot')


@pytest.mark.parametrize('count', [0, 1, 64])
def test_limit_and_duplicates(ledger, count):
    pairs = [(1, 1)] * count
    results = compare(ledger.db, pairs)
    assert len(results) == count
    if count:
        assert len(set(results)) == 1 and results[0] is not None
    with pytest.raises(ValueError, match='batch_limit'):
        loss.source_bindings(ledger.db, [(1, 1)] * 65)


def test_order_missing_and_other_patient(ledger):
    pairs = [(2, 3), (1, 1), (1, 3), (2, 1), (1, 99), (1, 1)]
    out = compare(ledger.db, pairs)
    assert out[0] and out[1] and out[0] != out[1]
    assert out[2:5] == [None, None, None]
    assert out[5] == out[1]


@pytest.mark.parametrize('field,value', [
    ('content_hash', 'b' * 64), ('body_text', '別の合成本文'), ('body_state', 'deleted'),
    ('parent_id', 999), ('sender_id', 'synthetic-other'), ('sender_name', '別の合成投稿者'),
    ('sender_type', 'synthetic-role'), ('profession', '合成職種'), ('organization', '合成組織'),
    ('posted_at', '2026-10-02T09:00:00+09:00'), ('posted_at_ts', 1790899200.0),
])
def test_every_source_field_changes_binding(ledger, field, value):
    original = loss.source_binding(ledger.db, 1, 1)
    ledger.db.execute(f'UPDATE messages SET {field}=? WHERE message_id=1', (value,))
    changed, = compare(ledger.db, [(1, 1)])
    assert changed != original
    assert ledger.db.in_transaction
    ledger.db.rollback()
    assert loss.source_binding(ledger.db, 1, 1) == original


def test_source_aba_is_distinct_but_unrelated_source_unchanged(ledger):
    original = _message(mid=1, project_id=1, body='完全合成の投稿1です。')
    first = loss.source_bindings(ledger.db, [(1, 1), (1, 2)])
    ledger.save_messages([replace(original, body_html='更新された完全合成投稿です。')])
    second = loss.source_bindings(ledger.db, [(1, 1), (1, 2)])
    ledger.save_messages([original])
    third = compare(ledger.db, [(1, 1), (1, 2)])
    assert len({first[0], second[0], third[0]}) == 3
    assert first[1] == second[1] == third[1]


def test_selected_occurrence_binding_is_snapshot_bound_and_incomplete_row_rejected(ledger):
    row = ledger.db.execute('SELECT m.message_id,m.project_id,' + loss.SOURCE_BINDING_COLUMNS
                            + ' FROM messages m WHERE m.message_id=1').fetchone()
    original = loss.source_binding_from_row(row)
    assert original == loss.source_binding(ledger.db, 1, 1)
    assert loss.source_binding_from_row({'body_text': row['body_text']}) is None
    ledger.save_messages([_message(mid=1, project_id=1, body='別の完全合成投稿です。')])
    assert loss.source_binding_from_row(row) == original
    assert loss.source_binding(ledger.db, 1, 1) != original


def test_uncommitted_change_is_seen_and_caller_rollback_remains_owned(ledger):
    before = loss.source_binding(ledger.db, 1, 1)
    ledger.db.execute("UPDATE messages SET body_text='変更された合成本文' WHERE message_id=1")
    changed, = compare(ledger.db, [(1, 1)])
    assert changed != before and ledger.db.in_transaction
    ledger.db.rollback()
    assert loss.source_binding(ledger.db, 1, 1) == before


def test_empty_and_invalid_input_do_not_execute_sql(ledger):
    statements = []
    ledger.db.set_trace_callback(statements.append)
    try:
        assert loss.source_bindings(ledger.db, []) == []
        for bad in (iter([(1, 1)]), [(1, True)], [(1, '1')], [(1, 2**63)], [(1,)]):
            with pytest.raises(TypeError):
                loss.source_bindings(ledger.db, bad)
        assert not statements
    finally:
        ledger.db.set_trace_callback(None)


def test_missing_revision_schema_is_only_read_if_message_exists():
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE messages (project_id INTEGER, message_id INTEGER, '
                   + ','.join(field + ' TEXT' for field in loss._SOURCE_FIELDS) + ')')
        assert loss.source_bindings(db, [(1, 1)]) == [None]
        db.execute('INSERT INTO messages(project_id,message_id) VALUES(1,1)')
        with pytest.raises(sqlite3.OperationalError, match='message_revisions'):
            loss.source_bindings(db, [(1, 99), (1, 1)])
        assert db.in_transaction
        db.rollback()
    finally:
        db.close()


@pytest.mark.parametrize('seqs,expected_seq', [
    ([], 1), ([None], 1), ([0], 0), ([-1], -1), ([None, 1], 1), ([0, 5], 5),
])
def test_legacy_nullable_revision_preserves_sql_max_and_coalesce(seqs, expected_seq):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE messages (project_id INTEGER, message_id INTEGER, '
                   + ','.join(field + ' TEXT' for field in loss._SOURCE_FIELDS) + ')')
        db.execute('CREATE TABLE message_revisions(message_id INTEGER, seq INTEGER)')
        fields = [f'合成項目{i}' for i in range(len(loss._SOURCE_FIELDS))]
        columns = ','.join(loss._SOURCE_FIELDS)
        placeholders = ','.join('?' for _ in fields)
        db.execute('INSERT INTO messages(project_id,message_id,' + columns + ') VALUES(1,1,' + placeholders + ')', fields)
        db.executemany('INSERT INTO message_revisions(message_id,seq) VALUES(1,?)', [(seq,) for seq in seqs])
        expected = hashlib.sha256(json.dumps([fields, expected_seq], sort_keys=True, ensure_ascii=False,
                                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        assert loss.source_binding(db, 1, 1) == expected
        assert loss.source_bindings(db, [(1, 1), (1, 1)]) == [expected, expected]
        assert db.in_transaction
        db.rollback()
    finally:
        db.close()


@pytest.mark.parametrize('code', [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, None])
def test_interruption_never_replays_singles(ledger, monkeypatch, code):
    error = sqlite3.OperationalError('synthetic interruption')
    if code is not None:
        error.sqlite_errorcode = code
    def fail(*args):
        raise error
    monkeypatch.setattr(loss, '_source_bindings_page', fail)
    monkeypatch.setattr(loss, 'source_binding', lambda *a, **k: pytest.fail('unexpected retry'))
    with pytest.raises(sqlite3.OperationalError) as raised:
        loss.source_bindings(ledger.db, [(1, 1), (1, 2)])
    assert raised.value is error and not ledger.db.in_transaction


def test_noninterrupt_error_replays_singles_in_original_order(ledger, monkeypatch):
    def fail_page(*args):
        error = sqlite3.OperationalError('too many SQL variables')
        error.sqlite_errorcode = sqlite3.SQLITE_ERROR
        raise error
    visited = []
    def fail_single(db, pid, mid):
        visited.append((pid, mid))
        if mid == 2:
            raise ValueError('first ordered failure')
        return None
    monkeypatch.setattr(loss, '_source_bindings_page', fail_page)
    monkeypatch.setattr(loss, 'source_binding', fail_single)
    with pytest.raises(ValueError, match='first ordered failure'):
        loss.source_bindings(ledger.db, [(1, 1), (1, 2), (1, 3)])
    assert visited == [(1, 1), (1, 2)]


@pytest.mark.parametrize('kind', ['runtime', 'type', 'value', 'unknown_sql', 'unclassified_sql'])
def test_unexpected_batch_error_never_becomes_a_successful_single_read(ledger, monkeypatch, kind):
    errors = {'runtime': RuntimeError('synthetic runtime failure'),
              'type': TypeError('synthetic type failure'),
              'value': ValueError('synthetic value failure'),
              'unknown_sql': sqlite3.OperationalError('synthetic unrecognized SQLite error'),
              'unclassified_sql': sqlite3.OperationalError('too many SQL variables')}
    errors['unknown_sql'].sqlite_errorcode = sqlite3.SQLITE_ERROR
    error = errors[kind]
    ledger.db.execute("UPDATE messages SET body_text='transaction-local synthetic text' WHERE message_id=1")
    monkeypatch.setattr(loss, '_source_bindings_page', lambda *_: (_ for _ in ()).throw(error))
    monkeypatch.setattr(loss, 'source_binding', lambda *_: pytest.fail('unexpected single replay'))
    with pytest.raises(type(error)) as caught:
        loss.source_bindings(ledger.db, [(1, 1)])
    assert caught.value is error and ledger.db.in_transaction
    assert ledger.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0] == 'transaction-local synthetic text'
    ledger.db.rollback()


def test_real_sqlite_variable_limit_replays_ordered_singles(ledger):
    pairs = [(1, 1), (1, 2), (2, 3)]
    expected = [loss.source_binding(ledger.db, *pair) for pair in pairs]
    ledger.db.execute("UPDATE messages SET body_text=body_text WHERE message_id=1")
    old_limit = ledger.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 2)
    try:
        assert loss.source_bindings(ledger.db, pairs) == expected
        assert ledger.db.in_transaction
    finally:
        ledger.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)
        ledger.db.rollback()


@pytest.mark.parametrize('message', [
    'Expression tree is too large (maximum depth 1000)',
    'no such table: message_revisions',
])
def test_known_sqlite_compatibility_error_replays_ordered_singles(ledger, monkeypatch, message):
    pairs = [(1, 2), (1, 1), (1, 99)]
    expected = [loss.source_binding(ledger.db, *pair) for pair in pairs]
    original = loss.source_binding
    visited = []
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = sqlite3.SQLITE_ERROR

    def single(db, pid, mid):
        visited.append((pid, mid))
        return original(db, pid, mid)

    ledger.db.execute("UPDATE messages SET body_text=body_text WHERE message_id=1")
    monkeypatch.setattr(loss, '_source_bindings_page', lambda *_: (_ for _ in ()).throw(error))
    monkeypatch.setattr(loss, 'source_binding', single)
    assert loss.source_bindings(ledger.db, pairs) == expected
    assert visited == pairs and ledger.db.in_transaction
    ledger.db.rollback()
