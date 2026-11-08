"""Synthetic lifecycle routing through existing installer/setup/updater."""
import mcs_runtime
import lifecycle_io
import setup_wizard
import json
import os
import plistlib
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
    monkeypatch.setattr(lifecycle_io, "store_keychain",
                        lambda account, password: calls.append(password) or True)
    monkeypatch.setattr(setup_wizard, "wizard", lambda cfg, **kwargs: None)
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
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


def test_setup_features_routes_to_features(pinned, monkeypatch):
    calls = []
    monkeypatch.setattr(mcs_setup, "main", lambda argv: calls.append(argv) or 0)
    assert mcs_cli.main(["setup", "features"]) == 0
    assert calls == [["features"]]


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
    monkeypatch.setattr(mcs_cli, 'load_config', lambda: {'runtime_mode': 'hermes'})
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
    monkeypatch.setattr(mcs_cli, 'load_config', lambda: {'runtime_mode': 'hermes'})
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


@pytest.mark.parametrize('phase', ['guide', 'plan', 'apply', 'rollback'])
def test_standalone_cli_uses_installed_update_contract(pinned, monkeypatch, phase):
    monkeypatch.setattr(mcs_cli, 'load_config', lambda: {'runtime_mode': 'standalone'})
    calls = []
    def execute(exe, argv):
        calls.append(argv)
        raise SystemExit(0)
    monkeypatch.setattr(mcs_cli.os, 'execv', execute)
    monkeypatch.setattr(mcs_cli.subprocess, 'run', lambda argv, **kw:
                        calls.append(argv) or subprocess.CompletedProcess(argv, 0, '{"route":"apply"}', ''))
    if phase == 'plan':
        assert mcs_cli.main(['update', phase, '--to', 'v1.2.0']) == 0
    else:
        with pytest.raises(SystemExit) as stopped:
            mcs_cli.main(['update', phase, *([] if phase == 'rollback' else ['--to', 'v1.2.0'])])
        assert stopped.value.code == 0
    if phase == 'rollback':
        assert calls == [[sys.executable, str(ROOT / 'mcs/ops/mcs_update.py'), 'rollback']]
    else:
        assert calls == [[sys.executable, str(ROOT / 'scripts/mcs_upgrade.py'),
                          '--repo', str(ROOT), '--installed-standalone',
                          *(['--no-fetch'] if phase == 'plan' else []), phase, '--to', 'v1.2.0']]


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
    runtime = json.dumps({"python": "3.14.8", "sqlite": "3.51.3",
                          "packages": {"discord.py": None, "slack-bolt": None,
                                       "slack-sdk": None}})
    python.write_text(f"#!/bin/sh\nprintf '%s\\n' '{runtime}'\n")
    python.chmod(0o700)
    before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    env = {**os.environ, "HOME": str(home), "MCS_ROOT": str(root),
           "MCS_LIFECYCLE_PINNED": "1"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "mcs/ops/mcs_cli.py"), "doctor", "--json"],
        env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == (0 if mcs_runtime.sqlite_wal_safe(sqlite3.sqlite_version_info) else 1)
    assert json.loads(result.stdout)["checks"]["services"]["status"] == "not_checked"
    assert "synthetic-private-id" not in result.stdout + result.stderr
    assert before == {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}


@pytest.mark.parametrize("command", ["lookup", "search", "compare"])
def test_drug_subcommand_routes_literal_arguments_without_setup_or_db(pinned, monkeypatch, command):
    import mcs_drug
    calls = []
    monkeypatch.setattr(mcs_drug, "main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(mcs_cli, "load_config", lambda: pytest.fail("pinned drug route must not read config"))
    arguments = [command, "架空名;$(touch NEVER)", "--limit", "0", "--json"]
    assert mcs_cli.main(["drug", *arguments]) == 0
    assert calls == [arguments]


@pytest.mark.parametrize("arguments", [
    ["drug", "search", "架空名;$(touch NEVER)", "--limit", "1", "--json"],
    # The shell launcher forwards the diagnostics command literally; only the fake interpreter runs.
    ["diagnostics", "status", "--state-root", "/synthetic/private;$(touch NEVER)"],
])
def test_bootstrap_drug_route_uses_only_temporary_pinned_interpreter(tmp_path, arguments):
    root = tmp_path / "synthetic-mcs"
    interpreter = root / "venv/bin/python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    interpreter.chmod(0o700)
    env = dict(os.environ, HOME=str(tmp_path), MCS_ROOT=str(root))
    result = subprocess.run(["/bin/sh", str(ROOT / "scripts/mcs"), *arguments],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(ROOT / "mcs/ops/mcs_cli.py"), *arguments]
    assert not (tmp_path / "NEVER").exists()


def test_bootstrap_help_and_invalid_command_are_japanese(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), MCS_ROOT=str(tmp_path / "root"))
    for arguments, code, expected in [
        (["--help"], 0, "初めて導入するとき"),
        (["--help"], 0, "diagnostics: 固定診断コードの送信許可・停止"),
        (["unknown"], 2, "操作名が正しくありません"),
        (["diagnostic"], 2, "操作名が正しくありません"),
        (["setup"], 1, "実行に必要なPythonが見つかりません"),
    ]:
        result = subprocess.run(["/bin/sh", str(ROOT / "scripts/mcs"), *arguments],
                                env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == code
        assert expected in result.stdout + result.stderr


def test_cli_update_help_is_japanese(pinned, capsys):
    with pytest.raises(SystemExit) as result:
        mcs_cli.main(["update", "--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "使い方:" in output
    assert "変更せず更新内容を確認" in output
    assert "再導入に渡すオプション" in output


@pytest.fixture
def recovery_command(monkeypatch, tmp_path):
    import run_log_install

    home = tmp_path.resolve()
    root = home / "custom-runtime"
    (root / "data").mkdir(parents=True, mode=0o700)
    recovery = home / ".mcs-recovery"
    run_log_install.prepare(root, ROOT, recovery, promote=True)
    helper = Path(run_log_install.helper_path(root, recovery, require_installed=True))
    tool = recovery / "mcs_recover.py"
    tool.write_text("# synthetic\nINTERACTIVE_GUIDE_VERSION = 1\n")
    template = (ROOT / "deployment/launchagents/org.mcs.recovery.plist").read_text()
    for key, value in {"__RECOVERY_PYTHON__": sys.executable,
                       "__RUN_LOG_HELPER__": str(helper), "__DATA__": str(root / "data"),
                       "__RECOVERY__": str(recovery)}.items():
        template = template.replace(key, value)
    plist = home / "Library/LaunchAgents/org.mcs.recovery.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text(template)
    monkeypatch.setattr(mcs_cli.Path, "home", lambda: home)
    monkeypatch.setattr(mcs_cli, "HOME", str(root), raising=False)
    monkeypatch.setattr(mcs_cli, "load_config", lambda: pytest.fail("recovery must bypass runtime config"))
    return root, helper, tool, plist


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("status", [False, True])
def test_recovery_cli_uses_verified_inner_python(recovery_command, monkeypatch, wrapped, status):
    _root, _helper, tool, plist = recovery_command
    if not wrapped:
        plist.write_bytes(plistlib.dumps({"ProgramArguments": [sys.executable, str(tool), "--if-stale"]}))
    before = {p: p.read_bytes() for p in plist.parents[2].rglob("*") if p.is_file()}
    calls = []
    monkeypatch.setattr(mcs_cli.os, "execv", lambda exe, argv: calls.append((exe, argv)))
    mcs_cli.main(["recover", *(["--status"] if status else [])])
    assert calls == [(sys.executable, [sys.executable, str(tool), "--status" if status else "--interactive"])]
    assert {p: p.read_bytes() for p in plist.parents[2].rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("change", ["data", "tool", "helper", "inner_python", "hash", "pointer",
                                   "missing", "shape", "program", "symlink"])
def test_recovery_cli_rejects_untrusted_wrapper(recovery_command, monkeypatch, change):
    root, helper, tool, plist = recovery_command
    import run_log_install

    value = plistlib.loads(plist.read_bytes())
    argv = value["ProgramArguments"]
    if change == "data":
        argv[5] = str(root.parent / "other-runtime/data")
    elif change == "tool":
        argv[8] = str(tool.parent / "other/mcs_recover.py")
    elif change == "helper":
        argv[2] = str(helper.parent / "unverified.py")
    elif change == "inner_python":
        argv[7] = "/synthetic/other-python"
    elif change == "hash":
        helper.write_bytes(helper.read_bytes() + b"\n# synthetic changed helper\n")
    elif change == "pointer":
        pointer = helper.parent / run_log_install._pointer(str(root))
        receipt = json.loads(pointer.read_bytes())
        receipt["root"] = str(root.parent / "other-runtime")
        pointer.write_text(json.dumps(receipt))
    elif change == "missing":
        helper.unlink()
    elif change == "shape":
        argv[-1] = "--interactive"
    elif change == "program":
        value["Program"] = sys.executable
    elif change == "symlink":
        saved = helper.with_suffix(".saved")
        helper.rename(saved)
        helper.symlink_to(saved)
    plist.write_bytes(plistlib.dumps(value))
    before = plist.read_bytes()
    monkeypatch.setattr(mcs_cli.os, "execv", lambda *args: pytest.fail("untrusted recovery must not execute"))
    assert mcs_cli.main(["recover", "--status"]) == 1
    assert plist.read_bytes() == before
