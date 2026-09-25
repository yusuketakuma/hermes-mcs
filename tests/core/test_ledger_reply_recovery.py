"""A migrated reply with no body state must still receive a fetch job."""

import time

import job_ops
from ingest_testkit import _ledger, _message


def test_null_body_state_reply_is_refetched(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([
        _message(mid=21, project_id=1, parent_id=20, state="snippet")
    ])
    # A legacy row gained body_state in a migration but never received
    # a value. No reply fetch job was reserved when it was stored.
    db.db.execute(
        "UPDATE messages SET body_state=NULL WHERE message_id=21")
    db.db.commit()

    assert [r["message_id"] for r in db.replies_without_job()] == [21]

    class Adapter:
        def fetch_thread(self, pid, parent_id):
            assert (pid, parent_id) == (1, 20)
            return [_message(mid=21, project_id=1, parent_id=20)]

    job_ops.run_reply_jobs(
        Adapter(), db, {"errors": []}, time.monotonic() + 60)
    assert db.job_state("reply", 1, message_id=21) == "done"
    assert db.db.execute(
        "SELECT body_state FROM messages WHERE message_id=21"
    ).fetchone()[0] == "full"
    db.close()
