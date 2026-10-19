"""Recovery retains interrupted state until workers actually restart."""
import restore_evidence
import mcs_util
import service_management
import update_discovery
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
    assert notices and restarts == [1]
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
    assert updater.recover_interrupted() == 0
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"] is None and restarts == [1]


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
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["restore_incomplete"] is True
    assert Path(live).read_bytes() == damaged
    assert not Path(updater.DATA, "restore_pending.json").exists()

    # once the backup is usable and the restore approved, the hold ends
    backup_broken.clear()
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 0
    assert restarts == [1] and not Path(updater.MARKER_PATH).exists()
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"] is None


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
        assert after["executed"]["synthetic-held"]["result"] == "escalated"
        assert Path(updater.MARKER_PATH).read_text() == "synthetic writer freeze"
        report = json.loads(Path(updater.REPORT_PATH).read_text())
        assert report["writers_stopped"] is True
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
