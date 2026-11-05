"""Synthetic install sh recovery contracts; shared helpers preserve isolated runtime state."""
import json
import plistlib
import shutil
import sys
import pytest
from install_sh_testkit import INSTALL as INSTALL, ROOT as ROOT, SAVED_FLAGS as SAVED_FLAGS, STOPPED as STOPPED, _bootstraps as _bootstraps, _calls as _calls, _real_config_writer as _real_config_writer, _run as _run, _without_supported_python as _without_supported_python, _world as _world


def test_existing_recovery_selection_preserves_other_settings(tmp_path):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    config = home / '.mcs/config.json'
    config.parent.mkdir(mode=0o700)
    initial = {'runtime_mode': 'hermes', 'unknown': {'keep': True}}
    config.write_text(json.dumps(initial))
    selected = str(stub / 'bin/python3.14')
    result = _run(env, None, '--recovery-python', selected, '--no-brew', '--no-llm',
                  '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(config.read_text()) == {**initial, 'recovery_python': selected, 'install_args': SAVED_FLAGS}


@pytest.mark.parametrize("runtime", ["unsafe", "unknown", "failed", "unsupported"])
@pytest.mark.parametrize("mode", ["install", "preflight", "dry-run"])
def test_recovery_runtime_gate_fails_before_watchdog_installation(
        tmp_path, runtime, mode):
    home, hermes_home, stub_root, env = _world(tmp_path)
    env["STUB_RECOVERY_RUNTIME"] = runtime
    flags = ["--no-brew", "--no-llm", "--no-plugin", "--no-services"]
    if mode != "install":
        flags.insert(0, "--" + mode)
    r = _run(env, hermes_home, *flags)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "復旧" in r.stdout + r.stderr
    assert not (home / ".mcs-recovery").exists()
    assert not (home / "Library/LaunchAgents/org.mcs.recovery.plist").exists()
    assert _bootstraps(stub_root, "org.mcs.recovery") == []
    assert not any(c.startswith("launchctl bootout") and "org.mcs.recovery" in c
                   for c in _calls(stub_root))
    assert f"runtime-metadata {stub_root}/bin/python3.14" in _calls(stub_root)
    if mode != "install":
        assert list(home.iterdir()) == []


def test_unsafe_recovery_probe_preserves_prior_tool_plist_and_receipt(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    recovery = home / ".mcs-recovery"
    recovery.mkdir()
    prior = {
        recovery / "mcs_recover.py": b"synthetic prior tool",
        recovery / "mcs_recover.py.prev": b"synthetic prior generation",
        recovery / "repo_path": (str(ROOT) + "\n").encode(),
    }
    watch = home / "Library/LaunchAgents/org.mcs.recovery.plist"
    watch.parent.mkdir(parents=True)
    prior[watch] = b"synthetic old plist"
    for path, content in prior.items():
        path.write_bytes(content)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in prior}
    env["STUB_RECOVERY_RUNTIME"] = "unsafe"
    r = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin", "--no-services")
    assert r.returncode == 1
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in prior} == before
    assert not list(recovery.glob("*.tmp"))
    assert _bootstraps(stub_root, "org.mcs.recovery") == []


def test_recovery_template_executable_is_probed_without_substitution(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    repo = tmp_path / "synthetic-checkout"
    (repo / "deployment/launchagents").mkdir(parents=True)
    (repo / "deployment/recovery").mkdir()
    shutil.copytree(ROOT / "mcs", repo / "mcs", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(INSTALL, repo / "install.sh")
    for helper in (ROOT / "deployment/recovery").glob("*.py"):
        shutil.copy2(helper, repo / "deployment/recovery" / helper.name)
    shutil.copytree(ROOT / "deployment/install", repo / "deployment/install")
    executable = stub_root / "explicit-recovery-python"
    executable.touch()
    executable.chmod(0o700)
    template = repo / "deployment/launchagents/org.mcs.recovery.plist"
    document = plistlib.loads(
        (ROOT / "deployment/launchagents/org.mcs.recovery.plist").read_bytes())
    document["Program"] = str(executable)
    document["ProgramArguments"][0] = document["ProgramArguments"][7] = str(executable)
    template.write_bytes(plistlib.dumps(document))
    r = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin", "--no-services",
             script=repo / "install.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "runtime-metadata " + str(executable) in _calls(stub_root)
    assert f"runtime-metadata {stub_root}/bin/python3.14" not in _calls(stub_root)
    installed = plistlib.loads((home / "Library/LaunchAgents/org.mcs.recovery.plist").read_bytes())
    assert installed["Program"] == str(executable)
    assert installed["ProgramArguments"][0] == installed["ProgramArguments"][7] == str(executable)


def test_explicit_no_recovery_keeps_watchdog_unconfigured_without_probe(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    env["STUB_RECOVERY_RUNTIME"] = "unsafe"
    r = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin",
             "--no-services", "--no-recovery")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(c.startswith("runtime-metadata") for c in _calls(stub_root))
    assert not (home / ".mcs-recovery").exists()


def test_explicit_recovery_flag_persists_and_renders_selected_target(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    selected = stub_root / "python & independent"
    selected.touch()
    selected.chmod(0o700)
    env["STUB_REAL_PYTHON"] = sys.executable
    flags = ["--no-brew", "--no-llm", "--no-plugin", "--no-services"]
    first = _run(env, hermes_home, *flags, "--recovery-python", str(selected))
    assert first.returncode == 0, first.stdout + first.stderr
    config = home / ".mcs/config.json"
    assert json.loads(config.read_text())["recovery_python"] == str(selected)
    assert config.stat().st_mode & 0o777 == 0o600
    path = home / "Library/LaunchAgents/org.mcs.recovery.plist"
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][0] == str(selected)
    second = _run(env, hermes_home, *flags)
    assert second.returncode == 0, second.stdout + second.stderr
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][0] == str(selected)
    assert "runtime-metadata " + str(selected) in _calls(stub_root)
    assert f"runtime-metadata {stub_root}/bin/python3.14" not in _calls(stub_root)


@pytest.mark.parametrize("runtime", ["unsafe", "unknown", "unsupported"])
@pytest.mark.parametrize('prior_state', ['absent', 'empty_root', 'empty_data'])
def test_bad_explicit_recovery_selection_precedes_any_installer_write(tmp_path, runtime, prior_state):
    home, hermes_home, stub_root, env = _world(tmp_path)
    if prior_state != 'absent':
        (home / '.mcs').mkdir(mode=0o700)
    if prior_state == 'empty_data':
        (home / '.mcs/data').mkdir(mode=0o700)
    before = sorted(str(path.relative_to(home)) for path in home.rglob('*'))
    selected = stub_root / "independent-python"
    selected.touch()
    selected.chmod(0o700)
    env["STUB_RECOVERY_RUNTIME"] = runtime
    result = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin",
                  "--no-services", "--recovery-python", str(selected))
    assert result.returncode == 1
    assert sorted(str(path.relative_to(home)) for path in home.rglob('*')) == before
    assert not (home / '.mcs/data/update.lock').exists()
    assert _bootstraps(stub_root, "org.mcs.recovery") == []


def test_missing_brew_fails_with_recovery_hint(tmp_path):
    home, hermes_home, _, env = _world(tmp_path, brew=False)
    r = _run(env, hermes_home)
    assert r.returncode == 1
    assert "Homebrew が見つかりません" in r.stderr
    assert "install.sh" in r.stderr                     # actionable hint


@pytest.mark.parametrize("switch,message", [
    ("STUB_FAIL_PLUGIN", "プラグインの有効化に失敗しました"),
    ("STUB_FAIL_SERVICES", "常駐サービスの登録に問題があります"),
    ("STUB_FAIL_WATCHDOG", "復旧監視を起動できません"),
    # exit 0 is not proof: the label must answer `print` afterwards
    ("STUB_WATCHDOG_NOLOAD", "復旧監視を起動できません"),
])
def test_failed_stage_stops_install_then_rerun_recovers(
        tmp_path, switch, message):
    """A failed stage is a non-zero stop with a repair hint — never a
    warn-and-continue 'ソフトの導入が完了しました。'. Once repaired, a plain re-run
    converges and the previous recovery generation survives both runs."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    rec = home / ".mcs-recovery"
    rec.mkdir(mode=0o700, parents=True)
    old_gen = b"# old generation - only thing that still runs"
    (rec / "mcs_recover.py").write_bytes(old_gen)

    r = _run({**env, switch: "1"}, hermes_home)
    assert r.returncode != 0
    assert message in r.stderr
    assert STOPPED in r.stderr
    assert "ソフトの準備が完了しました" not in r.stdout
    if switch not in ("STUB_FAIL_WATCHDOG", "STUB_WATCHDOG_NOLOAD"):
        # stopped before stage 6 — the old tool is untouched
        assert (rec / "mcs_recover.py").read_bytes() == old_gen
        assert not (rec / "mcs_recover.py.prev").exists()

    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    assert STOPPED not in r2.stderr
    assert (rec / "mcs_recover.py.prev").read_bytes() == old_gen
    assert (rec / "mcs_recover.py").read_bytes() == (
        ROOT / "deployment" / "recovery" / "mcs_recover.py").read_bytes()
    assert not list(rec.glob("*.tmp"))


def test_transient_watchdog_bootstrap_error_is_retried(tmp_path):
    """launchd's transient EIO right after bootout is retried, not a
    stopped install."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run({**env, "STUB_FAIL_WATCHDOG_ONCE": "1"}, hermes_home)
    assert r.returncode == 0, r.stderr
    assert STOPPED not in r.stderr
    assert "復旧監視を起動しました" in r.stdout


def test_fresh_mac_preflight_defers_recovery_check_when_brew_installs_python(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    _without_supported_python(stub_root)
    r = _run(env, None, "--preflight")
    assert r.returncode == 0, r.stdout
    assert "導入を妨げる問題 0件" in r.stdout
    assert "段階6で復旧用 Python を確認します" in r.stdout  # uname stub = Darwin
    # --recovery-python cannot be verified without Python: still a blocker
    r = _run(env, None, "--preflight", "--recovery-python", "/synthetic/recovery/python3")
    assert r.returncode == 1 and "復旧サービスのテンプレートに問題があります: Python の安全条件" in r.stdout
