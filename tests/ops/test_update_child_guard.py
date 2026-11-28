"""The child-side boundary of the real-parent update harness, checked without any updater code.

Every target here is a synthetic child of this test. Group-wide decisions
(0, -1, a reaped child) are read from the guard, never sent for real.
"""
import os
import signal
import subprocess
import sys
import time

import pytest

import update_child_guard_testkit

_SLEEP = "import time; time.sleep(120)"
_SEND = ("import os, signal, sys\n"
         "try:\n"
         "    os.kill(int(sys.argv[1]), signal.SIGTERM)\n"
         "    print('sent')\n"
         "except PermissionError:\n"
         "    print('refused')\n")


@pytest.fixture
def guard(tmp_path):
    return update_child_guard_testkit.create(tmp_path / "guard", tmp_path / "bin")


@pytest.fixture
def sleeper():
    process = subprocess.Popen([sys.executable, "-c", _SLEEP], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    yield process
    if process.poll() is None:
        process.kill()          # an unreaped child of this test
    process.wait(timeout=10)


def _child(guard, *args):
    return subprocess.run([str(guard.python), *args], capture_output=True, text=True, timeout=60)


def test_real_parent_harness_runs_only_when_explicitly_requested():
    # importing the harness module runs none of its tests
    import test_update_parent_v130 as harness
    opt_in = update_child_guard_testkit.HARNESS_OPT_IN
    for environ, wanted in (({}, False), ({opt_in: ""}, False), ({opt_in: "0"}, False),
                            ({opt_in: "true"}, False), ({opt_in: "1"}, True)):
        assert update_child_guard_testkit.harness_requested(environ) is wanted, environ
    mark = harness.pytestmark
    assert mark.name == "skipif"
    assert mark.args == (not update_child_guard_testkit.harness_requested(os.environ),)
    assert opt_in + "=1" in mark.kwargs["reason"]


def test_isolated_and_nested_children_load_the_guard(guard):
    nested = ("import subprocess, sys; print(subprocess.run([sys.executable, '-I', '-c', "
              "'import sys; print(\"mcs_harness_child_guard\" in sys.modules)'], "
              "capture_output=True, text=True).stdout.strip())")
    assert _child(guard, "-c", nested).stdout.split() == ["True"]
    installed = guard.records("installed")
    assert {row["isolated"] for row in installed} == {True, False}, installed


@pytest.mark.parametrize("isolation", [[], ["-I"]])
def test_child_refuses_a_target_outside_the_allow_list(guard, sleeper, isolation):
    result = _child(guard, *isolation, "-c", _SEND, str(sleeper.pid))
    assert result.stdout.split() == ["refused"], result.stderr
    time.sleep(0.2)
    assert sleeper.poll() is None
    rows = guard.records("signal")
    assert [(row["target"], row["signal"], row["allowed"]) for row in rows] == [
        (sleeper.pid, signal.SIGTERM, False)]


def test_child_signals_an_allowed_synthetic_target(guard, sleeper):
    guard.allow(sleeper.pid)
    result = _child(guard, "-c", _SEND, str(sleeper.pid))
    assert result.stdout.split() == ["sent"], result.stderr
    assert sleeper.wait(timeout=10) == -signal.SIGTERM
    rows = guard.records("signal")
    assert [(row["target"], row["signal"], row["allowed"]) for row in rows] == [
        (sleeper.pid, signal.SIGTERM, True)]


def test_child_may_stop_only_its_own_unreaped_children(guard):
    code = ("import os, signal, subprocess, sys\n"
            "import mcs_harness_child_guard as g\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
            " start_new_session=True)\n"
            "os.killpg(child.pid, signal.SIGTERM)\n"
            "print(child.wait(), g._allowed(child.pid), g._allowed(-child.pid), g._allowed(0), g._allowed(-1))\n")
    result = _child(guard, "-c", code)
    # stopped as its own session; once reaped the pid is no longer its to signal
    assert result.stdout.split() == [str(-signal.SIGTERM), "False", "False", "False", "False"], result.stderr


def test_fake_pgrep_reports_only_listed_live_pids(guard, sleeper, tmp_path):
    token = "mcs-harness-" + str(os.getpid())
    unlisted = subprocess.Popen([sys.executable, "-c", _SLEEP, token], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        search = [str(guard.pgrep), "-u", str(os.getuid()), "-f", token]
        # a process whose command line matches is still not reported unless listed
        assert subprocess.run(search, capture_output=True, text=True).returncode == 1
        guard.report(sleeper.pid)
        found = subprocess.run(search, capture_output=True, text=True)
        assert (found.returncode, found.stdout.split()) == (0, [str(sleeper.pid)])
    finally:
        unlisted.kill()
        unlisted.wait(timeout=10)


def test_fake_pgrep_skips_an_ended_child_and_cannot_answer_without_its_list(guard):
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(ended.pid)],
                                   capture_output=True, text=True).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.05)
        assert state.startswith("Z")          # ended, not yet reaped by this test
        guard.report(ended.pid)
        assert subprocess.run([str(guard.pgrep)], capture_output=True).returncode == 1
    finally:
        ended.wait(timeout=10)
    guard.report_file.unlink()
    assert subprocess.run([str(guard.pgrep)], capture_output=True).returncode == 3
