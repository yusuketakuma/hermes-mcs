"""An interrupted apply or rollback keeps its unfinished journal fenced by the update marker;
settled outcomes keep their existing cleanup."""
from pathlib import Path

import pytest

import service_management
import update_apply
import update_discovery
import update_recovery
from mcs_update_testkit import _apply_env as _apply_env, updater as updater
from ops_testkit import _git, in_process_post_merge

__all__ = ["updater"]


def _world(updater, monkeypatch):
    """The post-merge world of test_post_merge_bail_stops_restarted_writers_before_rollback:
    a real temp repo on v1.0.0, target v1.1.0, quiesce writing the real marker."""
    repo = _apply_env(updater, monkeypatch)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    previous = updater._head_sha()
    target = _git(repo, "rev-parse", "v1.1.0^{commit}").stdout.strip()
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda tag, **_lif05_paths: target)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda snapshot, **_service_kwargs: [])

    def quiesce():
        updater._write_marker()
        return []
    monkeypatch.setattr(updater, "quiesce", quiesce)
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    return previous, target


def _post_merge(updater, monkeypatch, run):
    monkeypatch.setattr(updater, "_run_post_merge", run)
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)


def _journal(updater):
    return updater.journal_store.load_state(updater.STATE_PATH)


@pytest.mark.parametrize("stage", ["post_merge", "bail_rollback"])
def test_an_interrupted_apply_keeps_the_marker_with_its_unfinished_journal(updater, monkeypatch, stage):
    _, target = _world(updater, monkeypatch)

    def post_merge(expected, *, run_lock_fd, **_kw):
        if stage == "post_merge":
            raise KeyboardInterrupt
        raise updater.UpdateError("post_merge_failed: synthetic")
    _post_merge(updater, monkeypatch, post_merge)
    if stage == "bail_rollback":
        def interrupted_rollback(entry, on_hold, **_kw):
            raise KeyboardInterrupt
        monkeypatch.setattr(updater, "_rollback_tree", interrupted_rollback)
        monkeypatch.setattr(update_apply, '_rollback_tree', updater._rollback_tree)
        monkeypatch.setattr(update_recovery, '_rollback_tree', updater._rollback_tree)
    with pytest.raises(KeyboardInterrupt):
        updater.apply("v1.1.0", target, "synthetic-command")
    state = _journal(updater)
    assert state["applying"] and state["stages"]           # recover_interrupted's input
    assert Path(updater.MARKER_PATH).exists()               # writers stay fenced until recovery


def test_a_settled_apply_failure_keeps_its_existing_cleanup(updater, monkeypatch):
    previous, target = _world(updater, monkeypatch)

    def post_merge(expected, *, run_lock_fd, **_kw):
        raise updater.UpdateError("post_merge_failed: synthetic")
    _post_merge(updater, monkeypatch, post_merge)
    assert updater.apply("v1.1.0", target, "synthetic-command") == 1
    state = _journal(updater)
    assert updater._head_sha() == previous and not state["applying"] and not state["stages"]
    assert not Path(updater.MARKER_PATH).exists()


def _applied(updater, monkeypatch):
    previous, target = _world(updater, monkeypatch)
    _post_merge(updater, monkeypatch, in_process_post_merge(updater, monkeypatch))
    assert updater.apply("v1.1.0", target, "synthetic-apply") == 0, Path(updater.REPORT_PATH).read_text()
    state = _journal(updater)
    assert state["applied"][-1]["prev_sha"] == previous and not Path(updater.MARKER_PATH).exists()
    return previous


def test_an_interrupted_rollback_keeps_the_marker_with_its_rollback_journal(updater, monkeypatch):
    _applied(updater, monkeypatch)

    def interrupted_rollback(entry, on_hold, **_kw):
        assert Path(updater.MARKER_PATH).exists()
        raise KeyboardInterrupt
    monkeypatch.setattr(updater, "_rollback_tree", interrupted_rollback)
    monkeypatch.setattr(update_apply, '_rollback_tree', updater._rollback_tree)
    monkeypatch.setattr(update_recovery, '_rollback_tree', updater._rollback_tree)
    with pytest.raises(KeyboardInterrupt):
        updater.rollback("synthetic-rollback")
    state = _journal(updater)
    assert state["applying"]["rollback"] is True and state["stages"]
    assert Path(updater.MARKER_PATH).exists()


def test_a_settled_rollback_keeps_its_existing_cleanup(updater, monkeypatch):
    previous = _applied(updater, monkeypatch)
    assert updater.rollback("synthetic-rollback") == 0
    state = _journal(updater)
    assert updater._head_sha() == previous and not state["applying"] and state["applied"] == []
    assert not Path(updater.MARKER_PATH).exists()


def _released_then_interrupted(updater, seen):
    """restart_agents' own order (update_services.py): drop the marker before the drainers
    start, then the operator's Ctrl-C lands before the journal's terminal save. While this
    process still holds run.lock, another connection cannot take it."""
    import writer_lock

    def restart():
        updater._remove_marker()
        fd = writer_lock.acquire_run_lock(updater.RUN_LOCK)
        seen.append(fd)
        if fd is not None:
            import os
            os.close(fd)
        raise KeyboardInterrupt
    return restart


def _patch_restart(updater, monkeypatch, restart):
    monkeypatch.setattr(updater, "restart_agents", restart)
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)


def test_an_interrupt_after_restart_dropped_the_marker_restores_it_before_run_lock_is_released(
        updater, monkeypatch):
    """apply's post-merge stage (run in-process here, not a real child; the real child path is
    the next test) is interrupted right after restart_agents removed the marker: the marker
    is back (through _ensure_recovery_marker) while run.lock was still held."""
    import update_runtime_migration
    _, target = _world(updater, monkeypatch)
    seen = []
    restart = _released_then_interrupted(updater, seen)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', restart)
    _post_merge(updater, monkeypatch, in_process_post_merge(updater, monkeypatch))
    with pytest.raises(KeyboardInterrupt):
        updater.apply("v1.1.0", target, "synthetic-command")
    assert seen == [None]                                    # no collection could start meanwhile
    assert Path(updater.MARKER_PATH).exists() and _journal(updater)["applying"]


def test_an_interrupt_before_the_rollback_terminal_save_restores_the_marker(updater, monkeypatch):
    _applied(updater, monkeypatch)
    seen = []
    _patch_restart(updater, monkeypatch, _released_then_interrupted(updater, seen))
    with pytest.raises(KeyboardInterrupt):
        updater.rollback("synthetic-rollback")
    assert seen == [None]
    assert Path(updater.MARKER_PATH).exists() and _journal(updater)["applying"]["rollback"] is True


def test_a_failed_marker_restore_is_noted_on_the_interrupt_and_left_visible(updater, monkeypatch):
    """The residual risk stays explicit: run.lock is released with no marker and the
    original interrupt carries the failure."""
    import update_lifecycle_context
    _applied(updater, monkeypatch)
    _patch_restart(updater, monkeypatch, _released_then_interrupted(updater, []))

    def broken():
        raise OSError("update_marker_unverifiable")
    monkeypatch.setattr(update_lifecycle_context, '_ensure_recovery_marker', broken)
    with pytest.raises(KeyboardInterrupt) as caught:
        updater.rollback("synthetic-rollback")
    assert "update_marker_restore_failed:OSError" in getattr(caught.value, '__notes__', [])
    assert not Path(updater.MARKER_PATH).exists() and _journal(updater)["applying"]["rollback"] is True


def test_an_interrupt_after_bail_saved_the_terminal_journal_is_not_unfinished(updater, monkeypatch):
    """bail saved the aborted journal, then restart_agents was interrupted: nothing is left to
    recover, so the marker is not recreated."""
    _world(updater, monkeypatch)
    target = _git(Path(updater.REPO), "rev-parse", "v1.1.0^{commit}").stdout.strip()

    def post_merge(expected, *, run_lock_fd, **_kw):
        raise updater.UpdateError("post_merge_failed: synthetic")
    _post_merge(updater, monkeypatch, post_merge)
    _patch_restart(updater, monkeypatch, _released_then_interrupted(updater, []))
    with pytest.raises(KeyboardInterrupt):
        updater.apply("v1.1.0", target, "synthetic-command")
    state = _journal(updater)
    assert not state["applying"] and not state["stages"]
    assert not Path(updater.MARKER_PATH).exists()



@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit, "second_interrupt"])
def test_an_interrupted_parent_reaps_the_child_then_restores_the_marker_under_run_lock(
        updater, monkeypatch, failure):
    """The real _run_post_merge with the Popen stand-in of test_update_child_reaping: the child
    already dropped the marker (its restart_agents) when the parent's wait is interrupted. The
    parent stops and reaps the child, restores the marker while still holding run.lock, then
    releases it; a second interrupt during the restore is noted and leaves no marker."""
    import os
    import subprocess
    import update_lifecycle_context
    import writer_lock
    _, target = _world(updater, monkeypatch)
    interrupt = KeyboardInterrupt if failure == "second_interrupt" else failure
    events = []

    class Child:
        pid = 4321
        returncode = None

        def communicate(self, timeout=None):
            if not events:
                updater._remove_marker()
                events.append("child_dropped_marker")
                raise interrupt("synthetic operator interrupt")
            self.returncode = -9
            events.append("reaped")
            return "", ""

        def wait(self, timeout=None):
            self.returncode = -9
            events.append("reaped")
            return self.returncode

    child = Child()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(os, "killpg", lambda pid, signum: events.append("kill_requested"))
    ensure = update_lifecycle_context._ensure_recovery_marker

    def restore():
        assert child.returncode is not None
        events.append("restore")
        fd = writer_lock.acquire_run_lock(updater.RUN_LOCK)
        events.append("run_lock_free" if fd is not None else "run_lock_held")
        if fd is not None:
            os.close(fd)
        if failure == "second_interrupt":
            raise KeyboardInterrupt("synthetic second interrupt")
        return ensure()
    monkeypatch.setattr(update_lifecycle_context, '_ensure_recovery_marker', restore)
    with pytest.raises(interrupt) as caught:
        updater.apply("v1.1.0", target, "synthetic-command")
    fd = writer_lock.acquire_run_lock(updater.RUN_LOCK)
    assert fd is not None                                   # released after the restore
    os.close(fd)
    events.append("run_lock_released")
    assert events == ["child_dropped_marker", "kill_requested", "reaped", "restore",
                      "run_lock_held", "run_lock_released"]
    assert _journal(updater)["applying"]
    if failure == "second_interrupt":
        assert "update_marker_restore_failed:KeyboardInterrupt" in caught.value.__notes__
        assert not Path(updater.MARKER_PATH).exists()
    else:
        assert Path(updater.MARKER_PATH).exists()
