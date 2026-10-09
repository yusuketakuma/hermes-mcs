"""Recovery retains interrupted state until workers actually restart."""
import json
from pathlib import Path
import subprocess

import pytest

from test_mcs_update import _schema_bump_world, _seed_consent, updater

__all__ = ["updater"]


@pytest.mark.parametrize("failure", [OSError("synthetic spawn failure"),
                                    subprocess.TimeoutExpired("git", 1)])
def test_check_ignore_failure_is_reported_by_preflight(updater, tmp_path, monkeypatch, failure):
    from ops_testkit import _make_repo
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    real = updater.subprocess.run

    def fail_check(argv, **kwargs):
        if "check-ignore" in argv:
            raise failure
        return real(argv, **kwargs)

    monkeypatch.setattr(updater.subprocess, "run", fail_check)
    errors = updater.precheck_tag("v1.1.0")
    assert "check_ignore_failed" in errors


def test_corrupt_live_db_recovery_escalates_without_empty_consent_hold(
        updater, tmp_path, monkeypatch):
    _repo, live, back, before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    damaged = b"synthetic damaged sqlite header"
    Path(live).write_bytes(damaged)
    state = updater.load_state()
    state["applying"] = {"tag": "v1.1.0", "sha": before, "prev_sha": after,
                         "rollback": True, "backup_path": back,
                         "command_id": "synthetic-unreadable"}
    state["stages"] = [{"stage": "rollback", "at": 1}]
    updater.save_state(state)
    monkeypatch.setattr(updater, "_head_sha", lambda: before)
    monkeypatch.setattr(updater, "_tree_clean", lambda: True)
    notices = []
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text: notices.append(text) or True)
    assert updater.recover_interrupted() == 1
    assert "live_db_unreadable" in updater.load_state()["executed"][
        "synthetic-unreadable"]["detail"]
    assert notices and restarts == [1]
    assert Path(live).read_bytes() == damaged
    assert not Path(updater.DATA, "restore_pending.json").exists()
    assert not Path(updater.RESTORE_REPORT_PATH).exists()


def test_loss_report_wraps_delayed_sqlite_read_failure(updater, tmp_path, monkeypatch):
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    Path(live).write_bytes(b"synthetic non-database")
    with pytest.raises(updater.UpdateError):
        updater._restore_loss_report(back)


def test_loss_report_wraps_publication_io_failure(updater, tmp_path, monkeypatch):
    _repo, _live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise OSError("synthetic report publish failed")

    monkeypatch.setattr(updater, "atomic_write", fail)
    with pytest.raises(updater.UpdateError):
        updater._restore_loss_report(back)


def test_failed_restart_remains_retryable(updater, monkeypatch):
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                         "prev_sha": "b" * 40, "command_id": "synthetic"}
    state["stages"] = [{"stage": "merge", "at": 1}]
    updater.save_state(state)
    monkeypatch.setattr(updater, "_head_sha", lambda: "b" * 40)
    calls, notices = [], []

    def restart(bounce=True):
        calls.append(bounce)
        return ["synthetic-worker"] if len(calls) == 1 else []

    monkeypatch.setattr(updater, "restart_agents", restart)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text: notices.append(text))
    assert updater._finish_recovery(state, "interrupted_pre_merge", []) == 1
    held = updater.load_state()
    assert held["applying"] and held["stages"] and not held["executed"]
    assert not notices
    assert updater._finish_recovery(held, "interrupted_pre_merge", []) == 0
    done = updater.load_state()
    assert done["applying"] is None and done["stages"] == []
    assert done["executed"]["synthetic"]["result"] == "interrupted_recovered"
    assert calls == [True, False] and len(notices) == 1


def test_approved_swap_failure_keeps_restore_hold(updater, tmp_path, monkeypatch):
    _repo, live, back, _before, _after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    _seed_consent(live, back)
    replace = updater._replace_database

    def fail_swap(path, expected_sha, before_replace):
        before_replace()
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(updater, "_replace_database", fail_swap)
    assert updater.rollback("synthetic") != 0
    assert restarts == [] and Path(updater.MARKER_PATH).exists()
    marker = json.loads(Path(updater.DATA, "restore_pending.json").read_text())
    assert marker["phase"] == "awaiting_consent"
    state = updater.load_state()
    assert state["restore_consent"] and state["applying"]["rollback"]
    assert "synthetic" not in state["executed"]
    monkeypatch.setattr(updater, "_replace_database", replace)
    assert updater.recover_interrupted() == 0
    assert updater.load_state()["applying"] is None and restarts == [1]
