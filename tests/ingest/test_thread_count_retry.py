"""A short successful thread response still needs durable retry work."""

import time

import job_ops
from ingest_testkit import _ledger, _message


def test_missing_reply_count_reserves_thread_until_all_replies_arrive(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    parent = _message(mid=10, project_id=1)
    parent.reply_count = 2

    class Adapter:
        complete = False

        def fetch_thread(self, pid, mid):
            assert (pid, mid) == (1, 10)
            replies = [_message(mid=21, project_id=pid, parent_id=mid)]
            if self.complete:
                replies.append(_message(mid=22, project_id=pid, parent_id=mid))
            return replies

    adapter = Adapter()
    merged = job_ops.merge_full_replies(
        adapter, [parent], 0, time.monotonic() + 60,
        {"errors": []}, ledger=db)
    assert not merged.checkpoint_safe
    assert db.job_state("thread", 1, message_id=10) == "pending"
    db.save_messages([parent], project_id=1)

    first = {"errors": []}
    job_ops.run_reply_jobs(adapter, db, first, time.monotonic() + 60)
    assert db.job_state("thread", 1, message_id=10) == "pending"
    assert "thread 10: replies_missing" in first["errors"]
    assert db.job_payload("thread", 1, message_id=10)["page"] == 1

    adapter.complete = True
    db.db.execute(
        "UPDATE fetch_jobs SET next_try=0 "
        "WHERE kind='thread' AND project_id=1 AND message_id=10")
    db.db.commit()
    job_ops.run_reply_jobs(adapter, db, {"errors": []}, time.monotonic() + 60)
    assert db.job_state("thread", 1, message_id=10) == "done"
    assert db.db.execute(
        "SELECT COUNT(*) FROM messages WHERE parent_id=10"
    ).fetchone()[0] == 2
    db.close()
