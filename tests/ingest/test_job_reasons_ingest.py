"""1.0.13 #3: reconcile and history-deepen failures keep stable reason codes."""
import json
import time

import job_ops
import mcs_adapter
from ingest_testkit import _ledger, _message


def _row(db, kind, pid=1):
    return db.db.execute(
        "SELECT state,attempts,reason_code,payload FROM fetch_jobs"
        " WHERE kind=? AND project_id=?", (kind, pid)).fetchone()


def test_reconcile_failures_record_code_without_losing_progress(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.ensure_patient(1)
        db.job_add("reconcile", 1, payload={"page": 4, "passes": 2,
                                            "last_pass_at": 123.0})
        err = mcs_adapter.MCSError("http", status=404)

        class Raises:
            def fetch_history(self, pid, since, **kw):
                raise err

        job_ops.run_reconcile_jobs(Raises(), db, {"errors": []},
                                   time.monotonic() + 100)
        row = _row(db, "reconcile")
        # code recorded, but a 4xx does not permanently fail the rotation
        assert (row["state"], row["attempts"], row["reason_code"]) == \
            ("pending", 1, "http_404")
        assert json.loads(row["payload"])["passes"] == 2

        class Partial:
            def fetch_history(self, pid, since, **kw):
                return mcs_adapter.MessageBatch(
                    [_message(mid=50)], pages=1,
                    error=mcs_adapter.MCSError("schema_error"))

        db.db.execute("UPDATE fetch_jobs SET next_try=0")
        job_ops.run_reconcile_jobs(Partial(), db, {"errors": []},
                                   time.monotonic() + 100)
        row = _row(db, "reconcile")
        assert (row["state"], row["attempts"], row["reason_code"]) == \
            ("pending", 2, "schema_error")
        pl = json.loads(row["payload"])
        assert (pl["page"], pl["passes"], pl["last_pass_at"]) == (5, 2, 123.0)
    finally:
        db.close()


def test_reconcile_invalid_payload_records_code(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.ensure_patient(1)
        db.job_add("reconcile", 1, payload={"page": 0})
        job_ops.run_reconcile_jobs(object(), db, {"errors": []},
                                   time.monotonic() + 100)
        row = _row(db, "reconcile")
        assert (row["state"], row["reason_code"]) == ("failed", "invalid_payload")
    finally:
        db.close()


def test_import_deepen_fails_corrupt_job_with_invalid_payload(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    fails = []
    real_fail = db.job_fail
    # the import immediately revives the same row, so observe the call
    monkeypatch.setattr(db, "job_fail", lambda job_id, **kw: (
        fails.append(kw), real_fail(job_id, **kw)))
    try:
        db.ensure_patient(1)
        db.job_add("history", 1, payload={"since": 0, "page": 7})
        db.db.execute("UPDATE fetch_jobs SET payload='[]' WHERE project_id=1")
        cmd_dir = tmp_path / "cmd"
        cmd_dir.mkdir()
        (cmd_dir / "synthetic.json").write_text(
            json.dumps({"cmd": "import", "project_id": 1}))
        job_ops.drain_commands(db, {"errors": []}, str(cmd_dir))
        assert fails == [{"reason_code": "invalid_payload"}]
    finally:
        db.close()
