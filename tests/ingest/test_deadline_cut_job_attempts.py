"""Regression: a run-deadline cut is unfinished work, not a failure — it
must not burn job attempts, which never reset, so cuts spread over a long
deep import used to fail a live patient's history job for good. Cuts are
bounded by consecutive no-progress ticks (stalls) instead, so a window that
never completes still surfaces 'failed'."""

import json
import time

import job_ops
import mcs_adapter
from ingest_testkit import _ledger, _message

_RUNS = 9  # one past ledger.job_retry's max_attempts=8


def _cut():
    return mcs_adapter.MCSError("deadline_exceeded", retryable=True)


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
            return mcs_adapter.MessageBatch(msgs, pages=self.pages,
                                            reached=False)
        while time.monotonic() < self.deadline:
            time.sleep(0.005)
        return mcs_adapter.MessageBatch(msgs, pages=self.pages,
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
        return mcs_adapter.MessageBatch([], pages=self.pages,
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
