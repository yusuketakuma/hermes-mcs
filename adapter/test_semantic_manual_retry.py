"""Human retry extensions stay bounded and tied to one semantic input."""
import json
import time
import uuid

import ledger
import mcs_requests
import semantic
import semantic_jev as jev
import semantic_runtime as runtime
from test_mcs_semantic import _cfg, _FakeJev, _llm, _message, _seeded


def _command(**fields):
    return {
        "version": 1,
        "cmd": "ops.retry",
        "command_id": str(uuid.uuid4()),
        "actor": "synthetic-reviewer",
        "human_confirmed": True,
        "project_id": 1,
        **fields,
    }


def _semantic_row(db):
    return db.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()


def test_manual_retry_extension_is_bounded_across_seed_and_restart(tmp_path):
    db = _seeded(tmp_path)
    try:
        original = _semantic_row(db)
        original_payload = json.loads(original["payload"])
        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET state='pending',attempts=6,next_try=0 "
                "WHERE job_id=?", (original["job_id"],))
        old_token = runtime.JobToken.from_row(_semantic_row(db))
        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET state='failed',next_try=0 "
                "WHERE job_id=?", (original["job_id"],))

        request = _command(
            job_id=original["job_id"],
            expected_payload_hash=mcs_requests.payload_hash(original_payload),
            additional_attempts=2,
            reason="人が確認して追加試行を承認",
        )
        receipt = mcs_requests.apply_command(db, request)
        assert receipt["outcome"] == "applied"
        assert receipt["old_attempt_limit"] == 6
        assert receipt["new_attempt_limit"] == 8
        assert receipt["additional_attempts"] == 2
        assert receipt["reason"] == request["reason"]
        row = _semantic_row(db)
        payload = json.loads(row["payload"])
        assert row["state"] == "pending" and row["attempts"] == 6
        assert payload["manual_attempt_limit"] == 8
        assert payload["retry_command_id"] == request["command_id"]
        assert payload["source_generation"] == original_payload["source_generation"]
        assert not runtime.transition(db, old_token, "retry")

        # A retryable fake failure consumes exactly the two explicitly
        # granted durable job attempts.  One job may make several Jev
        # questions, so the assertion belongs to the durable attempt count.
        failing = _FakeJev(error=jev.JevError("transport", retryable=True))
        for _ in range(2):
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET next_try=0 WHERE job_id=?",
                    (row["job_id"],))
            semantic.run_due(db, _cfg("shadow"), {"errors": []},
                             time.monotonic() + 300,
                             jev_client=failing, llm_fn=_llm)
            row = _semantic_row(db)
        assert row["state"] == "failed" and row["attempts"] == 8

        # Even if a stale queue row says pending, the entrance CAS closes it
        # without invoking the fake client.
        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET state='pending',next_try=0 WHERE job_id=?",
                (row["job_id"],))
        before_calls = len(failing.calls)
        semantic.run_due(db, _cfg("shadow"), {"errors": []},
                         time.monotonic() + 300,
                         jev_client=failing, llm_fn=_llm)
        row = _semantic_row(db)
        assert len(failing.calls) == before_calls and row["state"] == "failed"

        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET state='pending',attempts=-1,next_try=0 "
                "WHERE job_id=?", (row["job_id"],))
        before_calls = len(failing.calls)
        semantic.run_due(db, _cfg("shadow"), {"errors": []},
                         time.monotonic() + 300,
                         jev_client=failing, llm_fn=_llm)
        row = _semantic_row(db)
        assert len(failing.calls) == before_calls
        assert row["state"] == "failed" and row["attempts"] == -1
        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET state='failed',attempts=8,next_try=0 "
                "WHERE job_id=?", (row["job_id"],))

        # Re-seeding the same source does not reopen the exhausted budget,
        # including after reopening the SQLite file.
        db.close()
        db = ledger.Ledger(str(tmp_path / "ledger.db"))
        db.semantic_seed(1, [1], {"source": "replay"})
        row = _semantic_row(db)
        assert row["state"] == "failed" and row["attempts"] == 8
        assert json.loads(row["payload"])["manual_attempt_limit"] == 8

        # A source edit starts a new generation and drops human-only retry
        # fields rather than carrying the extra budget into it.
        db.save_messages([_message(1, body="編集された新しい本文")],
                         project_id=1, semantic=True)
        row = _semantic_row(db)
        payload = json.loads(row["payload"])
        assert row["state"] == "pending" and row["attempts"] == 0
        assert "manual_attempt_limit" not in payload
        assert "retry_command_id" not in payload
    finally:
        db.close()


def test_manual_retry_rejects_invalid_or_sql_overflow_limits(tmp_path):
    db = _seeded(tmp_path)
    try:
        row = _semantic_row(db)
        payload = json.loads(row["payload"])
        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET state='failed',attempts=? WHERE job_id=?",
                (runtime.MAX_SQL_INTEGER, row["job_id"]))
        digest = mcs_requests.payload_hash(payload)
        overflow = mcs_requests.apply_command(db, _command(
            job_id=row["job_id"], expected_payload_hash=digest,
            additional_attempts=1, reason="追加枠"))
        assert overflow["outcome"] == "rejected"
        assert overflow["error"] == "attempt_limit_overflow"

        invalid = _command(
            job_id=row["job_id"], expected_payload_hash=digest,
            additional_attempts=4, reason="追加枠")
        assert mcs_requests.validate(invalid) == "bad_additional_attempts"
        assert runtime.attempt_limit({"manual_attempt_limit": 5}) == 6
        assert runtime.attempt_limit({
            "manual_attempt_limit": runtime.MAX_SQL_INTEGER + 1}) == 6
    finally:
        db.close()
