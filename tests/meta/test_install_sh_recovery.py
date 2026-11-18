"""Synthetic install sh recovery contracts; shared helpers preserve isolated runtime state."""
import json
import plistlib
import shutil
import sys
import pytest
from install_sh_testkit import INSTALL as INSTALL, ROOT as ROOT, SAVED_FLAGS as SAVED_FLAGS, STOPPED as STOPPED, _bootstraps as _bootstraps, _calls as _calls, _real_config_writer as _real_config_writer, _run as _run, _run_reinstall as _run_reinstall, _without_supported_python as _without_supported_python, _world as _world


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


def test_interrupted_install_journal_names_stage_and_resume_notice(tmp_path):
    """A stopped install leaves a stage journal; the rerun names the stage
    and converges to a done record."""
    home, hermes_home, _stub, env = _world(tmp_path)
    journal = home / ".mcs/data/install_progress.json"
    r = _run({**env, "STUB_FAIL_PLUGIN": "1"}, hermes_home)
    assert r.returncode != 0
    progress = json.loads(journal.read_text())
    assert progress["status"] == "running"
    assert progress["stage"] == "plugin"
    assert progress["invocation"] == "install"
    assert progress["run_id"]
    assert "操作カード用プラグインの段階で止まっていました" in r.stderr
    assert "途中から再開します" in r.stderr

    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    assert ("前回の導入記録が【3/6】Slack／Discord の操作カード用プラグインの段階で"
            "残っています") in r2.stdout
    assert "完了は記録されていません" in r2.stdout
    assert "中断していました" not in r2.stdout
    done = json.loads(journal.read_text())
    assert done["status"] == "complete" and done["stage"] == "done"
    assert done["run_id"] != progress["run_id"]


def test_successful_install_records_done_journal(tmp_path):
    home, hermes_home, _stub, env = _world(tmp_path)
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    progress = json.loads((home / ".mcs/data/install_progress.json").read_text())
    assert progress["status"] == "complete" and progress["stage"] == "done"
    assert progress["invocation"] == "install"


def test_updater_reinstall_journal_marks_update_invocation(tmp_path):
    home, hermes_home, _stub, env = _world(tmp_path)
    assert _run(env, hermes_home).returncode == 0
    r = _run_reinstall(env, hermes_home, "--no-llm", "--no-plugin", "--no-services")
    assert r.returncode == 0, r.stderr
    progress = json.loads((home / ".mcs/data/install_progress.json").read_text())
    assert progress["status"] == "complete" and progress["invocation"] == "update"


@pytest.mark.parametrize("kind", ["dir", "link_dir", "link_file"])
def test_unwritable_journal_never_stops_install(tmp_path, kind):
    """A journal path that cannot be safely replaced is skipped, not fatal:
    mv must never move the tmp into a directory or through a symlink."""
    home, hermes_home, _stub, env = _world(tmp_path)
    assert _run(env, hermes_home).returncode == 0
    data = home / ".mcs/data"
    journal = data / "install_progress.json"
    journal.unlink()
    if kind == "dir":
        journal.mkdir()
        r = _run(env, hermes_home)
        assert r.returncode == 0, r.stderr
        assert journal.is_dir() and not list(journal.iterdir())
    else:
        target = data / ("linked-dir" if kind == "link_dir" else "linked-file")
        if kind == "link_dir":
            target.mkdir()
        else:
            target.write_bytes(b"sentinel journal target")
        journal.symlink_to(target)
        r = _run(env, hermes_home)
        assert r.returncode == 0, r.stderr
        assert journal.is_symlink()          # the link itself is preserved
        if kind == "link_dir":
            assert target.is_dir() and not list(target.iterdir())
        else:
            assert target.read_bytes() == b"sentinel journal target"


def test_exit_names_stage_from_memory_when_journal_unwritable(tmp_path):
    """The EXIT trap reports this run's stage even with no journal record."""
    home, hermes_home, _stub, env = _world(tmp_path)
    assert _run(env, hermes_home).returncode == 0
    journal = home / ".mcs/data/install_progress.json"
    journal.unlink()
    journal.mkdir()
    r = _run({**env, "STUB_FAIL_PLUGIN": "1"}, hermes_home)
    assert r.returncode != 0
    assert "操作カード用プラグインの段階で止まっていました" in r.stderr
    assert "途中から再開します" in r.stderr


@pytest.mark.parametrize("kind", ["symlink", "garbage", "unknown_stage", "complete_stage", "bad_version"])
def test_prior_run_journal_rejects_untrusted_records(tmp_path, kind):
    """A foreign or malformed journal is no record — never a crash or echo.
    JSON fixtures use the writer's compact separators so the shell reader's
    fixed-format check actually parses them (default dumps spacing would
    never match and make every negative vacuous)."""
    home, hermes_home, _stub, env = _world(tmp_path)
    assert _run(env, hermes_home).returncode == 0
    journal = home / ".mcs/data/install_progress.json"
    journal.unlink()
    if kind == "symlink":
        journal.symlink_to(home / ".mcs/config.json")
    elif kind == "garbage":
        journal.write_text("not json at all\n")
    elif kind == "complete_stage":
        # complete pairs only with done; a complete mid-stage record is malformed
        journal.write_text(json.dumps({
            "version": 1, "status": "complete", "stage": "services",
            "runtime_mode": "hermes", "invocation": "install",
            "run_id": "old-run", "updated_at": 1}, separators=(",", ":")))
    elif kind == "bad_version":
        journal.write_text(json.dumps({
            "version": 2, "status": "running", "stage": "services",
            "runtime_mode": "hermes", "invocation": "install",
            "run_id": "old-run", "updated_at": 1}, separators=(",", ":")))
    else:
        journal.write_text(json.dumps({
            "version": 1, "status": "running", "stage": "bogus_stage",
            "runtime_mode": "hermes", "invocation": "install",
            "run_id": "old-run", "updated_at": 1}, separators=(",", ":")))
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "前回の導入記録" not in r.stdout
    assert "bogus_stage" not in r.stdout + r.stderr


def test_prior_run_journal_compact_record_shows_stage_notice(tmp_path):
    """Positive control: a writer-shaped compact running record is parsed
    and its stage is named in the rerun notice."""
    home, hermes_home, _stub, env = _world(tmp_path)
    assert _run(env, hermes_home).returncode == 0
    journal = home / ".mcs/data/install_progress.json"
    journal.write_text(json.dumps({
        "version": 1, "status": "running", "stage": "services",
        "runtime_mode": "hermes", "invocation": "install",
        "run_id": "old-run", "updated_at": 1}, separators=(",", ":")))
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "前回の導入記録" in r.stdout
    assert "常駐サービス" in r.stdout
    assert "残っています" in r.stdout


def test_fresh_install_stop_keeps_hold_guidance_not_stage_claim(tmp_path):
    """A fresh install stopped at brew keeps the approval hold message."""
    home, hermes_home, _stub, env = _world(tmp_path, brew=False)
    r = _run(env, hermes_home)
    assert r.returncode != 0
    assert "導入は保留中です" in r.stderr
    assert "担当者に確認してください" in r.stderr


def test_unchanged_world_readable_repo_pointer_is_made_private(tmp_path):
    """Installs from before umask 077 left repo_path 0644 with the same content;
    services refuse the watchdog as unowned unless it is 0600, so the mode is
    repaired before stage 5 (here stage 5 fails and stage 6 never runs)."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    rec = home / ".mcs-recovery"
    rec.mkdir(mode=0o700, parents=True)
    pointer = rec / "repo_path"
    pointer.write_text(str(ROOT) + "\n")
    pointer.chmod(0o644)
    r = _run({**env, "STUB_FAIL_SERVICES": "1"}, hermes_home)
    assert r.returncode != 0
    assert "常駐サービスの登録に問題があります" in r.stderr
    assert pointer.read_text() == str(ROOT) + "\n"
    assert pointer.stat().st_mode & 0o777 == 0o600


def test_unrepairable_repo_pointer_does_not_stop_install_without_watchdog(tmp_path):
    """A repo_path whose mode cannot be changed (another owner, uchg) only
    warns: services and recovery are skipped here, and stage 5's ownership
    check refuses on its own when it is reached."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    (stub_root / "bin/chmod").write_text(
        '#!/bin/sh\ncase "$2" in */repo_path) exit 1 ;; esac\nexec /bin/chmod "$@"\n')
    (stub_root / "bin/chmod").chmod(0o700)
    rec = home / ".mcs-recovery"
    rec.mkdir(mode=0o700, parents=True)
    pointer = rec / "repo_path"
    pointer.write_text(str(ROOT) + "\n")
    pointer.chmod(0o644)
    r = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin",
             "--no-services", "--no-recovery")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "注意:" in r.stderr and "repo_path のアクセス権" in r.stderr
    assert pointer.read_text() == str(ROOT) + "\n"
    assert pointer.stat().st_mode & 0o777 == 0o644


def test_unchanged_world_readable_watchdog_files_are_made_private(tmp_path):
    """The pre-umask tool (0755) and plist (0644) also fail the ownership
    check, and stage 5 runs before stage 6 could republish them; files others
    can write are not laundered into private ones."""
    # An existing install: a fresh one refuses the leftover watchdog plist first.
    home, hermes_home, stub_root, env = _world(tmp_path, configured=True)
    rec = home / ".mcs-recovery"
    rec.mkdir(mode=0o700, parents=True)
    tool, pointer = rec / "mcs_recover.py", rec / "repo_path"
    watch = home / "Library/LaunchAgents/org.mcs.recovery.plist"
    watch.parent.mkdir(parents=True)
    tool.write_bytes(b"# synthetic prior tool\n")
    pointer.write_text(str(ROOT) + "\n")
    watch.write_bytes(plistlib.dumps({"Label": "org.mcs.recovery", "ProgramArguments": [
        "/usr/bin/python3", str(tool), "--if-stale"]}))
    tool.chmod(0o755)
    pointer.chmod(0o664)
    watch.chmod(0o644)
    before = {path: path.read_bytes() for path in (tool, pointer, watch)}
    r = _run({**env, "STUB_FAIL_SERVICES": "1"}, hermes_home)
    assert r.returncode != 0
    assert "常駐サービスの登録に問題があります" in r.stderr
    assert {path: path.read_bytes() for path in before} == before
    assert tool.stat().st_mode & 0o777 == 0o700
    assert watch.stat().st_mode & 0o777 == 0o600
    assert pointer.stat().st_mode & 0o777 == 0o664
    assert "repo_path のアクセス権を 600 に変更できませんでした" in r.stderr


def _loose(directory):
    import os
    import re
    import subprocess
    function = re.search(r"^recovery_dir_loose\(\) \{.*?^\}\n", INSTALL.read_text(), re.M | re.S).group(0)
    result = subprocess.run(["/bin/sh", "-c", function + 'recovery_dir_loose'],
                            env={**os.environ, "RECOVERY_DIR": str(directory)}, timeout=10)
    return result.returncode == 0


@pytest.mark.parametrize("mode, loose", [(0o755, True), (0o700, False), (0o775, False), (0o757, False)])
def test_only_an_own_recovery_directory_without_group_or_other_write_is_tightened(tmp_path, mode, loose):
    directory = tmp_path / "recovery"
    directory.mkdir()
    directory.chmod(mode)
    assert _loose(directory) is loose


def test_linked_and_missing_recovery_directories_are_left_alone(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target)
    assert not _loose(link)
    assert not _loose(tmp_path / "missing")


def test_legacy_0755_recovery_directory_is_tightened_by_install(tmp_path):
    """v1.0.3-v1.0.6 created ~/.mcs-recovery 0755; the log helper requires 0700."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    rec = home / ".mcs-recovery"
    rec.mkdir(parents=True)
    rec.chmod(0o755)
    _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin", "--no-services")
    assert rec.stat().st_mode & 0o777 == 0o700


def test_dry_run_leaves_a_legacy_recovery_directory_unchanged(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    rec = home / ".mcs-recovery"
    rec.mkdir(parents=True)
    rec.chmod(0o755)
    _run(env, hermes_home, "--dry-run")
    assert rec.stat().st_mode & 0o777 == 0o755


def test_private_recovery_file_skips_symlinks_and_hardlinks_without_chmod(tmp_path):
    import os

    home, hermes_home, stub_root, env = _world(tmp_path, configured=True)
    rec = home / ".mcs-recovery"
    rec.mkdir(mode=0o700, parents=True)
    sym_target = tmp_path / "sym-target"
    sym_target.write_text("synthetic\n")
    sym_target.chmod(0o644)
    (rec / "repo_path").symlink_to(sym_target)

    hard_target = tmp_path / "hard-target"
    hard_target.write_bytes(b"# synthetic tool\n")
    hard_target.chmod(0o755)
    os.link(hard_target, rec / "mcs_recover.py")

    r = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin",
             "--no-services", "--no-recovery")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sym_target.stat().st_mode & 0o777 == 0o644
    assert hard_target.stat().st_mode & 0o777 == 0o755
    assert "repo_path のアクセス権" in r.stderr
    assert "mcs_recover.py のアクセス権" in r.stderr


def test_loose_0755_recovery_dir_checks_python_in_preflight_and_tightens_only_after_confirm_and_lock(tmp_path):
    import errno
    import fcntl
    import os
    import pty
    import select
    import subprocess
    import time

    home, hermes_home, stub_root, env = _world(tmp_path)
    rec = home / ".mcs-recovery"
    rec.mkdir(parents=True)
    rec.chmod(0o755)
    selected = stub_root / "explicit-bad-python"
    selected.touch()
    selected.chmod(0o700)

    # 1. Preflight with 0755 recovery dir MUST NOT skip explicit recovery Python validation.
    r_pf_bad = _run({**env, "STUB_RECOVERY_RUNTIME": "unsafe"}, hermes_home,
                    "--preflight", "--no-brew", "--no-llm", "--no-plugin", "--no-services",
                    "--recovery-python", str(selected))
    assert r_pf_bad.returncode == 1
    assert "権限が 700 ではありません" in r_pf_bad.stdout
    assert "要対処" in r_pf_bad.stdout
    assert f"runtime-metadata {selected}" in _calls(stub_root)
    assert rec.stat().st_mode & 0o777 == 0o755

    # Preflight with valid recovery Python warns about 0755 and passes without chmod.
    r_pf_ok = _run(env, hermes_home, "--preflight", "--no-brew", "--no-llm", "--no-plugin", "--no-services")
    assert r_pf_ok.returncode == 0, r_pf_ok.stdout + r_pf_ok.stderr
    assert "権限が 700 ではありません" in r_pf_ok.stdout
    assert "復旧用 Python: 3.14以降で SQLite の安全条件を満たしています" in r_pf_ok.stdout
    assert rec.stat().st_mode & 0o777 == 0o755

    # 2. Invalid CLI flag and bad explicit --recovery-python in install mode leave 0755 untouched.
    r_flag = _run(env, hermes_home, "--unknown-flag")
    assert r_flag.returncode == 2
    assert rec.stat().st_mode & 0o777 == 0o755

    r_inst_bad = _run({**env, "STUB_RECOVERY_RUNTIME": "unsafe"}, hermes_home,
                      "--no-brew", "--no-llm", "--no-plugin", "--no-services",
                      "--recovery-python", str(selected))
    assert r_inst_bad.returncode == 1
    assert rec.stat().st_mode & 0o777 == 0o755
    assert not (home / ".mcs/data/update.lock").exists()

    # 3. Interactive invalid input + cancellation leaves 0755 untouched.
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["sh", str(INSTALL), "--mode", "hermes", "--no-brew", "--no-llm", "--no-plugin", "--no-services"],
        stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    out = b""
    deadline = time.monotonic() + 30
    try:
        while "この内容で導入する（推奨）".encode() not in out:
            assert select.select([master], [], [], max(0.1, deadline - time.monotonic()))[0]
            out += os.read(master, 4096)
        os.write(master, b"9\n")
        while "1 または 2 を入力してください".encode() not in out:
            assert select.select([master], [], [], max(0.1, deadline - time.monotonic()))[0]
            out += os.read(master, 4096)
        os.write(master, b"2\n")
        while proc.poll() is None:
            if select.select([master], [], [], 1.0)[0]:
                try:
                    out += os.read(master, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
        assert proc.wait(timeout=5) == 130
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    assert rec.stat().st_mode & 0o777 == 0o755

    # 4. Contended install lock also stops before tightening 0755.
    data = home / ".mcs/data"
    data.mkdir(mode=0o700, parents=True)
    (home / ".mcs").chmod(0o700)
    lock_path = data / "update.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # lockf on macOS shares flock or fcntl depending on lockf; hold via lockf child if available.
        holder = subprocess.Popen(
            ["python3", "-c", "import fcntl, os, sys, time; fd=os.open(sys.argv[1], os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX); sys.stdout.write('held\\n'); sys.stdout.flush(); time.sleep(30)",
             str(lock_path)], stdout=subprocess.PIPE, text=True)
        fcntl.flock(fd, fcntl.LOCK_UN)
        try:
            assert holder.stdout.readline().strip() == "held"
            r_locked = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin", "--no-services")
            assert r_locked.returncode == 1
            assert "進行中です" in r_locked.stderr
            assert rec.stat().st_mode & 0o777 == 0o755
        finally:
            holder.terminate()
            holder.wait(timeout=5)
    finally:
        os.close(fd)
