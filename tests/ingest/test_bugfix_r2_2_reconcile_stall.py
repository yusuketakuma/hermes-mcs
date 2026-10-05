"""Regression: reconcile must not pin forever on an uncertifiable window,
and the history 'waiting_replies' defer must not back off 0 s."""

import json
import time

import job_ops
import mcs_adapter
from ingest_testkit import _ledger, _message


def test_reconcile_skips_uncertifiable_window_after_stall_limit(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reconcile", 1, payload={"page": 1})
    calls = []

    class Adapter:
        def fetch_history(self, pid, since, max_pages=1, start_page=None):
            calls.append(start_page)
            return mcs_adapter.MessageBatch(
                [_message(mid=50, state="snippet")], pages=2, reached=False)

    result = {"errors": []}
    for _ in range(job_ops.HISTORY_STALL_LIMIT + 1):
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='reconcile'")
        job_ops.run_reconcile_jobs(Adapter(), db, result,
                                   time.monotonic() + 100)

    assert calls[:job_ops.HISTORY_STALL_LIMIT] == [1] * job_ops.HISTORY_STALL_LIMIT
    assert calls[-1] == 3
    job = db.job_pending("reconcile", 1)
    assert job["state"] == "pending" and job["attempts"] == 0
    assert json.loads(job["payload"])["page"] == 3
    assert "reconcile 1: window_stalled" in result["errors"]
    db.close()


def test_reconcile_rejects_invalid_stalls(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reconcile", 1, payload={"page": 1, "stalls": "x"})
    result = {"errors": []}
    job_ops.run_reconcile_jobs(object(), db, result, time.monotonic() + 100)
    assert "reconcile: invalid_payload" in result["errors"]
    db.close()


def test_waiting_replies_defer_backs_off(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, 20, payload={})
    db.job_add("history", 1, 0, payload={"since": 0})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch([], pages=1, reached=True)

    job_ops.run_history_jobs(Adapter(), db, {"errors": []},
                             time.monotonic() + 100)
    row = db.db.execute(
        "SELECT reason_code, next_try FROM fetch_jobs WHERE kind='history'"
    ).fetchone()
    assert row["reason_code"] == "waiting_replies"
    assert row["next_try"] - time.time() >= 590
    db.close()
