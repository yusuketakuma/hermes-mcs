"""Synthetic evidence for the limits of rejecting an unreadable seed."""
import json
import time

import pytest

import collection_stages
import ledger
import mcs_contract
from ingest_testkit import _ledger, _message, _unread_patient


class Adapter:
    def __init__(self, replies):
        self.replies = replies
        self.unread = True
        self.calls = []

    def list_unread(self):
        return mcs_contract.UnreadSnapshot(int(time.time()),
                                          [_unread_patient(1)] if self.unread else [])

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
        return self.unread

    def fetch_thread(self, pid, parent):
        return self.replies

    def read_thread(self, pid, parent):
        self.calls.append('thread_ack')
        self.unread = False
        return {r.message_id for r in self.replies}


def _result():
    return {'errors': [], 'incomplete': [], 'messages': 0,
            'new_messages': 0, 'marked_read': []}


def _tick(db, adapter):
    result, excluded = _result(), set()
    collection_stages.stage_unread(adapter, db, result, time.monotonic() + 180,
                                   db.begin_run(None), mark_read=True, semantic=True,
                                   ack_exclusions=excluded)
    collection_stages.stage_thread_read(adapter, db, result, time.monotonic() + 180,
                                        exclude_projects=excluded)
    return result, excluded


@pytest.mark.parametrize('raw', ['{', 'null', '[]',
    '{"inference_engine_version":"2.0", "new":"keep"}',
    '{"inference_engine_version":"1.0", "inference_engine_version":"2.0"}',
    '{"targets": []}', '{"targets": null}'])
@pytest.mark.parametrize('changed', [False, True])
def test_same_tick_ack_is_blocked_and_retry_requires_source_retention(tmp_path, raw, changed):
    db = _ledger(tmp_path)
    try:
        db.ensure_patient(1)
        root = _message(mid=10)
        old = _message(mid=11, parent_id=10, body='synthetic old reply')
        db.save_messages([root, old], project_id=1)
        db.semantic_seed(1, [10], {'source': 'history_import', 'notification_free': True})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic'", (raw,))
        before = tuple(db.db.execute("SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone())
        before_events = db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
        reply = _message(mid=11 if changed else 12, parent_id=10,
                         body='synthetic new content', unread=True)
        adapter = Adapter([reply] if changed else [old, reply])
        result, excluded = _tick(db, adapter)
        assert 'project 1: db_write_failed' in result['errors']
        assert excluded == {1}
        assert adapter.calls == []
        assert tuple(db.db.execute("SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()) == before
        assert db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0] == before_events
        assert db.db.execute('SELECT body_text FROM messages WHERE message_id=11').fetchone()[0] == 'synthetic old reply'
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=12').fetchone() is None
        # External read/removal between ticks makes the unread source unable to
        # deliver the rolled-back update or its event, even after repair.
        adapter.unread = False
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'", (json.dumps({'targets': [10]}),))
        result, excluded = _tick(db, adapter)
        assert not result['errors'] and not excluded
        assert db.db.execute('SELECT body_text FROM messages WHERE message_id=11').fetchone()[0] == 'synthetic old reply'
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=12').fetchone() is None
        assert db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0] == before_events
    finally:
        db.close()


def test_explicit_seed_failure_rolls_back_without_losing_an_unrelated_job(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.ensure_patient(1)
        db.save_messages([_message(mid=10), _message(mid=20)], project_id=1)
        db.semantic_seed(1, [10, 20], {'source': 'history_import'})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic' AND message_id=10")
        before = [tuple(row) for row in db.db.execute(
            "SELECT message_id,payload,state,attempts FROM fetch_jobs WHERE kind='semantic' ORDER BY message_id")]
        with pytest.raises(ledger.SemanticIntakeHeld):
            db.semantic_seed(1, [20, 10], {'source': 'history_import'})
        after = [tuple(row) for row in db.db.execute(
            "SELECT message_id,payload,state,attempts FROM fetch_jobs WHERE kind='semantic' ORDER BY message_id")]
        assert after == before and not db.db.in_transaction
    finally:
        db.close()
