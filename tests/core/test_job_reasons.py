"""Acquisition reasons persist without claiming a missing clinical action."""
import json
import sqlite3

import pytest

import job_ops
from ingest_testkit import _ledger, _message
from ledger import Ledger, SCHEMA_VERSION, job_reason_class, job_reason_code
from mcs_adapter import MCSError, MessageBatch, SchemaError
from views_testkit import _view


@pytest.fixture
def db(tmp_path):
    store = _ledger(tmp_path)
    store.ensure_patient(1)
    store.save_messages([
        _message(mid=20),
        _message(mid=21, parent_id=20, state="snippet"),
    ])
    store.job_add("reply", 1, 21, parent_id=20)
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize(("error", "reason", "classification", "state"), [
    (MCSError("http", "synthetic private detail", 404), "http_404", "unavailable", "failed"),
    (MCSError("http", "synthetic private detail", 429), "http_429", "transient", "pending"),
    (MCSError("http", "synthetic private detail", 408), "http_408", "transient", "pending"),
    (MCSError("http", "synthetic private detail", 503), "http_503", "transient", "pending"),
    (SchemaError("synthetic private detail"), "schema_error", "structural", "failed"),
    (MCSError("unexpected-provider-label", "synthetic private detail"),
     "fetch_error", "transient", "pending"),
])
def test_reply_failure_persists_only_a_reason_code(db, monkeypatch, error, reason,
                                                   classification, state):
    class Adapter:
        def fetch_thread(self, pid, parent_id):
            raise error

    monkeypatch.setattr(job_ops.time, "monotonic", lambda: 1000.0)
    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, 1100.0)
    job = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='reply'").fetchone()
    assert job["attempts"] == 1
    assert job["state"] == state
    assert job["reason_code"] == reason
    assert job_reason_class(job["reason_code"]) == classification
    assert "synthetic private detail" not in json.dumps(dict(job))


def test_retry_reason_survives_reopen_and_clears_on_progress_success_and_revival(db):
    job_id = db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='reply'").fetchone()[0]
    db.job_retry(job_id, max_attempts=1, reason_code="http_404")
    reopened = Ledger(db.db.execute("PRAGMA database_list").fetchone()[2])
    try:
        job = reopened.db.execute("SELECT * FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert job["state"] == "failed"
        assert job["reason_code"] == "http_404"
        reopened.job_add("reply", 1, 21, parent_id=20)
        assert reopened.job_pending("reply", 1, 21)["reason_code"] is None
        reopened.job_retry(job_id, reason_code="http_503")
        reopened.job_defer(job_id, 0, payload={"page": 2})
        assert reopened.job_pending("reply", 1, 21)["reason_code"] is None
        reopened.job_fail(job_id, reason_code="window_stalled")
        reopened.job_done(job_id)
        job = reopened.db.execute("SELECT * FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert job["state"] == "done"
        assert job["reason_code"] is None
    finally:
        reopened.close()


def test_permanent_history_failure_fails_once_and_transient_uses_the_limit(db):
    class Adapter:
        error = MCSError("http", "synthetic private detail", 403)

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            raise self.error

    db.job_add("history", 1, 0, payload={"since": 0})
    job_ops.run_history_jobs(Adapter(), db, {"errors": []}, 1e18)
    job = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='history'").fetchone()
    assert (job["state"], job["attempts"], job["reason_code"]) == ("failed", 1, "http_403")

    reply_id = db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='reply'").fetchone()[0]
    for _ in range(7):
        db.job_retry(reply_id, 0, reason_code="network_error")
    assert db.job_state("reply", 1, 21) == "pending"
    db.job_retry(reply_id, 0, reason_code="network_error")
    assert db.job_state("reply", 1, 21) == "failed"


def test_completed_reply_clears_its_reason_in_the_archive_transaction(db):
    job_id = db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='reply'").fetchone()[0]
    db.job_retry(job_id, reason_code="http_503")
    db.save_thread_replies([_message(mid=21, parent_id=20)], 1)
    job = db.db.execute("SELECT state,reason_code FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()
    assert tuple(job) == ("done", None)


def test_bad_reason_is_rejected_before_retry_mutation(db):
    job_id = db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='reply'").fetchone()[0]
    before = dict(db.db.execute("SELECT * FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone())
    with pytest.raises(ValueError, match="invalid_job_reason"):
        db.job_retry(job_id, payload={"page": 99}, reason_code="synthetic private text")
    after = dict(db.db.execute("SELECT * FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone())
    assert before == after
    assert job_reason_code(True) is None
    assert job_reason_class("synthetic private text") == "unknown"


def test_schema8_job_shape_upgrades_without_fabricating_reasons(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as old:
        old.executescript("""
            CREATE TABLE fetch_jobs(
              job_id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,project_id INTEGER,
              message_id INTEGER DEFAULT 0,parent_id INTEGER,payload TEXT,
              state TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_try REAL,
              created_at REAL,updated_at REAL,UNIQUE(kind,project_id,message_id));
            INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,payload,state,
              attempts,next_try,created_at,updated_at)
              VALUES('reply',1,21,20,'{"page":2}','failed',3,100,50,90);
            PRAGMA user_version=8;
        """)
    upgraded = Ledger(str(path))
    try:
        job = upgraded.db.execute("SELECT * FROM fetch_jobs").fetchone()
        assert job["state"] == "failed" and job["attempts"] == 3
        assert job["payload"] == '{"page":2}'
        assert job["next_try"] == 100 and job["updated_at"] == 90
        assert job["reason_code"] is None
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        upgraded.close()


def test_status_reports_verified_failure_class_but_not_clinical_absence(db, tmp_path):
    job_id = db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='reply'").fetchone()[0]
    db.job_fail(job_id, reason_code="http_404")
    view = _view(db, tmp_path)
    try:
        row = view.read("status")["items"][0]
    finally:
        view.close()
    failed = next(job for job in row["jobs"] if job["kind"] == "reply")
    assert failed["reason_codes"] == {"http_404": 1}
    assert failed["reason_classes"] == {"unavailable": 1}
    assert row["known_gaps"] == [{
        "kind": "reply", "reason": "http_404", "reason_class": "unavailable",
        "count": 1, "status": "verification_failed"}]


@pytest.mark.parametrize(("floor", "record"), [
    (1_700_000_000, "cutoff_recorded"), (0, "natural_end_recorded")])
def test_permanent_gap_keeps_floor_and_reports_known_gaps(db, tmp_path, floor, record):
    # #3-D1: a permanent reply gap must not block the floor, yet status must
    # still list it so coverage reads "unknown", never "gapless".
    db.set_history_floor(1, floor)
    job_id = db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='reply'").fetchone()[0]
    db.job_retry(job_id, max_attempts=1, reason_code="http_404")
    view = _view(db, tmp_path)
    try:
        row = view.read("status")["items"][0]
    finally:
        view.close()
    assert row["history_floor"] == (floor or -1)
    assert row["history_record"] == record
    assert row["gapless_verified"] is False
    assert [(g["kind"], g["reason"], g["count"]) for g in row["known_gaps"]] == [
        ("reply", "http_404", 1)]


def test_status_reads_legacy_job_columns_as_unknown(db, tmp_path):
    db.db.execute("ALTER TABLE fetch_jobs DROP COLUMN reason_code")
    db.db.execute("PRAGMA user_version=8")
    db.db.commit()
    view = _view(db, tmp_path)
    try:
        row = view.read("status")["items"][0]
    finally:
        view.close()
    job = next(job for job in row["jobs"] if job["kind"] == "reply")
    assert job["reason"] == "not_recorded"
    assert job["reason_classes"] == {"unknown": 1}
    assert row["known_gaps"] == []


def _job(db, kind):
    return db.db.execute("SELECT * FROM fetch_jobs WHERE kind=?", (kind,)).fetchone()


@pytest.mark.parametrize(("error", "reason", "state"), [
    (MCSError("http", "synthetic private detail", 503), "http_503", "pending"),
    (SchemaError("synthetic private detail"), "schema_error", "failed"),
])
def test_thread_window_error_records_reason_and_consumes_one_attempt(
        db, error, reason, state):
    class Adapter:
        def fetch_thread_window(self, pid, parent_id, start_page=1):
            return MessageBatch([], pages=0, reached=False, error=error)

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, 1e18)
    job = _job(db, "reply")
    assert (job["state"], job["attempts"], job["reason_code"]) == (state, 1, reason)
    assert json.loads(job["payload"])["page"] == 1
    assert _job(db, "thread")["reason_code"] is None
    assert "synthetic private detail" not in json.dumps(dict(job)) + str(result)


def test_full_thread_pass_without_target_records_replies_missing(db):
    db.db.execute("UPDATE messages SET reply_count=2 WHERE message_id=20")
    db.job_add("thread", 1, 20, parent_id=20)

    class Adapter:
        def fetch_thread_window(self, pid, parent_id, start_page=1):
            return MessageBatch([], pages=1, reached=True)

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, 1e18)
    for kind in ("reply", "thread"):
        job = _job(db, kind)
        assert (job["state"], job["attempts"], job["reason_code"]) == (
            "pending", 1, "replies_missing")
        assert json.loads(job["payload"])["page"] == 1
    assert "thread 20: replies_missing" in result["errors"]


@pytest.mark.parametrize(("error", "reason", "state"), [
    (MCSError("http", "synthetic private detail", 503), "http_503", "pending"),
    (SchemaError("synthetic private detail"), "schema_error", "failed"),
])
def test_aborted_history_walk_records_reason(db, error, reason, state):
    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return MessageBatch([], pages=0, error=error)

    db.job_add("history", 1, 0, payload={"since": 0})
    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result, 1e18)
    job = _job(db, "history")
    assert (job["state"], job["attempts"], job["reason_code"]) == (state, 1, reason)
    assert "synthetic private detail" not in json.dumps(dict(job)) + str(result)


@pytest.mark.parametrize(("batch", "stalls", "reason", "state"), [
    (MessageBatch([], pages=0), job_ops.HISTORY_STALL_LIMIT - 1,
     "window_stalled", "failed"),
    (MessageBatch([], pages=1, reached=True), 0, "waiting_replies", "pending"),
])
def test_history_stall_and_reply_wait_record_reason_without_attempt(
        db, batch, stalls, reason, state):
    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return batch

    db.job_add("history", 1, 0, payload={"since": 0, "stalls": stalls})
    job_ops.run_history_jobs(Adapter(), db, {"errors": []}, 1e18)
    job = _job(db, "history")
    assert (job["state"], job["attempts"], job["reason_code"]) == (state, 0, reason)
