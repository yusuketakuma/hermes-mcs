"""A tombstoned reply counts the same in the thread job, view and repair plan."""

import time

import job_ops
import mcs_repair
from ingest_testkit import _ledger, _message
from mcs_queries import incomplete_reply_roots


def test_tombstoned_reply_agrees_between_job_view_and_repair(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    parent = _message(mid=10, project_id=1)
    parent.reply_count = 2
    db.save_messages([parent], project_id=1)

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=21, project_id=pid, parent_id=mid),
                    _message(mid=22, project_id=pid, parent_id=mid, state="deleted")]

    db.job_add("thread", 1, 10, parent_id=10)
    job_ops.run_reply_jobs(Adapter(), db, {"errors": []}, time.monotonic() + 60)
    assert db.db.execute(
        "SELECT body_state FROM messages WHERE message_id=22").fetchone()[0] == "deleted"
    assert db.job_state("thread", 1, message_id=10) == "done"
    assert incomplete_reply_roots(db.db, 1) == 0
    assert db.db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE {mcs_repair.REPLY_GAP}").fetchone()[0] == 0
    db.close()
