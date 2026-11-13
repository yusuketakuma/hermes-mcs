"""The background lane serves notifiable semantic arrivals ahead of the
reprocess backlog, without changing what it selects, the QC order or
the realtime lane.

Synthetic fixtures and temp DB only."""
import json
import time

import pytest

import semantic_due_selection
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _ledger, _message, _patient


@pytest.fixture
def jobs_db(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    _patient(db, pid=2)
    # patient 2 is the more recently active one
    older, newer = _message(1), _message(2, pid=2)
    older.posted_at, newer.posted_at = '2026-01-01T00:00:00+09:00', '2026-10-01T00:00:00+09:00'
    db.save_messages([older], project_id=1)
    db.save_messages([newer], project_id=2)
    yield db
    db.close()


def _job(db, mid, pid=1, kind='semantic', **flags):
    now = time.time() - 10
    payload = {VERSION_KEY: CURRENT_VERSION, "targets": [mid],
               "generation": f"g{mid}", **flags}
    with db.db:
        return db.db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,"
            "payload,state,attempts,next_try,created_at,updated_at)"
            " VALUES(?,?,?,NULL,?,'pending',0,?,?,?)",
            (kind, pid, mid, json.dumps(payload, sort_keys=True), now, now, now)).lastrowid


def _order(db, prioritize, kinds=('semantic',)):
    due = semantic_due_selection._select_due_class(
        db, 'backlog', kinds, False, 8, {}, False, prioritize_patients=prioritize)[0]
    return [row['job_id'] for row in due]


@pytest.mark.parametrize('prioritize', [False, True])
def test_notifiable_arrival_precedes_older_reprocess(jobs_db, prioritize):
    old = _job(jobs_db, 100, pid=2, reprocess_only=True)
    free = _job(jobs_db, 101, pid=2, eligible=True, notification_free=True)
    arrival = _job(jobs_db, 102, pid=1, eligible=True)
    # before the change: [old, free, arrival] (job_id / patient activity order)
    assert _order(jobs_db, prioritize) == [arrival, old, free]


def test_notification_free_eligible_job_is_not_promoted(jobs_db):
    a = _job(jobs_db, 100, reprocess_only=True)
    b = _job(jobs_db, 101, eligible=True, notification_free=True)
    assert _order(jobs_db, False) == [a, b]


def test_malformed_payload_stays_selectable_behind_arrivals(jobs_db):
    arrival = _job(jobs_db, 102, eligible=True)
    with jobs_db.db:
        bad = jobs_db.db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,payload,state,"
            "attempts,next_try,created_at,updated_at) VALUES('semantic',1,50,NULL,"
            "'not json','pending',0,0,0,0)").lastrowid
    assert _order(jobs_db, False) == [arrival, bad]


def test_qc_order_is_unchanged(jobs_db):
    q1 = _job(jobs_db, 300, kind='extract_qc', eligible=True)
    q2 = _job(jobs_db, 301, kind='extract_qc')
    with jobs_db.db:
        jobs_db.db.execute("UPDATE fetch_jobs SET next_try=next_try-100 WHERE job_id=?", (q2,))
    assert _order(jobs_db, False, kinds=('extract_qc',)) == [q2, q1]
