"""Process matching and signals in tests stay inside the test run's own tree.

An update routine finds leftover workers with ``pgrep -u <uid> -f`` and stops
them with ``os.kill``. Neither depends on HOME or MCS_ROOT, so a test that runs
such a routine for real would reach the live services of the same user. The
decoy here stands for such a service: it matches by name, is not a descendant
of this run, and exits by itself.
"""
import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

import mcs_test_guard

_SLEEP = "import time; time.sleep(20)"


def _parent(pid):
    return mcs_test_guard._process_parent(pid)


def _alive(pid):
    try:
        mcs_test_guard._ORIGINAL_KILL(pid, 0)
    except OSError:
        return False
    return True


def _real_pgrep(token):
    for candidate in ("/usr/bin/pgrep", "/bin/pgrep"):
        if os.access(candidate, os.X_OK):
            # the unguarded Popen: the guard refuses the system pgrep to tests
            with mcs_test_guard._ORIGINAL_POPEN([candidate, "-f", token], stdout=subprocess.PIPE, text=True) as process:
                return {int(line) for line in process.communicate(timeout=30)[0].split()}
    pytest.skip("pgrep is not installed")


@pytest.fixture
def processes():
    """One owned child and one decoy outside this run, both named by a token."""
    token = "mcs-guard-" + uuid.uuid4().hex
    child = subprocess.Popen([sys.executable, "-c", _SLEEP, token], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The shell exits at once, so the decoy is re-parented away from this run.
    shell = subprocess.run(
        ["/bin/sh", "-c", '"$0" -c "$1" "$2" </dev/null >/dev/null 2>&1 & echo $!', sys.executable, _SLEEP, token],
        capture_output=True, text=True, timeout=30)
    decoy = int(shell.stdout.strip())
    try:
        deadline = time.monotonic() + 10
        while mcs_test_guard._in_test_tree(decoy) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _alive(decoy) and not mcs_test_guard._in_test_tree(decoy)
        assert child.poll() is None and _parent(child.pid) == os.getpid()
        yield token, child, decoy
    finally:
        for pid in (child.pid, decoy):
            if _alive(pid):
                mcs_test_guard._ORIGINAL_KILL(pid, signal.SIGKILL)
        child.wait(timeout=10)


def test_pgrep_returns_only_processes_of_this_run(processes):
    token, child, decoy = processes
    # the system pgrep matches both, which is what a live service would look like
    assert {child.pid, decoy} <= _real_pgrep(token)

    found = subprocess.run(["pgrep", "-f", token], capture_output=True, text=True)
    assert found.returncode == 0
    assert {int(line) for line in found.stdout.split()} == {child.pid}

    # a child interpreter has no in-process guard: the PATH shim still narrows it
    code = ("import subprocess, sys; r = subprocess.run(['pgrep', '-f', sys.argv[1]], capture_output=True, text=True); "
            "print(r.returncode, r.stdout.split())")
    nested = subprocess.run([sys.executable, "-c", code, token], capture_output=True, text=True, timeout=30)
    assert nested.stdout.strip() == f"0 ['{child.pid}']"


def test_pgrep_reports_no_match_when_only_outside_processes_match(processes):
    token, child, decoy = processes
    child.terminate()
    child.wait(timeout=10)
    assert decoy in _real_pgrep(token)
    found = subprocess.run(["pgrep", "-f", token], capture_output=True, text=True)
    assert (found.returncode, found.stdout) == (1, "")


def test_system_process_matchers_cannot_be_started_directly():
    for command in (["/usr/bin/pgrep", "-f", "x"], ["pkill", "-f", "mcs-guard-none"], ["killall", "mcs-guard-none"]):
        if os.sep in command[0]:
            with pytest.raises(RuntimeError, match="disabled in MCS tests"):
                subprocess.run(command, capture_output=True)
        else:
            assert subprocess.run(command, capture_output=True).returncode == 97


def test_signal_reaches_only_processes_of_this_run(processes):
    _token, child, decoy = processes
    for send in (os.kill, os.killpg):
        with pytest.raises(PermissionError, match="disabled in MCS tests"):
            send(decoy, signal.SIGTERM)
    time.sleep(0.2)
    assert _alive(decoy)

    os.kill(decoy, 0)                        # probing stays allowed
    child.terminate()                        # an owned child is stopped as before
    assert child.wait(timeout=10) == -signal.SIGTERM
    with pytest.raises(ProcessLookupError):  # an absent pid keeps the kernel's answer
        os.kill(child.pid, signal.SIGTERM)
