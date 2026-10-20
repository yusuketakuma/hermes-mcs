"""Synthetic regressions for the ingest audit fixes (temp DB + stubs only)."""
import time
import uuid
from types import SimpleNamespace

import pytest

import job_ops
import ledger
import mcs_adapter
import mcs_requests
import run_check
from ingest_testkit import _ledger, _message, _unread_patient


def _strict_adapter(latest_id, marks):
    return SimpleNamespace(
        list_unread=lambda: mcs_adapter.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_adapter.MessageBatch(
            messages=[_message(mid=5, unread=True)], reached=True),
        fetch_unread_replies=lambda *_: mcs_adapter.ReplyBatch([], []),
        fetch_latest=lambda pid: {"message_id": latest_id,
                                  "is_self_only": False},
        mark_patient_read=lambda *a, **k: marks.append(a),
    )


@pytest.mark.parametrize("latest_id,gap", [(9, True), (5, False)])
def test_strict_ack_checks_post_ack_gap(tmp_path, latest_id, gap):
    """F1: a post landing between list_unread and the strict mark-read GET
    is cleared server-side; the gap check must run on the strict path."""
    db = _ledger(tmp_path)
    marks = []
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    run_check.stage_unread(_strict_adapter(latest_id, marks), db,
                           SimpleNamespace(mark_read=True), result,
                           time.monotonic() + 30, db.begin_run(None))
    assert marks == [(1, 123)] and result["marked_read"] == [1]
    assert ("mark 1: post_ack_gap" in result["errors"]) is gap
    assert (db.job_pending("history_head", 1) is not None) is gap
    db.close()


def test_commands_drain_in_submission_order(tmp_path, monkeypatch):
    """F2: ops.pause then ops.resume must apply in that order, not in
    random temp-file-name order."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    cmd_dir = tmp_path / "data" / "cmd"
    cmd_dir.mkdir(parents=True)
    sent = []
    for i in range(20):
        req = {"version": 1, "command_id": str(uuid.uuid4()), "actor": "t",
               "human_confirmed": True, "project_id": 1,
               "cmd": "ops.pause" if i % 2 == 0 else "ops.resume",
               "feature": "semantic"}
        mcs_requests.enqueue(req, str(cmd_dir))
        sent.append(req["command_id"])
    seen = []
    read = mcs_requests.read_command

    def spy(path):
        req = read(path)
        seen.append(req["command_id"])
        return req
    monkeypatch.setattr(job_ops.mcs_requests, "read_command", spy)
    job_ops.drain_commands(db, {"errors": []}, str(cmd_dir))
    assert seen == sent
    db.close()


@pytest.mark.parametrize("stage,margin", [
    (run_check.stage_self_probe, run_check.SELF_PROBE_MARGIN_S),
    (run_check.stage_backfill, 30)])
def test_deadline_skipped_patients_are_reached_next_tick(
        tmp_path, monkeypatch, stage, margin):
    """F3: a tick that runs out of time must not skip the same tail
    patients every tick. Every patient is covered and nothing is new, so
    an order keyed on coverage would stay fixed."""
    db = _ledger(tmp_path)
    for pid in (1, 2, 3, 4):
        db.ensure_patient(pid)
        db.save_messages([_message(mid=pid * 10, project_id=pid)])
        db.set_coverage(pid, db.high_watermark(pid))
    visited = []

    def latest(pid):
        visited.append(pid)
        return {"message_id": pid * 10, "is_self_only": False}

    def history(pid, since, **kw):
        visited.append(pid)
        raise mcs_adapter.MCSError("http_error", status=502)
    adapter = SimpleNamespace(fetch_latest=latest, fetch_history=history)
    # each visit costs 100s of a fake clock; the deadline allows two
    monkeypatch.setattr(run_check, "time", SimpleNamespace(
        monotonic=lambda: len(visited) * 100, time=time.time))
    ticks = []
    for run_id in (1, 2, 3):
        visited.clear()
        result = {"errors": [], "new_messages": 0, "backfilled": 0}
        stage(adapter, db, result, margin + 150, run_id)
        ticks.append(list(visited))
    assert all(len(t) == 2 for t in ticks)
    assert 3 not in ticks[0] and 3 in ticks[1]
    assert {p for t in ticks for p in t} == {1, 2, 3, 4}
    db.close()


def test_ledger_connection_uses_full_fsync(tmp_path):
    """F4: macOS fsync() does not flush the drive cache."""
    db = ledger.Ledger(str(tmp_path / "l.db"))
    assert db.db.execute("PRAGMA fullfsync").fetchone()[0] == 1
    assert db.db.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
    db.close()
