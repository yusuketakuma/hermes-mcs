"""Regression: a merge cut by the run deadline is unfinished work, not a
window stall — history must not fail 'window_stalled' and reconcile must
not skip the unverified window."""

import json
import time

import job_ops
import mcs_adapter
from ingest_testkit import _ledger, _message


class _SlowAdapter:
    """Window fetch eats the run budget so merge stops at the deadline."""
    threads = 0

    def fetch_history(self, pid, since, max_pages=10, start_page=1):
        time.sleep(0.05)
        m = _message(mid=5, project_id=pid)
        m.reply_count = 3
        return mcs_adapter.MessageBatch([m], pages=1, reached=False)

    def fetch_thread(self, *a, **k):
        _SlowAdapter.threads += 1
        return []


def test_history_deadline_cut_is_not_a_stall(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(72)
    db.job_add("history", 72, payload={"since": 0, "page": 1, "pages": 2})
    result = {"errors": []}
    for _ in range(job_ops.HISTORY_STALL_LIMIT + 1):
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='history'")
        db.db.commit()
        job_ops.run_history_jobs(_SlowAdapter(), db, result,
                                 time.monotonic() + 0.01, min_margin=0)
    job = db.job_pending("history", 72)
    assert job["state"] == "pending"
    assert json.loads(job["payload"]).get("stalls", 0) == 0
    assert "import 72: window_stalled" not in result["errors"]
    db.close()


def test_reconcile_deadline_cut_does_not_skip_window(tmp_path, monkeypatch):
    # reconcile keeps a fixed 30 s margin: jump the clock 60 s per fetch
    real = time.monotonic
    skew = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: real() + skew[0])

    class Adapter(_SlowAdapter):
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            skew[0] += 60
            return super().fetch_history(pid, since, max_pages, start_page)

    db = _ledger(tmp_path)
    db.ensure_patient(72)
    db.job_add("reconcile", 72, payload={"page": 1})
    result = {"errors": []}
    for _ in range(job_ops.HISTORY_STALL_LIMIT + 1):
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='reconcile'")
        db.db.commit()
        job_ops.run_reconcile_jobs(Adapter(), db, result,
                                   time.monotonic() + 31)
    payload = json.loads(db.job_pending("reconcile", 72)["payload"])
    assert payload["page"] == 1 and payload.get("stalls", 0) == 0
    assert "reconcile 72: window_stalled" not in result["errors"]
    db.close()
