"""408/425/429 are retry-later for jobs and attachments alike; synthetic only."""
import time

import pytest

import mcs_adapter
from ingest_testkit import _ledger, _message
from ledger import job_reason_class


@pytest.mark.parametrize("code,expected", [
    ("http_404", "unavailable"), ("http_408", "transient"),
    ("http_425", "transient"), ("http_429", "transient"),
    ("http_503", "transient")])
def test_job_reason_class(code, expected):
    assert job_reason_class(code) == expected


@pytest.mark.parametrize("kind,state", [
    ("http_404", "failed"), ("http_408", "pending"),
    ("http_425", "pending"), ("http_429", "pending")])
def test_attachment_failure_class_matches_jobs(tmp_path, kind, state):
    db = _ledger(tmp_path)
    try:
        db.ensure_patient(1)
        m = _message(mid=5)
        m.attachments = [mcs_adapter.Attachment("f1", "a.pdf", "https://x/1")]
        db.save_messages([m])
        aid = db.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
        db.attachment_failed(aid, kind)
        row = db.db.execute("SELECT state,next_try FROM attachments").fetchone()
        assert row["state"] == state
        if state == "pending":
            assert row["next_try"] > time.time()
    finally:
        db.close()
