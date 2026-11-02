"""Synthetic regression: a discovery write failure names its exception type."""
import time

import job_ops
from ingest_testkit import _ledger, _unread_patient


class _Adapter:
    def list_projects(self):
        return [_unread_patient(95), _unread_patient(96)]


def test_discovery_write_failure_records_first_exception_type(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    job_ops.seed_discovery(db)
    failures = iter([TypeError("synthetic"), KeyError("synthetic")])
    visited = []

    def upsert(p, is_archived=False, *, notify_new=False, **kwargs):
        visited.append(p.project_id)
        raise next(failures)
    monkeypatch.setattr(db, "upsert_patient_info", upsert)
    result = {"errors": []}
    job_ops.run_discovery(_Adapter(), db, result, time.monotonic() + 600)
    assert visited == [95, 96]
    assert result["errors"] == ["discovery: patient_write_failed:TypeError"]
    assert db.job_pending("discovery", 0) is not None    # still deferred, not failed
    db.close()
