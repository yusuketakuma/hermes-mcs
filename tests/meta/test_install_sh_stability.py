"""Installer publication failures and read-only plans, using synthetic commands."""
import pytest

from test_install_sh import ROOT, STOPPED, _bootstraps, _run, _world


@pytest.mark.parametrize("target", ["llama", "llama_cleanup", "repo", "watchdog"])
def test_failed_atomic_publish_stops_and_rerun_converges(tmp_path, target):
    home, hermes_home, stub_root, env = _world(tmp_path)
    assert _run(env, hermes_home).returncode == 0
    agents = home / "Library" / "LaunchAgents"
    paths = {"llama": agents / "ai.mcs.llamaserver.plist",
             "llama_cleanup": agents / "ai.mcs.llamaserver.plist",
             "repo": home / ".mcs-recovery" / "repo_path",
             "watchdog": agents / "org.mcs.recovery.plist"}
    dst = paths[target]
    old = dst.read_text()
    if target != "llama_cleanup":
        dst.write_text(old + "\n")
    prior = dst.read_bytes()
    launches = len(_bootstraps(stub_root, "llamaserver")) + len(
        _bootstraps(stub_root, "org.mcs.recovery"))
    command = "rm" if target == "llama_cleanup" else "mv"
    stub = stub_root / "bin" / command
    stub.write_text('''#!/bin/sh
last=""
for arg in "$@"; do last="$arg"; done
if [ -n "${STUB_FAIL_PUBLISH:-}" ] && [ "$last" = "$STUB_FAIL_PUBLISH" ]; then
    echo "synthetic rename failure" >&2
    exit 73
fi
exec /bin/COMMAND "$@"
'''.replace("COMMAND", command))
    stub.chmod(0o755)
    failed_path = str(dst) + (".tmp" if target == "llama_cleanup" else "")
    failed = _run({**env, "STUB_FAIL_PUBLISH": failed_path}, hermes_home)
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert STOPPED in failed.stderr and "Installed." not in failed.stdout
    assert dst.read_bytes() == prior
    assert len(_bootstraps(stub_root, "llamaserver")) + len(
        _bootstraps(stub_root, "org.mcs.recovery")) == launches
    if target.startswith("llama"):
        assert "=== 5/6" not in failed.stdout
    repaired = _run(env, hermes_home)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert dst.read_text() == old
    assert (home / ".mcs-recovery" / "repo_path").read_text() == str(ROOT) + "\n"


@pytest.mark.parametrize("mode", ["--preflight", "--dry-run"])
def test_explicit_recovery_selection_checked_when_recovery_skipped(tmp_path, mode):
    home, hermes_home, _, env = _world(tmp_path)
    result = _run(env, hermes_home, mode, "--no-recovery",
                  "--recovery-python", "relative-python")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "recovery template interpreter unsafe or unverified" in result.stdout
    assert list(home.iterdir()) == []
