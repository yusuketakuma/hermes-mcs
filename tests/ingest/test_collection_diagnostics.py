"""Collection observations preserve privacy, read ownership, limits and alert semantics."""
import copy
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

import collection_diagnostics as diagnostics
import health_report
import health_watch


@pytest.fixture
def db():
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript('CREATE TABLE patients(project_id INTEGER, fetch_state TEXT, fetch_reason TEXT, is_archived INTEGER);'
                         'CREATE TABLE messages(message_id INTEGER,project_id INTEGER,parent_id INTEGER,body_state TEXT,first_seen REAL);')
        yield db


def insert(db, pid=1, state='incomplete', reason='parent_body_incomplete', archived=0,
           mid=1, parent=None, body='unknown', first=100):
    db.execute('INSERT INTO patients VALUES(?,?,?,?)', (pid, state, reason, archived))
    db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (mid, pid, parent, body, first))
    db.commit()


def observe(db, errors=None, now=3700):
    return health_report.collection_diagnostics_health(db, [] if errors is None else errors, now)


def test_counts_use_active_incomplete_patients_and_original_observation(db):
    insert(db)
    insert(db, pid=2, mid=2, reason='thread_incomplete', body='full')
    insert(db, pid=3, mid=3, state='complete')
    insert(db, pid=4, mid=4, archived=1)
    insert(db, pid=5, mid=5, parent=10, body='snippet')
    block = observe(db, ['project 1: parent_body_incomplete', 'attachment 4: fs_OSError'])
    assert block['incomplete'] == {'state': 'known', 'projects': 3,
        'reasons': {**dict.fromkeys(diagnostics.FETCH_LABELS, 0), 'parent_body_incomplete': 2, 'thread_incomplete': 1},
        'parent_bodies': 1, 'oldest_observed_age_s': 3600, 'age_unknown': 0}
    assert block['errors']['reasons']['incomplete'] == block['errors']['reasons']['storage'] == 1
    assert diagnostics.validated(block) == block
    assert '最長1.0時間' in diagnostics.detail_lines(block, 'incomplete')[1]
    db.execute('UPDATE patients SET fetch_state="complete"')
    db.commit()
    assert observe(db)['incomplete']['parent_bodies'] == 0


@pytest.mark.parametrize('first', [None, -1, 4000, 'synthetic-secret'])
def test_unknown_or_future_time_is_not_zero_or_fabricated_age(db, first):
    insert(db, first=first)
    block = observe(db)
    assert block['incomplete']['oldest_observed_age_s'] is None
    assert block['incomplete']['age_unknown'] == 1
    assert '経過時間不明' in diagnostics.detail_lines(block, 'incomplete')[1]
    assert 'synthetic-secret' not in json.dumps(block)


def test_incomplete_scan_does_not_visit_complete_patients_history(db):
    db.executescript('DROP TABLE patients;'
                     'CREATE TABLE patients(project_id INTEGER PRIMARY KEY, fetch_state TEXT, fetch_reason TEXT, is_archived INTEGER);'
                     'CREATE INDEX idx_messages_project_time ON messages(project_id);'
                     'CREATE INDEX idx_messages_parent ON messages(parent_id);')
    insert(db, pid=1, state='complete', body='full')
    db.executemany('INSERT INTO messages VALUES(?,1,NULL,"full",100)',
                   ((mid,) for mid in range(2, 10002)))
    insert(db, pid=2, mid=10002)
    steps = 0

    def stop_unrelated_scan():
        nonlocal steps
        steps += 100
        return int(steps > 2000)

    db.set_progress_handler(stop_unrelated_scan, 100)
    try:
        counts = diagnostics.incomplete_counts(db, 3700, lambda: None)
    finally:
        db.set_progress_handler(None, 0)
    assert counts['projects'] == counts['parent_bodies'] == 1
    assert counts['oldest_observed_age_s'] == 3600


def test_unknown_reason_and_mixed_ages_do_not_disclose_raw_data(db):
    insert(db, reason='SYNTHETIC-PRIVATE-NAME', first=None)
    insert(db, pid=2, mid=2, first=100)
    block = observe(db, ['project 3: http_error(status=429): secret-url',
                         'thread 4: schema_error: SYNTHETIC-PRIVATE-NAME',
                         'cmd_invalid: private-token', 'SYNTHETIC-PRIVATE-NAME'])
    assert block['errors']['reasons'] == {**dict.fromkeys(diagnostics.ERROR_LABELS, 0),
        'rate_limit': 1, 'schema': 1, 'command': 1, 'other': 1}
    assert block['incomplete']['reasons']['other'] == 1
    assert block['incomplete']['oldest_observed_age_s'] == 3600
    assert block['incomplete']['age_unknown'] == 1
    public = json.dumps(block) + ''.join(diagnostics.detail_lines(block, 'errors'))
    assert all(secret not in public for secret in ('secret-url', 'private-token', 'SYNTHETIC-PRIVATE-NAME'))


@pytest.mark.parametrize('error,reason', [
    ('session_expired: synthetic', 'authentication'), ('deadline_exceeded', 'deadline'),
    ('probe 42: http_error(status=503): private', 'network'),
    ('project 42: db_write_failed', 'storage'), ('update_spawn_failed', 'command'),
])
def test_fixed_producer_error_classes(error, reason):
    counts = diagnostics.error_counts([error])
    assert counts['reasons'][reason] == counts['total'] == 1


def test_error_work_is_bounded_and_unknown_is_distinct_from_zero(db):
    errors = ['deadline_exceeded'] * (diagnostics.MAX_ERRORS + 10)
    block = observe(db, errors)
    assert block['errors']['state'] == 'partial'
    assert block['errors']['total'] == diagnostics.MAX_ERRORS + 10
    assert sum(block['errors']['reasons'].values()) == diagnostics.MAX_ERRORS
    assert diagnostics.validated(block) == block
    assert diagnostics.error_counts(None)['total'] is None
    assert observe(db)['incomplete']['projects'] == 0


def test_row_bound_and_sql_errors_are_unknown_not_empty(db, monkeypatch):
    insert(db)
    insert(db, pid=2, mid=2)
    monkeypatch.setattr(diagnostics, 'MAX_ROWS', 1)
    assert observe(db)['incomplete'] == diagnostics.unknown_incomplete()
    assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
    db.execute('DROP TABLE messages')
    db.commit()
    assert observe(db)['incomplete'] == diagnostics.unknown_incomplete()


def test_borrowed_reader_keeps_transaction_wait_and_data(db):
    insert(db)
    db.execute('PRAGMA busy_timeout=4321')
    db.execute('INSERT INTO patients VALUES(9,"complete",NULL,0)')
    assert db.in_transaction
    observe(db)
    assert db.in_transaction
    assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 4321
    db.rollback()
    assert db.execute('SELECT COUNT(*) FROM patients').fetchone()[0] == 1


def test_expired_observation_does_not_escape_budget(db, monkeypatch):
    insert(db)
    clock = iter([0, 0, 1])
    monkeypatch.setattr(health_report, 'time', SimpleNamespace(monotonic=lambda: next(clock, 1)))
    block = observe(db)
    assert block['incomplete'] == diagnostics.unknown_incomplete()
    assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['bool_count', 'negative', 'private_reason', 'future_age', 'wrong_generation'])
def test_public_observer_rejects_malformed_counts(db, change):
    insert(db)
    block = copy.deepcopy(observe(db))
    if change == 'bool_count':
        block['incomplete']['projects'] = True
    elif change == 'negative':
        block['errors']['total'] = -1
    elif change == 'private_reason':
        block['errors']['reasons'] = {'SYNTHETIC-SECRET': 1}
    elif change == 'future_age':
        block['incomplete']['oldest_observed_age_s'] = -1
    else:
        block['observed_at'] = 3699
    assert diagnostics.validated(block, 3700) is None


def test_monitor_uses_counts_but_keeps_stale_reasons_unknown(tmp_path, db):
    insert(db)
    block = observe(db, ['deadline_exceeded'])
    block['SYNTHETIC-SECRET'] = 'SYNTHETIC-SECRET'
    path = tmp_path / 'health.json'
    health = {'at': 3700, 'overall': 'degraded', 'state_reasons': ['stage_errors', 'collection_incomplete'],
              'collection_diagnostics': block}
    path.write_text(json.dumps(health))
    report = health_watch.classify_health(str(path), 3710, 600)
    lines = '\n'.join(health_watch._reason_rows(report, report['state_reasons']))
    assert '時間切れ1件' in lines and '親本文未取得1件' in lines and '最長1.0時間' in lines
    assert 'SYNTHETIC-SECRET' not in json.dumps(report)
    stale = health_watch.classify_health(str(path), 5000, 600)
    assert stale['state_reasons'] is None
    assert '時間切れ1件' not in '\n'.join(health_watch._reason_rows(stale, None))
