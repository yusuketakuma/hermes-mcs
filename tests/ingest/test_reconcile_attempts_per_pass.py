"""Regression: the permanent reconcile rotation keeps its progress when
transient errors and run-deadline cuts are spread across completed passes.
Its attempt budget resets per pass and deadline cuts consume none, so the
row never fails and seed_reconcile never replaces page/passes/last_pass_at.
Fully synthetic."""

import json
import time

import job_ops
import mcs_adapter
from ingest_testkit import _ledger, _message


class _Adapter:
    def __init__(self, kind="network_error"):
        self.fail = False
        self.kind = kind

    def fetch_history(self, pid, since, max_pages=2, start_page=1):
        if self.fail:
            return mcs_adapter.MessageBatch(
                [], pages=0, reached=False,
                error=mcs_adapter.MCSError(self.kind, retryable=True))
        return mcs_adapter.MessageBatch(
            [_message(mid=900 + start_page, project_id=pid)],
            pages=1, reached=True)


def _due(db):
    db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='reconcile'")
    db.db.commit()


def test_reconcile_progress_survives_spread_transient_errors(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.set_history_floor(1, 0)          # floored (-1): reconcile is seeded
    job_ops.seed_reconcile(db)
    adapter = _Adapter()
    for _ in range(8):
        # a completed pass, then one transient blip before the next pass
        _due(db)
        adapter.fail = False
        job_ops.run_reconcile_jobs(adapter, db, {"errors": []},
                                   time.monotonic() + 100)
        _due(db)
        adapter.fail = True
        job_ops.run_reconcile_jobs(adapter, db, {"errors": []},
                                   time.monotonic() + 100)
    job_ops.seed_reconcile(db)          # every tick runs this (seed_discovery)
    row = db.db.execute(
        "SELECT state,attempts,payload FROM fetch_jobs "
        "WHERE kind='reconcile' AND project_id=1").fetchone()
    payload = json.loads(row["payload"])
    assert payload.get("passes", 0) >= 8, (row["state"], row["attempts"], payload)
    db.close()


def test_reconcile_deadline_cuts_between_passes_do_not_burn_the_job(tmp_path):
    # run-deadline cuts are "unfinished work, not a failure" for history
    # jobs; reconcile runs at the tail of _run_jobs and still burns them
    db = _ledger(tmp_path)
    db.ensure_patient(2)
    db.set_history_floor(2, 0)
    job_ops.seed_reconcile(db)
    adapter = _Adapter(kind="deadline_exceeded")
    for _ in range(8):
        _due(db)
        adapter.fail = False
        job_ops.run_reconcile_jobs(adapter, db, {"errors": []},
                                   time.monotonic() + 100)
        _due(db)
        adapter.fail = True
        job_ops.run_reconcile_jobs(adapter, db, {"errors": []},
                                   time.monotonic() + 100)
    row = db.db.execute(
        "SELECT state,attempts,payload FROM fetch_jobs "
        "WHERE kind='reconcile' AND project_id=2").fetchone()
    assert row["state"] == "pending", (row["state"], row["attempts"])
    db.close()
