"""A post must be offered for notification before MCS marks it read:
reply merges keep the unread flag, post-ack-gap walks notify read
arrivals, and replies acknowledged by thread-read before storage keep
unread evidence on their reply job. Synthetic stubs only — no real MCS."""
import json
import time
from types import SimpleNamespace

import job_ops
import mcs_adapter
import run_check
from ingest_testkit import _ledger, _message


def _notified(db):
    ids = []
    for row in db.db.execute(
            "SELECT payload FROM notify_outbox WHERE kind='new_messages'"):
        ids += json.loads(row["payload"])["message_ids"]
    return ids


def test_thread_merge_keeps_embedded_unread_flag(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    parent = _message(mid=10)
    parent.reply_count = 1
    parent.replies = [_message(mid=11, parent_id=10, state="snippet",
                               unread=True)]
    adapter = SimpleNamespace(fetch_thread=lambda pid, mid: [
        _message(mid=11, parent_id=10, unread=False)])
    stats = {"errors": [], "threads": 0, "reply_jobs": 0}
    job_ops.merge_full_replies(adapter, [parent], 0,
                               time.monotonic() + 60, stats, ledger=db)
    assert parent.replies[0].body_state == "full"
    assert parent.replies[0].is_unread is True
    db.save_messages([parent], project_id=1, notify={"source": "history"})
    assert 11 in _notified(db)


def test_post_ack_gap_walk_notifies_already_read_rows(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=100)], project_id=1)
    # a head job already pending (from backfill) gets the flag merged in
    db.job_add("history_head", 1, payload={"since": 0, "page": 1,
                                           "pages": 2, "trickle": False})
    result = {"errors": []}
    run_check._post_ack_gap(
        SimpleNamespace(fetch_latest=lambda pid: {"message_id": 200}),
        db, 1, result)
    assert result["errors"] == ["mark 1: post_ack_gap"]
    assert db.job_pending("history_head", 1)["payload"].count('"notify": true')
    adapter = SimpleNamespace(fetch_history=lambda *a, **k: mcs_adapter.MessageBatch(
        [_message(mid=200, unread=False)], pages=1, reached=True))
    job_ops.run_history_jobs(adapter, db, {"errors": []},
                             time.monotonic() + 300)
    assert _notified(db) == [200]


def test_plain_history_job_stays_quiet(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("history", 1, payload={"since": 0, "page": 1, "pages": 2})
    adapter = SimpleNamespace(fetch_history=lambda *a, **k: mcs_adapter.MessageBatch(
        [_message(mid=200, unread=False)], pages=1, reached=True))
    job_ops.run_history_jobs(adapter, db, {"errors": []},
                             time.monotonic() + 300)
    assert _notified(db) == []


def test_invalid_notify_flag_fails_the_job(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("history_head", 1, payload={"since": 0, "notify": "yes"})
    result = {"errors": []}
    job_ops.run_history_jobs(SimpleNamespace(), db, result,
                             time.monotonic() + 300)
    assert db.job_state("history_head", 1) == "failed"
    assert result["errors"] == ["import job: invalid_payload"]


def _store(db, mid, parent=None):
    db.db.execute(
        "INSERT INTO messages(message_id,project_id,parent_id,posted_at,"
        "posted_at_ts,body_text,body_state,content_hash,first_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (mid, 1, parent, "2026-10-06T09:00", 1, "本文", "full",
         f"{mid:064x}", time.time()))
    db.db.commit()


def test_reply_acknowledged_by_thread_read_is_notified_when_stored(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    _store(db, 10)
    _store(db, 11, parent=10)
    db.db.execute("UPDATE messages SET notified_at=1")
    db.db.commit()
    # 14 already has an unflagged pending job; 13 is new
    db.job_add("reply", 1, 14, parent_id=10)
    answers = [True, False]
    adapter = SimpleNamespace(
        thread_unread=lambda pid, parent: answers.pop(0),
        fetch_thread=lambda pid, parent: [SimpleNamespace(message_id=11)],
        read_thread=lambda pid, parent: {11, 13, 14})
    run_check.stage_thread_read(adapter, db, {"errors": []},
                                time.monotonic() + 120)
    for rid in (13, 14):
        assert json.loads(db.job_pending("reply", 1, rid)["payload"]) \
            == {"unread": True}
    replies = [_message(mid=i, parent_id=10, unread=False) for i in (11, 13, 14)]
    drain = SimpleNamespace(fetch_thread=lambda pid, parent: replies)
    job_ops.run_reply_jobs(drain, db, {"errors": []}, time.monotonic() + 120)
    assert sorted(_notified(db)) == [13, 14]
    assert db.job_state("reply", 1, 13) == "done"
