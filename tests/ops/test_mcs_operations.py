"""Synthetic contracts for the CCO-only durable operation commands."""

import json
import time
import uuid

from ledger import Ledger
import job_ops
import mcs_operations
import mcs_requests


def _command(cmd, project_id=1, **fields):
    return {
        "version": 1,
        "cmd": cmd,
        "command_id": str(uuid.uuid4()),
        "actor": "synthetic-cco",
        "human_confirmed": True,
        "project_id": project_id,
        **fields,
    }


def _job(db, job_id):
    return db.db.execute(
        "SELECT * FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()


def test_ops_retry_preserves_attempts_and_receipt_is_idempotent(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    payload = {"generation": "g1", "source_generation": "s1",
               "targets": [42]}
    job_id = db.job_add("semantic", 1, 42, payload=payload)
    db.db.execute(
        "UPDATE fetch_jobs SET state='failed',attempts=2,next_try=? "
        "WHERE job_id=?", (time.time() + 9999, job_id))
    db.db.commit()
    req = _command(
        "ops.retry", job_id=job_id,
        expected_payload_hash=mcs_requests.payload_hash(payload),
    )

    first = mcs_requests.apply_command(db, req)
    second = mcs_requests.apply_command(db, req)
    row = _job(db, job_id)
    assert first["outcome"] == second["outcome"] == "applied"
    assert first == second
    assert row["state"] == "pending" and row["attempts"] == 2
    assert row["next_try"] == 0
    assert json.loads(row["payload"])["retry_command_id"] == req["command_id"]
    db.close()


def test_ops_scan_deepens_existing_history_without_resetting_cursor(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    now = time.time()
    old_payload = {"since": int(now - 10 * 86400), "page": 4,
                   "pages": 2, "trickle": True}
    job_id = db.job_add("history", 1, payload=old_payload)
    req = _command("ops.scan", days=30, pages=5)

    receipt = mcs_requests.apply_command(db, req)
    row = _job(db, job_id)
    payload = json.loads(row["payload"])
    assert receipt["outcome"] == "applied"
    assert receipt["job_id"] == job_id
    assert row["state"] == "pending"
    assert payload["page"] == 4
    assert payload["pages"] == 5
    assert payload["since"] < old_payload["since"]
    assert payload["trickle"] is False
    db.close()


def test_pause_resume_records_control_and_invalidates_pending_tokens(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    payload = {"generation": "g1", "source_generation": "s1",
               "targets": [7]}
    job_id = db.job_add("semantic", 1, 7, payload=payload, next_try=9999)
    corrupt_id = db.job_add("semantic", 1, 8,
                            payload={"generation": "bad"}, next_try=9999)
    db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                  ("{corrupt", corrupt_id))
    db.db.execute(
        "UPDATE fetch_jobs SET attempts=3 WHERE job_id=?", (job_id,))
    db.db.commit()
    db.ensure_patient(1)
    corrupt_payload = _job(db, corrupt_id)["payload"]

    paused_receipt = mcs_requests.apply_command(
        db, _command("ops.pause", feature="semantic"))
    paused_row = _job(db, job_id)
    paused_payload = json.loads(paused_row["payload"])
    control = db.db.execute(
        "SELECT content,meta FROM artifacts WHERE kind='semantic_control' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    assert paused_receipt["outcome"] == "applied"
    assert paused_receipt["skipped_jobs"] == 1
    assert mcs_operations.paused(db, 1)
    assert control["content"] == "paused"
    assert json.loads(control["meta"])["actor"] == "synthetic-cco"
    assert paused_payload["control_generation"] == \
        paused_receipt["control_artifact_id"]
    assert paused_row["attempts"] == 3 and paused_row["next_try"] == 9999
    assert _job(db, corrupt_id)["payload"] == corrupt_payload

    resumed_receipt = mcs_requests.apply_command(
        db, _command("ops.resume", feature="semantic"))
    resumed_row = _job(db, job_id)
    resumed_payload = json.loads(resumed_row["payload"])
    assert resumed_receipt["outcome"] == "applied"
    assert resumed_receipt["skipped_jobs"] == 1
    assert not mcs_operations.paused(db, 1)
    assert resumed_payload["control_generation"] == \
        resumed_receipt["control_artifact_id"]
    assert resumed_payload["control_generation"] != \
        paused_payload["control_generation"]
    assert resumed_row["attempts"] == 3 and resumed_row["next_try"] == 0
    assert _job(db, corrupt_id)["payload"] == corrupt_payload
    assert db.db.execute(
        "SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic' "
        "AND project_id=1").fetchone()[0] == 2
    db.close()


def test_drain_commands_routes_ops_through_receipt_path(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    cmd_dir = tmp_path / "cmd"
    cmd_dir.mkdir()
    req = _command("ops.pause", feature="semantic")
    mcs_requests.enqueue(req, str(cmd_dir))

    result = {"errors": []}
    job_ops.drain_commands(db, result, str(cmd_dir))
    receipt = db.db.execute(
        "SELECT outcome FROM command_receipts WHERE command_id=?",
        (req["command_id"],)).fetchone()
    assert result["command_commands"] == 1
    assert result["ops_commands"] == 1
    assert receipt["outcome"] == "applied"
    assert not list(cmd_dir.glob("*.json"))
    db.close()
