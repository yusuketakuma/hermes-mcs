"""Recovery retains interrupted state until workers actually restart."""
import restore_evidence
import mcs_util
import service_management
import update_discovery
import json
from pathlib import Path
import subprocess

import pytest

from test_mcs_update import (_assert_old_schema_restore_held, _schema_bump_world,
                             _seed_consent, updater)

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
    state = updater.journal_store.load_state(updater.STATE_PATH)
    state["applying"] = {"tag": "v1.1.0", "sha": before, "prev_sha": after,
                         "rollback": True, "backup_path": back,
                         "command_id": "synthetic-unreadable"}
    state["stages"] = [{"stage": "rollback", "at": 1}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    monkeypatch.setattr(updater, "_head_sha", lambda: before)
    monkeypatch.setattr(updater, "_tree_clean", lambda: True)
    notices = []
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text: notices.append(text) or True)
    assert updater.recover_interrupted() == 1
    assert "live_db_unreadable" in updater.journal_store.load_state(updater.STATE_PATH)["executed"][
        "synthetic-unreadable"]["detail"]
    assert notices == [] and restarts == []
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["restore_incomplete"] is True
    assert Path(live).read_bytes() == damaged
    assert not Path(updater.DATA, "restore_pending.json").exists()
    assert not Path(updater.RESTORE_REPORT_PATH).exists()


def test_loss_report_wraps_delayed_sqlite_read_failure(updater, tmp_path, monkeypatch):
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    Path(live).write_bytes(b"synthetic non-database")
    with pytest.raises(restore_evidence.RestoreEvidenceError):
        restore_evidence.create_loss_report(live_path=updater.LEDGER, backup_path=back, report_path=updater.RESTORE_REPORT_PATH)


def test_loss_report_wraps_publication_io_failure(updater, tmp_path, monkeypatch):
    _repo, _live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise OSError("synthetic report publish failed")

    monkeypatch.setattr(restore_evidence, "atomic_write", fail)
    with pytest.raises(restore_evidence.RestoreEvidenceError,
                       match="restore_report_publish_failed"):
        restore_evidence.create_loss_report(
            live_path=updater.LEDGER, backup_path=back,
            report_path=updater.RESTORE_REPORT_PATH)
    # The application entrance retains the original public updater error token.
    import ledger
    monkeypatch.setattr(ledger, "valid_mcs_db", lambda path: True)
    with pytest.raises(updater.UpdateError, match="restore_report_publish_failed"):
        updater._restore_db(back)


def test_failed_restart_remains_retryable(updater, monkeypatch):
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                         "prev_sha": "b" * 40, "command_id": "synthetic"}
    state["stages"] = [{"stage": "merge", "at": 1}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    monkeypatch.setattr(updater, "_head_sha", lambda: "b" * 40)
    calls, notices = [], []

    def restart(bounce=True):
        calls.append(bounce)
        return ["synthetic-worker"] if len(calls) == 1 else []

    monkeypatch.setattr(updater, "restart_agents", restart)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text: notices.append(text))
    assert updater._finish_recovery(state, "interrupted_pre_merge", []) == 1
    held = updater.journal_store.load_state(updater.STATE_PATH)
    assert held["applying"] and held["stages"] and not held["executed"]
    assert not notices
    assert updater._finish_recovery(held, "interrupted_pre_merge", []) == 0
    done = updater.journal_store.load_state(updater.STATE_PATH)
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
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["restore_consent"] and state["applying"]["rollback"]
    assert "synthetic" not in state["executed"]
    monkeypatch.setattr(updater, "_replace_database", replace)
    assert updater.recover_interrupted() == 1
    _assert_old_schema_restore_held(updater, live, back, restarts, "synthetic")


@pytest.mark.parametrize("failure", ["restore_update_error", "restore_os_error", "marker_os_error"])
def test_manual_rollback_restore_failure_keeps_mixed_tree_and_db_frozen(
        updater, tmp_path, monkeypatch, failure):
    import notify_spool

    _repo, live, back, before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    live_bytes = Path(live).read_bytes()
    backup_checks = []
    check_backup = updater._rollback_backup_error

    def checked_backup(entry):
        result = check_backup(entry)
        backup_checks.append(result)
        return result

    monkeypatch.setattr(updater, "_rollback_backup_error", checked_backup)
    failures = []

    def fail_restore(*args, **kwargs):
        # This is after the actual git reset, not a preflight failure.
        assert updater._head_sha() == before
        failures.append(failure)
        if failure == "restore_update_error":
            raise updater.UpdateError("synthetic restore failure")
        raise OSError("synthetic restore failure")

    if failure == "marker_os_error":
        monkeypatch.setattr(notify_spool, "mark_restored", fail_restore)
    else:
        monkeypatch.setattr(updater, "_restore_db", fail_restore)

    assert updater.rollback("synthetic-manual-rollback") != 0
    assert backup_checks == [None] and failures == [failure]
    assert updater._head_sha() == before
    assert restore_evidence.database_version(live) == 8
    assert Path(live).read_bytes() == live_bytes
    assert Path(updater.MARKER_PATH).exists()
    assert restarts == []
    state = updater.journal_store.load_state(updater.STATE_PATH)
    applying = state["applying"]
    assert applying["rollback"] is True
    assert applying["sha"] == before and applying["prev_sha"] == after
    assert applying["backup_path"] == back
    assert applying["restore_incomplete"] is True
    assert state["stages"]
    assert state["applied"][-1]["sha"] == after


def test_manual_rollback_quiesce_failure_before_tree_change_does_not_freeze(
        updater, tmp_path, monkeypatch):
    _repo, live, _back, _before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    live_bytes = Path(live).read_bytes()

    def fail_quiesce():
        updater._write_marker()
        raise updater.UpdateError("synthetic stop failure")

    def no_tree_change(*args, **kwargs):
        pytest.fail("rollback tree must not run after quiesce fails")

    monkeypatch.setattr(updater, "quiesce", fail_quiesce)
    monkeypatch.setattr(updater, "_rollback_tree", no_tree_change)
    assert updater.rollback("synthetic-stop-failure") != 0
    assert updater._head_sha() == after
    assert Path(live).read_bytes() == live_bytes
    assert restarts == [1]
    assert not Path(updater.MARKER_PATH).exists()
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert not (state.get("applying") or {}).get("restore_incomplete")


def test_manual_schema_rollback_persists_hold_before_tree_change(
        updater, tmp_path, monkeypatch):
    _repo, live, back, before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    live_bytes = Path(live).read_bytes()
    remove_marker = updater._remove_marker
    seen = []

    def crash_before_tree(entry, on_hold=None, **kwargs):
        state = updater.journal_store.load_state(updater.STATE_PATH)
        applying = state["applying"]
        assert updater._head_sha() == after and Path(updater.MARKER_PATH).exists()
        assert applying["rollback"] is True and applying["restore_incomplete"] is True
        assert applying["sha"] == before and applying["prev_sha"] == after
        assert applying["backup_path"] == back and state["stages"]
        seen.append(True)
        # Simulate process death: Python finally cleanup does not run on a crash.
        monkeypatch.setattr(updater, "_remove_marker", lambda: None)
        raise SystemExit("synthetic crash")

    monkeypatch.setattr(updater, "_rollback_tree", crash_before_tree)
    with pytest.raises(SystemExit, match="synthetic crash"):
        updater.rollback("synthetic-manual-crash")
    monkeypatch.setattr(updater, "_remove_marker", remove_marker)
    assert seen == [True] and restarts == []
    assert updater.recover_interrupted() == 1
    assert updater._head_sha() == after and Path(live).read_bytes() == live_bytes
    assert Path(updater.MARKER_PATH).exists() and restarts == []
    applying = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
    assert applying["rollback"] is True and applying["restore_incomplete"] is True


def test_apply_bail_persists_schema_hold_before_tree_change(
        updater, tmp_path, monkeypatch):
    import maintenance
    from ops_testkit import _git

    repo, live, back, before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    updater.journal_store.save_state(updater.STATE_PATH, updater.journal_store.default_state())
    cfg = {"runtime_mode": "hermes", "update": {"mode": "notify"}}
    monkeypatch.setattr(mcs_util, "load_config", lambda: cfg)
    monkeypatch.setattr(service_management, "runtime_gate_errors", lambda *a, **k: [])
    monkeypatch.setattr(updater, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_discovery, "remote_tag_sha", lambda t, **kw: after)
    monkeypatch.setattr(updater, "precheck_tag", lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: back)
    monkeypatch.setattr(updater, "_baseline_check", lambda c: [])
    monkeypatch.setattr(updater, "_needs_migration", lambda v: False)
    monkeypatch.setattr(updater, "_standalone_target_supported", lambda ref: True)
    remove_marker = updater._remove_marker
    live_bytes = Path(live).read_bytes()
    seen = []

    def child_failed(sha, *, run_lock_fd):
        updater._remove_marker()
        raise updater.UpdateError("synthetic post-merge failure")

    def crash_before_tree(entry, on_hold=None, **kwargs):
        state = updater.journal_store.load_state(updater.STATE_PATH)
        applying = state["applying"]
        assert updater._head_sha() == after and Path(updater.MARKER_PATH).exists()
        assert applying["rollback"] is True and applying["restore_incomplete"] is True
        assert applying["sha"] == before and applying["prev_sha"] == after
        assert applying["backup_path"] == back and state["stages"]
        seen.append(True)
        monkeypatch.setattr(updater, "_remove_marker", lambda: None)
        raise SystemExit("synthetic crash")

    monkeypatch.setattr(updater, "_run_post_merge", child_failed)
    monkeypatch.setattr(updater, "_rollback_tree", crash_before_tree)
    with pytest.raises(SystemExit, match="synthetic crash"):
        updater.apply("v1.1.0", after, "synthetic-apply-crash")
    monkeypatch.setattr(updater, "_remove_marker", remove_marker)
    assert seen == [True] and restarts == []
    assert updater.recover_interrupted() == 1
    assert updater._head_sha() == after and Path(live).read_bytes() == live_bytes
    assert Path(updater.MARKER_PATH).exists() and restarts == []
    applying = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
    assert applying["rollback"] is True and applying["restore_incomplete"] is True


@pytest.mark.parametrize("runtime_mode", ["hermes", "standalone"])
def test_failed_schema_rollback_stays_frozen_until_restored(
        updater, tmp_path, monkeypatch, runtime_mode):
    import ledger
    import maintenance
    from ops_testkit import _git
    repo, live, back, before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    updater.journal_store.save_state(updater.STATE_PATH, updater.journal_store.default_state())
    cfg = {"runtime_mode": runtime_mode, "update": {"mode": "notify"}}
    monkeypatch.setattr(mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(service_management, 'runtime_gate_errors', lambda *a, **k: [])
    monkeypatch.setattr(updater, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: after)
    monkeypatch.setattr(updater, "precheck_tag", lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: back)
    monkeypatch.setattr(updater, "_baseline_check", lambda c: [])
    monkeypatch.setattr(updater, "_needs_migration", lambda v: False)
    monkeypatch.setattr(updater, "_standalone_target_supported", lambda ref: True)
    backup_broken = []
    real_valid = ledger.valid_mcs_db
    monkeypatch.setattr(ledger, "valid_mcs_db", lambda path, *a, **k: not (
        backup_broken and str(path) == back) and real_valid(path, *a, **k))

    def child_restarted_then_failed(sha, *, run_lock_fd):
        restarts.append("child")
        updater._remove_marker()
        backup_broken.append(True)       # unreadable before any consent hold
        raise updater.UpdateError("post_merge_failed: synthetic")

    monkeypatch.setattr(updater, "_run_post_merge", child_restarted_then_failed)
    damaged = Path(live).read_bytes()
    assert updater.apply("v1.1.0", after, "synthetic-apply") == 1
    assert updater._head_sha() == before and restarts == ["child"]
    assert updater.journal_store.load_state(updater.STATE_PATH)["executed"]["synthetic-apply"]["result"] == "failed"

    restarts.clear()
    notices = []
    monkeypatch.setattr(updater, "_enqueue_notice",
                        lambda text, *a, **k: notices.append(text) or True)
    prior_execution = updater.journal_store.load_state(updater.STATE_PATH)["executed"]["synthetic-apply"]
    assert updater.recover_interrupted() == 1
    assert restarts == [] and Path(updater.MARKER_PATH).exists()
    assert notices == []                 # frozen: report only, no ledger write
    report = json.loads(Path(updater.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked" and report.get("writers_stopped") is not True
    assert "drainers_key" not in report
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["restore_incomplete"] is True
    assert Path(live).read_bytes() == damaged
    assert not Path(updater.DATA, "restore_pending.json").exists()

    # The shared journal hold preserves the original failed receipt exactly.
    assert updater.journal_store.load_state(updater.STATE_PATH)["executed"]["synthetic-apply"] == prior_execution
    assert prior_execution["result"] == "failed"
    # Once usable and approved, the copy succeeds but old-schema reconcile stays held.
    backup_broken.clear()
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 1
    _assert_old_schema_restore_held(updater, live, back, restarts, "synthetic-apply",
                                    prior_execution=prior_execution)
    assert notices == []


@pytest.mark.parametrize("shape", ["previous_clean", "previous_dirty", "merge_head",
                                   "missing_backup", "unknown_head"])
def test_incomplete_restore_never_finishes_without_restoring_target(
        updater, tmp_path, monkeypatch, shape):
    state = updater.journal_store.default_state()
    applying = {"sha": "a" * 40, "prev_sha": "b" * 40, "rollback": True,
                "backup_path": str(tmp_path / "synthetic-backup.db"),
                "restore_incomplete": True, "command_id": "synthetic-held"}
    if shape == "missing_backup":
        applying.pop("backup_path")
    state.update(applying=applying, stages=[{"stage": "rollback", "at": 1}])
    updater.journal_store.save_state(updater.STATE_PATH, state)
    Path(updater.MARKER_PATH).write_text("synthetic writer freeze")
    Path(updater.MARKER_PATH).chmod(0o600)
    if shape == "merge_head":
        git_dir = Path(updater.REPO, ".git")
        git_dir.mkdir(parents=True)
        (git_dir / "MERGE_HEAD").write_text("c" * 40)
    monkeypatch.setattr(updater, "_head_sha", lambda: "c" * 40 if shape == "unknown_head" else "b" * 40)
    monkeypatch.setattr(updater, "_clean_stale_git_locks", lambda: [])

    def forbidden(*args, **kwargs):
        pytest.fail("incomplete restore must not reset, finish, restore an unbound DB, or restart writers")

    for name in ("_git", "_git_out", "_restore_db", "restart_agents", "_enqueue_notice",
                 "_finish_recovery", "_undo_interrupted_migration", "_tree_clean"):
        monkeypatch.setattr(updater, name, forbidden)
    for _ in range(2):
        assert updater.recover_interrupted() == 1
        after = updater.journal_store.load_state(updater.STATE_PATH)
        assert after["applying"] == applying and after["stages"] == state["stages"]
        assert after["executed"] == state["executed"]
        assert "synthetic-held" not in after["executed"]
        assert Path(updater.MARKER_PATH).read_text() == "synthetic writer freeze"
        report = json.loads(Path(updater.REPORT_PATH).read_text())
        assert report.get("writers_stopped") is not True
        assert report["result"] == "restore_consent_blocked"
        if shape == "missing_backup":
            assert "without a rollback" in report["detail"]
        else:
            assert "restore_incomplete" in report["detail"]


@pytest.mark.parametrize("failure_stage", ["layout_floor", "standalone_target", "runtime"])
def test_apply_pre_reset_rollback_failure_survives_recovery(
        updater, tmp_path, monkeypatch, failure_stage):
    import maintenance
    from ops_testkit import _git

    repo, live, back, _before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    updater.journal_store.save_state(updater.STATE_PATH, updater.journal_store.default_state())
    cfg = {"runtime_mode": "standalone", "update": {"mode": "notify"}}
    monkeypatch.setattr(mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(service_management, 'runtime_gate_errors', lambda *a, **k: [])
    monkeypatch.setattr(updater, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: after)
    monkeypatch.setattr(updater, "precheck_tag", lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: back)
    monkeypatch.setattr(updater, "_baseline_check", lambda c: [])
    monkeypatch.setattr(updater, "_needs_migration", lambda v: False)
    monkeypatch.setattr(updater, "_standalone_target_supported", lambda ref: True)

    def runtime_failure(*args):
        raise updater.UpdateError("synthetic runtime restore failure")

    def child_failed(sha, *, run_lock_fd):
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
    applying = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
    assert applying["restore_incomplete"] is True
    assert applying["prev_sha"] == after
    for _ in range(2):
        assert updater.recover_interrupted() == 1
        assert updater.journal_store.load_state(updater.STATE_PATH)["applying"] == applying
        assert Path(updater.MARKER_PATH).exists()
        assert updater._head_sha() == after
        assert Path(live).read_bytes() == original_db
        assert restarts == []


@pytest.mark.parametrize("phase", ["source_only", "venv_partial", "pip_done"])
@pytest.mark.parametrize("consent_pending", [False, True])
@pytest.mark.parametrize("legacy_journal", [False, True])
def test_python314_pin_refresh_crashes_restore_original_dependencies(
        updater, tmp_path, monkeypatch, phase, consent_pending, legacy_journal):
    from ops_testkit import _git
    from test_python314_runtime_migration import _checkout, _interrupted

    repo, prev, venv, runtime, order, notices = _interrupted(
        updater, tmp_path, monkeypatch)
    backup = Path(str(venv) + ".pre-python314")
    # Both environments run Python 3.14: dependency identity must also recover.
    (backup / "bin/python3").write_text("#!/bin/sh\necho 3 14\n")
    (backup / "dependencies").write_text("old-installed-dependencies")
    (venv / "dependencies").write_text("new-installed-dependencies")
    pip_mark = venv / ".mcs-pip-incomplete"
    if phase == "source_only":
        venv.rename(runtime / "not-built-yet")
        backup.rename(venv)
    elif phase == "venv_partial":
        pip_mark.touch()
    else:
        (venv / "bin/hermes").write_text("#!/bin/sh\nexit 0\n")
    checkout = tmp_path / "hermes-source"
    old_head, new_head = _checkout(checkout)
    _git(checkout, "checkout", "-q", "--detach", new_head)
    state = updater.journal_store.load_state(updater.STATE_PATH)
    applying = state["applying"]
    applying["runtime_migration"].update(checkout=str(checkout), checkout_head=old_head,
                                         checkout_target=new_head)
    if legacy_journal:
        applying["runtime_migration"].pop("checkout_target")
        monkeypatch.setattr(updater, "HERMES_PIN", new_head)
    applying.update(schema_bump=consent_pending,
                    backup_path=str(tmp_path / "synthetic-schema-backup.db"))
    updater.journal_store.save_state(updater.STATE_PATH, state)
    Path(updater.MARKER_PATH).write_text("synthetic writer freeze")
    Path(updater.MARKER_PATH).chmod(0o600)
    held = []
    if consent_pending:
        def needs_consent(*args, **kwargs):
            raise updater.RestoreConsentPending({"report_id": "synthetic-pin-refresh"})

        monkeypatch.setattr(updater, "_restore_db", needs_consent)
        monkeypatch.setattr(updater, "_consent_hold",
                            lambda *args: held.append(True) or "synthetic-pin-refresh")
    assert updater.recover_interrupted() == 0
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == old_head
    assert (venv / "bin/python3").read_text().endswith("echo 3 14\n")
    assert (venv / "dependencies").read_text() == "old-installed-dependencies"
    assert not backup.exists()
    assert order.index("quiesce") < order.index("runtime") < order.index("reset")
    if phase != "source_only":
        assert (runtime / "venv.python314-aside/dependencies").read_text() \
            == "new-installed-dependencies"
        assert (runtime / "venv.python314-aside/.mcs-pip-incomplete").exists() \
            == (phase == "venv_partial")
    after = updater.journal_store.load_state(updater.STATE_PATH)
    if consent_pending:
        assert held and "restart" not in order and not notices
        assert after["applying"]["rollback"] is True
        assert after["applying"]["runtime_migration"]["checkout_head"] == old_head
        assert Path(updater.MARKER_PATH).read_text() == "synthetic writer freeze"
    else:
        assert "restart" in order and after["applying"] is None
        assert after["executed"]["cid-mig"]["result"] == "rolled_back"
        assert not Path(updater.MARKER_PATH).exists()


@pytest.mark.parametrize("phase", ["source_only", "venv_backed_up"])
@pytest.mark.parametrize("change", ["unknown", "branch", "dirty", "other_known_pin"])
@pytest.mark.parametrize("operation", ["recover", "restore", "rollback"])
def test_pin_refresh_restore_preserves_source_drift_and_both_environments(
        updater, tmp_path, monkeypatch, phase, change, operation):
    from ops_testkit import _git
    from test_python314_runtime_migration import _checkout, _interrupted

    _repo, _prev, venv, runtime, order, _notices = _interrupted(
        updater, tmp_path, monkeypatch)
    backup = Path(str(venv) + ".pre-python314")
    (backup / "dependencies").write_text("old-installed-dependencies")
    (venv / "dependencies").write_text("new-installed-dependencies")
    if phase == "source_only":
        venv.rename(runtime / "not-built-yet")
        backup.rename(venv)
    checkout = tmp_path / "hermes-source"
    old_head, target = _checkout(checkout)
    _git(checkout, "checkout", "-q", "--detach", target)
    if change in ("unknown", "other_known_pin"):
        (checkout / "f").write_text("synthetic later source")
        _git(checkout, "add", "f")
        _git(checkout, "-c", "user.email=f@example.invalid", "-c", "user.name=F",
             "commit", "-q", "-m", "synthetic later commit")
        other = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        monkeypatch.setattr(updater, "HERMES_PREVIOUS_PINS",
                            frozenset({old_head, other} if change == "other_known_pin"
                                      else {old_head}))
    elif change == "branch":
        _git(checkout, "checkout", "-q", "-b", "local/org-build")
    else:
        (checkout / "f").write_text("synthetic manual edit")
    state = updater.journal_store.load_state(updater.STATE_PATH)
    applying = state["applying"]
    applying["runtime_migration"].update(checkout=str(checkout), checkout_head=old_head,
                                         checkout_target=target)
    if operation == "rollback":
        state["applied"] = [applying]
        state["applying"] = None
        state["stages"] = []
    updater.journal_store.save_state(updater.STATE_PATH, state)
    Path(updater.MARKER_PATH).write_text("synthetic writer freeze")
    Path(updater.MARKER_PATH).chmod(0o600)
    journal = Path(updater.STATE_PATH).read_bytes()
    source = (checkout / "f").read_bytes()
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    envs = {path: {str(item.relative_to(path)): (item.read_bytes(), item.stat().st_ino)
                   for item in path.rglob("*") if item.is_file()}
            for path in (venv, backup)}

    def forbidden(*args, **kwargs):
        pytest.fail("source drift must stop before journals, venvs, or workers change")

    for name in ("_clean_stale_git_locks", "_services_reconcile", "restart_agents",
                 "_enqueue_notice", "quiesce", "_reinstall"):
        monkeypatch.setattr(updater, name, forbidden)
    for attempt in range(2):
        if operation == "restore":
            with pytest.raises(updater.UpdateError, match="runtime_restore_checkout_changed"):
                updater._restore_runtime(applying)
        else:
            assert (updater.recover_interrupted() if operation == "recover"
                    else updater.rollback()) == 1
        assert Path(updater.STATE_PATH).read_bytes() == journal
        assert (checkout / "f").read_bytes() == source
        assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == head
        for path, before in envs.items():
            assert {str(item.relative_to(path)): (item.read_bytes(), item.stat().st_ino)
                    for item in path.rglob("*") if item.is_file()} == before
        assert backup.exists() == (phase == "venv_backed_up")
        assert not list(runtime.glob("venv.python314-aside*"))
        assert Path(updater.MARKER_PATH).read_text() == "synthetic writer freeze"
        assert order == (["runtime"] * (attempt + 1) if operation == "restore" else [])


def test_pin_refresh_restore_keeps_explicitly_confirmed_clean_branch(
        updater, tmp_path, monkeypatch):
    from ops_testkit import _git
    from test_python314_runtime_migration import _checkout, _interrupted

    _repo, _prev, venv, _runtime, _order, _notices = _interrupted(
        updater, tmp_path, monkeypatch)
    checkout = tmp_path / "hermes-source"
    head, _target = _checkout(checkout)
    _git(checkout, "checkout", "-q", "-b", "local/org-build")
    state = updater.journal_store.load_state(updater.STATE_PATH)
    state["applying"]["runtime_migration"].update(
        checkout=str(checkout), checkout_head=head, checkout_target=head, kept_checkout=True)
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.recover_interrupted() == 0
    assert (venv / "bin/python3").read_text().endswith("echo 3 13\n")
    assert _git(checkout, "symbolic-ref", "--short", "HEAD").stdout.strip() == "local/org-build"
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == head
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"] is None


@pytest.mark.parametrize("case", ["null", "false", "zero", "empty_string", "empty_list",
                                  "empty_dict", "string", "list", "missing_paths",
                                  "venv_type", "backup_type", "checkout_empty",
                                  "head_type", "target_type", "keep_type"])
@pytest.mark.parametrize("operation", ["plan", "restore", "recover"])
def test_invalid_runtime_migration_is_preserved_before_any_action(
        updater, tmp_path, monkeypatch, case, operation):
    venv, backup = tmp_path / "venv", tmp_path / "venv.pre-python314"
    venv.mkdir()
    backup.mkdir()
    (venv / "dependencies").write_text("synthetic current dependencies")
    (backup / "dependencies").write_text("synthetic old dependencies")
    valid = {"venv": str(venv), "backup_path": str(backup)}
    source = {**valid, "checkout": str(tmp_path / "checkout"), "checkout_head": "a" * 40}
    invalid = {"null": None, "false": False, "zero": 0, "empty_string": "",
               "empty_list": [], "empty_dict": {}, "string": "broken", "list": [valid],
               "missing_paths": {"updater_python": "3.14.8"},
               "venv_type": {**valid, "venv": []},
               "backup_type": {**valid, "backup_path": False},
               "checkout_empty": {**valid, "checkout": ""},
               "head_type": {**source, "checkout_head": []},
               "target_type": {**source, "checkout_target": None},
               "keep_type": {**source, "kept_checkout": "yes"}}[case]
    state = updater.journal_store.default_state()
    applying = {"sha": "a" * 40, "prev_sha": "b" * 40, "tag": "v1.0.25",
                "runtime_migration": invalid, "reinstall": True}
    state.update(applying=applying, stages=[{"stage": "runtime_migration", "at": 1}])
    updater.journal_store.save_state(updater.STATE_PATH, state)
    Path(updater.MARKER_PATH).write_text("synthetic writer freeze")
    Path(updater.MARKER_PATH).chmod(0o600)
    journal = Path(updater.STATE_PATH).read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("invalid migration must stop before planning or changing assets")

    for name in ("_runtime_venv", "_clean_stale_git_locks", "journal", "quiesce",
                 "restart_agents", "_enqueue_notice", "_reinstall"):
        monkeypatch.setattr(updater, name, forbidden)
    if operation == "recover":
        assert updater.recover_interrupted() == 1
    else:
        with pytest.raises(updater.UpdateError):
            if operation == "plan":
                updater._plan_runtime_migration(state, held_fd=5)
            else:
                updater._restore_runtime(applying)
    assert Path(updater.STATE_PATH).read_bytes() == journal
    assert state["applying"]["runtime_migration"] == invalid
    assert (venv / "dependencies").read_text() == "synthetic current dependencies"
    assert (backup / "dependencies").read_text() == "synthetic old dependencies"
    assert not list(tmp_path.glob("venv.python314-aside*"))
    assert Path(updater.MARKER_PATH).read_text() == "synthetic writer freeze"


def test_existing_standalone_migration_is_verified_without_replanning(updater, tmp_path, monkeypatch):
    migration = {"venv": str(tmp_path / "venv"),
                 "backup_path": str(tmp_path / "venv.pre-python314")}
    state = updater.journal_store.default_state()
    state["applying"] = {"runtime_migration": migration}
    updater.journal_store.save_state(updater.STATE_PATH, state)
    journal = Path(updater.STATE_PATH).read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("a valid existing migration must be kept without re-planning")

    monkeypatch.setattr(updater, "_runtime_venv", forbidden)
    updater._plan_runtime_migration(state, held_fd=5)
    assert state["applying"]["runtime_migration"] == migration
    assert Path(updater.STATE_PATH).read_bytes() == journal


def _synthetic_flagless_rollback(updater, tmp_path, monkeypatch, engine):
    from contextlib import nullcontext
    from ops_testkit import _git, _load

    repo, live, back, before, after, restarts = _schema_bump_world(updater, tmp_path, monkeypatch)
    _git(repo, "reset", "-q", "--hard", before)
    state = updater.journal_store.load_state(updater.STATE_PATH)
    state["applying"] = {"tag": "rollback:v1.1.0", "sha": before, "prev_sha": after,
                         "rollback": True, "schema_bump": True, "backup_path": back,
                         "command_id": "synthetic-recovery", "at": 1}
    state["stages"] = [{"stage": "rollback", "at": 1}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    Path(updater.MARKER_PATH).write_text("synthetic writer hold")
    Path(updater.MARKER_PATH).chmod(0o600)
    if engine == "updater":
        monkeypatch.setattr(updater, "_migrate_log_tail", lambda: None)
        return updater, updater.recover_interrupted, repo, live, back, before, restarts
    recovery = _load()
    for name in ("DATA", "REPO", "LEDGER", "STATE_PATH", "UPDATE_LOCK", "RUN_LOCK",
                 "MARKER_PATH", "REPORT_PATH"):
        monkeypatch.setattr(recovery, name, getattr(updater, name))
    monkeypatch.setattr(recovery, "_settings_migration_pending", lambda: False)
    monkeypatch.setattr(recovery, "_release_scope", lambda path: nullcontext())
    monkeypatch.setattr(recovery, "_notify", lambda text: None)
    return recovery, recovery.recover, repo, live, back, before, restarts


@pytest.mark.parametrize("engine", ["updater", "watchdog"])
@pytest.mark.parametrize("failure", ["error", "os_error"])
def test_flagless_rollback_restore_failure_retains_durable_hold(
        updater, tmp_path, monkeypatch, engine, failure):
    from ops_testkit import _git

    tool, recover, repo, live, back, before, restarts = _synthetic_flagless_rollback(
        updater, tmp_path, monkeypatch, engine)
    db_bytes = Path(live).read_bytes()
    calls = []

    def fail_restore(path, on_hold):
        assert path == back
        assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["restore_incomplete"] is True
        calls.append(True)
        if failure == "os_error":
            raise OSError("synthetic restore failure")
        if engine == "updater":
            raise updater.UpdateError("synthetic restore failure")
        return "backup_unreadable: synthetic"

    def forbidden(*args, **kwargs):
        pytest.fail("failed restore must not restart, clear the marker or write a DB notice")

    monkeypatch.setattr(tool, "_restore_db", fail_restore)
    for name in (("restart_agents", "restart_services", "_remove_marker", "_enqueue_notice")
                 if engine == "updater" else ("_restart_drainers", "_restart_services", "_remove_marker")):
        monkeypatch.setattr(tool, name, forbidden)
    assert recover() == 1 and calls == [True]
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["restore_incomplete"] is True
    assert Path(updater.MARKER_PATH).read_text() == "synthetic writer hold"
    assert Path(live).read_bytes() == db_bytes and restarts == []
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before


@pytest.mark.parametrize("engine", ["updater", "watchdog"])
@pytest.mark.parametrize("outcome", ["success", "consent"])
def test_flagless_rollback_reaches_existing_restore_outcomes(
        updater, tmp_path, monkeypatch, engine, outcome):
    tool, recover, _repo, _live, back, _before, restarts = _synthetic_flagless_rollback(
        updater, tmp_path, monkeypatch, engine)
    report = {"report_id": "a" * 64, "backup_sha256": "b" * 64,
              "backup_schema": 7, "intervening_messages": 3, "external_effect_rows": 0}
    called = []

    def restore(path, on_hold):
        assert path == back
        assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["restore_incomplete"] is True
        called.append(True)
        if outcome == "consent":
            on_hold(report)
            if engine == "updater":
                raise updater.RestoreConsentPending(report)
            return "restore_consent_pending:" + report["report_id"]
        return None

    monkeypatch.setattr(tool, "_restore_db", restore)
    if engine == "updater":
        monkeypatch.setattr(updater, "_restore_runtime", lambda entry: None)
        monkeypatch.setattr(updater, "restart_services", lambda *a, **k: None)
    else:
        monkeypatch.setattr(tool, "_runtime_mode", lambda: "hermes")
        monkeypatch.setattr(tool, "_reconcile_membership", lambda snapshot: [])
        monkeypatch.setattr(tool, "_restart_drainers", lambda bounce=True: restarts.append(1) or [])
        monkeypatch.setattr(tool, "_restart_services", lambda *a, **k: 0)
    assert recover() == 0 and called == [True]
    state = updater.journal_store.load_state(updater.STATE_PATH)
    if outcome == "consent":
        assert state["applying"]["restore_incomplete"] is True
        assert state["restore_consent"]["report_id"] == report["report_id"]
        assert Path(updater.MARKER_PATH).exists() and restarts == []
    else:
        assert state["applying"] is None and restarts == [1]
        assert not Path(updater.MARKER_PATH).exists()


@pytest.mark.parametrize("engine", ["updater", "watchdog"])
def test_flagless_rollback_does_not_overwrite_changed_journal(
        updater, tmp_path, monkeypatch, engine):
    tool, recover, _repo, live, _back, _before, restarts = _synthetic_flagless_rollback(
        updater, tmp_path, monkeypatch, engine)
    db_bytes = Path(live).read_bytes()
    changed = []

    def restore(path, on_hold):
        state = updater.journal_store.load_state(updater.STATE_PATH)
        state["applying"]["command_id"] = "synthetic-other-owner"
        updater.journal_store.save_state(updater.STATE_PATH, state)
        changed.append(Path(updater.STATE_PATH).read_bytes())
        return None

    def forbidden(*args, **kwargs):
        pytest.fail("changed rollback identity must not resume or clear hold")

    monkeypatch.setattr(tool, "_restore_db", restore)
    for name in (("restart_agents", "restart_services", "_remove_marker", "_enqueue_notice")
                 if engine == "updater" else ("_restart_drainers", "_restart_services", "_remove_marker")):
        monkeypatch.setattr(tool, name, forbidden)
    assert recover() == 1 and len(changed) == 1
    assert Path(updater.STATE_PATH).read_bytes() == changed[0]
    assert Path(updater.MARKER_PATH).exists() and restarts == []
    assert Path(live).read_bytes() == db_bytes


@pytest.mark.parametrize("bad_backup", [None, "", "  ", 123, [], {}])
@pytest.mark.parametrize("entry_point", ["manual", "tree"])
def test_schema_rollback_rejects_missing_or_invalid_backup_before_reset(
        updater, tmp_path, monkeypatch, bad_backup, entry_point):
    from ops_testkit import _git

    repo, live, _back, _before, after, restarts = _schema_bump_world(updater, tmp_path, monkeypatch)
    state = updater.journal_store.load_state(updater.STATE_PATH)
    entry = state["applied"][-1]
    if bad_backup is None:
        entry.pop("backup_path")
    else:
        entry["backup_path"] = bad_backup
    updater.journal_store.save_state(updater.STATE_PATH, state)
    db_bytes = Path(live).read_bytes()
    real_git_out = updater._git_out

    def git_out(args, *a, **kw):
        if args and args[0] == "reset":
            pytest.fail("invalid backup must stop before reset")
        return real_git_out(args, *a, **kw)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid backup must not restore or restart")

    monkeypatch.setattr(updater, "_git_out", git_out)
    for name in ("_restore_db", "restart_agents", "restart_services"):
        monkeypatch.setattr(updater, name, forbidden)
    if entry_point == "manual":
        assert updater.rollback() != 0
    else:
        with pytest.raises(updater.UpdateError, match="backup_invalid"):
            updater._rollback_tree(entry)
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == after
    assert Path(live).read_bytes() == db_bytes and restarts == []


@pytest.mark.parametrize("engine", ["updater", "watchdog"])
@pytest.mark.parametrize("head_position", ["target", "previous", "unknown"])
def test_legacy_rollback_creates_hold_before_head_classification(
        updater, tmp_path, monkeypatch, engine, head_position):
    from ops_testkit import _git

    tool, recover, repo, live, back, before, restarts = _synthetic_flagless_rollback(
        updater, tmp_path, monkeypatch, engine)
    marker = Path(updater.MARKER_PATH)
    marker.unlink()
    db_bytes = Path(live).read_bytes()
    initial_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    initial = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
    head = {"target": before, "previous": initial["prev_sha"], "unknown": "c" * 40}[head_position]
    observed, restores = [], []

    def measured_head():
        state = updater.journal_store.load_state(updater.STATE_PATH)
        assert state["applying"]["restore_incomplete"] is True
        assert marker.is_file() and not marker.is_symlink()
        observed.append(True)
        return head

    def fail_restore(path, on_hold):
        assert path == back and marker.is_file()
        assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["restore_incomplete"] is True
        restores.append(True)
        if engine == "updater":
            raise updater.UpdateError("synthetic early restore failure")
        return "synthetic early restore failure"

    def forbidden(*args, **kwargs):
        pytest.fail("unverified rollback must not restart, notify, reset, or clear its marker")

    monkeypatch.setattr(tool, "_head_sha" if engine == "updater" else "_head", measured_head)
    monkeypatch.setattr(tool, "_restore_db", fail_restore)
    for name in (("restart_agents", "restart_services", "_remove_marker", "_enqueue_notice")
                 if engine == "updater" else
                 ("_restart_drainers", "_restart_services", "_remove_marker", "_notify")):
        monkeypatch.setattr(tool, name, forbidden)
    assert recover() == 1
    assert observed and restores == ([True] if head_position == "target" else [])
    assert marker.is_file() and not marker.is_symlink()
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["restore_incomplete"] is True
    assert Path(live).read_bytes() == db_bytes and restarts == []
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == initial_head
    report = json.loads(Path(updater.REPORT_PATH).read_text())
    assert report.get("writers_stopped") is not True


@pytest.mark.parametrize("engine", ["updater", "watchdog"])
@pytest.mark.parametrize("bad_backup", [None, "", "  ", 123, [], {}])
def test_legacy_schema_rollback_invalid_backup_preserves_source(
        updater, tmp_path, monkeypatch, engine, bad_backup):
    from ops_testkit import _git

    tool, recover, repo, live, _back, _before, restarts = _synthetic_flagless_rollback(
        updater, tmp_path, monkeypatch, engine)
    state = updater.journal_store.load_state(updater.STATE_PATH)
    if bad_backup is None:
        state["applying"].pop("backup_path")
    else:
        state["applying"]["backup_path"] = bad_backup
    updater.journal_store.save_state(updater.STATE_PATH, state)
    Path(updater.MARKER_PATH).unlink()
    db_bytes = Path(live).read_bytes()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    def forbidden(*args, **kwargs):
        pytest.fail("invalid backup must not restore, restart, notify, or classify HEAD")

    for name in (("_head_sha", "_restore_db", "restart_agents", "restart_services", "_enqueue_notice")
                 if engine == "updater" else
                 ("_head", "_restore_db", "_restart_drainers", "_restart_services", "_notify")):
        monkeypatch.setattr(tool, name, forbidden)
    assert recover() == 1
    assert Path(live).read_bytes() == db_bytes and restarts == []
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["schema_bump"] is True
    assert json.loads(Path(updater.REPORT_PATH).read_text()).get("writers_stopped") is not True


@pytest.mark.parametrize("engine", ["updater", "watchdog"])
def test_legacy_rollback_refuses_symlink_marker_without_following_it(
        updater, tmp_path, monkeypatch, engine):
    from ops_testkit import _git

    tool, recover, repo, live, _back, _before, restarts = _synthetic_flagless_rollback(
        updater, tmp_path, monkeypatch, engine)
    marker = Path(updater.MARKER_PATH)
    marker.unlink()
    target = tmp_path / "synthetic-marker-target"
    target.write_text("unchanged synthetic target")
    marker.symlink_to(target)
    journal = Path(updater.STATE_PATH).read_bytes()
    db_bytes = Path(live).read_bytes()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe marker must stop before restore, restart, or notification")

    for name in (("_head_sha", "_restore_db", "restart_agents", "restart_services", "_enqueue_notice", "_remove_marker")
                 if engine == "updater" else
                 ("_head", "_restore_db", "_restart_drainers", "_restart_services", "_notify", "_remove_marker")):
        monkeypatch.setattr(tool, name, forbidden)
    assert recover() == 1
    assert marker.is_symlink() and target.read_text() == "unchanged synthetic target"
    assert Path(updater.STATE_PATH).read_bytes() == journal
    assert Path(live).read_bytes() == db_bytes and restarts == []
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert json.loads(Path(updater.REPORT_PATH).read_text()).get("writers_stopped") is not True


@pytest.mark.parametrize("change", ["callback_command", "callback_manifest", "return_identity", "return_staging"])
def test_manual_rollback_preserves_changed_restore_journal(
        updater, tmp_path, monkeypatch, change):
    from ops_testkit import _git

    repo, live, back, before, _after, restarts = _schema_bump_world(updater, tmp_path, monkeypatch)
    db_bytes = Path(live).read_bytes()
    saved = []
    report = {"report_id": "a" * 64, "backup_sha256": "b" * 64,
              "backup_schema": 7, "intervening_messages": 3, "external_effect_rows": 0}

    def mutate_journal(path, on_hold):
        assert path == back and _git(repo, "rev-parse", "HEAD").stdout.strip() == before
        state = updater.journal_store.load_state(updater.STATE_PATH)
        applying = state["applying"]
        assert applying["restore_incomplete"] is True
        if change == "callback_manifest":
            applying["manifest_snapshot"] = {"synthetic": "changed"}
        elif change == "return_staging":
            applying["restore_staging"] = {"name": ".restore.synthetic.db", "dev": 1, "ino": 2, "uid": 3}
        else:
            applying["command_id"] = "synthetic-changed-command"
        updater.journal_store.save_state(updater.STATE_PATH, state)
        saved.append(Path(updater.STATE_PATH).read_bytes())
        if change.startswith("callback_"):
            on_hold(report)

    def forbidden(*args, **kwargs):
        pytest.fail("changed restore journal must not cancel notices, alter services, restart or clear marker")

    monkeypatch.setattr(updater, "_restore_db", mutate_journal)
    for name in ("_cancel_unsent_notices", "_services_reconcile", "restart_agents", "restart_services",
                 "_enqueue_notice", "_remove_marker"):
        monkeypatch.setattr(updater, name, forbidden)
    monkeypatch.setattr(service_management, "retire_to_snapshot", forbidden)
    assert updater.rollback("synthetic-original-command") != 0
    assert len(saved) == 1 and Path(updater.STATE_PATH).read_bytes() == saved[0]
    assert Path(updater.MARKER_PATH).exists() and restarts == []
    assert Path(live).read_bytes() == db_bytes
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before


@pytest.mark.parametrize("engine", ["updater", "watchdog"])
def test_legacy_rollback_refuses_fifo_marker_without_blocking(
        updater, tmp_path, monkeypatch, engine):
    import os

    tool, recover, _repo, live, _back, _before, restarts = _synthetic_flagless_rollback(
        updater, tmp_path, monkeypatch, engine)
    marker = Path(updater.MARKER_PATH)
    marker.unlink()
    os.mkfifo(marker)
    journal = Path(updater.STATE_PATH).read_bytes()
    db_bytes = Path(live).read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("non-regular marker must stop recovery")

    for name in (("_restore_db", "restart_agents", "_remove_marker")
                 if engine == "updater" else
                 ("_restore_db", "_restart_drainers", "_remove_marker")):
        monkeypatch.setattr(tool, name, forbidden)
    assert recover() == 1
    assert marker.exists() and Path(updater.STATE_PATH).read_bytes() == journal
    assert Path(live).read_bytes() == db_bytes and restarts == []


@pytest.mark.parametrize("change", ["missing_applying", "identity_changed"])
def test_manual_rollback_checks_frozen_journal_before_reset(updater, tmp_path, monkeypatch, change):
    from ops_testkit import _git

    repo, live, _back, _before, after, restarts = _schema_bump_world(updater, tmp_path, monkeypatch)
    db_bytes = Path(live).read_bytes()
    saved = []
    freeze = updater._freeze_schema_rollback
    git_out = updater._git_out

    def mutate_after_freeze(state, entry):
        result = freeze(state, entry)
        current = updater.journal_store.load_state(updater.STATE_PATH)
        assert current["applying"]["restore_incomplete"] is True
        if change == "missing_applying":
            current["applying"] = None
        else:
            current["applying"]["command_id"] = "synthetic-other-command"
        updater.journal_store.save_state(updater.STATE_PATH, current)
        saved.append(Path(updater.STATE_PATH).read_bytes())
        return result

    def forbid_reset(args, *a, **kw):
        if args and args[0] == "reset":
            pytest.fail("changed frozen journal must stop before git reset")
        return git_out(args, *a, **kw)

    def forbidden(*args, **kwargs):
        pytest.fail("changed frozen journal must not restore, notify, restart or clear marker")

    monkeypatch.setattr(updater, "_freeze_schema_rollback", mutate_after_freeze)
    monkeypatch.setattr(updater, "_git_out", forbid_reset)
    for name in ("_restore_db", "_cancel_unsent_notices", "_services_reconcile", "restart_agents",
                 "restart_services", "_enqueue_notice", "_remove_marker"):
        monkeypatch.setattr(updater, name, forbidden)
    monkeypatch.setattr(service_management, "retire_to_snapshot", forbidden)
    assert updater.rollback("synthetic-original-command") != 0
    assert len(saved) == 1 and Path(updater.STATE_PATH).read_bytes() == saved[0]
    assert Path(updater.MARKER_PATH).exists() and restarts == []
    assert Path(live).read_bytes() == db_bytes
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == after
