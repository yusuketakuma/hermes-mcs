"""Synthetic investigation of caller-connection state after claim write failures."""
import extract_llm_carry
import extract_llm_errors
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import sqlite3

import pytest

import extract_llm as worker
from extract_testkit import _ledger, _message


class FailingConnection:
    def __init__(self, db, boundary):
        self.db = db
        self.boundary = boundary
        self.reached = 0

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args):
        result = self.db.execute(sql, *args)
        if self.boundary == 'write' and sql.lstrip().split()[0] in ('DELETE', 'UPDATE', 'INSERT'):
            self.reached += 1
            raise sqlite3.OperationalError('synthetic write completion failure')
        return result

    def commit(self):
        if self.boundary == 'commit':
            self.reached += 1
            raise sqlite3.OperationalError('synthetic commit failure')
        return self.db.commit()


def setup_claim(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.save_messages([_message(body='完全合成のトランザクション対象')])
    row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    lease = worker._claim(ledger, row)
    assert lease is not None
    return ledger, row, lease


@pytest.mark.parametrize('operation', ['release', 'renew'])
@pytest.mark.parametrize('boundary', ['write', 'commit'])
def test_claim_cleanup_failure_rolls_back_and_allows_retry(tmp_path, monkeypatch, operation, boundary):
    ledger, row, lease = setup_claim(tmp_path)
    real = ledger.db
    proxy = FailingConnection(real, boundary)
    ledger.db = proxy
    monkeypatch.setattr(worker, 'llm_extract', lambda *a, **kw: pytest.fail('no model call'))
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    try:
        with pytest.raises(sqlite3.OperationalError):
            (worker._release if operation == 'release' else worker._renew)(ledger, row, lease)
        assert proxy.reached == 1
        assert not real.in_transaction
        assert real.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 1
        ledger.db = real
        result = (worker._release if operation == 'release' else worker._renew)(ledger, row, lease)
        assert not real.in_transaction
        assert (result is None) if operation == 'release' else result is not None
        assert real.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == (0 if operation == 'release' else 1)
        monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: False)
        monkeypatch.setattr(extract_llm_carry, '_writer_current', worker._writer_current)
        monkeypatch.setattr(extract_llm_errors, '_writer_current', worker._writer_current)
        monkeypatch.setattr(extract_llm_resident, '_writer_current', worker._writer_current)
        monkeypatch.setattr(extract_llm_runner, '_writer_current', worker._writer_current)
        monkeypatch.setattr(extract_llm_selection, '_writer_current', worker._writer_current)
        monkeypatch.setattr(extract_llm_source_contract, '_writer_current', worker._writer_current)
        resumed = worker.run_pending(ledger, limit=1)
        assert resumed.get('updated') and not resumed.get('transaction_active')
    finally:
        ledger.db = real
        ledger.close()


@pytest.mark.parametrize('boundary', ['write', 'commit'])
def test_claim_failure_rolls_back_owned_transaction(tmp_path, boundary):
    ledger, row, _lease = setup_claim(tmp_path)
    real = ledger.db
    with real:
        real.execute("DELETE FROM fetch_jobs WHERE kind='extract_claim'")
    proxy = FailingConnection(real, boundary)
    ledger.db = proxy
    try:
        with pytest.raises(sqlite3.OperationalError):
            worker._claim(ledger, row)
        assert proxy.reached == 1
        assert not real.in_transaction
        assert real.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 0
    finally:
        ledger.db = real
        ledger.close()


@pytest.mark.parametrize('operation', ['release', 'renew', 'claim'])
def test_claim_helpers_leave_caller_transaction_untouched(tmp_path, operation):
    ledger, row, lease = setup_claim(tmp_path)
    try:
        ledger.db.execute('BEGIN IMMEDIATE')
        ledger.db.execute("UPDATE messages SET sender_name='synthetic-caller' WHERE message_id=1")
        changes = ledger.db.total_changes
        if operation == 'claim':
            assert worker._claim(ledger, row) is None
        else:
            assert (worker._release if operation == 'release' else worker._renew)(ledger, row, lease) is None
        assert ledger.db.in_transaction and ledger.db.total_changes == changes
        ledger.db.rollback()
        assert ledger.db.execute("SELECT sender_name FROM messages WHERE message_id=1").fetchone()[0] != 'synthetic-caller'
    finally:
        ledger.close()


@pytest.mark.parametrize('operation', ['release', 'renew'])
def test_rollback_failure_preserves_original_write_error(tmp_path, operation):
    ledger, row, lease = setup_claim(tmp_path)
    real = ledger.db
    class FailedRollback(FailingConnection):
        def rollback(self):
            raise sqlite3.OperationalError('synthetic secondary rollback failure')
    ledger.db = FailedRollback(real, 'write')
    try:
        with pytest.raises(sqlite3.OperationalError, match='synthetic write completion failure'):
            (worker._release if operation == 'release' else worker._renew)(ledger, row, lease)
        assert real.in_transaction  # A failed rollback is never claimed to have recovered.
    finally:
        real.rollback()
        ledger.db = real
        ledger.close()
