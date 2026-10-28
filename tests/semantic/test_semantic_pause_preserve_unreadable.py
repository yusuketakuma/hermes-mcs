"""Synthetic pause/resume preservation cases for uncertain semantic jobs."""
import json
import uuid

import mcs_queries
import mcs_requests
import pytest
import semantic_drain as drain
import semantic_runtime as runtime

from semantic_testkit import _message, _patient, _seeded


def _control(db, action):
    return mcs_requests.apply_command(db, {
        "version": 1, "cmd": "ops." + action,
        "command_id": str(uuid.uuid4()), "actor": "synthetic-human",
        "human_confirmed": True, "project_id": 1, "feature": "semantic",
    })


def _job(db, project_id, root):
    return db.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='semantic' AND project_id=? AND message_id=?",
        (project_id, root)).fetchone()


def _token(db, project_id, root):
    row = db.db.execute(
        "SELECT fetch_jobs.*, " + runtime.CONTROL_EPOCH_SQL + " AS patient_control_epoch "
        "FROM fetch_jobs WHERE kind='semantic' AND project_id=? AND message_id=?",
        (project_id, root)).fetchone()
    return runtime.JobToken.from_row(row)


def test_control_epoch_invalidates_pre_pause_token_even_after_resume(tmp_path):
    db = _seeded(tmp_path)
    try:
        token = _token(db, 1, 1)
        before = dict(_job(db, 1, 1))
        db.artifact_add("semantic_control", "paused", project_id=1)
        db.artifact_add("semantic_control", "running", project_id=1)
        assert dict(_job(db, 1, 1)) == before
        assert not runtime.job_matches(db, token)
        assert not runtime.transition(db, token, "done")
        fresh = _token(db, 1, 1)
        assert runtime.job_matches(db, fresh)
        assert runtime.transition(db, fresh, "done")
    finally:
        db.close()


def test_control_epoch_is_scoped_to_patient(tmp_path):
    db = _seeded(tmp_path)
    try:
        _patient(db, 2)
        token = _token(db, 1, 1)
        db.artifact_add("semantic_control", "paused", project_id=2)
        assert runtime.job_matches(db, token)
    finally:
        db.close()


def test_pause_between_guard_and_reservation_does_not_create_usage(tmp_path):
    db = _seeded(tmp_path)
    try:
        token = _token(db, 1, 1)
        reserve = runtime.usage_reserver(db, token, kind="semantic_usage",
                                         model="synthetic", project_id=1, message_id=1)
        db.artifact_add("semantic_control", "paused", project_id=1)
        db.artifact_add("semantic_control", "running", project_id=1)
        with pytest.raises(runtime.RuntimeStale, match="reservation"):
            reserve({"synthetic": True}, 1.0)
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize("lane", [None, "realtime", "backlog"])
def test_scheduled_token_captures_control_epoch_with_job_snapshot(tmp_path, lane):
    db = _seeded(tmp_path)
    try:
        db.artifact_add("semantic_control", "running", project_id=1)
        selected = drain._select_due(db, lane, ("semantic",), False, 1)[0]
        assert len(selected) == 1
        token = runtime.JobToken.from_row(selected[0])
        assert token.control_epoch > 0 and runtime.job_matches(db, token)
        db.artifact_add("semantic_control", "paused", project_id=1)
        db.artifact_add("semantic_control", "running", project_id=1)
        assert not runtime.job_matches(db, token)
        assert not runtime.transition(db, token, "done")
    finally:
        db.close()


def test_large_patient_pause_is_effective_without_partial_job_rewrite(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.executemany(
                "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,attempts,next_try) "
                "VALUES('semantic',1,?,?,'pending',0,123456)",
                [(mid, json.dumps({"inference_engine_version": "1.0", "targets": [mid]}))
                 for mid in range(3, 131)])
        token = _token(db, 1, 1)
        before = [tuple(row) for row in db.db.execute(
            "SELECT job_id,payload,state,attempts,next_try,updated_at FROM fetch_jobs "
            "WHERE kind='semantic' AND project_id=1 ORDER BY job_id")]
        result = _control(db, "pause")
        assert result["outcome"] == "applied" and result["jobs_unscanned"] is True
        assert result["updated_jobs"] == 0 and result["skipped_jobs"] is None
        assert mcs_queries.paused(db, 1)
        assert not runtime.job_matches(db, token)
        assert not runtime.transition(db, token, "done")
        assert [tuple(row) for row in db.db.execute(
            "SELECT job_id,payload,state,attempts,next_try,updated_at FROM fetch_jobs "
            "WHERE kind='semantic' AND project_id=1 ORDER BY job_id")] == before
        resumed = _control(db, "resume")
        assert resumed["outcome"] == "rejected"
        assert resumed["error"] == "semantic_control_job_limit"
        assert mcs_queries.paused(db, 1)
        assert [tuple(row) for row in db.db.execute(
            "SELECT job_id,payload,state,attempts,next_try,updated_at FROM fetch_jobs "
            "WHERE kind='semantic' AND project_id=1 ORDER BY job_id")] == before
    finally:
        db.close()


def test_pause_budget_expiry_rolls_back_partial_updates_and_still_pauses(tmp_path, monkeypatch):
    import mcs_operations

    db = _seeded(tmp_path)
    try:
        db.save_messages([_message(3)], project_id=1, semantic=True)
        before = [dict(row) for row in db.db.execute(
            "SELECT * FROM fetch_jobs WHERE kind='semantic' ORDER BY job_id")]
        times = iter([0.0, 0.0, 0.0, 0.0, 3.0])
        monkeypatch.setattr(mcs_operations.time, "monotonic", lambda: next(times, 3.0))
        result = _control(db, "pause")
        assert result["outcome"] == "applied" and result["jobs_unscanned"] is True
        assert mcs_queries.paused(db, 1)
        assert [dict(row) for row in db.db.execute(
            "SELECT * FROM fetch_jobs WHERE kind='semantic' ORDER BY job_id")] == before
    finally:
        db.close()


@pytest.mark.parametrize("raw", [
    '{"inference_engine_version":"2.0","targets":[1],"future":"keep"}',
    '{"inference_engine_version":"1.0","inference_engine_\\u0076ersion":"2.0","targets":[1]}',
    '{"inference_engine_version":"2.0","inference_engine_\\u0076ersion":"1.0","targets":[1]}',
    '{"targets":',
    '[]',
    '{"targets":[1],"extra":"' + 'x' * (1024 * 1024) + '"}',
])
def test_control_preserves_uncertain_jobs_and_updates_other_projects_correctly(tmp_path, raw):
    db = _seeded(tmp_path)
    try:
        db.save_messages([_message(3)], project_id=1, semantic=True)
        _patient(db, 2)
        db.save_messages([_message(4, pid=2)], project_id=2, semantic=True)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4,next_try=123456 "
                          "WHERE kind='semantic' AND project_id=1 AND message_id=1", (raw,))
        before = dict(_job(db, 1, 1))
        other = dict(_job(db, 2, 4))
        good = dict(_job(db, 1, 3))
        for action in ("pause", "resume"):
            receipt = _control(db, action)
            assert receipt["outcome"] == "applied"
            assert mcs_queries.paused(db, 1) is (action == "pause")
            assert dict(_job(db, 1, 1)) == before
            assert dict(_job(db, 2, 4)) == other
            current = dict(_job(db, 1, 3))
            assert current["attempts"] == good["attempts"]
            assert json.loads(current["payload"])["control_generation"] == receipt["control_artifact_id"]
            if action == "resume":
                assert current["next_try"] == 0
    finally:
        db.close()
