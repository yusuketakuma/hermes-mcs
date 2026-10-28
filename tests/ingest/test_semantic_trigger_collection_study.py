"""Synthetic experiment: old semantic writer blocked by a DB trigger."""
import json
import sqlite3
import time

import pytest

import collection_stages
import ledger
import mcs_contract
from ingest_testkit import _ledger, _message, _unread_patient


class FakeAdapter:
    def __init__(self, replies):
        self.replies = replies
        self.unread = True
        self.calls = []

    def list_unread(self):
        patients = [_unread_patient(1)] if self.unread else []
        return mcs_contract.UnreadSnapshot(int(time.time()), patients)

    def fetch_unread_messages(self, pid, snapshot_ts):
        parent = _message(mid=10, project_id=pid)
        parent.replies = self.replies
        return mcs_contract.MessageBatch([parent], pages=1, reached=True)

    def fetch_unread_replies(self, parent):
        return mcs_contract.ReplyBatch(parent.replies, [])

    def fetch_latest(self, pid):
        return {'message_id': 10}

    def mark_patient_read(self, *args, **kwargs):
        self.calls.append('patient_ack')

    def thread_unread(self, pid, parent):
        self.calls.append('thread_check')
        return self.unread

    def fetch_thread(self, pid, parent):
        self.calls.append('thread_fetch')
        return self.replies

    def read_thread(self, pid, parent):
        self.calls.append('thread_ack')
        self.unread = False
        return {reply.message_id for reply in self.replies}

    def fetch_thread_window(self, pid, parent, start_page=1):
        self.calls.append('reply_refetch')
        return mcs_contract.MessageBatch(self.replies, pages=1, reached=True)


def _result():
    return {'errors': [], 'incomplete': [], 'messages': 0,
            'new_messages': 0, 'marked_read': []}


def _world(tmp_path, changed):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    root = _message(mid=10)
    old = _message(mid=11, parent_id=10, body='synthetic old reply')
    db.save_messages([root, old], project_id=1)
    db.semantic_seed(1, [10], {'source': 'history_import', 'notification_free': True})
    db.db.executescript("""
        CREATE TRIGGER synthetic_semantic_hold_update BEFORE UPDATE ON fetch_jobs
        WHEN OLD.kind='semantic' AND OLD.project_id=1 AND OLD.message_id=10
        BEGIN SELECT RAISE(ABORT, 'synthetic hold'); END;
        CREATE TRIGGER synthetic_semantic_hold_delete BEFORE DELETE ON fetch_jobs
        WHEN OLD.kind='semantic' AND OLD.project_id=1 AND OLD.message_id=10
        BEGIN SELECT RAISE(ABORT, 'synthetic hold'); END;
    """)
    reply = _message(mid=11 if changed else 12, parent_id=10,
                     body='synthetic edited reply' if changed else 'synthetic new reply',
                     unread=True)
    replies = [reply] if changed else [old, reply]
    return db, FakeAdapter(replies)


def _unread(db, adapter, excluded):
    result = _result()
    collection_stages.stage_unread(adapter, db, result, time.monotonic() + 180,
                                   db.begin_run(None), mark_read=True, semantic=True, ack_exclusions=excluded)
    return result


@pytest.mark.parametrize('changed', [False, True], ids=['new-reply', 'edited-existing-reply'])
def test_failed_unread_save_blocks_same_tick_thread_ack_then_retries(tmp_path, changed):
    db, adapter = _world(tmp_path, changed)
    try:
        original = tuple(db.db.execute("SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone())
        with pytest.raises(sqlite3.IntegrityError, match='synthetic hold'):
            with db.db:
                db.db.execute("DELETE FROM fetch_jobs WHERE kind='semantic' AND message_id=10")
        before_outbox = db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
        excluded = set()
        result = _unread(db, adapter, excluded)
        assert 'project 1: db_write_failed' in result['errors']
        assert excluded == {1}
        assert 'unread_save_failed_projects' not in json.dumps(result)
        assert 'patient_ack' not in adapter.calls
        assert tuple(db.db.execute("SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()) == original
        assert db.db.execute('SELECT body_text FROM messages WHERE message_id=11').fetchone()[0] == 'synthetic old reply'
        assert db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0] == before_outbox
        assert not db.db.in_transaction
        collection_stages.stage_thread_read(adapter, db, result, time.monotonic() + 180, exclude_projects=excluded)
        assert 'thread_ack' not in adapter.calls
        assert db.db.execute('SELECT 1 FROM thread_read_marks WHERE parent_id=10').fetchone() is None
        db.close()
        db = ledger.Ledger(str(tmp_path / 'ledger.db'))
        db.db.executescript('DROP TRIGGER synthetic_semantic_hold_update; DROP TRIGGER synthetic_semantic_hold_delete;')
        second = _unread(db, adapter, set())
        assert not second['errors']
        assert 'patient_ack' in adapter.calls
        mid = 11 if changed else 12
        expected = 'synthetic edited reply' if changed else 'synthetic new reply'
        assert db.db.execute('SELECT body_text FROM messages WHERE message_id=?', (mid,)).fetchone()[0] == expected
        collection_stages.stage_thread_read(adapter, db, second, time.monotonic() + 180)
        assert 'thread_ack' in adapter.calls
    finally:
        db.close()


def test_failed_patient_is_excluded_before_thread_candidate_limit(tmp_path):
    db, adapter = _world(tmp_path, True)
    try:
        for mid in range(20, 32):
            db.save_messages([_message(mid=mid * 10, parent_id=mid)], project_id=1)
        db.ensure_patient(2)
        db.save_messages([_message(mid=900, project_id=2),
                          _message(mid=901, project_id=2, parent_id=900)], project_id=2)
        excluded = set()
        result = _unread(db, adapter, excluded)
        assert excluded == {1}
        assert 'unread_save_failed_projects' not in json.dumps(result)
        patients = db.thread_read_candidates(0, 10, exclude_projects=excluded)
        assert [row['project_id'] for row in patients] == [2]
    finally:
        db.close()


def test_relogin_keeps_exclusion_after_successful_replay_without_result_field(tmp_path):
    db, base = _world(tmp_path, True)
    db.ensure_patient(2)
    expired = False

    class Adapter(FakeAdapter):
        marked = []

        def list_unread(self):
            return mcs_contract.UnreadSnapshot(int(time.time()), [_unread_patient(1), _unread_patient(2)])

        def fetch_unread_messages(self, pid, snapshot_ts):
            nonlocal expired
            if pid == 2 and not expired:
                expired = True
                raise mcs_contract.SessionExpired(status=403)
            if pid == 2:
                return mcs_contract.MessageBatch([_message(mid=30, project_id=2)], pages=1, reached=True)
            return super().fetch_unread_messages(pid, snapshot_ts)

        def auto_login(self, **kwargs):
            db.db.executescript('DROP TRIGGER synthetic_semantic_hold_update; DROP TRIGGER synthetic_semantic_hold_delete;')
            return 'ok'

        def mark_patient_read(self, pid, snapshot_ts, **kwargs):
            self.marked.append(pid)

        def fetch_latest(self, pid):
            return {'message_id': 10 if pid == 1 else 30}

    adapter = Adapter(base.replies)
    excluded, result = set(), _result()
    try:
        run_id = db.begin_run(None)
        collection_stages.with_relogin(
            adapter, db, result, 'unread', collection_stages.stage_unread,
            adapter, db, result, time.monotonic() + 180, run_id,
            mark_read=True, semantic=True, ack_exclusions=excluded)
        assert expired and excluded == {1}
        assert adapter.marked == [2]
        assert not any('db_write_failed' in item for item in result['errors'])
        assert 'unread_save_failed_projects' not in json.dumps(result)
        assert db.db.execute('SELECT body_text FROM messages WHERE message_id=11').fetchone()[0] == 'synthetic edited reply'
        collection_stages.stage_thread_read(adapter, db, result, time.monotonic() + 180,
                                            exclude_projects=excluded)
        assert 'thread_ack' not in adapter.calls
    finally:
        db.close()


def test_failure_reporting_error_still_records_same_tick_exclusion(tmp_path, monkeypatch):
    db, adapter = _world(tmp_path, True)
    excluded = set()
    try:
        def fail_reporting(*args):
            raise OSError('synthetic reporting failure')

        monkeypatch.setattr(db, 'patient_fetch_failed', fail_reporting)
        with pytest.raises(OSError, match='synthetic reporting failure'):
            _unread(db, adapter, excluded)
        assert excluded == {1}
        result = _result()
        collection_stages.stage_thread_read(adapter, db, result, time.monotonic() + 180,
                                            exclude_projects=excluded)
        assert 'thread_ack' not in adapter.calls
        assert 'unread_save_failed_projects' not in json.dumps(result)
    finally:
        db.close()
