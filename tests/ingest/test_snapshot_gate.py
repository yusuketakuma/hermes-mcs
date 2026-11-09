"""Snapshot verification gate: the run lock is released only while the
fenced child validates, then reacquired and identity-checked before
publication proceeds. Any verification failure forbids terminal writes."""
import fcntl
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import collection_stages
import maintenance
import notify_cards
import notify_cmds
import notify_spool
import run_check
import snapshots
import writer_lock
from extract_testkit import _ledger, _message
from run_check_stage_testkit import _point_run_check_at


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
    # Same point _main captures it: right after Ledger-open, run lock held.
    return db, updater, lock_fd, run_check._db_identity(db)


def _publish(db, tmp_path, lock_fd, state, baseline=None, context="default"):
    snapshots._reset_status()
    with snapshots._publication_lock(maintenance.SNAPSHOT_DIR) as fence:
        assert fence is not None
        return snapshots._publish_snapshot_locked(
            str(tmp_path / "ledger.db"), maintenance.SNAPSHOT_DIR, fence,
            verification_context=(
                run_check._verification_context_factory(
                    db, lock_fd, state, baseline if baseline is not None
                    else run_check._db_identity(db))
                if context == "default" else context))


def _probe(tmp_path):
    return os.open(str(tmp_path / "run.lock"), os.O_RDONLY)


def _dest():
    return os.path.join(maintenance.SNAPSHOT_DIR, "ledger-snapshot.db")


def test_gate_releases_run_lock_only_during_child_validation(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
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
    assert _publish(db, tmp_path, lock_fd, state, baseline) is not None
    assert held["during"] is False                       # run.lock was free in the window
    assert state == {"allowed": True, "attempted": True}
    with pytest.raises(BlockingIOError):                 # and held again afterwards
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(probe)
    os.close(lock_fd)
    db.close()


def test_gate_updater_during_window_is_a_gate_failure(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    original = snapshots._valid_snapshot_candidate

    def validating(*args, **kwargs):
        updater["state"] = (True, 7)                     # update marker appeared
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(run_check._SnapshotGateUnverified) as caught:
        _publish(db, tmp_path, lock_fd, state, baseline)
    # snapshot-origin reacquire loss is funnelled to the no-write gate;
    # the RunLockLost cause (held: an updater won) is preserved.
    assert isinstance(caught.value.__cause__, writer_lock.RunLockLost) \
        or isinstance(caught.value.__context__, writer_lock.RunLockLost)
    assert state == {"allowed": False, "attempted": True}
    assert not os.path.exists(_dest())                   # never published
    os.close(lock_fd)
    db.close()


def test_gate_reacquire_timeout_is_a_gate_failure(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    probe = _probe(tmp_path)
    monkeypatch.setattr(writer_lock, "RUN_LOCK_REACQUIRE_S", 0.05)
    original = snapshots._valid_snapshot_candidate

    def validating(*args, **kwargs):
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another writer holds it
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(run_check._SnapshotGateUnverified) as caught:
        _publish(db, tmp_path, lock_fd, state, baseline)
    lost = caught.value.__cause__ or caught.value.__context__
    assert isinstance(lost, writer_lock.RunLockLost) and lost.held is False
    assert state["allowed"] is False
    assert not os.path.exists(_dest())
    fcntl.flock(probe, fcntl.LOCK_UN)
    os.close(probe)
    os.close(lock_fd)
    db.close()


def test_gate_db_swap_during_window_fails_no_write(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    original = snapshots._valid_snapshot_candidate
    db_path = str(tmp_path / "ledger.db")

    def validating(*args, **kwargs):
        os.replace(db_path, db_path + ".stale")          # restore-style inode swap
        open(db_path, "wb").close()
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(run_check._SnapshotGateUnverified):
        _publish(db, tmp_path, lock_fd, state, baseline)
    assert state["allowed"] is False
    assert not os.path.exists(_dest())
    os.close(lock_fd)
    db.close()


def test_gate_db_swapped_before_entry_fails_while_lock_held(tmp_path, monkeypatch):
    """The baseline is a Ledger-open point: an old connection on a
    swapped-out file must be caught BEFORE the lock is ever released."""
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    db_path = str(tmp_path / "ledger.db")
    os.replace(db_path, db_path + ".stale")              # old connection stays open
    open(db_path, "wb").close()                          # new file at the source path
    probe = _probe(tmp_path)
    with pytest.raises(run_check._SnapshotGateUnverified):
        _publish(db, tmp_path, lock_fd, state, baseline)
    # the raised gate error forbids writes by itself; 'attempted' only
    # marks a window where the lock was actually released.
    assert state == {"allowed": False, "attempted": False}
    with pytest.raises(BlockingIOError):                 # the run lock was never released
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not os.path.exists(_dest())
    os.close(probe)
    os.close(lock_fd)
    db.close()


def test_gate_run_lock_path_swap_during_window_fails(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    original = snapshots._valid_snapshot_candidate
    lock_path = str(tmp_path / "run.lock")

    def validating(*args, **kwargs):
        os.replace(lock_path, lock_path + ".stale")      # fd/path binding broken
        open(lock_path, "wb").close()
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(run_check._SnapshotGateUnverified):
        _publish(db, tmp_path, lock_fd, state, baseline)
    assert state["allowed"] is False
    assert not os.path.exists(_dest())
    os.close(lock_fd)
    db.close()


def test_gate_validator_error_with_reacquire_keeps_original(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    probe = _probe(tmp_path)

    def validating(*args, **kwargs):
        raise ValueError("synthetic validator crash")

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(ValueError):                      # the original error propagates
        _publish(db, tmp_path, lock_fd, state, baseline)
    assert state["allowed"] is True                      # reacquire + identity did pass
    with pytest.raises(BlockingIOError):
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(probe)
    os.close(lock_fd)
    db.close()


def test_gate_validator_error_with_failed_reacquire_chains_both(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}

    def validating(*args, **kwargs):
        updater["state"] = (True, 7)
        raise ValueError("synthetic validator crash")

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(run_check._SnapshotGateUnverified) as caught:
        _publish(db, tmp_path, lock_fd, state, baseline)
    error = caught.value
    assert isinstance(error.__cause__, ValueError)       # in-flight exception kept
    assert isinstance(error.__context__, writer_lock.RunLockLost)
    assert state["allowed"] is False
    os.close(lock_fd)
    db.close()


def test_gate_control_exception_is_not_normalized(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}

    def validating(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(KeyboardInterrupt):
        _publish(db, tmp_path, lock_fd, state, baseline)
    os.close(lock_fd)
    db.close()


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("losing", ["updater", "held"])
def test_gate_control_exception_survives_reacquire_failure(
        tmp_path, monkeypatch, control, losing):
    """A validator KI/SystemExit stays the propagated object even when the
    reacquire fails — the RunLockLost rides as its cause, allowed stays
    False, and the same instance (not a normalized gate error) surfaces."""
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    sentinel = control()
    probe = _probe(tmp_path) if losing == "held" else None
    if losing == "held":
        monkeypatch.setattr(writer_lock, "RUN_LOCK_REACQUIRE_S", 0.05)

    def validating(*args, **kwargs):
        if losing == "updater":
            updater["state"] = (True, 7)                 # RunLockLost(held=True)
        else:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held=False path
        raise sentinel

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(control) as caught:
        _publish(db, tmp_path, lock_fd, state, baseline)
    assert caught.value is sentinel                     # same object propagates
    lost = caught.value.__cause__
    assert isinstance(lost, writer_lock.RunLockLost)
    assert lost.held is (losing == "updater")
    assert state["allowed"] is False
    assert not os.path.exists(_dest())
    if probe is not None:
        fcntl.flock(probe, fcntl.LOCK_UN)
        os.close(probe)
    os.close(lock_fd)
    db.close()


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_gate_control_exception_survives_identity_mismatch(
        tmp_path, monkeypatch, control):
    """Reacquire succeeds but the DB identity check fails while a control
    exception is in flight: the control exception still propagates and the
    gate failure is kept as its cause."""
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    sentinel = control()
    db_path = str(tmp_path / "ledger.db")

    def validating(*args, **kwargs):
        os.replace(db_path, db_path + ".stale")          # inode swap post-copy
        open(db_path, "wb").close()
        raise sentinel

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(control) as caught:
        _publish(db, tmp_path, lock_fd, state, baseline)
    assert caught.value is sentinel
    assert isinstance(caught.value.__cause__, run_check._SnapshotGateUnverified)
    assert state["allowed"] is False
    assert not os.path.exists(_dest())
    os.close(lock_fd)
    db.close()


def test_gate_non_finite_deadline_is_rejected_without_unlock(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    gate = run_check._verification_context_factory(
        db, lock_fd, state, baseline)(deadline=float("inf"))
    probe = _probe(tmp_path)
    with pytest.raises(run_check._SnapshotGateUnverified):
        with gate:
            pass
    assert state == {"allowed": False, "attempted": True}
    with pytest.raises(BlockingIOError):                 # never released
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(probe)
    os.close(lock_fd)
    db.close()


def test_gate_broken_publication_fence_fails_no_write(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    original = snapshots._valid_snapshot_candidate
    lock_path = os.path.join(maintenance.SNAPSHOT_DIR, ".snapshot.lock")

    def validating(*args, **kwargs):
        os.chmod(lock_path, 0o640)                       # fence identity broken
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    with pytest.raises(run_check._SnapshotGateUnverified):
        _publish(db, tmp_path, lock_fd, state, baseline)
    assert state["allowed"] is False
    assert not os.path.exists(_dest())
    os.close(lock_fd)
    db.close()


def test_gate_validator_false_keeps_the_normal_failure_path(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", lambda *a, **kw: False)
    assert _publish(db, tmp_path, lock_fd, state, baseline) is None
    assert snapshots.last_failure()["reason"] == "validator_false"
    # Reacquire and identity checks passed — ordinary validation failure
    # keeps the existing partial-run processing (writes allowed).
    assert state["allowed"] is True
    os.close(lock_fd)
    db.close()


def test_gate_verify_in_budget_but_publish_after_deadline_stays_unpublished(
        tmp_path, monkeypatch):
    """verified is sampled inside the unlock scope while still in budget;
    the clock only expires after reacquire, at the publication fence —
    the run must stop at publish_deadline with no rename."""
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    clock = [time.monotonic()]
    monkeypatch.setattr(snapshots, "monotonic", lambda: clock[0])
    outer_deadline = clock[0] + 5

    def context_factory(*, deadline):
        gate = run_check._verification_context_factory(
            db, lock_fd, state, baseline)(deadline=deadline)
        confirm = gate.confirm_fence

        def confirm_then_expire(fd, dest_dir):
            # The unlock window, reacquire and identity checks all ran
            # in-budget; only the post-window publication sees expiry.
            clock[0] = outer_deadline + 1
            return confirm(fd, dest_dir)

        gate.confirm_fence = confirm_then_expire
        return gate

    with snapshots._publication_lock(maintenance.SNAPSHOT_DIR) as fence:
        assert snapshots._publish_snapshot_locked(
            str(tmp_path / "ledger.db"), maintenance.SNAPSHOT_DIR, fence,
            deadline=outer_deadline,
            verification_context=context_factory) is None
    assert snapshots.last_failure()["reason"] == "publish_deadline"
    assert state["allowed"] is True                      # gate itself passed
    assert not os.path.exists(_dest())
    os.close(lock_fd)
    db.close()


def test_gate_without_deadline_keeps_the_lock_held(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    state = {"allowed": False, "attempted": False}
    gate = run_check._verification_context_factory(
        db, lock_fd, state, baseline)(deadline=None)
    probe = _probe(tmp_path)
    with gate:
        with pytest.raises(BlockingIOError):             # never released
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert state["allowed"] is True
    os.close(probe)
    os.close(lock_fd)
    db.close()


def test_publish_without_verification_context_keeps_legacy_behaviour(
        tmp_path, monkeypatch):
    """Callers that pass no factory keep the held-lock publish path."""
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
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
    assert _publish(db, tmp_path, lock_fd, {"allowed": False, "attempted": False},
                    context=None) is not None
    assert held["during"] is True                        # run.lock stayed held
    os.close(probe)
    os.close(lock_fd)
    db.close()


def _finish_env(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(run_check, "DB", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(run_check, "_WATCHDOG_EXIT_AT", time.monotonic() + 60)
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **kw: 0)
    monkeypatch.setattr(notify_spool, "publish_flags", lambda *a, **kw: None)
    return db, updater, lock_fd, baseline


def test_finish_run_gate_failure_forbids_the_partial_correction(
        tmp_path, monkeypatch):
    """A transport exit that fails with a non-lock error (e.g. flock
    OSError) must not reach the generic except's finish_run write."""
    db, updater, lock_fd, baseline = _finish_env(tmp_path, monkeypatch)
    monkeypatch.setattr(notify_cards, "snapshot_dirty", lambda *a: False)
    recorded = []
    original_record = db.finish_run

    def record(*args, **kwargs):
        recorded.append(args[1] if len(args) > 1 else kwargs.get("status"))
        return original_record(*args, **kwargs)

    monkeypatch.setattr(db, "finish_run", record)

    class BrokenTransport:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            raise OSError("synthetic flock failure")

    monkeypatch.setattr(writer_lock, "unlocked_transport", lambda *a, **kw: BrokenTransport())
    result = {"notify": {}, "errors": [], "incomplete": []}
    run_id = db.begin_run(None)
    with pytest.raises(run_check._SnapshotGateUnverified) as caught:
        run_check._finish_run(db, {}, result, run_id, deadline=time.monotonic() + 60,
                              run_lock_fd=lock_fd, db_baseline=baseline)
    assert isinstance(caught.value.__cause__, OSError)
    assert recorded == ["ok"]          # only the pre-publish record; no correction
    os.close(lock_fd)
    db.close()


def test_finish_run_gate_failure_keeps_the_dirty_flag(tmp_path, monkeypatch):
    """Reacquire loss leaves the snapshot-dirty generation set so the
    next run republishes — no CAS clear, no published copy."""
    db, updater, lock_fd, baseline = _finish_env(tmp_path, monkeypatch)
    monkeypatch.setattr(notify_cards, "snapshot_dirty", lambda *a: True)
    cleared = []
    monkeypatch.setattr(notify_cards, "clear_snapshot_dirty",
                        lambda *a, **kw: cleared.append(1))
    monkeypatch.setattr(writer_lock, "RUN_LOCK_REACQUIRE_S", 0.05)
    probe = _probe(tmp_path)
    original = snapshots._valid_snapshot_candidate

    def validating(*args, **kwargs):
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # other writer wins
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    result = {"notify": {}, "errors": [], "incomplete": []}
    run_id = db.begin_run(None)
    with pytest.raises(run_check._SnapshotGateUnverified):
        run_check._finish_run(db, {}, result, run_id, deadline=time.monotonic() + 60,
                              run_lock_fd=lock_fd, db_baseline=baseline)
    assert cleared == []
    assert not os.path.exists(_dest())
    fcntl.flock(probe, fcntl.LOCK_UN)
    os.close(probe)
    os.close(lock_fd)
    db.close()


def test_finish_run_gate_reacquire_with_mid_publish_dirty_mark_keeps_generation(
        tmp_path, monkeypatch):
    """Real interleaving: while the gate window has run.lock released, a
    second connection takes it, marks the snapshot dirty and commits.
    After reacquire the publish succeeds, and the stale-generation CAS
    clear must leave the newer mark — the next run republishes."""
    db, updater, lock_fd, baseline = _finish_env(tmp_path, monkeypatch)
    db_path = str(tmp_path / "ledger.db")
    lock_path = str(tmp_path / "run.lock")
    notify_cards.mark_snapshot_dirty(db.db)
    db.db.commit()
    assert notify_cards.snapshot_dirty(db) == 1
    original = snapshots._valid_snapshot_candidate

    def validating(*args, **kwargs):
        other = os.open(lock_path, os.O_RDONLY)
        # Non-vacuous: this flock only succeeds because the gate released
        # the run lock for the validation window.
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with closing(sqlite3.connect(db_path, timeout=10)) as other_db:
                notify_cards.mark_snapshot_dirty(other_db)
                other_db.commit()                    # generation 1 -> 2
        finally:
            fcntl.flock(other, fcntl.LOCK_UN)
            os.close(other)
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    result = {"notify": {}, "errors": [], "incomplete": []}
    run_id = db.begin_run(None)
    assert run_check._finish_run(db, {}, result, run_id,
                                 deadline=time.monotonic() + 60,
                                 run_lock_fd=lock_fd,
                                 db_baseline=baseline) == "ok"
    assert os.path.exists(_dest())                     # publish went through
    # clear_snapshot_dirty ran for real with the pre-publish generation;
    # the mid-publish mark bumped it, so the CAS clear was a no-op.
    assert notify_cards.snapshot_dirty(db) == 2
    os.close(lock_fd)
    db.close()


def test_finish_run_raises_gate_error_when_writes_left_forbidden(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _finish_env(tmp_path, monkeypatch)
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
                              run_lock_fd=lock_fd, db_baseline=baseline)
    opened[0]._transport.__exit__(None, None, None)      # reacquire for cleanup
    os.close(lock_fd)
    db.close()


def test_finish_run_unchanged_without_gate_engagement(tmp_path, monkeypatch):
    db, updater, lock_fd, baseline = _finish_env(tmp_path, monkeypatch)
    monkeypatch.setattr(notify_cards, "snapshot_dirty", lambda *a: False)
    # 'unchanged' never reaches the publisher, so the gate stays unrun
    # and _finish_run must not treat the idle flag as a gate failure.
    monkeypatch.setattr(maintenance, "refresh_snapshot", lambda *a, **kw: "unchanged")
    result = {"notify": {}, "errors": [], "incomplete": []}
    run_id = db.begin_run(None)
    assert run_check._finish_run(db, {}, result, run_id,
                                 deadline=time.monotonic() + 60,
                                 run_lock_fd=lock_fd, db_baseline=baseline) == "ok"
    assert result["snapshot_state"] == "unchanged"
    os.close(lock_fd)
    db.close()


def test_main_gate_failure_writes_nothing_terminal(tmp_path, monkeypatch, capsys):
    """Through _main: an updater win during the snapshot window forbids
    every correction — no finish_run rewrite, health, outbox, marker,
    dirty-CAS or snapshot rename."""
    data = _point_run_check_at(tmp_path, monkeypatch)
    monkeypatch.setattr(run_check, "_config", lambda: {})
    monkeypatch.setattr(run_check, "_semantic_enabled", lambda *args: False)
    monkeypatch.setattr(run_check, "_code_changed", lambda *args: False)
    monkeypatch.setattr(run_check, "MCSAdapter",
                        lambda **kwargs: SimpleNamespace(set_deadline=lambda deadline: None))
    later = Mock()
    for name in ("_run_jobs", "stage_derive", "_deliver", "_run_semantic",
                 "_run_metadata_shadow", "_housekeeping"):
        monkeypatch.setattr(run_check, name, later)
    monkeypatch.setattr(collection_stages, "with_relogin", later)
    monkeypatch.setattr(run_check, "_stage_fetch", lambda *a: None)
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **k: None)
    monkeypatch.setattr(notify_spool, "publish_flags", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(maintenance, "backup_overdue", lambda: False)
    cleared = []
    monkeypatch.setattr(notify_cards, "clear_snapshot_dirty",
                        lambda *a, **kw: cleared.append(1))
    updater = {"state": (False, 7)}
    monkeypatch.setattr(writer_lock, "updater_state", lambda: updater["state"])
    recorded = []
    original_record = run_check.Ledger.finish_run

    def record(self, run_id, status, *args, **kwargs):
        recorded.append(status)
        return original_record(self, run_id, status, *args, **kwargs)

    monkeypatch.setattr(run_check.Ledger, "finish_run", record)
    original = snapshots._valid_snapshot_candidate

    def validating(*args, **kwargs):
        updater["state"] = (True, 7)                     # updater wins mid-window
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", validating)
    # faulthandler has no fileno under capture — set the watchdog
    # deadline directly so _hard_deadline() stays finite.
    monkeypatch.setattr(run_check, "_WATCHDOG_EXIT_AT", time.monotonic() + 300)
    # Pre-seed the snapshot reuse marker: a failed publish must leave its
    # bytes untouched (it is only rewritten after a successful publish).
    sig_path = maintenance._snapshot_sig_path()
    os.makedirs(os.path.dirname(sig_path), exist_ok=True)
    with open(sig_path, "wb") as marker:
        marker.write(b"synthetic-sig-marker")
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])
    assert run_check._main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "snapshot_gate_unverified" in out["errors"]
    assert len(recorded) == 1                            # pre-publish record only
    assert cleared == []                                 # dirty CAS never ran
    assert not (data / "health.json").exists()           # no health write
    assert not (data / run_check.UPDATE_MARKER_NAME).exists()
    with open(sig_path, "rb") as marker:
        assert marker.read() == b"synthetic-sig-marker"  # reuse marker untouched
    assert not os.path.exists(_dest())                   # no rename
    with closing(sqlite3.connect(data / "ledger.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == recorded[0]
