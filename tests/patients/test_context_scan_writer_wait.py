"""A competing SQLite writer must not outwait the context scan deadline."""
import sqlite3
import time

import pytest

import patient_context_refresh as refresh
from test_patient_context_refresh import document, runtime as runtime, seed, step


@pytest.mark.parametrize('stage', ['bookmark', 'claim'])
def test_contended_scan_writer_respects_remaining_time_and_restores_timeout(runtime, monkeypatch, stage):
    ledger, _, calls = runtime
    seed(ledger, mid=1, pid=1)
    ledger.db.execute('PRAGMA busy_timeout=2000')
    timeout = ledger.db.execute('PRAGMA busy_timeout').fetchone()[0]
    path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    other = sqlite3.connect(path, timeout=0)
    select = refresh._select_patient

    def select_then_lock(*args, **kwargs):
        result = select(*args, **kwargs)
        assert result[1] == 1
        other.execute('BEGIN IMMEDIATE')
        return result

    try:
        if stage == 'bookmark':
            other.execute('BEGIN IMMEDIATE')
        else:
            monkeypatch.setattr(refresh, '_select_patient', select_then_lock)
        started = time.monotonic()
        result = step(runtime, seconds=refresh.COMMIT_MARGIN + 0.12)
        elapsed = time.monotonic() - started
        assert result['status'] == 'deferred' and result['reason'] == 'patient_context_scan_incomplete'
        assert elapsed < 1.0  # the caller's 2-second timeout was bounded by remaining budget
        assert other.in_transaction and not ledger.db.in_transaction
        assert ledger.db.execute('PRAGMA busy_timeout').fetchone()[0] == timeout
        assert document(ledger, refresh.PROGRESS, pid=1) is None and calls == []
    finally:
        other.rollback()
        other.close()
