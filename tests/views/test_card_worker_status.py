"""Read-only startup and restore diagnostics using synthetic private directories."""
import json
import os

import card_worker_status as workers


def test_owner_evidence_and_restore_hold(tmp_path):
    cfg = {'notify': {'interactive': 'slack', 'slack': {'profile': 'primary'}}}
    assert workers.diagnose(tmp_path, cfg)['state'] == 'unknown'
    workers.record(tmp_path, 'slack', 'primary', 'worker_started')
    workers.record(tmp_path, 'slack', 'foreign', 'profile_mismatch')
    assert workers.diagnose(tmp_path, cfg)['state'] == 'started'
    (tmp_path / 'restore_pending.json').write_text('{}')
    assert workers.diagnose(tmp_path, cfg)['restore_delivery_held'] is True
    assert workers.diagnose(tmp_path, cfg)['recovery_scope'] == 'collect_only'
    assert 'primary' not in ''.join(p.read_text() for p in (tmp_path / 'worker_status').iterdir())


def test_old_version_and_dead_worker_are_not_start_proof(tmp_path, monkeypatch):
    workers.record(tmp_path, 'discord', 'primary', 'worker_started')
    path = workers._path(tmp_path, 'discord', 'primary')
    data = json.loads(path.read_text())
    data['version'] = 'old'
    path.write_text(json.dumps(data))
    assert workers.status(tmp_path, 'discord', 'primary')['reason'] == 'worker_restart_required'
    data['version'] = workers.VERSION
    path.write_text(json.dumps(data))
    monkeypatch.setattr(os, 'kill', lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
    assert workers.status(tmp_path, 'discord', 'primary')['state'] == 'unknown'


def test_individual_restore_hold_and_unreadable_are_distinct(tmp_path):
    import sqlite3
    cfg = {'runtime_mode': 'standalone'}
    assert workers.diagnose(tmp_path, cfg, probe_data=True)['restore_delivery_held'] is None
    with sqlite3.connect(tmp_path / 'ledger.db') as db:
        db.execute('CREATE TABLE notification_restore_holds(released_at REAL)')
        db.execute('INSERT INTO notification_restore_holds VALUES (NULL)')
    assert workers.diagnose(tmp_path, cfg)['restore_delivery_held'] is None
    assert workers.diagnose(tmp_path, cfg, probe_data=True)['restore_delivery_held'] is True
    with sqlite3.connect(tmp_path / 'ledger.db') as db:
        db.execute('UPDATE notification_restore_holds SET released_at=123')
    assert workers.diagnose(tmp_path, cfg, probe_data=True)['restore_delivery_held'] is False


def test_data_probe_closes_connection_on_success_and_query_failure(tmp_path, monkeypatch):
    import sqlite3
    import pytest
    real_connect = sqlite3.connect
    db = real_connect(tmp_path / 'ledger.db')
    db.close()
    opened = []
    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection
    monkeypatch.setattr(workers.sqlite3, 'connect', connect)
    assert workers.diagnose(tmp_path, {}, probe_data=True)['restore_delivery_held'] is False
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[-1].execute('SELECT 1')
    db = real_connect(tmp_path / 'ledger.db')
    try:
        db.execute('CREATE TABLE notification_restore_holds(other TEXT)')
        db.commit()
    finally:
        db.close()
    assert workers.diagnose(tmp_path, {}, probe_data=True)['restore_delivery_held'] is None
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[-1].execute('SELECT 1')


def test_malformed_notify_block_is_unverified_not_a_crash(tmp_path):
    for cfg in ({'notify': 'slack'}, {'notify': ['slack']},
                {'notify': {'interactive': 'slack', 'slack': 'default'}}):
        result = workers.diagnose(tmp_path, cfg)
        assert (result['state'], result['reason']) == ('unknown', 'worker_start_unverified')


def test_empty_malformed_notify_values_are_unverified(tmp_path):
    workers.record(tmp_path, 'slack', 'default', 'worker_started')
    for cfg in ({'notify': []}, {'notify': ''},
                {'notify': {'interactive': 'slack', 'slack': []}}):
        result = workers.diagnose(tmp_path, cfg)
        assert (result['state'], result['reason']) == ('unknown', 'worker_start_unverified'), cfg
