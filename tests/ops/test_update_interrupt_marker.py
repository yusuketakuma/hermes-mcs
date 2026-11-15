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
    assert updater.apply("v1.1.0", target, "synthetic-apply") == 0
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
