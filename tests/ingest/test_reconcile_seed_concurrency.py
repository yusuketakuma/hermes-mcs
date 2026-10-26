"""Reconcile seeding releases its read snapshot before other writers commit."""
import sqlite3

import job_ops
from common_testkit import ledger_fixture

db = ledger_fixture(name="db")


def test_reconcile_seeding_accepts_an_interleaved_writer(db, monkeypatch):
    for pid in (1, 2):
        db.ensure_patient(pid)
        db.set_history_floor(pid, 0)
    path = db.db.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path, timeout=1)
    pending = db.job_pending
    committed = []

    def concurrent_pending(kind, project_id, *args, **kwargs):
        result = pending(kind, project_id, *args, **kwargs)
        if not committed:
            other.execute("UPDATE patients SET last_seen=0 WHERE project_id=2")
            other.commit()
            committed.append(True)
        return result

    monkeypatch.setattr(db, "job_pending", concurrent_pending)
    try:
        job_ops.seed_reconcile(db)
        assert committed == [True]
        assert all(db.job_pending("reconcile", pid) is not None for pid in (1, 2))
        before = db.db.execute("SELECT job_id,next_try FROM fetch_jobs ORDER BY job_id").fetchall()
        job_ops.seed_reconcile(db)
        after = db.db.execute("SELECT job_id,next_try FROM fetch_jobs ORDER BY job_id").fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]
    finally:
        other.close()
