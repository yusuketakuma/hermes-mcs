"""Synthetic patient scheduling plans are bounded, durable and read-only."""
import json
import sqlite3

import pytest

import patient_context_schedule as schedule
import patient_priority


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE patients(project_id INTEGER PRIMARY KEY, is_archived INTEGER)')
    conn.execute('CREATE TABLE messages(project_id INTEGER, posted_at_ts REAL)')
    conn.execute('CREATE INDEX idx_messages_project_time ON messages(project_id, posted_at_ts)')
    yield conn
    conn.close()


def add(db, pid, archived=0, timestamp=None):
    db.execute('INSERT INTO patients VALUES (?,?)', (pid, archived))
    if timestamp is not None:
        db.execute('INSERT INTO messages VALUES (?,?)', (pid, timestamp))


def test_activity_order_matches_shared_key_with_null_ties_and_archives(db):
    rows = [(7, 1, 500), (3, 0, None), (2, 0, 10), (1, 0, 10), (4, 0, 0), (5, 0, -1)]
    for pid, archived, timestamp in rows:
        add(db, pid, archived, timestamp)
    expected = [pid for pid, archived, timestamp in sorted(rows, key=lambda r:
        patient_priority.priority_key(r[0], r[2], archived=bool(r[1])))]
    state, actual = None, []
    while True:
        turn = state['turn'] if state is not None else 0
        result = schedule.plan(db, state)
        state = result['next']
        if turn != 3 and result['reason'] == 'cycle_end':
            break
        if turn != 3:
            actual.append(result['project_id'])
    assert actual == expected == [1, 2, 4, 5, 3, 7]


def test_fair_snapshot_visits_all_existing_patients_despite_reordering_and_new_arrivals(db):
    for pid in range(1, 13):
        add(db, pid, archived=int(pid % 3 == 0), timestamp=pid)
    state, fair, first_upper = None, [], None
    for tick in range(120):
        db.execute('UPDATE messages SET posted_at_ts=? WHERE project_id=?', (1000 + tick, 12 - tick % 3))
        turn = state['turn'] if state is not None else 0
        result = schedule.plan(db, state)
        state = result['next']
        if turn == 3 and result['project_id'] is not None:
            fair.append(result['project_id'])
            if first_upper is None:
                first_upper = state['fair_upper']
                add(db, 13, 0, 9999)
        if 13 in fair:
            break
    assert first_upper == 12
    assert fair == list(range(1, 13)) + list(range(1, 14))


def test_deleted_bound_patient_does_not_extend_fair_cycle(db):
    for pid in (1, 2, 3):
        add(db, pid)
    state = {'version': 1, 'turn': 3, 'activity_after': None, 'fair_after': 0, 'fair_upper': None}
    first = schedule.plan(db, state)
    assert first['project_id'] == 1 and first['next']['fair_upper'] == 3
    db.execute('DELETE FROM patients WHERE project_id=3')
    add(db, 4)
    state = {**first['next'], 'turn': 3}
    second = schedule.plan(db, state)
    assert second['project_id'] == 2
    last = schedule.plan(db, {**second['next'], 'turn': 3})
    assert last['project_id'] is None and last['reason'] == 'cycle_end'
    assert last['next']['fair_after'] == 0 and last['next']['fair_upper'] is None


@pytest.mark.parametrize('value', [True, -1, 4, 2**63, float('nan')])
def test_invalid_turn_is_held(db, value):
    with pytest.raises(ValueError, match='patient_scan_bookmark_invalid'):
        schedule.plan(db, {'version': 1, 'turn': value, 'activity_after': None,
                           'fair_after': 0, 'fair_upper': None})


@pytest.mark.parametrize('change', [
    {'version': True}, {'version': 2}, {'fair_after': True}, {'fair_after': -1},
    {'fair_after': 1}, {'fair_upper': 0}, {'fair_upper': 2**63},
    {'fair_after': 2, 'fair_upper': 1}, {'activity_after': [0, float('nan'), 1]},
    {'activity_after': [0, float('inf'), 1]}, {'activity_after': [0, 10**1000, 1]},
    {'activity_after': [False, 1, 1]}, {'activity_after': [0, 1, True]},
    {'activity_after': [0, 1, -1]}, {'activity_after': [0, 1, 2**63]},
    {'activity_after': [0, '1', 1]}, {'activity_after': [0, None]}, {'extra': 1},
])
def test_malformed_bookmark_never_advances(db, change):
    state = {'version': 1, 'turn': 0, 'activity_after': None, 'fair_after': 0, 'fair_upper': None}
    state.update(change)
    before = repr(state)
    with pytest.raises(ValueError, match='patient_scan_bookmark_invalid'):
        schedule.plan(db, state)
    assert repr(state) == before


def test_no_patients_and_nonfinite_source_are_explicit(db):
    result = schedule.plan(db, None)
    assert result['project_id'] is None and result['exhausted'] and result['reason'] == 'empty'
    add(db, 1, 0, float('inf'))
    with pytest.raises(ValueError, match='patient_scan_source_invalid'):
        schedule.plan(db, None)


@pytest.mark.parametrize('borrowed', [False, True])
def test_plan_borrows_connection_and_uses_only_bounded_selects(db, borrowed):
    add(db, 1, 0, 10)
    db.commit()
    if borrowed:
        db.execute('BEGIN')
    statements = []
    db.set_trace_callback(statements.append)
    changes = db.total_changes
    for turn in (0, 3):
        state = {'version': 1, 'turn': turn, 'activity_after': None, 'fair_after': 0, 'fair_upper': None}
        assert schedule.plan(db, state)['project_id'] == 1
    assert db.in_transaction is borrowed and db.total_changes == changes
    assert statements and all(statement.lstrip().upper().startswith('SELECT ') and
                              'LIMIT 1' in statement.upper() for statement in statements)


def test_two_plans_require_cas_before_advancing(db):
    add(db, 1, 0, 10)
    db.execute('CREATE TABLE bookmark(value TEXT NOT NULL)')
    original = json.dumps({'version': 1, 'turn': 0, 'activity_after': None,
                           'fair_after': 0, 'fair_upper': None}, sort_keys=True)
    db.execute('INSERT INTO bookmark VALUES (?)', (original,))
    first = schedule.plan(db, json.loads(original))
    second = schedule.plan(db, json.loads(original))
    assert first == second and first['project_id'] == 1
    next_value = json.dumps(first['next'], sort_keys=True)
    assert db.execute('UPDATE bookmark SET value=? WHERE value=?', (next_value, original)).rowcount == 1
    assert db.execute('UPDATE bookmark SET value=? WHERE value=?', (next_value, original)).rowcount == 0
