"""Synthetic regression: a failed thread fetch queues only replies whose bodies are still missing."""
import time

import job_ops
import mcs_adapter
from ingest_testkit import _iso, _ledger, _msg_at


class _FailingThreads:
    def fetch_thread(self, pid, mid, **kw):
        raise mcs_adapter.MCSError("network_error", retryable=True)


def test_failed_thread_fetch_leaves_terminal_replies_alone(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(60)
    parent = _msg_at(600, 60, _iso(1))
    parent.reply_count = 3
    full = _msg_at(601, 60, _iso(1))
    snippet = _msg_at(602, 60, _iso(1))
    snippet.body_state = "snippet"
    parent.replies = [full, snippet]
    db.job_add("reply", 60, 601, parent_id=600)
    db.job_done(db.job_pending("reply", 60, 601)["job_id"])
    stats = {"errors": []}
    merged = job_ops.merge_full_replies(_FailingThreads(), [parent], 0,
                                        time.monotonic() + 60, stats, ledger=db)
    assert merged.reply_jobs == 1 and stats["reply_jobs"] == 1
    assert db.job_state("reply", 60, 601) == "done"       # not revived
    assert db.job_state("reply", 60, 602) == "pending"
    assert not merged.checkpoint_safe                     # thread still unverified
    db.close()
