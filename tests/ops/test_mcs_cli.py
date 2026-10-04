"""Synthetic lifecycle routing through existing installer/setup/updater."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

import mcs_cli
import mcs_setup
from ops_testkit import _git, _make_repo
from test_install_sh import _world, _run
from test_mcs_setup import _init_env
from test_mcs_upgrade import _release

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setenv("MCS_LIFECYCLE_PINNED", "1")


def test_setup_preserves_configuration_and_checks_once(pinned, monkeypatch, tmp_path):
    # Given existing synthetic settings and a final-check observer.
    original = {"mcs_login_id": "synthetic", "notify_target": "local",
                "self_posts": True, "signals": {"notify": False}}
    _init_env(monkeypatch, tmp_path, original)
    calls = []
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: calls.append(args) or 0)
    # When the public default setup route is used.
    assert mcs_cli.main(["setup", "--yes"]) == 0
    # Then settings survive and init's final check is not duplicated.
    assert json.loads((tmp_path / "c.json").read_text()) == original
    assert len(calls) == 1


def test_setup_keeps_hidden_secret_input_on_terminal(pinned, monkeypatch, tmp_path, capsys):
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "synthetic", "notify_target": "local"})
    secret = "synthetic-terminal-secret"
    calls = []
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda prompt: secret)
    monkeypatch.setattr(mcs_setup, "_keychain_store",
                        lambda account, password: calls.append(password) or True)
    monkeypatch.setattr(mcs_setup, "_wizard", lambda cfg: None)
    assert mcs_cli.main(["setup"]) == 0
    assert calls == [secret]
    assert secret not in capsys.readouterr().out
    assert secret in (tmp_path / ".env").read_text()


def test_setup_services_is_explicit(pinned, monkeypatch):
    calls = []
    monkeypatch.setattr(mcs_setup, "cmd_services",
                        lambda args: calls.append(args.dry_run) or 0)
    assert mcs_cli.main(["setup", "services", "--dry-run"]) == 0
    assert calls == [True]


def test_setup_check_routes_to_check(pinned, monkeypatch):
    calls = []
    monkeypatch.setattr(mcs_setup, "main", lambda argv: calls.append(argv) or 0)
    assert mcs_cli.main(["setup", "check"]) == 0
    assert calls == [["check"]]


@pytest.mark.parametrize("argv", [
    ["doctor", "--probe", "all"], ["doctor", "--fix"],
    ["setup", "--password", "synthetic"],
    ["update", "plan", "--reinstall"],
    ["update", "apply", "--install-arg=--no-services"],
    ["update", "plan", "--to", "HEAD;echo unsafe"],
    ["update", "rollback", "--to", "v1.2.0"],
])
def test_invalid_arguments_exit_two(pinned, argv):
    with pytest.raises(SystemExit) as result:
        mcs_cli.main(argv)
    assert result.value.code == 2


@pytest.mark.parametrize("phase", ["apply", "rollback"])
def test_mutating_update_retains_existing_entrypoints(pinned, monkeypatch, phase):
    calls = []
    monkeypatch.setattr(mcs_cli.os, "execv", lambda exe, argv: calls.append((exe, argv)))
    args = ["update", phase]
    if phase == "apply":
        args += ["--to", "v1.2.0", "--reinstall", "--install-arg=--no-llm"]
    mcs_cli.main(args)
    exe, argv = calls[0]
    assert exe == sys.executable
    if phase == "apply":
        assert argv[1:] == [str(ROOT / "scripts/mcs_upgrade.py"), "--repo",
                           str(ROOT), "apply", "--to", "v1.2.0",
                           "--reinstall", "--install-arg=--no-llm"]
    else:
        assert argv[1:] == [str(ROOT / "mcs/ops/mcs_update.py"), "rollback"]


@pytest.mark.parametrize("route,exit_code", [("apply", 0), ("blocked", 1)])
def test_plan_uses_target_bootstrap_without_fetch_or_live_writes(
        pinned, monkeypatch, tmp_path, capsys, route, exit_code):
    repo, bare = _make_repo(tmp_path)
    stub = ("import json,os,sys\nprint(json.dumps("
            f"{{'route':{route!r},'argv':sys.argv[1:],"
            "'repo':os.environ['MCS_UPDATE_REPO']}))\n")
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"mcs/ops/mcs_update.py": stub, "mcs/_mcs_path.py": ""})
    _git(repo, "fetch", "-q", "--tags")
    (repo / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/mcs_upgrade.py", repo / "scripts/mcs_upgrade.py")
    # An unusable remote proves plan never fetches. Snapshot the live tree.
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing"))
    before = {str(p.relative_to(repo)): p.read_bytes()
              for p in repo.rglob("*") if p.is_file()}
    monkeypatch.setattr(mcs_cli, "REPO", repo)
    assert mcs_cli.main(["update", "plan", "--to", "v1.2.0"]) == exit_code
    seen = json.loads(capsys.readouterr().out)
    assert seen["argv"] == ["plan", "--tag", "v1.2.0"]
    assert seen["repo"] == str(repo)
    assert before == {str(p.relative_to(repo)): p.read_bytes()
                      for p in repo.rglob("*") if p.is_file()}


def test_bootstrap_install_does_not_need_python_or_path_launcher(tmp_path):
    # The existing installer test world stubs all host/network commands.
    _, _, _, env = _world(tmp_path)
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/mcs"), "install", "--help"],
        env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert not (Path(env["HOME"]) / ".local/bin/mcs").exists()


@pytest.mark.parametrize("mode", ["hermes", "standalone"])
def test_installer_pins_launcher_and_forwards_literal_arguments(tmp_path, mode):
    home, hermes, _, env = _world(tmp_path / "synthetic space's")
    result = _run(env, None, "--mode", mode, "--no-llm", "--no-plugin",
                  "--no-services", "--no-recovery")
    assert result.returncode == 0, result.stderr
    python = (home / ".mcs/venv/bin/python3" if mode == "standalone"
              else hermes / "hermes-agent/venv/bin/python")
    # Recording interpreter lives only in the temporary synthetic installation.
    python.write_text(f"#!{sys.executable}\nimport json,sys\n"
                      "print(json.dumps(sys.argv[1:]))\n")
    args = ["setup", "--set", "value=space's;$(touch NEVER)", ""]
    result = subprocess.run([str(home / ".local/bin/mcs"), *args],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == [str(ROOT / "mcs/ops/mcs_cli.py"), *args]
    assert (ROOT / "mcs").is_dir()


def test_installer_preserves_foreign_launcher(tmp_path):
    home, _, _, env = _world(tmp_path)
    launcher = home / ".local/bin/mcs"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("foreign command\n")
    result = _run(env, None, "--no-llm", "--no-plugin",
                  "--no-services", "--no-recovery")
    assert result.returncode == 1
    assert launcher.read_text() == "foreign command\n"


def test_bootstrap_reexecs_the_selected_installed_runtime(monkeypatch, tmp_path):
    monkeypatch.delenv("MCS_LIFECYCLE_PINNED", raising=False)
    selected = tmp_path / "installed-python"
    selected.write_text("#!/bin/sh\nexit 0\n")
    selected.chmod(0o700)
    monkeypatch.setattr(mcs_cli.mcs_runtime, "python_executable", lambda cfg: str(selected))
    calls = []
    def exec_selected(exe, argv):
        calls.append((exe, argv))
        raise SystemExit(0)
    monkeypatch.setattr(mcs_cli.os, "execv", exec_selected)
    with pytest.raises(SystemExit):
        mcs_cli.main(["setup", "--yes"])
    assert calls == [(str(selected), [str(selected), str(ROOT / "mcs/ops/mcs_cli.py"),
                                     "setup", "--yes"])]


def test_doctor_runs_through_real_cli_without_touching_synthetic_files(tmp_path):
    home = tmp_path / "home"
    root = home / ".mcs"
    root.mkdir(parents=True)
    config = root / "config.json"
    config.write_text(json.dumps({"mcs_login_id": "synthetic-private-id",
                                  "notify_target": "local"}))
    python = home / ".hermes/hermes-agent/venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    env = {**os.environ, "HOME": str(home), "MCS_ROOT": str(root),
           "MCS_LIFECYCLE_PINNED": "1"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "mcs/ops/mcs_cli.py"), "doctor", "--json"],
        env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == (0 if mcs_setup.sqlite_wal_safe(sqlite3.sqlite_version_info) else 1)
    assert json.loads(result.stdout)["checks"]["services"]["status"] == "not_checked"
    assert "synthetic-private-id" not in result.stdout + result.stderr
    assert before == {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
