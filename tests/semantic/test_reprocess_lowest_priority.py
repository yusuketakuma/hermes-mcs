"""Notification-free reprocess jobs stay selectable but come after every
other due semantic job (owner decision 2026-11-13), in every lane.

Synthetic fixtures and temp DB only."""
import json
import time

import pytest

import semantic_due_selection
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _ledger, _message, _patient

REPROCESS = {"reprocess_only": True, "notification_free": True}


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


def _job(db, mid, pid=1, age=10, **flags):
    now = time.time() - age
    payload = {VERSION_KEY: CURRENT_VERSION, "targets": [mid],
               "generation": f"g{mid}", **flags}
    with db.db:
        return db.db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,"
            "payload,state,attempts,next_try,created_at,updated_at)"
            " VALUES('semantic',?,?,NULL,?,'pending',0,?,?,?)",
            (pid, mid, json.dumps(payload, sort_keys=True), now, now, now)).lastrowid


def _order(db, lane='backlog', prioritize=False):
    due = semantic_due_selection._select_due_class(
        db, lane, ('semantic',), False, 8, {}, False, prioritize_patients=prioritize)[0]
    return [row['job_id'] for row in due]


@pytest.mark.parametrize('prioritize', [False, True])
def test_reprocess_follows_other_backlog_work(jobs_db, prioritize):
    # lowest job_id on the most active patient: first under the old order
    reprocess = _job(jobs_db, 100, pid=2, **REPROCESS)
    history = _job(jobs_db, 101, notification_free=True)
    arrival = _job(jobs_db, 102, eligible=True)
    order = _order(jobs_db, prioritize=prioritize)
    assert set(order) == {reprocess, history, arrival}
    assert order[-1] == reprocess


def test_unreadable_payload_is_not_buried_behind_reprocess(jobs_db):
    reprocess = _job(jobs_db, 100, **REPROCESS)
    with jobs_db.db:
        bad = jobs_db.db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,payload,state,"
            "attempts,next_try,created_at,updated_at) VALUES('semantic',1,150,NULL,"
            "'not json','pending',0,0,0,0)").lastrowid
    assert _order(jobs_db) == [bad, reprocess]


def test_archive_turn_does_not_serve_reprocess_first(jobs_db):
    with jobs_db.db:
        jobs_db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    archived_reprocess = _job(jobs_db, 100, pid=2, **REPROCESS)
    active_history = _job(jobs_db, 101, notification_free=True)
    # the archive turn alternates; neither turn puts the reprocess row first
    for _ in range(2):
        due = semantic_due_selection._select_due(jobs_db, 'backlog', ('semantic',), False, 1, {})[0]
        assert due[0]['job_id'] == active_history
    with jobs_db.db:
        jobs_db.db.execute("UPDATE fetch_jobs SET state='done' WHERE job_id=?", (active_history,))
    due = semantic_due_selection._select_due(jobs_db, 'backlog', ('semantic',), False, 1, {})[0]
    assert due[0]['job_id'] == archived_reprocess


def test_realtime_serves_newer_reprocess_seed_after_arrival(jobs_db):
    # an old arrival job keeps eligible=True after a reprocess merge
    arrival = _job(jobs_db, 100, age=20, eligible=True)
    reprocess = _job(jobs_db, 101, age=5, eligible=True, **REPROCESS)
    assert _order(jobs_db, lane='realtime') == [arrival, reprocess]
