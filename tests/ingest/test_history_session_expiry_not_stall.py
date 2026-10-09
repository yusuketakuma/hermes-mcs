"""Regression: a session that expires while a history window's threads
merge aborts the run like the raised fetch path — it is not a window
stall, so repeated re-logins never fail the import 'window_stalled'."""

import json
import time

import pytest

import job_ops
import mcs_adapter
from ingest_testkit import _ledger, _message


class _ExpiringAdapter:
    def fetch_history(self, pid, since, max_pages=10, start_page=1):
        m = _message(mid=5, project_id=pid)
        m.reply_count = 3
        return mcs_adapter.MessageBatch([m], pages=1, reached=False)

    def fetch_thread(self, *a, **k):
        raise mcs_adapter.SessionExpired("session_expired")


def test_session_expiry_during_merge_is_not_a_stall(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(72)
    db.job_add("history", 72, payload={"since": 0, "page": 1, "pages": 2})
    result = {"errors": []}
    for _ in range(job_ops.HISTORY_STALL_LIMIT):
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='history'")
        db.db.commit()
        with pytest.raises(mcs_adapter.SessionExpired):
            job_ops.run_history_jobs(_ExpiringAdapter(), db, result,
                                     time.monotonic() + 60, min_margin=0)
    job = db.job_pending("history", 72)
    assert job["state"] == "pending"
    assert json.loads(job["payload"]).get("stalls", 0) == 0
    assert "import 72: window_stalled" not in result["errors"]
    db.close()
