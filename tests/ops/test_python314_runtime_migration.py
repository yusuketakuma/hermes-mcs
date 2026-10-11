"""Python 3.14 runtime migration in the updater — synthetic venvs and repos only."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

import mcs_update
from test_mcs_update import updater as updater  # noqa: F401  (fixture)

OLD = (3, 13, 9)


@pytest.fixture(autouse=True)
def _no_leaked_services_python(monkeypatch):
    """_use_migrated_runtime sets module state; keep it per test."""
    monkeypatch.setattr(mcs_update, "_SERVICES_PYTHON", None)


def _state(updater, **applying):
    state = updater._default_state()
    state["applying"] = {"tag": "v1.0.18", "sha": "b" * 40, "prev_sha": "a" * 40,
                         "reinstall": False, "install_args": [], **applying}
    updater.save_state(state)
    return updater.load_state()


def _venv(root, version_ok=True):
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python3"
    python.write_text("#!/bin/sh\necho %s\n" % ("3 14" if version_ok else "3 13"))
    python.chmod(0o700)
    return venv


@pytest.fixture
def old_interpreter(updater, monkeypatch, tmp_path):
    venv = _venv(tmp_path / "old-runtime", version_ok=False)
    monkeypatch.setattr(sys, "version_info", OLD)
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), None))
    return venv


@pytest.mark.parametrize("runtime", ["current", "unrunnable"])
def test_current_or_unknown_runtime_is_not_migrated(updater, monkeypatch, tmp_path, runtime):
    venv = _venv(tmp_path)
    if runtime == "unrunnable":
        (venv / "bin" / "python3").write_text("")
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), None))
    state = _state(updater)
    updater._plan_runtime_migration(state)
    assert "runtime_migration" not in state["applying"]


def test_old_parent_without_inherited_lock_rolls_back_instead(updater, old_interpreter, monkeypatch):
    monkeypatch.delenv(updater.RUN_LOCK_FD_ENV, raising=False)
    state = _state(updater)
    with pytest.raises(updater.UpdateError, match="runtime_migration_requires_upgrade_tool"):
        updater._plan_runtime_migration(state)
    assert "runtime_migration" not in updater.load_state()["applying"]


def test_migration_is_journaled_before_install(updater, old_interpreter, monkeypatch):
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "7")
    (old_interpreter.parent / "venv.pre-python314").mkdir()
    state = _state(updater, install_args=["--no-brew", "--no-llm"])
    updater._plan_runtime_migration(state)
    saved = updater.load_state()["applying"]
    migration = saved["runtime_migration"]
    assert migration["venv"] == str(old_interpreter)
    assert migration["backup_path"] == str(old_interpreter) + ".pre-python314.1"
    assert migration["updater_python"] == "3.13.9" and "checkout" not in migration
    assert saved["reinstall"] is True
    assert saved["install_args"] == ["--no-brew", "--no-llm", "--no-plugin"]
    assert updater.load_state()["stages"][-1]["stage"] == "runtime_migration"


def _checkout(path):
    path.mkdir(parents=True)
    git = ["git", "-C", str(path), "-c", "user.email=f@example.invalid", "-c", "user.name=F"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    heads = []
    for n in range(2):
        (path / "f").write_text(str(n))
        subprocess.run(git + ["add", "f"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", f"fictional {n}"], check=True)
        heads.append(subprocess.run(git[:3] + ["rev-parse", "HEAD"], check=True,
                                    capture_output=True, text=True).stdout.strip())
    subprocess.run(git[:3] + ["checkout", "-q", "--detach", heads[0]], check=True)
    return heads


def test_hermes_checkout_head_is_recorded_and_restored(updater, tmp_path, monkeypatch):
    checkout = tmp_path / "hermes-agent"
    old_head, new_head = _checkout(checkout)
    venv = _venv(checkout, version_ok=False)
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "7")
    state = _state(updater)
    updater._plan_runtime_migration(state)
    migration = state["applying"]["runtime_migration"]
    assert migration["checkout_head"] == old_head
    # install.sh moved the venv aside, rebuilt it and advanced the checkout
    os.rename(venv, migration["backup_path"])
    _venv(checkout)
    (venv / "marker-new").write_text("new")
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", new_head], check=True)
    updater._restore_runtime(state["applying"])
    assert not (venv / "marker-new").exists() and venv.is_dir()
    assert (checkout / "venv.python314-aside" / "marker-new").exists()
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert head == old_head


def test_restore_without_a_backup_changes_nothing(updater, tmp_path):
    venv = _venv(tmp_path)
    updater._restore_runtime({"runtime_migration": {
        "venv": str(venv), "backup_path": str(tmp_path / "missing")}})
    assert venv.is_dir() and not list(tmp_path.glob("venv.failed-*"))


def test_reinstall_hands_lock_and_backup_to_install_sh(updater, monkeypatch, tmp_path):
    calls = []

    class Child:
        returncode = 0

        def communicate(self, timeout=None):
            return "", None

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Child()

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "9")
    backup = str(tmp_path / "venv.pre-python314")
    applying = {"reinstall": True, "install_args": ["--no-llm", "--no-plugin"],
                "runtime_migration": {"venv": str(tmp_path / "venv"), "backup_path": backup}}
    updater._reinstall(applying)
    argv, kwargs = calls[0]
    assert argv[-3:] == ["--no-llm", "--no-plugin", "--no-services"]
    assert kwargs["pass_fds"] == (9,)
    assert kwargs["env"][updater.RUNTIME_BACKUP_ENV] == backup
    assert kwargs["env"][updater.RUN_LOCK_FD_ENV] == "9"
    # a plain operator reinstall never carries the migration permission
    calls.clear()
    updater._reinstall({"reinstall": True, "install_args": []})
    assert calls[0][1]["pass_fds"] == ()
    assert updater.RUN_LOCK_FD_ENV not in calls[0][1]["env"]


@pytest.mark.parametrize("version_ok, backup", [(True, True), (False, True), (True, False)])
def test_services_use_the_rebuilt_runtime_only_when_verified(
        updater, monkeypatch, tmp_path, version_ok, backup):
    venv = _venv(tmp_path, version_ok)
    if backup:
        (tmp_path / "venv.pre-python314").mkdir()
    monkeypatch.setattr(updater, "_SERVICES_PYTHON", None)
    migration = {"venv": str(venv), "backup_path": str(tmp_path / "venv.pre-python314")}
    if version_ok and backup:
        updater._use_migrated_runtime(migration)
        seen = []
        monkeypatch.setattr(updater, "_run_owned", lambda argv, *a, **k:
                            seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
        updater._services_reconcile()
        assert seen[0][0] == str(venv / "bin" / "python3")
    else:
        with pytest.raises(updater.UpdateError, match="runtime_migration_unverified"):
            updater._use_migrated_runtime(migration)
        assert updater._SERVICES_PYTHON is None


def test_post_merge_child_inherits_the_held_run_lock(updater, monkeypatch, tmp_path):
    seen = {}

    class Child:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def popen(argv, **kwargs):
        seen.update(kwargs)
        return Child()

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setattr(updater, "_HELD_RUN_LOCK_FD", 11)
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "99")   # a stale value never leaks
    updater._run_post_merge("b" * 40)
    assert seen["pass_fds"] == (11,) and seen["env"][updater.RUN_LOCK_FD_ENV] == "11"
    monkeypatch.setattr(updater, "_HELD_RUN_LOCK_FD", None)
    updater._run_post_merge("b" * 40)
    assert seen["pass_fds"] == () and updater.RUN_LOCK_FD_ENV not in seen["env"]


def test_post_merge_migrates_then_reconciles_with_new_python(updater, old_interpreter, monkeypatch):
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "7")
    monkeypatch.setattr(updater, "_SERVICES_PYTHON", None)
    order = []

    def reinstall(applying):
        migration = applying["runtime_migration"]
        os.rename(migration["venv"], migration["backup_path"])
        _venv(Path(migration["venv"]).parent)
        applying["reinstall_done"] = True
        order.append("reinstall")

    monkeypatch.setattr(updater, "_reinstall", reinstall)
    monkeypatch.setattr(updater, "_services_reconcile",
                        lambda: order.append(("services", updater._SERVICES_PYTHON)))
    monkeypatch.setattr(updater, "restart_agents", lambda: order.append("restart") or [])
    monkeypatch.setattr(updater, "_postcheck", lambda state, sha: [])
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: True)
    assert updater._post_merge(_state(updater)) == 0
    assert order == ["reinstall", ("services", str(old_interpreter / "bin" / "python3")), "restart"]
    applied = updater.load_state()["applied"][-1]
    assert applied["reinstall_done"] is True
    # a later explicit rollback can still return to the pre-migration runtime
    assert applied["runtime_migration"]["venv"] == str(old_interpreter)
    updater._restore_runtime(applied)
    assert (old_interpreter / "bin" / "python3").read_text().endswith("echo 3 13\n")
    assert (old_interpreter.parent / "venv.python314-aside").is_dir()


def test_module_constants_match_the_installer_contract():
    assert mcs_update.RUNTIME_MIN == (3, 14)
    assert mcs_update.RUN_LOCK_FD_ENV == "MCS_UPDATE_RUN_LOCK_FD"
    assert mcs_update.RUNTIME_BACKUP_ENV == "MCS_UPDATE_RUNTIME_BACKUP"


@pytest.mark.parametrize("case", ["eligible", "unknown", "branch", "dirty", "current_python"])
def test_precheck_reports_unmovable_hermes_checkout_before_merge(updater, tmp_path, monkeypatch, case):
    checkout = tmp_path / "hermes-agent"
    old_head, _ = _checkout(checkout)
    venv = _venv(checkout, version_ok=case == "current_python")
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    if case != "unknown":
        monkeypatch.setattr(updater, "HERMES_PREVIOUS_PINS", frozenset({old_head}))
    if case == "branch":
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "-b", "local"], check=True)
    if case == "dirty":
        (checkout / "f").write_text("local edit")
    expected = {"eligible": None, "current_python": None,
                "unknown": "hermes_checkout_unknown_commit",
                "branch": "hermes_checkout_branch_or_dirty",
                "dirty": "hermes_checkout_branch_or_dirty"}[case]
    assert updater._migration_checkout_problem({}) == expected


def test_standalone_venv_has_no_checkout_to_move(updater, old_interpreter):
    assert updater._migration_checkout_problem({}) is None


def test_runtime_venv_follows_the_selected_service_interpreter(updater, tmp_path, monkeypatch):
    import mcs_setup
    checkout = tmp_path / "hermes-agent"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(mcs_setup, "HERMES_PY", str(checkout / "venv/bin/python"))
    assert updater._runtime_venv({}) == (str(checkout / "venv"), str(checkout))
    # the standalone venv's parent is the MCS checkout, never a Hermes checkout
    monkeypatch.setattr(mcs_setup.mcs_runtime, "python_executable",
                        lambda cfg, root: str(tmp_path / "mcs/venv/bin/python3"))
    assert updater._runtime_venv({"runtime_mode": "standalone"}) == (str(tmp_path / "mcs/venv"), None)


def test_parent_decision_needs_no_environment_and_lets_child_skip(updater, old_interpreter, monkeypatch):
    monkeypatch.delenv(updater.RUN_LOCK_FD_ENV, raising=False)
    state = _state(updater)
    updater._plan_runtime_migration(state, held_fd=5)        # the apply parent
    assert updater.load_state()["applying"]["reinstall"] is True
    child = updater.load_state()
    updater._plan_runtime_migration(child)                   # no FD env: already decided
    assert child["applying"]["runtime_migration"] == state["applying"]["runtime_migration"]


def test_checkout_is_restored_even_when_install_failed_before_the_venv_move(updater, tmp_path):
    checkout = tmp_path / "hermes-agent"
    old_head, new_head = _checkout(checkout)
    venv = _venv(checkout, version_ok=False)
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", new_head], check=True)
    updater._restore_runtime({"runtime_migration": {
        "venv": str(venv), "backup_path": str(checkout / "venv.pre-python314"),
        "checkout": str(checkout), "checkout_head": old_head}})
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert head == old_head and (venv / "bin" / "python3").read_text().endswith("echo 3 13\n")


def test_rollback_record_carries_migration_only_when_present(updater):
    plain = updater._rollback_applying({"prev_sha": "a" * 40, "tag": "v1"}, "b" * 40, None)
    assert "runtime_migration" not in plain
    migration = {"venv": "/x/venv", "backup_path": "/x/venv.pre-python314"}
    carried = updater._rollback_applying(
        {"prev_sha": "a" * 40, "tag": "v1", "runtime_migration": migration}, "b" * 40, None)
    assert carried["runtime_migration"] == migration


def test_linked_worktree_checkout_head_is_recorded_too(updater, tmp_path, monkeypatch):
    import mcs_setup
    main = tmp_path / "main"
    old_head, _ = _checkout(main)
    checkout = tmp_path / "hermes-agent"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "--detach",
                    str(checkout), old_head], check=True)
    assert (checkout / ".git").is_file()
    venv = _venv(checkout, version_ok=False)
    monkeypatch.setattr(mcs_setup, "HERMES_PY", str(venv / "bin/python3"))
    assert updater._runtime_venv({}) == (str(venv), str(checkout))
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "7")
    state = _state(updater)
    updater._plan_runtime_migration(state)
    assert state["applying"]["runtime_migration"]["checkout_head"] == old_head


def test_checkout_already_at_the_current_pin_with_an_old_venv_is_accepted(updater, tmp_path, monkeypatch):
    checkout = tmp_path / "hermes-agent"
    old_head, _ = _checkout(checkout)
    venv = _venv(checkout, version_ok=False)
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    monkeypatch.setattr(updater, "HERMES_PREVIOUS_PINS", frozenset())
    monkeypatch.setattr(updater, "HERMES_PIN", old_head)
    assert updater._migration_checkout_problem({}) is None
    (checkout / "f").write_text("local edit")      # dirty stays refused at the pin too
    assert updater._migration_checkout_problem({}) == "hermes_checkout_branch_or_dirty"


def _interrupted(updater, tmp_path, monkeypatch, *, reset=False, rollback_shaped=False):
    import time
    from ops_testkit import _git, _make_repo
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    prev = _git(repo, "rev-list", "-n1", "v1.0.0").stdout.strip()
    target = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    assert prev != target
    # merged (HEAD=target) then crashed; or already reset back to prev
    _git(repo, "reset", "-q", "--hard", prev if reset else target)
    runtime = tmp_path / "rt"
    old = _venv(runtime, version_ok=False)
    os.rename(old, str(old) + ".pre-python314")             # install.sh moved it ...
    _venv(runtime)                                          # ... and half-built the new one
    order, notices = [], []
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(updater, "_services_reconcile", lambda: order.append("services"))
    monkeypatch.setattr(updater, "_reconcile_membership", lambda snapshot: [])
    monkeypatch.setattr(updater, "_postcheck", lambda state, sha: [])
    monkeypatch.setattr(updater, "quiesce", lambda: order.append("quiesce") or [])
    monkeypatch.setattr(updater, "restart_agents", lambda bounce=True: order.append("restart") or [])
    restore = updater._restore_runtime
    monkeypatch.setattr(updater, "_restore_runtime",
                        lambda entry: order.append("runtime") or restore(entry))
    git_out = updater._git_out
    monkeypatch.setattr(updater, "_git_out", lambda args, *a, **k: (
        order.append("reset") if args[:1] == ["reset"] else None) or git_out(args, *a, **k))
    applying = {"tag": "v1.1.0", "sha": target, "prev_sha": prev, "reinstall": True,
                "reinstall_done": False, "command_id": "cid-mig",
                "runtime_migration": {"venv": str(old), "backup_path": str(old) + ".pre-python314"},
                "at": time.time() - 4000}
    state = updater._default_state()
    state["applying"] = (updater._rollback_applying(applying, target, "cid-mig")
                         if rollback_shaped else applying)
    state["stages"] = [{"stage": "rollback" if rollback_shaped else "post_merge",
                        "at": time.time() - 3000}]
    updater.save_state(state)
    return repo, prev, old, runtime, order, notices


def _assert_undone(updater, repo, prev, old, runtime, order, notices):
    from ops_testkit import _git
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev
    assert (old / "bin/python3").read_text().endswith("echo 3 13\n")
    assert (runtime / "venv.python314-aside").is_dir()
    after = updater.load_state()
    assert after["applying"] is None and after["executed"]["cid-mig"]["result"] == "rolled_back"
    assert "restart" in order


def test_interrupted_migration_is_quiesced_then_undone_not_rerun(updater, tmp_path, monkeypatch):
    repo, prev, old, runtime, order, notices = _interrupted(updater, tmp_path, monkeypatch)
    assert updater.recover_interrupted() == 0
    _assert_undone(updater, repo, prev, old, runtime, order, notices)
    # the old tree must never come back before the runtime it needs
    assert order.index("quiesce") < order.index("runtime") < order.index("reset") \
        < order.index("restart")
    assert any("mcs_upgrade.py" in text for text in notices)


def test_undo_resumes_when_interrupted_after_the_reset(updater, tmp_path, monkeypatch):
    # the rollback-shaped journal was written and the tree reset, then a crash
    repo, prev, old, runtime, order, notices = _interrupted(
        updater, tmp_path, monkeypatch, reset=True, rollback_shaped=True)
    assert updater.recover_interrupted() == 0
    _assert_undone(updater, repo, prev, old, runtime, order, notices)
    assert order.index("quiesce") < order.index("runtime")


def test_undo_resumes_when_interrupted_before_the_reset(updater, tmp_path, monkeypatch):
    repo, prev, old, runtime, order, notices = _interrupted(
        updater, tmp_path, monkeypatch, rollback_shaped=True)
    assert updater.recover_interrupted() == 0
    _assert_undone(updater, repo, prev, old, runtime, order, notices)
    assert order.index("quiesce") < order.index("runtime")


def test_undo_waiting_for_restore_consent_holds_with_drainers_stopped(updater, tmp_path, monkeypatch):
    repo, prev, old, runtime, order, notices = _interrupted(updater, tmp_path, monkeypatch)
    held = []

    def needs_consent(entry, on_hold=None):
        raise updater.RestoreConsentPending({"report_id": "synthetic-report"})

    monkeypatch.setattr(updater, "_rollback_tree", needs_consent)
    monkeypatch.setattr(updater, "_consent_hold", lambda state, e, path: held.append(e) or "rid")
    assert updater.recover_interrupted() == 0
    assert held and "restart" not in order and not notices
    after = updater.load_state()
    assert after["applying"]["rollback"] is True
    assert after["applying"]["runtime_migration"]["venv"] == str(old)


def test_symlinked_venv_is_restored_as_the_same_link(updater, tmp_path):
    real = _venv(tmp_path / "external", version_ok=False)
    venv = tmp_path / "rt" / "venv"
    venv.parent.mkdir()
    venv.symlink_to(real)
    backup = tmp_path / "rt" / "venv.pre-python314"
    os.rename(venv, backup)                     # what install.sh's mv does
    _venv(tmp_path / "rt")                      # rebuilt as a real directory
    migration = {"venv": str(venv), "backup_path": str(backup)}
    updater._use_migrated_runtime(migration)    # the link counts as the backup
    updater._restore_runtime({"runtime_migration": migration})
    assert venv.is_symlink() and os.readlink(venv) == str(real)
    assert (real / "bin/python3").read_text().endswith("echo 3 13\n")
    assert (tmp_path / "rt" / "venv.python314-aside").is_dir()
