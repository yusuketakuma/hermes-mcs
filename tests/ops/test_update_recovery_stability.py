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


@pytest.mark.parametrize("runtime_mode", ["hermes", "standalone"])
def test_failed_schema_rollback_stays_frozen_until_restored(
        updater, tmp_path, monkeypatch, runtime_mode):
    import ledger
    import maintenance
    import mcs_setup
    from ops_testkit import _git
    repo, live, back, before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    updater.save_state(updater._default_state())
    cfg = {"runtime_mode": runtime_mode, "update": {"mode": "notify"}}
    monkeypatch.setattr(updater, "load_config", lambda: cfg)
    monkeypatch.setattr(mcs_setup, "runtime_gate_errors", lambda *a, **k: [])
    monkeypatch.setattr(updater, "precheck_local", lambda c: [])
    monkeypatch.setattr(updater, "remote_tag_sha", lambda t: after)
    monkeypatch.setattr(updater, "precheck_tag", lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: back)
    monkeypatch.setattr(updater, "_baseline_check", lambda c: [])
    monkeypatch.setattr(updater, "_needs_migration", lambda v: False)
    monkeypatch.setattr(updater, "_standalone_target_supported", lambda ref: True)
    backup_broken = []
    real_valid = ledger.valid_mcs_db
    monkeypatch.setattr(ledger, "valid_mcs_db", lambda path, *a, **k: not (
        backup_broken and str(path) == back) and real_valid(path, *a, **k))

    def child_restarted_then_failed(sha):
        restarts.append("child")
        updater._remove_marker()
        backup_broken.append(True)       # unreadable before any consent hold
        raise updater.UpdateError("post_merge_failed: synthetic")

    monkeypatch.setattr(updater, "_run_post_merge", child_restarted_then_failed)
    damaged = Path(live).read_bytes()
    assert updater.apply("v1.1.0", after, "synthetic-apply") == 1
    assert updater._head_sha() == before and restarts == ["child"]

    restarts.clear()
    notices = []
    monkeypatch.setattr(updater, "_enqueue_notice",
                        lambda text, *a, **k: notices.append(text) or True)
    assert updater.recover_interrupted() == 1
    assert restarts == [] and Path(updater.MARKER_PATH).exists()
    assert notices == []                 # frozen: report only, no ledger write
    report = json.loads(Path(updater.REPORT_PATH).read_text())
    assert report["result"] == "escalate" and report["writers_stopped"] is True
    assert "drainers_key" not in report
    assert updater.load_state()["applying"]["restore_incomplete"] is True
    assert Path(live).read_bytes() == damaged
    assert not Path(updater.DATA, "restore_pending.json").exists()

    # once the backup is usable and the restore approved, the hold ends
    backup_broken.clear()
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 0
    assert restarts == [1] and not Path(updater.MARKER_PATH).exists()
    assert updater.load_state()["applying"] is None


@pytest.mark.parametrize("shape", ["previous_clean", "previous_dirty", "merge_head",
                                   "missing_backup", "unknown_head"])
def test_incomplete_restore_never_finishes_without_restoring_target(
        updater, tmp_path, monkeypatch, shape):
    state = updater._default_state()
    applying = {"sha": "a" * 40, "prev_sha": "b" * 40, "rollback": True,
                "backup_path": str(tmp_path / "synthetic-backup.db"),
                "restore_incomplete": True, "command_id": "synthetic-held"}
    if shape == "missing_backup":
        applying.pop("backup_path")
    state.update(applying=applying, stages=[{"stage": "rollback", "at": 1}])
    updater.save_state(state)
    Path(updater.MARKER_PATH).write_text("synthetic writer freeze")
    if shape == "merge_head":
        git_dir = Path(updater.REPO, ".git")
        git_dir.mkdir(parents=True)
        (git_dir / "MERGE_HEAD").write_text("c" * 40)
    monkeypatch.setattr(updater, "_head_sha", lambda: "c" * 40 if shape == "unknown_head" else "b" * 40)
    monkeypatch.setattr(updater, "_tree_clean", lambda: shape != "previous_dirty")
    monkeypatch.setattr(updater, "_clean_stale_git_locks", lambda: [])

    def forbidden(*args, **kwargs):
        pytest.fail("incomplete restore must not reset, finish, restore an unbound DB, or restart writers")

    for name in ("_git", "_git_out", "_restore_db", "restart_agents", "_enqueue_notice",
                 "_finish_recovery", "_undo_interrupted_migration"):
        monkeypatch.setattr(updater, name, forbidden)
    for _ in range(2):
        assert updater.recover_interrupted() == 1
        after = updater.load_state()
        assert after["applying"] == applying and after["stages"] == state["stages"]
        assert after["executed"]["synthetic-held"]["result"] == "escalated"
        assert Path(updater.MARKER_PATH).read_text() == "synthetic writer freeze"
        report = json.loads(Path(updater.REPORT_PATH).read_text())
        assert report["writers_stopped"] is True
        assert "restore_incomplete" in report["detail"]


@pytest.mark.parametrize("failure_stage", ["layout_floor", "standalone_target", "runtime"])
def test_apply_pre_reset_rollback_failure_survives_recovery(
        updater, tmp_path, monkeypatch, failure_stage):
    import maintenance
    import mcs_setup
    from ops_testkit import _git

    repo, live, back, _before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    updater.save_state(updater._default_state())
    cfg = {"runtime_mode": "standalone", "update": {"mode": "notify"}}
    monkeypatch.setattr(updater, "load_config", lambda: cfg)
    monkeypatch.setattr(mcs_setup, "runtime_gate_errors", lambda *a, **k: [])
    monkeypatch.setattr(updater, "precheck_local", lambda c: [])
    monkeypatch.setattr(updater, "remote_tag_sha", lambda t: after)
    monkeypatch.setattr(updater, "precheck_tag", lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: back)
    monkeypatch.setattr(updater, "_baseline_check", lambda c: [])
    monkeypatch.setattr(updater, "_needs_migration", lambda v: False)
    monkeypatch.setattr(updater, "_standalone_target_supported", lambda ref: True)

    def runtime_failure(*args):
        raise updater.UpdateError("synthetic runtime restore failure")

    def child_failed(sha):
        updater._remove_marker()
        if failure_stage == "layout_floor":
            monkeypatch.setattr(updater, "_rollback_layout_error",
                                lambda entry: "rollback_blocked_layout_floor: synthetic")
        elif failure_stage == "standalone_target":
            monkeypatch.setattr(updater, "_standalone_target_supported", lambda ref: False)
        else:
            monkeypatch.setattr(updater, "_restore_runtime", runtime_failure)
        raise updater.UpdateError("post_merge_failed: synthetic")

    monkeypatch.setattr(updater, "_run_post_merge", child_failed)
    original_db = Path(live).read_bytes()
    assert updater.apply("v1.1.0", after, "synthetic-apply") == 1
    assert updater._head_sha() == after
    applying = updater.load_state()["applying"]
    assert applying["restore_incomplete"] is True
    assert applying["prev_sha"] == after
    for _ in range(2):
        assert updater.recover_interrupted() == 1
        assert updater.load_state()["applying"] == applying
        assert Path(updater.MARKER_PATH).exists()
        assert updater._head_sha() == after
        assert Path(live).read_bytes() == original_db
        assert restarts == []
