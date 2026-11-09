"""Snapshot verification gate: the run lock is released only while the
fenced child validates, then reacquired and identity-checked before
publication proceeds. Any verification failure forbids terminal writes."""
import fcntl
import os
import time

import pytest

import maintenance
import notify_cards
import notify_cmds
import notify_spool
import run_check
import snapshots
import writer_lock
from extract_testkit import _ledger, _message


def _env(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(1, body="合成の経過記録")])
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    updater = {"state": (False, 7)}
    monkeypatch.setattr(writer_lock, "updater_state", lambda: updater["state"])
    lock_path = str(tmp_path / "run.lock")
    monkeypatch.setattr(run_check, "LOCKFILE", lock_path)
    lock_fd = run_check.acquire_run_lock(lock_path)
    assert lock_fd is not None
    return db, updater, lock_fd


def _publish(db, tmp_path, lock_fd, state):
    snapshots._reset_status()
    with snapshots._publication_lock(maintenance.SNAPSHOT_DIR) as fence:
        assert fence is not None
        return snapshots._publish_snapshot_locked(
            str(tmp_path / "ledger.db"), maintenance.SNAPSHOT_DIR, fence,
            verification_context=run_check._verification_context_factory(
                db, lock_fd, state))


def _probe(tmp_path):
    return os.open(str(tmp_path / "run.lock"), os.O_RDONLY)


def test_gate_releases_run_lock_only_during_child_validation(tmp_path, monkeypatch):
    db, updater, lock_fd = _env(tmp_path, monkeypatch)
    state = {"allowed": False}
    probe = _probe(tmp_path)
    held = {}
    original = snapshots._valid_snapshot_candidate

    def validating(*args, **kwargs):
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held["during"] = True
        else:
            held["during"] = False
            fcntl.flock(probe, fcntl.LOCK_UN)
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    assert _publish(db, tmp_path, lock_fd, state) is not None
    assert held["during"] is False                       # run.lock was free in the window
    assert state["allowed"] is True
    with pytest.raises(BlockingIOError):                 # and held again afterwards
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(probe)
    os.close(lock_fd)
    db.close()


def test_gate_updater_during_window_forbids_terminal_writes(tmp_path, monkeypatch):
    db, updater, lock_fd = _env(tmp_path, monkeypatch)
    state = {"allowed": False}
    original = snapshots._valid_snapshot_candidate

    def validating(*args, **kwargs):
        updater["state"] = (True, 7)                     # update marker appeared
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(writer_lock.RunLockLost) as caught:
        _publish(db, tmp_path, lock_fd, state)
    assert caught.value.held is True
    assert state["allowed"] is False
    os.close(lock_fd)
    db.close()


def test_gate_db_swap_during_window_fails_no_write(tmp_path, monkeypatch):
    db, updater, lock_fd = _env(tmp_path, monkeypatch)
    state = {"allowed": False}
    original = snapshots._valid_snapshot_candidate
    db_path = str(tmp_path / "ledger.db")

    def validating(*args, **kwargs):
        os.replace(db_path, db_path + ".stale")          # restore-style inode swap
        open(db_path, "wb").close()
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(run_check._SnapshotGateUnverified):
        _publish(db, tmp_path, lock_fd, state)
    assert state["allowed"] is False
    os.close(lock_fd)
    db.close()


def test_gate_broken_publication_fence_fails_no_write(tmp_path, monkeypatch):
    db, updater, lock_fd = _env(tmp_path, monkeypatch)
    state = {"allowed": False}
    original = snapshots._valid_snapshot_candidate
    lock_path = os.path.join(maintenance.SNAPSHOT_DIR, ".snapshot.lock")

    def validating(*args, **kwargs):
        os.chmod(lock_path, 0o640)                       # fence identity broken
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(run_check._SnapshotGateUnverified):
        _publish(db, tmp_path, lock_fd, state)
    assert state["allowed"] is False
    os.close(lock_fd)
    db.close()


def test_gate_validator_false_keeps_the_normal_failure_path(tmp_path, monkeypatch):
    db, updater, lock_fd = _env(tmp_path, monkeypatch)
    state = {"allowed": False}
    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", lambda *a, **kw: False)
    assert _publish(db, tmp_path, lock_fd, state) is None
    assert snapshots.last_failure()["reason"] == "validator_false"
    # Reacquire and identity checks passed — ordinary validation failure
    # keeps the existing partial-run processing (writes allowed).
    assert state["allowed"] is True
    os.close(lock_fd)
    db.close()


def test_gate_without_deadline_keeps_the_lock_held(tmp_path, monkeypatch):
    db, updater, lock_fd = _env(tmp_path, monkeypatch)
    state = {"allowed": False}
    gate = run_check._verification_context_factory(db, lock_fd, state)(deadline=None)
    probe = _probe(tmp_path)
    with gate:
        with pytest.raises(BlockingIOError):             # never released
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert state["allowed"] is True
    os.close(probe)
    os.close(lock_fd)
    db.close()


def test_finish_run_raises_gate_error_when_writes_left_forbidden(tmp_path, monkeypatch):
    db, updater, lock_fd = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(run_check, "_WATCHDOG_EXIT_AT", time.monotonic() + 60)
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **kw: 0)
    monkeypatch.setattr(notify_spool, "publish_flags", lambda *a, **kw: None)
    monkeypatch.setattr(notify_cards, "snapshot_dirty", lambda *a: False)
    opened = []

    def stalled(*a, **kw):
        gate = kw["verification_context"](deadline=time.monotonic() + 30)
        gate.__enter__()                      # engaged, never exited
        opened.append(gate)
        return "unchanged"

    monkeypatch.setattr(maintenance, "refresh_snapshot", stalled)
    result = {"notify": {}, "errors": [], "incomplete": []}
    run_id = db.begin_run(None)
    with pytest.raises(run_check._SnapshotGateUnverified):
        run_check._finish_run(db, {}, result, run_id, deadline=time.monotonic() + 60,
                              run_lock_fd=lock_fd)
    opened[0].__exit__(None, None, None)      # reacquire for cleanup
    os.close(lock_fd)
    db.close()


def test_finish_run_unchanged_without_gate_engagement(tmp_path, monkeypatch):
    db, updater, lock_fd = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(run_check, "_WATCHDOG_EXIT_AT", time.monotonic() + 60)
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **kw: 0)
    monkeypatch.setattr(notify_spool, "publish_flags", lambda *a, **kw: None)
    monkeypatch.setattr(notify_cards, "snapshot_dirty", lambda *a: False)
    # 'unchanged' never reaches the publisher, so the gate stays unrun
    # and _finish_run must not treat the idle flag as a gate failure.
    monkeypatch.setattr(maintenance, "refresh_snapshot", lambda *a, **kw: "unchanged")
    result = {"notify": {}, "errors": [], "incomplete": []}
    run_id = db.begin_run(None)
    assert run_check._finish_run(db, {}, result, run_id,
                                 deadline=time.monotonic() + 60,
                                 run_lock_fd=lock_fd) == "ok"
    assert result["snapshot_state"] == "unchanged"
    os.close(lock_fd)
    db.close()
