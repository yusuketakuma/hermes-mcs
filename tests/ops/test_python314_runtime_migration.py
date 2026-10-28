"""Python 3.14 runtime migration in the updater — synthetic venvs and repos only."""
from ops_testkit import select_updater_service_python
import service_management
import owned_process
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
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.0.18", "sha": "b" * 40, "prev_sha": "a" * 40,
                         "reinstall": False, "install_args": [],
                         "install_options_base": updater._install_options_snapshot()[0], **applying}
    updater.journal_store.save_state(updater.STATE_PATH, state)
    return updater.journal_store.load_state(updater.STATE_PATH)


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
    assert "runtime_migration" not in updater.journal_store.load_state(updater.STATE_PATH)["applying"]


def test_migration_is_journaled_before_install(updater, old_interpreter, monkeypatch):
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "7")
    (old_interpreter.parent / "venv.pre-python314").mkdir()
    state = _state(updater, install_args=["--no-brew", "--no-llm"])
    updater._plan_runtime_migration(state)
    saved = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
    migration = saved["runtime_migration"]
    assert migration["venv"] == str(old_interpreter)
    assert migration["backup_path"] == str(old_interpreter) + ".pre-python314.1"
    assert migration["updater_python"] == "3.13.9" and "checkout" not in migration
    assert saved["reinstall"] is True
    assert saved["install_args"] == ["--no-brew", "--no-llm", "--no-plugin"]
    assert updater.journal_store.load_state(updater.STATE_PATH)["stages"][-1]["stage"] == "runtime_migration"


def test_old_journal_missing_options_base_stops_before_replanning(updater, old_interpreter, monkeypatch):
    state = _state(updater, reinstall=True, install_options_base=None)
    before = Path(updater.STATE_PATH).read_bytes()
    monkeypatch.setattr(updater, '_plan_runtime_migration', lambda *_: pytest.fail('journal changed before check'))
    with pytest.raises(updater.UpdateError, match='install_options_unverified'):
        updater._post_merge(state)
    assert Path(updater.STATE_PATH).read_bytes() == before


def test_new_runtime_migration_requires_options_base_before_journaling(updater, old_interpreter, monkeypatch):
    state = _state(updater, install_options_base=None)
    before = Path(updater.STATE_PATH).read_bytes()
    with pytest.raises(updater.UpdateError, match='install_options_unverified'):
        updater._plan_runtime_migration(state, held_fd=5)
    assert Path(updater.STATE_PATH).read_bytes() == before
    assert not Path(str(old_interpreter) + '.pre-python314').exists()


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
    monkeypatch.setattr(updater, "HERMES_PIN", new_head)
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "7")
    state = _state(updater)
    updater._plan_runtime_migration(state)
    migration = state["applying"]["runtime_migration"]
    assert migration["checkout_head"] == old_head
    assert migration["checkout_target"] == new_head
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
    import fcntl
    calls = []

    class Child:
        returncode = 0

        def communicate(self, timeout=None):
            return "", None

    def popen(argv, **kwargs):
        inherited_update = int(kwargs['env'][updater.UPDATE_LOCK_FD_ENV])
        assert inherited_update in kwargs['pass_fds']
        assert os.fstat(inherited_update) == os.fstat(update_fd)
        if updater.RUN_LOCK_FD_ENV in kwargs['env']:
            inherited_run = int(kwargs['env'][updater.RUN_LOCK_FD_ENV])
            assert inherited_run in kwargs['pass_fds']
            assert os.fstat(inherited_run) == os.fstat(lock.fileno())
        calls.append((argv, kwargs))
        return Child()

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    backup = str(tmp_path / "venv.pre-python314")
    baseline = updater._install_options_snapshot()[0]
    applying = {"reinstall": True, "install_args": ["--no-llm", "--no-plugin"],
                "install_options_base": baseline,
                "runtime_migration": {"venv": str(tmp_path / "venv"), "backup_path": backup}}
    update_fd = os.open(updater.UPDATE_LOCK, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(update_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setenv(updater.UPDATE_LOCK_FD_ENV, str(update_fd))
        with (tmp_path / 'run.lock').open('w+b') as lock:
            monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, str(lock.fileno()))
            updater._reinstall(applying)
            os.fstat(lock.fileno())  # The child borrows a copy; the parent keeps its descriptor.
        with pytest.raises(OSError):
            os.fstat(calls[0][1]['pass_fds'][0])
        argv, kwargs = calls[0]
        assert argv[-3:] == ["--no-llm", "--no-plugin", "--no-services"]
        assert kwargs["env"][updater.RUNTIME_BACKUP_ENV] == backup
        assert kwargs["env"]["MCS_UPDATE_OPTIONS_BASE"] == baseline
        # A plain reinstall still inherits update.lock, but not the run lock.
        calls.clear()
        updater._reinstall({"reinstall": True, "install_args": [], "install_options_base": baseline})
        assert len(calls[0][1]["pass_fds"]) == 1
        assert updater.RUN_LOCK_FD_ENV not in calls[0][1]["env"]
    finally:
        os.close(update_fd)


def test_reinstall_rejects_unknown_update_lock_before_child(updater, monkeypatch):
    baseline = updater._install_options_snapshot()[0]
    monkeypatch.delenv(updater.UPDATE_LOCK_FD_ENV, raising=False)
    monkeypatch.setattr(updater.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('unexpected child'))
    with pytest.raises(updater.UpdateError, match='排他制御'):
        updater._reinstall({'reinstall': True, 'install_options_base': baseline})


@pytest.mark.parametrize('old_journal', [False, True])
def test_reinstall_stops_before_child_when_options_changed_or_not_recorded(updater, monkeypatch, old_journal):
    import mcs_util
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"install_args": []}')
    baseline = updater._install_options_snapshot()[0]
    config.write_text('{"install_args": ["--no-llm"]}')
    applying = {'reinstall': True, 'install_args': ['--no-plugin']}
    if not old_journal:
        applying['install_options_base'] = baseline
    monkeypatch.setattr(updater.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('unexpected install'))
    with pytest.raises(updater.UpdateError, match='install_options_unverified'):
        updater._reinstall(applying)
    assert config.read_text() == '{"install_args": ["--no-llm"]}'


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
        monkeypatch.setattr(owned_process, 'run_owned', lambda argv, *a, **k:
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
        if kwargs['pass_fds']:
            inherited, = kwargs['pass_fds']
            assert inherited != lock.fileno()
            assert kwargs['env'][updater.RUN_LOCK_FD_ENV] == str(inherited)
            assert os.fstat(inherited) == os.fstat(lock.fileno())
        seen.update(kwargs)
        return Child()

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "99")   # a stale value never leaks
    with (tmp_path / 'run.lock').open('w+b') as lock:
        updater._run_post_merge("b" * 40, run_lock_fd=lock.fileno())
        os.fstat(lock.fileno())
    with pytest.raises(OSError):
        os.fstat(seen['pass_fds'][0])
    updater._run_post_merge("b" * 40, run_lock_fd=None)
    assert seen["pass_fds"] == () and updater.RUN_LOCK_FD_ENV not in seen["env"]


def test_post_merge_child_inherits_both_parent_locks(updater, monkeypatch, tmp_path):
    seen = {}

    class Child:
        returncode = 0
        def communicate(self, timeout=None):
            return '', ''

    def popen(_argv, **kwargs):
        inherited_update = int(kwargs['env'][updater.UPDATE_LOCK_FD_ENV])
        inherited_run = int(kwargs['env'][updater.RUN_LOCK_FD_ENV])
        assert os.fstat(inherited_update) == os.fstat(update.fileno())
        assert os.fstat(inherited_run) == os.fstat(run.fileno())
        seen.update(kwargs)
        return Child()

    monkeypatch.setattr(updater.subprocess, 'Popen', popen)
    monkeypatch.setenv('MCS_INSTALL_UPDATE_LOCK_FD', '99')
    with (tmp_path / 'update.lock').open('w+b') as update, (tmp_path / 'run.lock').open('w+b') as run:
        token = updater._POST_MERGE_UPDATE_FD.set(update.fileno())
        try:
            updater._run_post_merge('b' * 40, run_lock_fd=run.fileno())
        finally:
            updater._POST_MERGE_UPDATE_FD.reset(token)
    assert len(seen['pass_fds']) == 2
    assert 'MCS_INSTALL_UPDATE_LOCK_FD' not in seen['env']


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
    applied = updater.journal_store.load_state(updater.STATE_PATH)["applied"][-1]
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
    select_updater_service_python(monkeypatch, updater, str(checkout / 'venv/bin/python'))
    assert updater._runtime_venv({}) == (str(checkout / "venv"), str(checkout))
    # the standalone venv's parent is the MCS checkout, never a Hermes checkout
    monkeypatch.setattr(updater, "HOME", str(tmp_path / "mcs"))
    assert updater._runtime_venv({"runtime_mode": "standalone"}) == (str(tmp_path / "mcs/venv"), None)


def test_parent_decision_needs_no_environment_and_lets_child_skip(updater, old_interpreter, monkeypatch):
    monkeypatch.delenv(updater.RUN_LOCK_FD_ENV, raising=False)
    state = _state(updater)
    updater._plan_runtime_migration(state, held_fd=5)        # the apply parent
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["reinstall"] is True
    child = updater.journal_store.load_state(updater.STATE_PATH)
    updater._plan_runtime_migration(child)                   # no FD env: already decided
    assert child["applying"]["runtime_migration"] == state["applying"]["runtime_migration"]


def test_checkout_is_restored_even_when_install_failed_before_the_venv_move(updater, tmp_path):
    checkout = tmp_path / "hermes-agent"
    old_head, new_head = _checkout(checkout)
    venv = _venv(checkout, version_ok=False)
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", new_head], check=True)
    updater._restore_runtime({"runtime_migration": {
        "venv": str(venv), "backup_path": str(checkout / "venv.pre-python314"),
        "checkout": str(checkout), "checkout_head": old_head, "checkout_target": new_head}})
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
    select_updater_service_python(monkeypatch, updater, str(venv / 'bin/python3'))
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
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda snapshot, **_service_kwargs: [])
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
    state = updater.journal_store.default_state()
    state["applying"] = (updater._rollback_applying(applying, target, "cid-mig")
                         if rollback_shaped else applying)
    state["stages"] = [{"stage": "rollback" if rollback_shaped else "post_merge",
                        "at": time.time() - 3000}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    return repo, prev, old, runtime, order, notices


def _assert_undone(updater, repo, prev, old, runtime, order, notices):
    from ops_testkit import _git
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev
    assert (old / "bin/python3").read_text().endswith("echo 3 13\n")
    assert (runtime / "venv.python314-aside").is_dir()
    after = updater.journal_store.load_state(updater.STATE_PATH)
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


def test_undo_reports_manual_content_without_restoring_runtime(updater, tmp_path, monkeypatch):
    from ops_testkit import _git
    repo, _, old, _, order, _ = _interrupted(
        updater, tmp_path, monkeypatch, rollback_shaped=True)
    (repo / "f.txt").write_text("manual content")
    head = _git(repo, "rev-parse", "HEAD").stdout
    applying = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
    python = (old / "bin/python3").read_bytes()
    assert updater.recover_interrupted() == 1
    assert updater._last_report()["detail"] == "local changes outside update: f.txt"
    assert (repo / "f.txt").read_text() == "manual content"
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"] == applying
    assert (old / "bin/python3").read_bytes() == python
    assert "runtime" not in order and "reset" not in order


def test_undo_waiting_for_restore_consent_holds_with_drainers_stopped(updater, tmp_path, monkeypatch):
    repo, prev, old, runtime, order, notices = _interrupted(updater, tmp_path, monkeypatch)
    held = []

    def needs_consent(entry, on_hold=None):
        raise updater.RestoreConsentPending({"report_id": "synthetic-report"})

    monkeypatch.setattr(updater, "_rollback_tree", needs_consent)
    monkeypatch.setattr(updater, "_consent_hold", lambda state, e, path: held.append(e) or "rid")
    assert updater.recover_interrupted() == 0
    assert held and "restart" not in order and not notices
    after = updater.journal_store.load_state(updater.STATE_PATH)
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


@pytest.mark.parametrize("spec, admitted", [
    (">=3.11,<3.15", True), (">=3.14", True), ("<3.15,>3.13", True), (">=3.14.1", True),
    ("==3.14", False), ("<3.14.1", False), ("!=3.14.8", False),
    (">=3.11,<3.14", False), ("~=3.11", False), (">=3.11,<3.15.*", False),
    ("", False), (">=3.11,", False), (None, False)])
def test_requires_python_is_judged_against_the_full_interpreter_version(spec, admitted):
    import requires_python_admits
    assert requires_python_admits.admits(spec, (3, 14, 8)) is admitted


@pytest.mark.parametrize("toml, ok", [
    ('[project]\nrequires-python = ">=3.0"\n', True),
    ('[tool.example]\nrequires-python = ">=3.0"\n[project]\nrequires-python = "<3.0"\n', False),
    ('requires-python = ">=3.0"\n', False),                 # not under [project]
    ('[project]\nrequires-python = 3\n', False),
    ('[project\n', False)])
def test_only_project_requires_python_counts(tmp_path, toml, ok):
    import requires_python_admits
    path = tmp_path / "pyproject.toml"
    path.write_text(toml)
    assert (requires_python_admits.main(["x", str(path)]) == 0) is ok


def _own_checkout(updater, tmp_path, monkeypatch, *, pin=None):
    checkout = tmp_path / "hermes-agent"
    head, _ = _checkout(checkout)
    if pin == "previous":
        monkeypatch.setattr(updater, "HERMES_PREVIOUS_PINS", frozenset({head}))
    elif pin == "current":
        monkeypatch.setattr(updater, "HERMES_PIN", head)
    else:
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "-b", "local/org-build"],
                       check=True)
    venv = _venv(checkout, version_ok=False)
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    return checkout, head


@pytest.mark.parametrize("case, expected", [
    ("confirmed", None), ("no_keep", "hermes_checkout_unknown_commit"),
    ("mismatch", "keep_hermes_checkout_mismatch"), ("invalid", "keep_hermes_checkout_invalid"),
    ("old_python", "keep_hermes_checkout_python_unsupported"),
    ("dirty", "hermes_checkout_branch_or_dirty")])
def test_confirmed_own_checkout_is_kept_only_when_exact_clean_and_ready(
        updater, tmp_path, monkeypatch, case, expected):
    checkout, head = _own_checkout(updater, tmp_path, monkeypatch)
    monkeypatch.setattr(updater, "_checkout_admits_python314", lambda c: case != "old_python")
    keep = {"confirmed": head, "old_python": head, "dirty": head,
            "mismatch": "b" * 40, "invalid": "zz"}.get(case)
    if keep:
        monkeypatch.setenv(updater.KEEP_CHECKOUT_ENV, keep)
    else:
        monkeypatch.delenv(updater.KEEP_CHECKOUT_ENV, raising=False)
    if case == "dirty":
        (checkout / "f").write_text("local edit")
    assert updater._migration_checkout_problem({}) == expected
    if case == "confirmed":
        monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "7")
        state = _state(updater)
        updater._plan_runtime_migration(state)
        migration = state["applying"]["runtime_migration"]
        assert migration["kept_checkout"] is True and migration["checkout_head"] == head


@pytest.mark.parametrize("pin", ["previous", "current"])
@pytest.mark.parametrize("keep, ready, expected", [
    ("head", True, None), ("head", False, "keep_hermes_checkout_python_unsupported"),
    ("other", True, "keep_hermes_checkout_mismatch")])
def test_an_explicit_keep_is_decided_before_the_pin_rules(
        updater, tmp_path, monkeypatch, pin, keep, ready, expected):
    checkout, head = _own_checkout(updater, tmp_path, monkeypatch, pin=pin)
    monkeypatch.setattr(updater, "_checkout_admits_python314", lambda c: ready)
    monkeypatch.setenv(updater.KEEP_CHECKOUT_ENV, head if keep == "head" else "c" * 40)
    assert updater._migration_checkout_problem({}) == expected


def test_supported_previous_pin_refresh_is_journaled_and_restores_old_dependencies(
        updater, tmp_path, monkeypatch):
    checkout = tmp_path / "hermes-agent"
    old_head, new_head = _checkout(checkout)
    venv = _venv(checkout)
    (venv / "synthetic-deps").write_text("previous pin dependencies")
    (venv / "bin/hermes").write_text("synthetic installed CLI")
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    monkeypatch.setattr(updater, "HERMES_PREVIOUS_PINS", frozenset({old_head}))
    monkeypatch.setattr(updater, "HERMES_PIN", new_head)
    monkeypatch.delenv(updater.KEEP_CHECKOUT_ENV, raising=False)
    monkeypatch.setenv(updater.RUN_LOCK_FD_ENV, "7")
    state = _state(updater)
    updater._plan_runtime_migration(state, held_fd=5)
    saved = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
    migration = saved["runtime_migration"]
    assert migration["checkout"] == str(checkout)
    assert migration["checkout_head"] == old_head
    assert migration["checkout_target"] == new_head
    assert migration["venv"] == str(venv)
    assert saved["reinstall"] is True and "--no-plugin" in saved["install_args"]
    assert not Path(migration["backup_path"]).exists()
    assert (venv / "synthetic-deps").read_text() == "previous pin dependencies"
    assert updater.journal_store.load_state(updater.STATE_PATH)["stages"][-1]["stage"] == "runtime_migration"
    order = []

    def reinstall(applying):
        assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["runtime_migration"] == migration
        os.rename(venv, migration["backup_path"])
        _venv(checkout)
        (venv / "synthetic-deps").write_text("new pin dependencies")
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", new_head],
                       check=True)
        applying["reinstall_done"] = True
        order.append("reinstall")

    monkeypatch.setattr(updater, "_reinstall", reinstall)
    monkeypatch.setattr(updater, "_services_reconcile",
                        lambda: order.append(("services", updater._SERVICES_PYTHON)))
    monkeypatch.setattr(updater, "restart_agents", lambda: order.append("restart") or [])
    monkeypatch.setattr(updater, "_postcheck", lambda state, sha: [])
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: True)
    assert updater._post_merge(state) == 0
    assert order == ["reinstall", ("services", str(venv / "bin/python3")), "restart"]
    applied = updater.journal_store.load_state(updater.STATE_PATH)["applied"][-1]
    assert applied["runtime_migration"] == migration and applied["reinstall_done"] is True
    assert (Path(migration["backup_path"]) / "synthetic-deps").read_text() \
        == "previous pin dependencies"
    assert (venv / "synthetic-deps").read_text() == "new pin dependencies"
    updater._restore_runtime(applied)
    # Both runtimes report 3.14: distinguish dependency generations, not versions.
    assert (venv / "bin/python3").read_text().endswith("echo 3 14\n")
    assert (venv / "synthetic-deps").read_text() == "previous pin dependencies"
    assert (checkout / "venv.python314-aside/synthetic-deps").read_text() == "new pin dependencies"
    assert subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip() == old_head


@pytest.mark.parametrize("case, expected", [
    ("current", None), ("unknown", None),
    ("branch", "hermes_checkout_branch_or_dirty"), ("dirty", "hermes_checkout_branch_or_dirty"),
    ("keep", None), ("mismatched_keep", "keep_hermes_checkout_mismatch"),
    ("unready_keep", "keep_hermes_checkout_python_unsupported"),
    ("keep_dirty", "hermes_checkout_branch_or_dirty")])
def test_supported_runtime_checkout_guards_and_idempotence(
        updater, tmp_path, monkeypatch, case, expected):
    checkout = tmp_path / "hermes-agent"
    head, next_head = _checkout(checkout)
    venv = _venv(checkout)
    (venv / "synthetic-deps").write_text("installed dependencies")
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    monkeypatch.setattr(updater, "HERMES_PREVIOUS_PINS",
                        frozenset() if case == "unknown" else frozenset({head}))
    monkeypatch.setattr(updater, "HERMES_PIN", head if case == "current" else next_head)
    monkeypatch.setattr(updater, "_checkout_admits_python314", lambda c: case != "unready_keep")
    monkeypatch.delenv(updater.KEEP_CHECKOUT_ENV, raising=False)
    if "keep" in case:
        monkeypatch.setenv(updater.KEEP_CHECKOUT_ENV,
                           "c" * 40 if case == "mismatched_keep" else head)
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "-b", "local/org-build"],
                       check=True)
    if case == "branch":
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "-b", "local/org-build"],
                       check=True)
    if case in ("dirty", "keep_dirty"):
        (checkout / "f").write_text("local edit")
    assert updater._migration_checkout_problem({}) == expected
    state = _state(updater)
    if expected is None:
        updater._plan_runtime_migration(state, held_fd=5)
        assert "runtime_migration" not in state["applying"]
        assert state["applying"]["reinstall"] is False
        assert not list(checkout.glob("venv.pre-python314*"))
    assert (venv / "synthetic-deps").read_text() == "installed dependencies"
    assert subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip() == head
    assert (checkout / "f").read_text() == ("local edit" if case in ("dirty", "keep_dirty") else "0")


def test_supported_runtime_precheck_refuses_unreadable_branch_ref(updater, tmp_path, monkeypatch):
    checkout = tmp_path / "hermes-agent"
    head, target = _checkout(checkout)
    venv = _venv(checkout)
    (venv / "synthetic-deps").write_text("installed dependencies")
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    monkeypatch.setattr(updater, "HERMES_PIN", target)
    monkeypatch.setattr(updater, "HERMES_PREVIOUS_PINS", frozenset({head}))
    monkeypatch.delenv(updater.KEEP_CHECKOUT_ENV, raising=False)
    run = updater.subprocess.run
    calls = []

    def unreadable_branch(argv, *args, **kwargs):
        calls.append(argv)
        if argv[:3] == ["git", "-C", str(checkout)] and argv[3:4] == ["symbolic-ref"]:
            return subprocess.CompletedProcess(argv, 128, "", "synthetic unreadable branch ref")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(updater.subprocess, "run", unreadable_branch)
    assert updater._migration_checkout_problem({}) == "hermes_checkout_unreadable"
    assert any(argv[3:4] == ["symbolic-ref"] for argv in calls)
    assert all(argv[0] in ("git", str(venv / "bin/python3")) for argv in calls)
    assert all(argv[3] in ("rev-parse", "symbolic-ref", "status", "ls-files")
               for argv in calls if argv[0] == "git")
    assert (checkout / "f").read_text() == "0"
    assert (venv / "synthetic-deps").read_text() == "installed dependencies"
    assert not list(checkout.glob("venv.pre-python314*"))


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("kept", [False, True])
@pytest.mark.parametrize("operation", ["preflight", "restore"])
def test_hidden_hermes_edits_stop_before_environment_changes(
        updater, tmp_path, monkeypatch, flag, kept, operation):
    from ops_testkit import _git

    checkout = tmp_path / "hermes-agent"
    old_head, target = _checkout(checkout)
    _git(checkout, "checkout", "-q", "--detach", target)
    venv = _venv(checkout)
    (venv / "dependencies").write_text("synthetic new dependencies")
    backup = checkout / "venv.pre-python314"
    backup.mkdir()
    (backup / "dependencies").write_text("synthetic old dependencies")
    _git(checkout, "update-index", flag, "f")
    (checkout / "f").write_text("synthetic manual edit")
    # Real Git hides the edit from status, but it is still operator-owned.
    assert _git(checkout, "status", "--porcelain", "--untracked-files=no").stdout == ""
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    monkeypatch.setattr(updater, "HERMES_PIN", target)
    monkeypatch.setattr(updater, "_checkout_admits_python314", lambda _: True)
    monkeypatch.delenv(updater.KEEP_CHECKOUT_ENV, raising=False)
    if kept:
        monkeypatch.setenv(updater.KEEP_CHECKOUT_ENV, target)
    before = {p: (p.read_bytes(), p.stat().st_ino)
              for root in (venv, backup) for p in root.rglob("*") if p.is_file()}
    if operation == "preflight":
        assert updater._migration_checkout_problem({}) == "hermes_checkout_branch_or_dirty"
    else:
        migration = {"checkout": str(checkout), "checkout_head": target if kept else old_head,
                     "checkout_target": target, "venv": str(venv), "backup_path": str(backup)}
        if kept:
            migration["kept_checkout"] = True
        with pytest.raises(updater.UpdateError) as rejected:
            updater._restore_runtime({"runtime_migration": migration})
        assert all(p.exists() and (p.read_bytes(), p.stat().st_ino) == value
                   for p, value in before.items())
        assert str(rejected.value) == "runtime_restore_checkout_changed"
    assert (checkout / "f").read_text() == "synthetic manual edit"
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == target
    assert not list(checkout.glob("venv.python314-aside*"))


@pytest.mark.parametrize("operation", ["preflight", "restore"])
def test_unreadable_hermes_index_stops_before_environment_changes(
        updater, tmp_path, monkeypatch, operation):
    checkout = tmp_path / "hermes-agent"
    head, target = _checkout(checkout)
    venv = _venv(checkout)
    backup = checkout / "venv.pre-python314"
    backup.mkdir()
    (backup / "dependencies").write_text("synthetic original dependencies")
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    monkeypatch.setattr(updater, "HERMES_PIN", head)
    run = updater.subprocess.run

    def unreadable_index(argv, *args, **kwargs):
        if argv[:4] == ["git", "-C", str(checkout), "ls-files"]:
            return subprocess.CompletedProcess(argv, 128, "", "synthetic unreadable index")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(updater.subprocess, "run", unreadable_index)
    if operation == "preflight":
        assert updater._migration_checkout_problem({}) == "hermes_checkout_unreadable"
    else:
        with pytest.raises(updater.UpdateError, match="runtime_restore_checkout_unreadable"):
            updater._restore_runtime({"runtime_migration": {
                "checkout": str(checkout), "checkout_head": head, "checkout_target": target,
                "venv": str(venv), "backup_path": str(backup)}})
    assert (venv / "bin/python3").read_text().endswith("echo 3 14\n")
    assert (backup / "dependencies").read_text() == "synthetic original dependencies"
    assert not list(checkout.glob("venv.python314-aside*"))


@pytest.mark.parametrize("pin", ["current", "unknown"])
@pytest.mark.parametrize("local_edit", ["branch", "dirty"])
def test_nonmigrating_checkout_is_preserved_by_precheck_and_plan(
        updater, tmp_path, monkeypatch, pin, local_edit):
    """A pure MCS update must not impose the installer migration contract."""
    from ops_testkit import _git

    checkout = tmp_path / "hermes-agent"
    head, target = _checkout(checkout)
    venv = _venv(checkout)
    (venv / "dependencies").write_text("installed synthetic dependencies")
    monkeypatch.setattr(updater, "_runtime_venv", lambda cfg: (str(venv), str(checkout)))
    monkeypatch.setattr(updater, "HERMES_PIN", head if pin == "current" else target)
    monkeypatch.setattr(updater, "HERMES_PREVIOUS_PINS", frozenset())
    monkeypatch.delenv(updater.KEEP_CHECKOUT_ENV, raising=False)
    if local_edit == "branch":
        _git(checkout, "checkout", "-q", "-b", "local/retained")
    else:
        (checkout / "f").write_text("synthetic manual edit")
    before = {p: (p.read_bytes(), p.stat().st_ino)
              for p in (checkout / "f", venv / "bin/python3", venv / "dependencies")}
    assert updater._migration_checkout_problem({}) is None
    state = _state(updater)
    updater._plan_runtime_migration(state, held_fd=5)
    assert "runtime_migration" not in state["applying"]
    assert state["applying"]["reinstall"] is False
    assert {p: (p.read_bytes(), p.stat().st_ino) for p in before} == before
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == head
    assert not list(checkout.glob("venv.pre-python314*"))
