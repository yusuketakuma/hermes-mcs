"""Regression: a run-deadline cut is unfinished work, not a failure — it
must not burn job attempts, which never reset, so cuts spread over a long
deep import used to fail a live patient's history job for good. Cuts are
bounded by consecutive no-progress ticks (stalls) instead, so a window that
never completes still surfaces 'failed'."""
import mcs_contract

import json
import time

import job_ops
from ingest_testkit import _ledger, _message

_RUNS = 9  # one past ledger.job_retry's max_attempts=8


def _cut():
    return mcs_contract.MCSError("deadline_exceeded", retryable=True)


class _HistoryAdapter:
    """Like the real adapter, reports deadline_exceeded only once the run
    deadline has passed, with the pages it completed before the cut."""

    def __init__(self, pages, cut=True):
        self.pages = pages
        self.cut = cut
        self.deadline = None

    def fetch_history(self, pid, since, max_pages=10, start_page=1):
        msgs = [_message(mid=1000 + start_page, project_id=pid)] \
            if self.pages else []
        if not self.cut:
            return mcs_contract.MessageBatch(msgs, pages=self.pages,
                                            reached=False)
        while time.monotonic() < self.deadline:
            time.sleep(0.005)
        return mcs_contract.MessageBatch(msgs, pages=self.pages,
                                        reached=False, error=_cut())


def _tick(db, adapter, budget=0.02):
    db.db.execute("UPDATE fetch_jobs SET next_try=0")
    db.db.commit()
    adapter.deadline = time.monotonic() + budget
    job_ops.run_history_jobs(adapter, db, {"errors": []}, adapter.deadline,
                             min_margin=0)


def _history(db):
    return db.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='history'").fetchone()


def test_history_deadline_cuts_between_progress_keep_attempts(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(81)
    db.job_add("history", 81, payload={"since": 0, "page": 1, "pages": 2})
    cut, ok = _HistoryAdapter(pages=1), _HistoryAdapter(pages=2, cut=False)
    for _ in range(_RUNS):
        _tick(db, cut)
        _tick(db, ok, budget=60)
    job = _history(db)
    assert job["state"] == "pending"
    assert job["attempts"] == 0
    assert json.loads(job["payload"])["page"] > 1
    db.close()


def test_history_deadline_cut_every_tick_stays_bounded(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(82)
    db.job_add("history", 82, payload={"since": 0, "page": 1, "pages": 2})
    adapter = _HistoryAdapter(pages=1)
    for _ in range(job_ops.HISTORY_STALL_LIMIT - 1):
        _tick(db, adapter)
    assert _history(db)["state"] == "pending"
    _tick(db, adapter)
    job = _history(db)
    assert job["state"] == "failed"
    assert job["attempts"] == 0
    assert job["reason_code"] == "deadline_exceeded"
    assert json.loads(job["payload"])["page"] == 1
    db.close()


def test_history_deadline_cut_without_pages_stays_bounded(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(83)
    db.job_add("history", 83, payload={"since": 0, "page": 1, "pages": 2})
    adapter = _HistoryAdapter(pages=0)
    for _ in range(job_ops.HISTORY_STALL_LIMIT):
        _tick(db, adapter)
    job = _history(db)
    assert job["state"] == "failed"
    assert job["reason_code"] == "deadline_exceeded"
    db.close()


class _ThreadAdapter:
    def __init__(self, pages):
        self.pages = pages

    def fetch_thread_window(self, pid, parent_id, start_page=1):
        return mcs_contract.MessageBatch([], pages=self.pages,
                                        reached=False, error=_cut())


def _drain_replies(db, adapter):
    for _ in range(_RUNS):
        db.db.execute("UPDATE fetch_jobs SET next_try=0")
        db.db.commit()
        job_ops.run_reply_jobs(adapter, db, {"errors": []},
                               time.monotonic() + 60)
    return {r["kind"]: r for r in db.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind IN ('reply','thread')")}


def test_reply_deadline_cut_with_progress_keeps_attempts(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(84)
    db.job_add("reply", 84, 301, parent_id=300)
    jobs = _drain_replies(db, _ThreadAdapter(pages=1))
    assert set(jobs) == {"reply", "thread"}
    for job in jobs.values():
        assert job["state"] == "pending"
        assert job["attempts"] == 0
    db.close()


def test_reply_deadline_cut_without_progress_stays_bounded(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(85)
    db.job_add("reply", 85, 401, parent_id=400)
    jobs = _drain_replies(db, _ThreadAdapter(pages=0))
    assert jobs["reply"]["state"] == "failed"
    assert jobs["reply"]["attempts"] == 8
    assert jobs["reply"]["reason_code"] == "deadline_exceeded"
    db.close()


# Regression: reconcile must not pin forever on an uncertifiable window,
# and the history 'waiting_replies' defer must not back off 0 s.
def test_reconcile_skips_uncertifiable_window_after_stall_limit(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reconcile", 1, payload={"page": 1})
    calls = []

    class Adapter:
        def fetch_history(self, pid, since, max_pages=1, start_page=None):
            calls.append(start_page)
            return mcs_contract.MessageBatch(
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
            return mcs_contract.MessageBatch([], pages=1, reached=True)

    job_ops.run_history_jobs(Adapter(), db, {"errors": []},
                             time.monotonic() + 100)
    row = db.db.execute(
        "SELECT reason_code, next_try FROM fetch_jobs WHERE kind='history'"
    ).fetchone()
    assert row["reason_code"] == "waiting_replies"
    assert row["next_try"] - time.time() >= 590
    db.close()


# Regression: a merge cut by the run deadline is unfinished work, not a
# window stall — history must not fail 'window_stalled' and reconcile must
# not skip the unverified window.
class _SlowAdapter:
    """Window fetch eats the run budget so merge stops at the deadline."""
    threads = 0

    def fetch_history(self, pid, since, max_pages=10, start_page=1):
        time.sleep(0.05)
        m = _message(mid=5, project_id=pid)
        m.reply_count = 3
        return mcs_contract.MessageBatch([m], pages=1, reached=False)

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
