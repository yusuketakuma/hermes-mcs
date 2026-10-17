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


def _ops(cmd):
    return {"version": 1, "command_id": str(uuid.uuid4()), "actor": "t", "human_confirmed": True,
            "project_id": 1, "cmd": cmd, "feature": "semantic"}


def _drained(db, cmd_dir, monkeypatch):
    seen = []
    read = mcs_requests.read_command
    monkeypatch.setattr(job_ops.mcs_requests, "read_command",
                        lambda path: seen.append(read(path)["cmd"]) or read(path))
    job_ops.drain_commands(db, {"errors": []}, str(cmd_dir))
    return seen


def test_drain_never_overtakes_a_stalled_writer(tmp_path, monkeypatch):
    """F2 race: a pause that took the queue first but stalls in fsync is applied before
    a resume submitted meanwhile; a drain during the stall applies nothing at all."""
    import threading
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    cmd_dir = tmp_path / "data" / "cmd"
    cmd_dir.mkdir(parents=True)
    stalled, resume = threading.Event(), threading.Event()
    real_fsync = mcs_requests.os.fsync

    def fsync(fd):
        if not stalled.is_set():  # the first writer stops mid-publication
            stalled.set()
            resume.wait(10)
        real_fsync(fd)
    monkeypatch.setattr(mcs_requests.os, "fsync", fsync)
    first = threading.Thread(target=mcs_requests.enqueue, args=(_ops("ops.pause"), str(cmd_dir)))
    first.start()
    assert stalled.wait(10)
    second = threading.Thread(target=mcs_requests.enqueue, args=(_ops("ops.resume"), str(cmd_dir)))
    second.start()
    deadline = time.monotonic() + 1  # give the later writer every chance to publish first
    while time.monotonic() < deadline and not list(cmd_dir.glob("*.json")):
        time.sleep(0.01)
    # however long the stall lasts (no time limit may declare the writer gone)
    later = time.time() + 10 ** 6
    monkeypatch.setattr(job_ops.time, "time", lambda: later)
    assert _drained(db, cmd_dir, monkeypatch) == []  # the queue is held by the stalled writer
    monkeypatch.undo()
    resume.set()
    first.join(10)
    second.join(10)
    assert _drained(db, cmd_dir, monkeypatch) == ["ops.pause", "ops.resume"]
    db.close()


def test_queue_order_survives_a_clock_step_back(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    cmd_dir = tmp_path / "data" / "cmd"
    cmd_dir.mkdir(parents=True)
    clock = iter([3_000, 2_000, 1_000])
    monkeypatch.setattr(mcs_requests.time, "time_ns", lambda: next(clock))
    for cmd in ("ops.pause", "ops.pause", "ops.resume"):
        mcs_requests.enqueue(_ops(cmd), str(cmd_dir))
    assert _drained(db, cmd_dir, monkeypatch) == ["ops.pause", "ops.pause", "ops.resume"]
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


@pytest.mark.parametrize("stage,margin", [
    (run_check.stage_self_probe, run_check.SELF_PROBE_MARGIN_S),
    (run_check.stage_backfill, 30)])
@pytest.mark.parametrize("patients,run_ids", [(2, (1, 3, 5)), (3, (1, 4, 7))])
def test_walk_resumes_where_the_deadline_stopped_whatever_the_run_ids(
        tmp_path, monkeypatch, stage, margin, patients, run_ids):
    """CR: run ids are shared with deep and other runs, so a rotation keyed
    on them restarted at the same patient every tick. Each stage keeps its
    own cursor: one visit per tick still reaches every patient in turn."""
    db = _ledger(tmp_path)
    for pid in range(1, patients + 1):
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
    monkeypatch.setattr(run_check, "time", SimpleNamespace(
        monotonic=lambda: len(visited) * 100, time=time.time))
    ticks = []
    for run_id in run_ids:
        visited.clear()
        stage(adapter, db, {"errors": [], "new_messages": 0, "backfilled": 0}, margin + 50, run_id)
        ticks.append(list(visited))
    assert ticks == [[i % patients + 1] for i in range(len(run_ids))]   # in turn, wrapping
    db.close()
