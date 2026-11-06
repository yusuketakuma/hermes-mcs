"""Synthetic SQLite writer failures preserve exceptions, transactions and privacy."""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

import ledger
import mcs_contract


KEYS = {'phase', 'sqlite_errorcode', 'sqlite_errorname',
        'transaction_open_before', 'elapsed_s', 'at'}
SYNTHETIC_SECRET = 'SYNTHETIC_PATIENT_AND_TOKEN_NEVER_EXPORT'


def message():
    return SimpleNamespace(body_html=SYNTHETIC_SECRET)


def diagnostic(exc):
    value = exc._mcs_message_write_failure
    assert set(value) == KEYS
    assert SYNTHETIC_SECRET not in json.dumps(value)
    assert value['phase'] == 'message_write_reserve'
    return value


@pytest.mark.parametrize('code', [None, True, '5', 5.0, -1, 0, 2**100])
def test_invalid_code_and_unapproved_fields_are_null(code):
    value = ledger.message_write_failure_diagnostic({
        'phase': 'message_write_reserve', 'sqlite_errorcode': code,
        'sqlite_errorname': SYNTHETIC_SECRET, 'transaction_open_before': 'yes',
        'elapsed_s': float('nan'), 'at': float('inf'), 'body': SYNTHETIC_SECRET,
    })
    assert set(value) == KEYS
    assert value == {'phase': 'message_write_reserve', 'sqlite_errorcode': None,
                     'sqlite_errorname': None, 'transaction_open_before': None,
                     'elapsed_s': None, 'at': None}


@pytest.mark.parametrize('name', ['SQLITE_LIMIT_LENGTH', 'SQLITE_BUSY_SNAPSHOT',
                                  SYNTHETIC_SECRET, None, True])
def test_constant_name_must_be_an_error_matching_the_integer(name):
    value = ledger.message_write_failure_diagnostic({
        'phase': 'message_write_reserve', 'sqlite_errorcode': sqlite3.SQLITE_BUSY,
        'sqlite_errorname': name, 'transaction_open_before': False,
        'elapsed_s': .001, 'at': 100.,
    })
    assert value['sqlite_errorcode'] == sqlite3.SQLITE_BUSY
    assert value['sqlite_errorname'] is None


@pytest.mark.parametrize('value', [None, [], SYNTHETIC_SECRET,
                                   {'phase': SYNTHETIC_SECRET}])
def test_unknown_diagnostic_is_not_serialized(value):
    assert ledger.message_write_failure_diagnostic(value) is None


@pytest.mark.parametrize('helper_fails', [False, True])
def test_same_exception_no_additional_sql_or_transaction_operation(monkeypatch, helper_fails):
    exc = sqlite3.OperationalError(SYNTHETIC_SECRET)
    calls = []

    class Connection:
        in_transaction = True

        def execute(self, sql):
            calls.append(sql)
            raise exc

    store = ledger.Ledger.__new__(ledger.Ledger)
    store.db = Connection()
    if helper_fails:
        def broken(_value):
            raise RuntimeError(SYNTHETIC_SECRET)
        monkeypatch.setattr(ledger, 'message_write_failure_diagnostic', broken)
    with pytest.raises(sqlite3.OperationalError) as caught:
        store._upsert_message(message())
    assert caught.value is exc
    assert calls == ['UPDATE messages SET message_id=message_id WHERE 0']
    assert store.db.in_transaction
    if not helper_fails:
        assert diagnostic(exc)['transaction_open_before'] is True
    else:
        assert not hasattr(exc, '_mcs_message_write_failure')


def test_writer_busy_and_wal_snapshot_promotion(tmp_path):
    path = tmp_path / 'synthetic.db'
    store = ledger.Ledger(str(path))
    other = sqlite3.connect(path, timeout=0)
    store.db.execute('PRAGMA busy_timeout=1')
    try:
        other.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError) as caught:
            store._upsert_message(message())
        data = diagnostic(caught.value)
        assert data['sqlite_errorcode'] == sqlite3.SQLITE_BUSY
        assert data['sqlite_errorname'] == 'SQLITE_BUSY'
        assert data['transaction_open_before'] is False
        assert data['elapsed_s'] >= 0 and data['at'] <= time.time()
        store.db.rollback()
        other.rollback()

        store.db.execute('BEGIN')
        store.db.execute('SELECT COUNT(*) FROM messages').fetchone()
        with other:
            other.execute("INSERT INTO patients(project_id,patient_name) VALUES(1,'synthetic')")
        with pytest.raises(sqlite3.OperationalError) as caught:
            store._upsert_message(message())
        data = diagnostic(caught.value)
        assert data['sqlite_errorcode'] == sqlite3.SQLITE_BUSY_SNAPSHOT
        assert data['sqlite_errorname'] == 'SQLITE_BUSY_SNAPSHOT'
        assert data['transaction_open_before'] is True
        assert store.db.in_transaction   # the reservation never owns rollback
        store.db.rollback()
        assert store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
    finally:
        other.close()
        store.db.close()


def test_borrowed_transaction_and_savepoint_remain_owned_by_caller(tmp_path):
    store = ledger.Ledger(str(tmp_path / 'synthetic.db'))
    raw = store.db
    exc = sqlite3.OperationalError(SYNTHETIC_SECRET)

    class FailReservation:
        @property
        def in_transaction(self):
            return raw.in_transaction

        def execute(self, sql):
            assert sql == 'UPDATE messages SET message_id=message_id WHERE 0'
            raise exc

    try:
        raw.execute('BEGIN IMMEDIATE')
        raw.execute('SAVEPOINT caller')
        raw.execute("INSERT INTO patients(project_id,patient_name) VALUES(1,'synthetic')")
        store.db = FailReservation()
        with pytest.raises(sqlite3.OperationalError) as caught:
            store._upsert_message(message())
        assert caught.value is exc and raw.in_transaction
        assert diagnostic(exc)['transaction_open_before'] is True
        raw.execute('ROLLBACK TO caller')
        raw.execute('RELEASE caller')
        assert raw.execute('SELECT COUNT(*) FROM patients').fetchone()[0] == 0
        raw.rollback()
    finally:
        raw.close()


def test_success_has_no_failure_diagnostic_or_transaction_change(tmp_path):
    store = ledger.Ledger(str(tmp_path / 'synthetic.db'))
    try:
        store.ensure_patient(1)
        with store.db:
            store.db.execute("INSERT INTO messages(message_id,project_id,body_state,body_text,content_hash) "
                             "VALUES(1,1,'full','synthetic source','same')")
        m = mcs_contract.Message(
            message_id=1, project_id=1, parent_id=None, sender_id=None,
            sender_name='', sender_type='', profession='', organization='',
            posted_at='', body_html='synthetic', body_state='snippet',
            is_unread=False, reply_count=0)
        assert store._upsert_message(m) == 0
        assert store.db.in_transaction
        store.db.rollback()
        assert not hasattr(m, '_mcs_message_write_failure')
    finally:
        store.db.close()
