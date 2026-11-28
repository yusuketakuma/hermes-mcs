"""scripts/run_tests.sh picks the test home's parent and always removes the home."""
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts/run_tests.sh"
SIX_GIB_KB = 6 * 1024 * 1024


def _world(tmp_path, available_kb):
    """A fake df reporting `available_kb`, a fake /dev/shm and a fake Python."""
    tools = tmp_path / "bin"
    tools.mkdir()
    df = tools / "df"
    df.write_text("#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
                  f"echo 'tmpfs {available_kb + 1} 1 {available_kb} 1% /dev/shm'\n")
    df.chmod(0o755)
    python = tmp_path / "python"
    # The runner starts Python under `env -i`, so the wait is requested by a
    # file: an environment flag never reaches this stub.
    python.write_text('#!/bin/sh\necho "home=$HOME tmp=$TMPDIR'
                      ' harness=${MCS_RUN_UPDATE_PARENT_HARNESS-unset} other=${MCS_UNRELATED_SETTING-unset}"\n'
                      f'[ ! -e "{tmp_path}/stub-wait" ] || exec sleep 30\n')
    python.chmod(0o755)
    shm = tmp_path / "shm"
    shm.mkdir()
    env = {"PATH": f"{tools}:/usr/bin:/bin", "MCS_TEST_PYTHON": str(python),
           "MCS_TEST_SHM_DIR": str(shm)}
    return shm, env


def _home(result):
    assert result.returncode == 0, result.stdout + result.stderr
    home = Path(result.stdout.split("home=", 1)[1].split()[0])
    assert f"test home: {home}" in result.stderr
    assert not home.exists()
    return home


def _run(env):
    return subprocess.run(["/bin/sh", str(RUNNER), "-q"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=30)


def test_large_shm_is_used_when_tmpdir_is_unset(tmp_path):
    shm, env = _world(tmp_path, SIX_GIB_KB)
    home = _home(_run(env))
    assert home.parent == shm
    assert not list(shm.iterdir())   # probe and home are both removed


@pytest.mark.parametrize("available", [SIX_GIB_KB - 1, 1024])
def test_small_shm_keeps_the_default_temporary_directory(tmp_path, available):
    shm, env = _world(tmp_path, available)
    home = _home(_run(env))
    assert home.parent == Path("/tmp")


@pytest.mark.parametrize("parallel,extra_kb,expected", [
    ("4", 3 * 256 * 1024, "shm"), ("4", 3 * 256 * 1024 - 1, "tmp"),
    ("1", 0, "shm"), ("08", 0, "shm"), ("65", 0, "shm"), ("x", 0, "shm"),
])
def test_shm_needs_room_for_every_parallel_runner(tmp_path, parallel, extra_kb, expected):
    # N shards start together and each sees the same free space; invalid counts mean one runner.
    shm, env = _world(tmp_path, SIX_GIB_KB + extra_kb)
    home = _home(_run({**env, "MCS_TEST_PARALLEL": parallel}))
    assert home.parent == (shm if expected == "shm" else Path("/tmp"))


def test_explicit_tmpdir_wins_over_shm(tmp_path):
    shm, env = _world(tmp_path, SIX_GIB_KB)
    chosen = tmp_path / "chosen-tmp"
    chosen.mkdir()
    assert _home(_run({**env, "TMPDIR": str(chosen)})).parent == chosen


def test_shm_that_is_not_a_directory_is_skipped(tmp_path):
    shm, env = _world(tmp_path, SIX_GIB_KB)
    shm.rmdir()
    shm.write_text("not a directory")
    assert _home(_run(env)).parent == Path("/tmp")


def test_mcs_test_tmpdir_is_the_parent_when_usable(tmp_path):
    shm, env = _world(tmp_path, SIX_GIB_KB)
    chosen = tmp_path / "ramdisk"
    chosen.mkdir()
    assert _home(_run({**env, "MCS_TEST_TMPDIR": str(chosen), "TMPDIR": str(tmp_path)})).parent == chosen


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_unusable_mcs_test_tmpdir_is_refused(tmp_path, kind):
    shm, env = _world(tmp_path, SIX_GIB_KB)
    chosen = tmp_path / "ramdisk"
    if kind == "file":
        chosen.write_text("")
    result = _run({**env, "MCS_TEST_TMPDIR": str(chosen)})
    assert result.returncode == 2
    assert "MCS_TEST_TMPDIR" in result.stderr and "home=" not in result.stdout


@pytest.mark.parametrize("value,seen", [(None, "unset"), ("1", "1")])
def test_only_the_real_parent_harness_opt_in_passes_through(tmp_path, value, seen):
    # env -i drops every other caller setting; the opt-in arrives only when set
    shm, env = _world(tmp_path, SIX_GIB_KB)
    env["MCS_UNRELATED_SETTING"] = "1"
    if value is not None:
        env["MCS_RUN_UPDATE_PARENT_HARNESS"] = value
    result = _run(env)
    _home(result)
    assert f" harness={seen} " in result.stdout
    assert " other=unset" in result.stdout


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signalled_runner_still_removes_the_test_home(tmp_path, signum):
    shm, env = _world(tmp_path, SIX_GIB_KB)
    (tmp_path / "stub-wait").write_text("")
    process = subprocess.Popen(["/bin/sh", str(RUNNER)], cwd=ROOT, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        line = process.stdout.readline()
        home = Path(line.split("home=", 1)[1].split()[0])
        assert home.is_dir()
        # Like ci/run_pytest_groups.py: the whole process group gets the signal.
        os.killpg(process.pid, signum)
        process.wait(timeout=15)
        assert process.returncode == 128 + signum
        deadline = time.monotonic() + 5
        while home.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not home.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
