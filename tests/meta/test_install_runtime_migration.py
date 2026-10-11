"""install.sh moves only a previous, clean, detached Hermes pin during an
updater-proven Python runtime migration. Synthetic HOME and stubbed git."""
import fcntl
import os

import pytest

from test_install_sh import _pin, _run, _world

FLAGS = ('--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
PREVIOUS = "fd50a275e2616118c48fe07e7e1c878782b15ccd"


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


def test_previous_pin_moves_to_the_current_pin_then_venv_is_rebuilt(tmp_path):
    home, hermes_home, checkout, venv, lock, env = _established(tmp_path, PREVIOUS)
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
    expected = {"unknown_commit": "unknown commit", "branch": "on a branch or has local changes",
                "dirty": "on a branch or has local changes",
                "no_lock": "lock or backup is invalid"}[case]
    assert expected in result.stderr


def test_updater_and_installer_agree_on_pins():
    import re
    from pathlib import Path

    import mcs_update
    text = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    pins = re.search(r'^HERMES_PREVIOUS_PINS="([0-9a-f ]+)"$', text, re.M)
    assert pins and set(pins[1].split()) == mcs_update.HERMES_PREVIOUS_PINS
    current = re.search(r'^HERMES_PIN="([0-9a-f]{40})"$', text, re.M)
    assert current and current[1] == mcs_update.HERMES_PIN


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
