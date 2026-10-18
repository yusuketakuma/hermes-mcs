"""install.sh moves only a previous, clean, detached Hermes pin during an
updater-proven Python runtime migration. Synthetic HOME and stubbed git."""
import fcntl
import os
from pathlib import Path
import shutil

import pytest

from test_install_sh import _calls, _pin, _run, _world

FLAGS = ('--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
PREVIOUS = "fd50a275e2616118c48fe07e7e1c878782b15ccd"
PREVIOUS_314 = ["f36c1da0016ce14c9c531cb9514ab4b0b667240d",
                "b7758d8555931846f4b8f798d4ea9715f65077ca"]


def _established(tmp_path, head):
    home, hermes_home, stub_root, env = _world(tmp_path)
    checkout = hermes_home / "hermes-agent"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text(head + "\n")
    venv = checkout / "venv"
    (venv / "bin").mkdir(parents=True)
    old = venv / "bin/python"
    old.write_text("#!/bin/sh\nexit 1\n")   # answers no to the 3.14 probe
    old.chmod(0o700)
    data = home / ".mcs/data"
    data.mkdir(parents=True)
    (data / "ledger.db").write_text("synthetic established installation")
    lock = data / "run.lock"
    lock.touch(mode=0o600)
    return home, hermes_home, checkout, venv, lock, env


def _held(lock):
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _ready314(venv, env, *, cli=True):
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    for name in ("python", "python3", *(("hermes",) if cli else ())):
        source = "venv-hermes" if name == "hermes" else "venv-python"
        shutil.copyfile(Path(env["STUB_ROOT"]) / source, venv / "bin" / name)
        (venv / "bin" / name).chmod(0o700)


def _established314(tmp_path, head):
    result = _established(tmp_path, head)
    _, _, checkout, venv, _, env = result
    _ready314(venv, env)
    (checkout / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.0"\n')
    (venv / "synthetic-deps").write_text("previous pin dependencies")
    return result


def _runtime_files(venv):
    return {str(path.relative_to(venv)): (path.read_bytes(), path.stat().st_mtime_ns,
                                         path.stat().st_ino)
            for path in venv.rglob("*") if path.is_file()}


def _mutations(env):
    return [call for call in _calls(Path(env["STUB_ROOT"]))
            if call.startswith(("uv venv ", "uv pip ", "git clone "))
            or call.startswith("git -C ") and (" checkout " in call or " fetch " in call)]


@pytest.mark.parametrize("previous", PREVIOUS_314)
def test_python314_previous_pin_refreshes_dependencies_once(tmp_path, previous):
    _, hermes_home, checkout, venv, lock, env = _established314(tmp_path, previous)
    before = _runtime_files(venv)
    backup = checkout / "venv.pre-python314"
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd), MCS_UPDATE_RUNTIME_BACKUP=str(backup))
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
        assert result.returncode == 0, result.stdout + result.stderr
        assert (checkout / ".git/HEAD").read_text().strip() == _pin()
        assert _runtime_files(backup) == before
        assert not (venv / "synthetic-deps").exists()
        assert (venv / "bin/hermes").is_file()
        assert not (venv / ".mcs-pip-incomplete").exists()
        calls = _mutations(env)
        assert sum(call.startswith("uv venv ") for call in calls) == 1
        assert sum(call.startswith("uv pip ") for call in calls) == 1
        assert sum(" checkout " in call for call in calls) == 1
        assert not any(call.startswith("git clone ") for call in calls)
        assert (Path(env["STUB_ROOT"]) / "uv-pip-cwd").read_text().strip() == str(checkout)
        # A crash after package installation can leave the same journal pending.
        rerun = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
        assert rerun.returncode == 0, rerun.stdout + rerun.stderr
        assert _mutations(env) == calls
        assert _runtime_files(backup) == before
    finally:
        os.close(fd)


@pytest.mark.parametrize("case", ["current", "keep"])
def test_python314_kept_or_current_checkout_stays_installed(tmp_path, case):
    head = _pin() if case == "current" else PREVIOUS_314[0]
    _, hermes_home, checkout, venv, lock, env = _established314(tmp_path, head)
    before = _runtime_files(venv)
    if case == "keep":
        env.update(MCS_UPDATE_KEEP_HERMES_CHECKOUT=head, STUB_GIT_BRANCH="local/org-build")
    fd = _held(lock)
    try:
        if case == "keep":
            env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                       MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
    finally:
        os.close(fd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (checkout / ".git/HEAD").read_text().strip() == head
    assert _runtime_files(venv) == before
    assert not (checkout / "venv.pre-python314").exists()
    assert _mutations(env) == []


@pytest.mark.parametrize("case", ["unknown", "branch", "dirty", "no_lock",
                                  "keep_mismatch", "keep_unready", "keep_dirty"])
def test_python314_migration_refuses_unapproved_checkout(tmp_path, case):
    head = "2" * 40 if case == "unknown" else PREVIOUS_314[0]
    _, hermes_home, checkout, venv, lock, env = _established314(tmp_path, head)
    before = _runtime_files(venv)
    if case == "branch":
        env["STUB_GIT_BRANCH"] = "local/org-build"
    if case in ("dirty", "keep_dirty"):
        env["STUB_GIT_DIRTY"] = "1"
    if case.startswith("keep_"):
        env["MCS_UPDATE_KEEP_HERMES_CHECKOUT"] = "4" * 40 if case == "keep_mismatch" else head
        if case == "keep_unready":
            (checkout / "pyproject.toml").write_text('[project]\nrequires-python = "<3.0"\n')
    source = (checkout / "pyproject.toml").read_bytes()
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                   MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS,
                      pass_fds=() if case == "no_lock" else (fd,))
    finally:
        os.close(fd)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (checkout / ".git/HEAD").read_text().strip() == head
    assert (checkout / "pyproject.toml").read_bytes() == source
    assert _runtime_files(venv) == before
    assert not (checkout / "venv.pre-python314").exists()
    assert _mutations(env) == []


def test_python314_unrelated_checkout_without_migration_is_kept(tmp_path):
    head = "2" * 40
    _, hermes_home, checkout, venv, _, env = _established314(tmp_path, head)
    before = _runtime_files(venv)
    result = _run(env, hermes_home, "--mode", "hermes", *FLAGS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "取得済みソースを保持" in result.stdout + result.stderr
    assert (checkout / ".git/HEAD").read_text().strip() == head
    assert _runtime_files(venv) == before
    assert _mutations(env) == []


@pytest.mark.parametrize("flag", ["h", "S", "unreadable"])
@pytest.mark.parametrize("kept", [False, True])
@pytest.mark.parametrize("head", [_pin(), PREVIOUS_314[0]])
def test_hidden_or_unreadable_index_stops_before_installer_changes(tmp_path, flag, kept, head):
    _, hermes_home, checkout, venv, lock, env = _established314(tmp_path, head)
    before = _runtime_files(venv)
    env.update(STUB_GIT_INDEX_FLAG=flag)
    if flag == "unreadable":
        env["STUB_FAIL_INDEX"] = "1"
    if kept:
        env["MCS_UPDATE_KEEP_HERMES_CHECKOUT"] = head
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                   MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
    finally:
        os.close(fd)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ("追跡設定を確認できません" if flag == "unreadable"
            else "変更の確認を省略する Git 設定") in result.stderr
    assert _runtime_files(venv) == before
    assert (checkout / ".git/HEAD").read_text().strip() == head
    assert not (checkout / "venv.pre-python314").exists()
    assert _mutations(env) == []


def test_normal_reinstall_keeps_hidden_index_without_migration(tmp_path):
    _, hermes_home, checkout, venv, _, env = _established314(tmp_path, _pin())
    before = _runtime_files(venv)
    env["STUB_GIT_INDEX_FLAG"] = "h"
    result = _run(env, hermes_home, "--mode", "hermes", *FLAGS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _runtime_files(venv) == before
    assert _mutations(env) == []


@pytest.mark.parametrize("phase", ["checkout_moved", "pip_pending", "pip_complete"])
def test_python314_refresh_resumes_installer_crash(tmp_path, phase):
    _, hermes_home, checkout, venv, lock, env = _established314(tmp_path, _pin())
    backup = checkout / "venv.pre-python314"
    before = _runtime_files(venv)
    if phase != "checkout_moved":
        os.rename(venv, backup)
        _ready314(venv, env, cli=phase == "pip_complete")
        (venv / "synthetic-deps").write_text("new pin dependencies")
        if phase == "pip_pending":
            (venv / ".mcs-pip-incomplete").touch()
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd), MCS_UPDATE_RUNTIME_BACKUP=str(backup))
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
    finally:
        os.close(fd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (checkout / ".git/HEAD").read_text().strip() == _pin()
    assert _runtime_files(backup) == before
    calls = _mutations(env)
    assert sum(call.startswith("uv venv ") for call in calls) == (phase == "checkout_moved")
    assert sum(call.startswith("uv pip ") for call in calls) == (phase != "pip_complete")
    assert not any(call.startswith("git ") for call in calls)
    assert (venv / "bin/hermes").is_file()
    assert not (venv / ".mcs-pip-incomplete").exists()
    if phase == "checkout_moved":
        assert not (venv / "synthetic-deps").exists()
    else:
        assert (venv / "synthetic-deps").read_text() == "new pin dependencies"


@pytest.mark.parametrize("case", ["branch", "dirty"])
def test_python314_refresh_at_current_pin_refuses_local_checkout(tmp_path, case):
    _, hermes_home, checkout, venv, lock, env = _established314(tmp_path, _pin())
    env["STUB_GIT_BRANCH" if case == "branch" else "STUB_GIT_DIRTY"] = "local"
    before = _runtime_files(venv)
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                   MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
    finally:
        os.close(fd)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (checkout / ".git/HEAD").read_text().strip() == _pin()
    assert _runtime_files(venv) == before
    assert not (checkout / "venv.pre-python314").exists()
    assert _mutations(env) == []


@pytest.mark.parametrize("case", ["status", "symbolic_ref", "keep_status"])
def test_python314_migration_refuses_failed_git_read(tmp_path, case):
    _, hermes_home, checkout, venv, lock, env = _established314(tmp_path, _pin())
    if case == "keep_status":
        env["MCS_UPDATE_KEEP_HERMES_CHECKOUT"] = _pin()
    git_stub = Path(env["STUB_ROOT"]) / "bin/git"
    command = "symbolic-ref" if case == "symbolic_ref" else "status"
    dispatch = f"      {command})\n"
    body = git_stub.read_text()
    assert dispatch in body
    git_stub.write_text(body.replace(dispatch, dispatch + "        exit 128\n", 1))
    before = _runtime_files(venv)
    source = {name: (checkout / name).read_bytes() for name in (".git/HEAD", "pyproject.toml")}
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                   MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
    finally:
        os.close(fd)
    assert result.returncode == 1, result.stdout + result.stderr
    assert {name: (checkout / name).read_bytes() for name in source} == source
    assert _runtime_files(venv) == before
    assert not (checkout / "venv.pre-python314").exists()
    assert _mutations(env) == []


@pytest.mark.parametrize("previous", [PREVIOUS, "6f35b1ed84536f7e7fd2f605f7779dd051cce05f",
                                      "f36c1da0016ce14c9c531cb9514ab4b0b667240d",
                                      "b7758d8555931846f4b8f798d4ea9715f65077ca"])
def test_previous_pin_moves_to_the_current_pin_then_venv_is_rebuilt(tmp_path, previous):
    home, hermes_home, checkout, venv, lock, env = _established(tmp_path, previous)
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                   MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
    finally:
        os.close(fd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (checkout / ".git/HEAD").read_text().strip() == _pin()
    assert (checkout / "venv.pre-python314/bin/python").read_text() == "#!/bin/sh\nexit 1\n"
    assert (venv / "bin/python3").exists()


@pytest.mark.parametrize("case", ["unknown_commit", "branch", "dirty", "no_lock"])
def test_unsafe_checkout_or_missing_proof_changes_nothing(tmp_path, case):
    head = "2" * 40 if case == "unknown_commit" else PREVIOUS
    home, hermes_home, checkout, venv, lock, env = _established(tmp_path, head)
    if case == "branch":
        env["STUB_GIT_BRANCH"] = "local/org-build"
    if case == "dirty":
        env["STUB_GIT_DIRTY"] = "1"
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                   MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS,
                      pass_fds=() if case == "no_lock" else (fd,))
    finally:
        os.close(fd)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (checkout / ".git/HEAD").read_text().strip() == head
    assert (venv / "bin/python").read_text() == "#!/bin/sh\nexit 1\n"
    assert not (checkout / "venv.pre-python314").exists()
    expected = {"unknown_commit": "未検証の版", "branch": "ブランチ上にあるか未コミットの変更",
                "dirty": "ブランチ上にあるか未コミットの変更",
                "no_lock": "ロックまたはバックアップが不正"}[case]
    assert expected in result.stderr


def test_updater_and_installer_agree_on_pins():
    import re
    from pathlib import Path

    import mcs_update
    root = Path(__file__).resolve().parents[2]
    text = (root / "install.sh").read_text()
    pins = re.search(r'^HERMES_PREVIOUS_PINS="([0-9a-f ]+)"$', text, re.M)
    assert pins and set(pins[1].split()) == mcs_update.HERMES_PREVIOUS_PINS
    current = re.search(r'^HERMES_PIN="([0-9a-f]{40})"$', text, re.M)
    assert current and current[1] == mcs_update.HERMES_PIN
    # The CI hermes-integration lane must test the Hermes that users install.
    workflow = (root / ".github/workflows/ci.yml").read_text()
    ci = re.findall(r"repository: yusuketakuma/hermes-agent\s+ref: ([0-9a-f]{40})", workflow)
    assert ci == [mcs_update.HERMES_PIN]


def test_checkout_already_at_the_current_pin_rebuilds_the_venv_only(tmp_path):
    home, hermes_home, checkout, venv, lock, env = _established(tmp_path, _pin())
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                   MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
    finally:
        os.close(fd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (checkout / ".git/HEAD").read_text().strip() == _pin()
    assert (checkout / "venv.pre-python314/bin/python").exists()


def _own_checkout(tmp_path, spec, head="3" * 40):
    home, hermes_home, checkout, venv, lock, env = _established(tmp_path, head)
    (checkout / "pyproject.toml").write_text(f'[project]\nrequires-python = "{spec}"\n')
    env["STUB_GIT_BRANCH"] = "local/org-build"
    return head, home, hermes_home, checkout, venv, lock, env


# ">=3.0" / "<3.0" hold for any test interpreter; the script itself is tested in tests/ops
@pytest.mark.parametrize("case", ["confirmed", "mismatch", "old_python", "dirty", "no_lock",
                                  "previous_pin_kept", "previous_pin_unready"])
def test_confirmed_own_checkout_is_kept_and_venv_rebuilt(tmp_path, case):
    spec = "<3.0" if case in ("old_python", "previous_pin_unready") else ">=3.0"
    head = PREVIOUS if case.startswith("previous_pin") else "3" * 40
    head, home, hermes_home, checkout, venv, lock, env = _own_checkout(tmp_path, spec, head)
    env["MCS_UPDATE_KEEP_HERMES_CHECKOUT"] = "4" * 40 if case == "mismatch" else head
    if case == "dirty":
        env["STUB_GIT_DIRTY"] = "1"
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                   MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS,
                      pass_fds=() if case == "no_lock" else (fd,))
    finally:
        os.close(fd)
    assert (checkout / ".git/HEAD").read_text().strip() == head      # never moved
    if case in ("confirmed", "previous_pin_kept"):
        assert result.returncode == 0, result.stdout + result.stderr
        assert (checkout / "venv.pre-python314/bin/python").exists()
        assert (venv / "bin/python3").exists()
        return
    assert result.returncode == 1, result.stdout + result.stderr
    assert (venv / "bin/python").read_text() == "#!/bin/sh\nexit 1\n"
    assert {"mismatch": "と一致しません", "old_python": "Python 3.14 対応を宣言していません",
            "previous_pin_unready": "Python 3.14 対応を宣言していません",
            "dirty": "コミットされていない変更", "no_lock": "ロックまたはバックアップが不正"}[case] \
        in result.stderr


def test_installer_judges_with_the_shared_script():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    assert '"$REPO/mcs/ops/requires_python_admits.py" "$HERMES_DIR/pyproject.toml"' in text


@pytest.mark.parametrize("case", ["kept", "mismatch", "unready", "dirty"])
def test_an_explicit_keep_is_judged_before_the_current_pin_too(tmp_path, case):
    spec = "<3.0" if case == "unready" else ">=3.0"
    head, home, hermes_home, checkout, venv, lock, env = _own_checkout(tmp_path, spec, _pin())
    env["MCS_UPDATE_KEEP_HERMES_CHECKOUT"] = "5" * 40 if case == "mismatch" else head
    if case == "dirty":
        env["STUB_GIT_DIRTY"] = "1"
    fd = _held(lock)
    try:
        env.update(MCS_UPDATE_RUN_LOCK_FD=str(fd),
                   MCS_UPDATE_RUNTIME_BACKUP=str(venv) + ".pre-python314")
        result = _run(env, hermes_home, "--mode", "hermes", *FLAGS, pass_fds=(fd,))
    finally:
        os.close(fd)
    assert (checkout / ".git/HEAD").read_text().strip() == head
    if case == "kept":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "確認済みの Hermes" in result.stdout
        return
    assert result.returncode == 1, result.stdout + result.stderr
    assert (venv / "bin/python").read_text() == "#!/bin/sh\nexit 1\n"
    assert {"mismatch": "と一致しません", "unready": "Python 3.14 対応を宣言していません",
            "dirty": "コミットされていない変更"}[case] in result.stderr
