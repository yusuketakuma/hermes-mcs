"""Synthetic restore comparison of retained and unstored intake evidence."""
from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

import restore_evidence
from ops_testkit import _load


INTAKE_CASES = [
    ('held_intents', 'artifacts', 'semantic_intake_hold_v1'),
    ('released_intents', 'artifacts', 'semantic_intake_released_v1'),
    ('release_receipts', 'artifacts', 'semantic_intake_release_receipt_v1'),
    ('archived_intake', 'fetch_jobs', 'semantic_intake_archive_v1'),
    ('unstored_intake', 'fetch_jobs', 'semantic_intake_unstored_v1'),
    ('unstored_epoch', 'fetch_jobs', 'semantic_intake_unstored_epoch_v1'),
    ('unstored_roots', 'fetch_jobs', 'semantic_intake_unstored_root_v1'),
]


def _database(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            PRAGMA user_version=9;
            CREATE TABLE messages(message_id INTEGER PRIMARY KEY, posted_at_ts REAL);
            CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT, project_id INTEGER,
                                   message_id INTEGER, content TEXT, model TEXT, meta TEXT, created_at REAL);
            CREATE TABLE fetch_jobs(job_id INTEGER PRIMARY KEY, kind TEXT, project_id INTEGER,
                                    message_id INTEGER, payload TEXT, state TEXT, attempts INTEGER);
        """)


def _insert_evidence(db, table, kind, row_id):
    if table == 'artifacts':
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',
                   (row_id, kind, 1, None, '{"synthetic":true}', '1', '{}', 1))
    else:
        db.execute('INSERT INTO fetch_jobs VALUES(?,?,?,?,?,?,?)',
                   (row_id, kind, 1, row_id, '{"synthetic":true}', 'pending', 0))


def _reports(tmp_path, monkeypatch, live, backup):
    watchdog = _load()
    monkeypatch.setattr(watchdog, 'LEDGER', str(live))
    monkeypatch.setattr(watchdog, 'DATA', str(tmp_path))
    normal = restore_evidence.create_loss_report(
        live_path=str(live), backup_path=str(backup), report_path=str(tmp_path / 'report.json'))
    separate = watchdog._loss_report(str(backup))
    assert {k: v for k, v in normal.items() if k != 'computed_at'} == {
        k: v for k, v in separate.items() if k != 'computed_at'}
    return normal, watchdog


def test_no_intake_records_keep_legacy_report_identity(tmp_path, monkeypatch):
    live, backup = tmp_path / 'live.db', tmp_path / 'backup.db'
    _database(live)
    _database(backup)
    report, _ = _reports(tmp_path, monkeypatch, live, backup)
    assert 'semantic_intake_evidence' not in report
    metrics = {k: v for k, v in report.items() if k not in ('computed_at', 'report_id')}
    assert report['report_id'] == hashlib.sha256(json.dumps(
        metrics, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


@pytest.mark.parametrize('label,table,kind', INTAKE_CASES)
def test_report_exposes_unchanged_and_replaced_intake_rows(tmp_path, monkeypatch, label, table, kind):
    live, backup = tmp_path / 'live.db', tmp_path / 'backup.db'
    _database(live)
    _database(backup)
    for path, offset in ((live, 0), (backup, 1)):
        with closing(sqlite3.connect(path)) as db, db:
            _insert_evidence(db, table, kind, 1)
            _insert_evidence(db, table, kind, 2 + offset)
    report, _ = _reports(tmp_path, monkeypatch, live, backup)
    assert set(report['semantic_intake_evidence']) == {label}
    entry = report['semantic_intake_evidence'][label]
    assert (entry['live'], entry['backup'], entry['live_not_identical_in_backup']) == (2, 2, 1)
    assert 'synthetic' not in json.dumps(report)


@pytest.mark.parametrize('label,table,kind', INTAKE_CASES)
@pytest.mark.parametrize('mutation', ['add', 'remove', 'replace', 'update'])
def test_intake_change_invalidates_previous_receipt(tmp_path, monkeypatch, mutation, label, table, kind):
    live, backup = tmp_path / 'live.db', tmp_path / 'backup.db'
    _database(live)
    _database(backup)
    with closing(sqlite3.connect(live)) as db, db:
        _insert_evidence(db, table, kind, 1)
        db.execute('CREATE TABLE command_receipts(command_id TEXT, receipt_json TEXT, outcome TEXT, processed_at REAL)')
    report, watchdog = _reports(tmp_path, monkeypatch, live, backup)
    receipt = {'cmd': 'ops.restore_approve', 'scheduled': True, 'command_id': 'test',
               **{k: report[k] for k in ('report_id', 'backup_sha256', 'backup_schema')}}
    with closing(sqlite3.connect(live)) as db, db:
        db.execute("INSERT INTO command_receipts VALUES('test',?,'applied',1)", (json.dumps(receipt),))
    assert restore_evidence.find_restore_consent(report, live_path=str(live)) == 'test'
    assert watchdog._consent_for(report) == 'test'
    key = 'artifact_id' if table == 'artifacts' else 'job_id'
    with closing(sqlite3.connect(live)) as db, db:
        if mutation == 'add':
            _insert_evidence(db, table, kind, 2)
        elif mutation == 'remove':
            db.execute(f'DELETE FROM {table} WHERE {key}=1')
        elif mutation == 'replace':
            db.execute(f'DELETE FROM {table} WHERE {key}=1')
            _insert_evidence(db, table, kind, 2)
        else:
            field = 'meta' if table == 'artifacts' else 'payload'
            db.execute(f'UPDATE {table} SET {field}=? WHERE {key}=1', ('{"changed":true}',))
    changed, _ = _reports(tmp_path, monkeypatch, live, backup)
    assert changed.get('semantic_intake_evidence', {}).get(label) != report['semantic_intake_evidence'][label]
    assert changed['report_id'] != report['report_id']
    assert restore_evidence.find_restore_consent(changed, live_path=str(live)) is None
    assert watchdog._consent_for(changed) is None


@pytest.mark.parametrize('limit', ['rows', 'bytes', 'bytes_hold', 'identity', 'missing_kind'])
def test_unverifiable_evidence_refuses_restore_report(tmp_path, monkeypatch, limit):
    live, backup = tmp_path / 'live.db', tmp_path / 'backup.db'
    _database(live)
    _database(backup)
    with closing(sqlite3.connect(live)) as db, db:
        if limit == 'missing_kind':
            db.execute('DROP TABLE fetch_jobs')
            db.execute('CREATE TABLE fetch_jobs(job_id INTEGER PRIMARY KEY, payload TEXT)')
            db.execute("INSERT INTO fetch_jobs VALUES(1,'{}')")
        elif limit == 'identity':
            db.execute('DROP TABLE fetch_jobs')
            db.execute('CREATE TABLE fetch_jobs(job_id INTEGER, kind TEXT, payload TEXT)')
            db.execute("INSERT INTO fetch_jobs VALUES(1,'semantic_intake_unstored_v1','{}')")
        elif limit == 'bytes_hold':
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',
                       (1, 'semantic_intake_hold_v1', 1, None, 'x' * 70000, '1', '{}', 1))
        else:
            count = 4097 if limit == 'rows' else 1
            payload = 'x' * 70000 if limit == 'bytes' else '{}'
            db.executemany('INSERT INTO fetch_jobs VALUES(?,?,?,?,?,?,?)', [
                (i, 'semantic_intake_unstored_v1', i, 0, payload, 'pending', 0)
                for i in range(1, count + 1)])
    watchdog = _load()
    monkeypatch.setattr(watchdog, 'LEDGER', str(live))
    monkeypatch.setattr(watchdog, 'DATA', str(tmp_path))
    with pytest.raises(restore_evidence.RestoreEvidenceError, match='restore_report_read_failed'):
        restore_evidence.create_loss_report(live_path=str(live), backup_path=str(backup),
                                            report_path=str(tmp_path / 'report.json'))
    with pytest.raises(sqlite3.Error):
        watchdog._loss_report(str(backup))
