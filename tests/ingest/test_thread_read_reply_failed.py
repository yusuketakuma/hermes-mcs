"""A thread whose missing reply job burnt out (state=failed) is reported
once per thread and never treated as settled: no read acknowledgement,
no confirm, the job is not revived. Synthetic stubs only — no real MCS."""
import time
from types import SimpleNamespace

import run_check
from ingest_testkit import _ledger
from test_thread_read import _store


def _adapter(server):
    calls = []
    return SimpleNamespace(
        calls=calls,
        thread_unread=lambda pid, parent: calls.append("check") or True,
        fetch_thread=lambda pid, parent: [SimpleNamespace(message_id=i) for i in server],
        read_thread=lambda pid, parent: calls.append("clear") or set(server))


def _fail(db, rid, parent=10):
    db.job_add("reply", 1, rid, parent_id=parent)
    db.job_fail(db.job_pending("reply", 1, rid)["job_id"])


def _tick(db, adapter):
    result = {"errors": []}
    run_check.stage_thread_read(adapter, db, result, time.monotonic() + 120)
    return result


def test_burnt_out_missing_reply_is_reported_once_and_never_settled(tmp_path):
    db = _ledger(tmp_path)
    _store(db, 10)
    _store(db, 11, parent=10)
    _fail(db, 12)
    _fail(db, 13)
    adapter = _adapter(server=[11, 12, 13, 14])
    result = _tick(db, adapter)
    assert result["errors"] == ["thread_read: reply_failed"]     # once per thread
    assert adapter.calls == ["check"]                            # never cleared
    assert result["threads_marked_read"] == []
    assert db.job_state("reply", 1, 12) == "failed"              # not revived
    assert db.job_state("reply", 1, 14) == "pending"             # new gap still queued
    assert db.db.execute("SELECT count(*) FROM thread_read_marks").fetchone()[0] == 0
    # stays a candidate and is reported again next tick
    assert _tick(db, _adapter(server=[11, 12, 13, 14]))["errors"] == [
        "thread_read: reply_failed"]


def test_pending_missing_reply_is_not_reported(tmp_path):
    db = _ledger(tmp_path)
    _store(db, 10)
    _store(db, 11, parent=10)
    assert _tick(db, _adapter(server=[11, 12]))["errors"] == []
    assert db.job_state("reply", 1, 12) == "pending"
