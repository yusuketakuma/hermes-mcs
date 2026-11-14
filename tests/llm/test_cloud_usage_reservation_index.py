"""Synthetic reservation-index lookups decide exactly what the retained-history scans decide."""
from contextlib import closing
import json

import pytest

import cloud_policy as c
import cloud_usage_accounting as acc
import ledger_schema
from ledger import Ledger
from test_cloud_policy import _reserve, _result, now

__all__ = ['now']

KINDS = ledger_schema.CLOUD_USAGE_KINDS
FOREIGN = '0123456789abcdef0123456789abcdef'


def _history(ledger):
    """Three settled units, one discarded unit and one unit still unresolved."""
    reservations = [_reserve(ledger, unit=f'unit-{i}') for i in range(5)]
    for reservation in reservations[:4]:
        c.record_result(ledger, reservation, _result(reservation))
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        c.record_disposition_tx(ledger, reservations[3], 'discarded_scope')
    return reservations


def _outcome(call):
    try:
        value = call()
    except Exception as error:   # the decision includes how a lookup refuses
        return type(error).__name__, error.args
    if isinstance(value, dict):
        return sorted(value.items())
    return [tuple(row) for row in value]


def _decisions(ledger, ids):
    reserve_rows = [row[0] for row in ledger.db.execute(
        "SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve' ORDER BY artifact_id")]
    seen = {}
    for kind in KINDS:
        for rid in ids:
            seen['bound', kind, rid] = _outcome(lambda: acc._matching_bound_rows(ledger, kind, rid))
            seen['window', kind, rid] = _outcome(lambda: acc._matching_rows(ledger, kind, rid))
        seen['many', kind] = _outcome(lambda: acc._matching_rows_many(ledger, kind, ids))
    for artifact_id in reserve_rows:
        seen['evidence', artifact_id] = _outcome(lambda: acc.reservation_evidence_batch(ledger.db, [artifact_id]))
    seen['evidence', 'all'] = _outcome(lambda: acc.reservation_evidence_batch(ledger.db, reserve_rows))
    return seen


def _scan_decisions(ledger, ids):
    """The same questions with the guard removed, so only the scans can answer."""
    ledger.db.execute('SAVEPOINT scans_only')
    try:
        ledger.db.execute('DROP TRIGGER cloud_usage_guard_delete')
        assert ledger_schema.cloud_usage_guard_state(ledger.db) is None
        return _decisions(ledger, ids)
    finally:
        ledger.db.execute('ROLLBACK TO scans_only')
        ledger.db.execute('RELEASE scans_only')


def _traced(ledger, call):
    statements = []
    ledger.db.set_trace_callback(statements.append)
    try:
        call()
    finally:
        ledger.db.set_trace_callback(None)
    return statements


def test_index_answers_every_lookup_like_the_scans(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservations = _history(ledger)
        ids = [r['reservation_id'] for r in reservations] + [FOREIGN]
        assert ledger_schema.cloud_usage_guard_state(ledger.db) == (True, 0)
        statements = _traced(ledger, lambda: _decisions(ledger, ids))
        lookups = [sql for sql in statements if 'FROM artifacts' in sql and 'reservation_id' in sql]
        assert lookups and all(ledger_schema.CLOUD_USAGE_INDEX in sql for sql in lookups)
        assert not any('json_tree(meta)' in sql for sql in statements)
        indexed = _decisions(ledger, ids)
        assert indexed == _scan_decisions(ledger, ids)
        assert len(indexed['bound', 'cloud_usage_result', ids[0]]) == 1
        assert indexed['evidence', 'all'][0] == 'ValueError'   # the discarded and unresolved units refuse


@pytest.mark.parametrize('raw', [
    json.dumps({'reservation_id': FOREIGN}),          # names a reservation from another, NULL patient
    json.dumps({'reservation_id': 123}),
    json.dumps({'reservation_id': None}),
    json.dumps({'reservation_id': ['aaaaaaaaaaaaaaaaaaaaaaaaaaaa']}),   # its JSON text is itself 32 characters
    json.dumps({'other': 1}),
    r'{"reservation\u005fid":"%s"}' % FOREIGN,        # an escaped member name: either path may own it
])
def test_well_formed_foreign_rows_keep_both_answers_equal(tmp_path, now, raw):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservations = _history(ledger)
        with ledger.db:
            for kind in KINDS:
                ledger.db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                                  (kind, '{}', 'synthetic', raw, now))
        if '\\' not in raw:
            assert ledger_schema.cloud_usage_guard_state(ledger.db) == (True, 0)
        ids = [reservations[0]['reservation_id'], FOREIGN, '["aaaaaaaaaaaaaaaaaaaaaaaaaaaa"]']
        assert _decisions(ledger, ids) == _scan_decisions(ledger, ids)


@pytest.mark.parametrize('raw', [
    '{"reservation_id":"x","reservation_id":"%s"}' % FOREIGN,     # duplicate member
    '{"reservation_id":"%s"' % FOREIGN,                           # invalid JSON text naming the id
    FOREIGN.encode(),                                             # blob naming the id
    '["%s"]' % FOREIGN,
    '{"reservation_id":"%s","value":1e999}' % FOREIGN,
    json.dumps({'reservation_id': FOREIGN, 'pad': 'x' * 40000}),
])
@pytest.mark.parametrize('by_update', [False, True])
def test_malformed_row_hands_every_lookup_back_to_the_scans(tmp_path, now, raw, by_update):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservations = _history(ledger)
        ids = [reservations[0]['reservation_id'], FOREIGN]
        with ledger.db:
            if by_update:
                ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=(SELECT MAX(artifact_id) "
                                  "FROM artifacts WHERE kind='cloud_usage_result')", (raw,))
            else:
                ledger.db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) "
                                  "VALUES('cloud_usage_result','{}','synthetic',?,?)", (raw, now))
        verified, mutations = ledger_schema.cloud_usage_guard_state(ledger.db)
        assert not verified and mutations == int(by_update)
        statements = _traced(ledger, lambda: _decisions(ledger, ids))
        assert not any(ledger_schema.CLOUD_USAGE_INDEX in sql for sql in statements)
        refused = _decisions(ledger, ids)
        assert refused == _scan_decisions(ledger, ids)
        assert refused['window', 'cloud_usage_result', FOREIGN][0] == 'AnthropicError'
        assert refused['evidence', 'all'] == ('ValueError', ('cloud_receipt_invalid',))


def test_guard_counts_changes_to_retained_rows_but_not_appends(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _history(ledger)
        state = lambda: ledger_schema.cloud_usage_guard_state(ledger.db)   # noqa: E731
        assert state() == (True, 0)
        first = ledger.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='cloud_usage_reserve' "
                                  "ORDER BY artifact_id LIMIT 1").fetchone()
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET model=model WHERE artifact_id=?", (first[0],))
            assert state() == (True, 1)
            ledger.db.execute("INSERT OR REPLACE INTO artifacts(artifact_id,kind,content,model,meta,created_at) "
                              "VALUES(?,'cloud_usage_reserve','{}','synthetic',?,?)", (first[0], first[1], now))
            assert state() == (True, 2)
            ledger.db.execute("UPDATE artifacts SET kind='cloud_usage_dryrun' WHERE artifact_id=?", (first[0],))
            assert state() == (True, 3)
            ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_disposition'")
            assert state() == (True, 4)
            ledger.db.execute("INSERT INTO artifacts(kind,content,meta,created_at) VALUES('other','{}','{}',?)", (now,))
            ledger.db.execute("UPDATE artifacts SET meta='[' WHERE kind='other'")
            assert state() == (True, 4)
            newest = ledger.db.execute("SELECT MAX(artifact_id) FROM artifacts").fetchone()[0]
            ledger.db.execute("INSERT INTO artifacts(artifact_id,kind,content,meta,created_at) VALUES(?,'other','{}','{}',?)",
                              (newest + 100, now))
            ledger.db.execute("INSERT INTO artifacts(artifact_id,kind,content,model,meta,created_at) "
                              "VALUES(?,'cloud_usage_result','{}','synthetic','{}',?)", (newest + 50, now))
            assert state() == (True, 5)


def test_existing_ledger_is_audited_once_and_a_cleared_guard_is_restored(tmp_path, now, monkeypatch):
    path = str(tmp_path / 'synthetic.db')
    with closing(Ledger(path)) as ledger:
        _history(ledger)
        with ledger.db:
            for name in ledger_schema._CLOUD_USAGE_DDL:
                ledger.db.execute(('DROP INDEX ' if name == ledger_schema.CLOUD_USAGE_INDEX else 'DROP TRIGGER ') + name)
            ledger.db.execute('DROP TABLE cloud_usage_guard')
            ledger.db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) "
                              "VALUES('cloud_usage_result','{}','synthetic','[',?)", (now,))
    with closing(Ledger(path)) as ledger:
        assert ledger_schema.cloud_usage_guard_state(ledger.db) == (False, 0)
        with ledger.db:
            ledger.db.execute("DELETE FROM artifacts WHERE meta='['")
    with closing(Ledger(path)) as ledger:    # a failed audit is not repeated on every open
        assert ledger_schema.cloud_usage_guard_state(ledger.db) == (False, 1)
    monkeypatch.setattr(ledger_schema, 'CLOUD_USAGE_RECHECK_S', 0.0)
    with closing(Ledger(path)) as ledger:
        assert ledger_schema.cloud_usage_guard_state(ledger.db) == (True, 1)
        statements = _traced(ledger, lambda: ledger_schema.install_cloud_usage_guard(ledger.db))
        assert not any('BEGIN' in sql or 'json_tree' in sql for sql in statements)


def test_paid_result_save_waits_out_a_busy_writer(monkeypatch):
    import sqlite3

    import cloud_inference

    def busy(name='SQLITE_BUSY'):
        error = sqlite3.OperationalError('database is locked')
        error.sqlite_errorcode, error.sqlite_errorname = getattr(sqlite3, name), name
        return error

    calls = []

    def save(failures, error):
        calls.append(1)
        if len(calls) <= failures:
            raise error
        return 'saved'

    assert cloud_inference._save_result(lambda: save(2, busy())) == 'saved' and len(calls) == 3
    calls.clear()
    with pytest.raises(sqlite3.OperationalError):     # a writer that never frees stays an error, not a silent drop
        cloud_inference._save_result(lambda: save(99, busy()))
    assert len(calls) == cloud_inference._RESULT_SAVE_TRIES
    calls.clear()
    with pytest.raises(sqlite3.OperationalError):     # any other storage failure is not retried
        cloud_inference._save_result(lambda: save(99, busy('SQLITE_CORRUPT')))
    assert len(calls) == 1
