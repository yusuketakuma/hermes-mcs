"""Synthetic retention scope failures do not block independent database upkeep."""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

import health_report
import maintenance
import run_check
from common_testkit import make_ledger


def _scope_hold(tmp_path, now):
    db = make_ledger(tmp_path)
    receipt = db.artifact_add('cloud_usage_reserve', '{}', project_id=1,
                              meta={'project_id': 1})
    repair = db.artifact_add('patient_context_source_quality_repair_v1', '{}',
                             meta={'repair_input_key': 'a' * 64})
    with db.db:
        db.db.execute('UPDATE artifacts SET created_at=? WHERE artifact_id=?',
                      (now - maintenance.CLOUD_USAGE_KEEP_S - 1, receipt))
    before = [tuple(row) for row in db.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    db.close()
    return receipt, repair, before


@pytest.mark.parametrize('busy', [False, True])
def test_scope_hold_reports_partial_and_still_attempts_checkpoint(tmp_path, monkeypatch, busy):
    now = time.time()
    receipt, repair, before = _scope_hold(tmp_path, now)
    data = tmp_path / 'data'
    data.mkdir()
    state_path = data / 'db_maintenance.json'
    previous_daily = now - maintenance.DB_MAINT_INTERVAL_S - 1
    state_path.write_text(json.dumps({'daily_at': previous_daily}))
    monkeypatch.setattr(maintenance, 'DB_MAINT_STATE', str(state_path))
    monkeypatch.setattr(run_check, 'DB', str(tmp_path / 'ledger.db'))
    monkeypatch.setattr(run_check, '_hard_deadline', lambda: None)
    monkeypatch.setattr(maintenance, 'daily_backup', lambda *_a, **_k: None)
    monkeypatch.setattr(maintenance, 'prune_attachments', lambda *_a, **_k: 0)
    monkeypatch.setattr(maintenance, 'prune_leftovers', lambda: None)
    calls = []
    real_connect = sqlite3.connect

    class TracedConnection:
        def __init__(self, con):
            self.con = con

        def __getattr__(self, name):
            return getattr(self.con, name)

        def __enter__(self):
            self.con.__enter__()
            return self

        def __exit__(self, *args):
            return self.con.__exit__(*args)

        def execute(self, sql, *args):
            if 'wal_checkpoint' in sql:
                calls.append(sql)
                if busy:
                    return SimpleNamespace(fetchone=lambda: (1, 5, 0))
            return self.con.execute(sql, *args)

    with monkeypatch.context() as patch:
        patch.setattr(maintenance.sqlite3, 'connect',
                      lambda *a, **k: TracedConnection(real_connect(*a, **k)))
        result = {'errors': []}
        run_check._housekeeping(result)
    assert result['errors'] == ['db_maintenance: source_repair_retention_scope_invalid']
    assert calls == ['PRAGMA wal_checkpoint(TRUNCATE)']
    state = json.loads(state_path.read_text())
    assert state['daily_at'] == previous_daily and state['fts_at'] >= now
    assert state.get('checkpoint_pending', False) is busy
    health = health_report.db_maintenance_ages(str(data), now + 10)
    assert health['db_maintenance_daily_age_s'] >= maintenance.DB_MAINT_INTERVAL_S
    assert health['db_checkpoint_pending'] is busy
    with sqlite3.connect(tmp_path / 'ledger.db') as con:
        assert [tuple(row) for row in con.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
        con.execute('UPDATE artifacts SET project_id=1 WHERE artifact_id=?', (repair,))
    # Once scope is repaired, the next pass can finish; held receipts stay held.
    out = maintenance.db_maintenance(str(tmp_path / 'ledger.db'), now=now + 20)
    assert out['checkpoint']['busy'] == 0
    assert json.loads(state_path.read_text())['daily_at'] == now + 20
    with sqlite3.connect(tmp_path / 'ledger.db') as con:
        assert con.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (receipt,)).fetchone()


def test_scope_failure_preserves_prior_committed_cloud_batch(tmp_path, monkeypatch):
    now = time.time()
    db = make_ledger(tmp_path)
    try:
        db.artifact_add('patient_context_source_quality_repair_v1', '{}', project_id=1,
                        meta={'repair_input_key': 'a' * 64})
        first = db.artifact_add('cloud_usage_reserve', '{}', project_id=2, meta={'project_id': 2})
        ambiguous = db.artifact_add('cloud_usage_result', '{}', meta={})
        with db.db:
            db.db.execute('UPDATE artifacts SET created_at=? WHERE artifact_id IN (?,?)',
                          (now - maintenance.CLOUD_USAGE_KEEP_S - 1, first, ambiguous))
        original = tuple(db.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (ambiguous,)).fetchone())
        monkeypatch.setattr(maintenance, 'RETENTION_BATCH', 1)
        with pytest.raises(maintenance.MaintenanceError, match='^source_repair_retention_scope_invalid$'):
            maintenance.prune_history(db.db, now=now)
        assert db.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (first,)).fetchone() is None
        assert tuple(db.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (ambiguous,)).fetchone()) == original
        assert not db.db.in_transaction
    finally:
        db.close()
