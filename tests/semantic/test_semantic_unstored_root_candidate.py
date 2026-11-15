"""Synthetic root-scoped barriers for intake that could not be saved."""
import sqlite3

import pytest

import semantic_intake_hold as hold
import semantic_runtime as runtime
import semantic_send_gate
from semantic_testkit import _message, _seeded


def test_known_unstored_root_blocks_that_root_without_blocking_another(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.save_messages([_message(10)], project_id=1, semantic=True)
        jobs = {row['message_id']: runtime.JobToken.from_row(row) for row in db.db.execute(
            "SELECT * FROM fetch_jobs WHERE kind='semantic' AND project_id=1")}
        hold.record_unstored(db, 1, roots=[1])
        assert hold.has_held_root(db.db, 1, 1)
        assert not hold.has_held_root(db.db, 1, 10)
        assert semantic_send_gate.intake_root_held(db.db, 1, 1)
        assert not semantic_send_gate.intake_root_held(db.db, 1, 10)
        assert not runtime.job_matches(db, jobs[1])
        assert runtime.job_matches(db, jobs[10])
        with db.db:
            assert not runtime.transition_tx(db, jobs[1], 'done')
            assert runtime.transition_tx(db, jobs[10], 'done')
        assert db.db.execute('SELECT state FROM fetch_jobs WHERE job_id=?', (jobs[1].job_id,)).fetchone()[0] == 'pending'
    finally:
        db.close()


def test_new_arrival_on_unstored_root_keeps_new_intent_held(tmp_path):
    db = _seeded(tmp_path)
    try:
        before = tuple(db.db.execute("SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone())
        hold.record_unstored(db, 1, roots=[1])
        db.save_messages([_message(3, parent=1)], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        assert tuple(db.db.execute("SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()) == before
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=3').fetchone()
        assert db.db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=1 AND model='1'",
                             (hold.HOLD_KIND,)).fetchone()
    finally:
        db.close()


@pytest.mark.parametrize('messages', [[], None, [_message(7, pid=2)],
                                        [_message(i + 100) for i in range(513)]])
def test_undeterminable_or_large_input_marks_scope_unknown(tmp_path, messages):
    db = _seeded(tmp_path)
    try:
        assert hold.failure_roots(messages, 1) is None
        hold.record_unstored(db, 1, roots=hold.failure_roots(messages, 1))
        assert hold.has_unstored_root(db.db, 1, 1)
        assert hold.has_unstored_root(db.db, 1, 999)
    finally:
        db.close()


def test_marker_without_scope_fails_closed(tmp_path):
    db = _seeded(tmp_path)
    try:
        hold.record_unstored(db, 1, roots=[1])
        with db.db:
            db.db.execute('DELETE FROM fetch_jobs WHERE kind=?', (hold.UNSTORED_ROOT_KIND,))
        assert hold.has_unstored_root(db.db, 1, 10)
    finally:
        db.close()


def test_failure_scope_includes_original_stored_parent_binding(tmp_path):
    db = _seeded(tmp_path)
    try:
        incoming = _message(2, parent=None)
        scope = hold._failure_scope([incoming], 1)
        assert scope == ([2], [2])
        hold.record_unstored(db, 1, roots=scope[0], observed_ids=scope[1])
        assert hold.has_unstored_root(db.db, 1, 1)
        assert hold.has_unstored_root(db.db, 1, 2)
        assert not hold.has_unstored_root(db.db, 1, 10)
    finally:
        db.close()


def test_open_transaction_is_not_committed_by_diagnostic(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.db.execute("UPDATE patients SET disease='synthetic uncommitted' WHERE project_id=1")
        with pytest.raises(sqlite3.DatabaseError, match='transaction_unverified'):
            hold.record_unstored(db, 1, roots=[1])
        assert db.db.in_transaction
        db.db.rollback()
        assert db.db.execute('SELECT disease FROM patients WHERE project_id=1').fetchone()[0] != 'synthetic uncommitted'
        assert not hold.has_unstored(db.db, 1)
    finally:
        db.close()
