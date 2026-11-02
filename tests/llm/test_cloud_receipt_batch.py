"""Synthetic financial receipts share a snapshot without sharing stale proofs."""
from contextlib import closing
import sqlite3

import pytest

import cloud_policy
from ledger import Ledger
from test_cloud_policy import _reserve, _result, now

__all__ = ['now']


def _receipts(db, count):
    ids = []
    for i in range(count):
        reservation = _reserve(db, unit=f'synthetic-{i}')
        cloud_policy.record_result(db, reservation, _result(reservation))
        ids.append(db.db.execute(
            "SELECT MAX(artifact_id) FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0])
    return ids


def test_batch_preserves_individual_witnesses_and_one_global_scan(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as db:
        ids = _receipts(db, 3)
        expected = {aid: cloud_policy.reservation_evidence(db.db, aid) for aid in ids}
        statements = []
        before = db.db.total_changes
        db.db.execute('BEGIN')
        db.db.set_trace_callback(statements.append)
        try:
            assert cloud_policy.reservation_evidence_batch(db.db, ids) == expected
            assert db.db.in_transaction and db.db.total_changes == before
        finally:
            db.db.set_trace_callback(None)
            db.db.rollback()
        assert sum('FROM artifacts WHERE kind IN' in s and 'json_tree(meta)' in s
                   for s in statements) == 1
        assert sum('json_each(meta)' in s and 'reservation_id' in s for s in statements) == 1


def test_batch_rejects_bool_even_if_equal_to_a_valid_id(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as db:
        ids = _receipts(db, 1)
        assert ids[0] == 1
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            cloud_policy.reservation_evidence_batch(db.db, [ids[0], True])


@pytest.mark.parametrize('bad', ['{broken', '{"x":1,"x":2}', '{"value":1e999}', '[]'])
def test_foreign_corrupt_accounting_holds_every_batch_proof(tmp_path, now, bad):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as db:
        ids = _receipts(db, 2)
        db.artifact_add('cloud_usage_disposition', '{}', project_id=2)
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_disposition'", (bad,))
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            cloud_policy.reservation_evidence_batch(db.db, ids)
        assert not db.db.in_transaction


def test_batch_never_reuses_proof_after_same_connection_write(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as db:
        ids = _receipts(db, 2)
        db.db.execute('BEGIN')
        try:
            assert len(cloud_policy.reservation_evidence_batch(db.db, ids)) == 2
            db.db.execute("UPDATE artifacts SET meta='{broken' WHERE artifact_id=?", (ids[1],))
            with pytest.raises(ValueError, match='cloud_receipt_invalid'):
                cloud_policy.reservation_evidence_batch(db.db, ids)
            assert db.db.in_transaction
        finally:
            db.db.rollback()
        assert len(cloud_policy.reservation_evidence_batch(db.db, ids)) == 2


def test_batch_proof_tracks_its_borrowed_snapshot_and_next_read(tmp_path, now):
    path = tmp_path / 'synthetic.db'
    with closing(Ledger(str(path))) as db:
        ids = _receipts(db, 2)
        other = db.artifact_add('cloud_usage_disposition', '{}', project_id=2)
        db.db.execute('BEGIN')
        try:
            expected = cloud_policy.reservation_evidence_batch(db.db, ids)
            with sqlite3.connect(path) as writer:
                writer.execute("UPDATE artifacts SET meta='{broken' WHERE artifact_id=?", (other,))
            assert cloud_policy.reservation_evidence_batch(db.db, ids) == expected
            assert db.db.in_transaction
        finally:
            db.db.rollback()
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            cloud_policy.reservation_evidence_batch(db.db, ids)


def test_batch_keeps_original_sqlite_error_when_caller_rolls_back(tmp_path, monkeypatch):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as db:
        def interrupted(connection, _ids):
            connection.rollback()
            raise sqlite3.OperationalError('synthetic interruption')
        monkeypatch.setattr(cloud_policy, '_reservation_evidence_batch_snapshot', interrupted)
        with pytest.raises(sqlite3.OperationalError, match='synthetic interruption'):
            cloud_policy.reservation_evidence_batch(db.db, [1])


@pytest.mark.parametrize('borrowed', [False, True])
def test_batch_persistent_cancel_preserves_caller_transaction_and_handler(tmp_path, monkeypatch, borrowed):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        db = ledger.db
        db.execute('CREATE TEMP TABLE synthetic_marker(value INTEGER)')
        if borrowed:
            db.execute('INSERT INTO synthetic_marker VALUES(7)')
        def interrupted(connection, _ids):
            connection.set_progress_handler(lambda: 1, 1)
            return connection.execute('SELECT value FROM synthetic_marker').fetchall()
        monkeypatch.setattr(cloud_policy, '_reservation_evidence_batch_snapshot', interrupted)
        try:
            with pytest.raises(sqlite3.OperationalError) as caught:
                cloud_policy.reservation_evidence_batch(db, [1])
            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
            assert db.in_transaction is borrowed
            with pytest.raises(sqlite3.OperationalError):
                db.execute('SELECT 1')
        finally:
            db.set_progress_handler(None, 0)
        assert [r[0] for r in db.execute('SELECT value FROM synthetic_marker')] == ([7] if borrowed else [])
        if borrowed:
            db.rollback()
            assert db.execute('SELECT COUNT(*) FROM synthetic_marker').fetchone()[0] == 0
