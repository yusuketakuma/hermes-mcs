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


def test_snippet_siblings_do_not_revive_each_others_failed_jobs(tmp_path):
    """Two replies the thread API keeps returning as snippets must burn
    out and stay failed — a sibling's thread save may not reset them."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=100, project_id=1)])
    db.save_messages([
        _message(mid=101, project_id=1, parent_id=100, state="snippet"),
        _message(mid=102, project_id=1, parent_id=100, state="snippet"),
    ])

    class Adapter:
        def fetch_thread(self, pid, parent_id):
            return [
                _message(mid=101, project_id=1, parent_id=100,
                         state="snippet"),
                _message(mid=102, project_id=1, parent_id=100,
                         state="snippet"),
            ]

    for _ in range(40):
        db.db.execute(
            "UPDATE fetch_jobs SET next_try=0 WHERE state='pending'")
        db.db.commit()
        job_ops.run_reply_jobs(
            Adapter(), db, {"errors": []}, time.monotonic() + 60)
    assert db.job_state("reply", 1, message_id=101) == "failed"
    assert db.job_state("reply", 1, message_id=102) == "failed"
    assert db.pending_reply_jobs(1) == 0
    db.close()


def test_unparseable_posted_at_is_not_rewritten_on_reopen(tmp_path):
    import ledger
    db = _ledger(tmp_path)
    m = _message(mid=7, project_id=1)
    m.posted_at = ""
    db.save_messages([m])
    db.close()
    reopened = ledger.Ledger(str(tmp_path / "ledger.db"))
    try:
        assert reopened.db.total_changes == 0
        row = reopened.db.execute(
            "SELECT posted_at_ts, body_text FROM messages"
            " WHERE message_id=7").fetchone()
        assert row[0] is None and row[1]
    finally:
        reopened.close()
