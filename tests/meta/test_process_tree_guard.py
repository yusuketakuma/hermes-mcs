"""Process matching and signals in tests stay inside the test run's own tree.

An update routine finds leftover workers with ``pgrep -u <uid> -f`` and stops
them with ``os.kill``. Neither depends on HOME or MCS_ROOT, so a test that runs
such a routine for real would reach the live services of the same user. The
decoy here stands for such a service: it matches by name and is not a
descendant of this run. It lives only while the test holds the write end of a
FIFO, so no cleanup ever signals a bare pid outside the run.
"""
import errno
import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

import mcs_test_guard

_SLEEP = "import time; time.sleep(30)"
# Blocks until the test opens the FIFO, then exits when the test closes it. If the
# test never opens it (an error or an interruption before the handshake), the
# alarm ends the decoy by itself.
_DECOY = "import signal, sys; signal.alarm(30); stream = open(sys.argv[2]); signal.alarm(0); stream.read()"


def _exists(pid):
    table = mcs_test_guard._process_table()
    assert table is not None
    return pid in table


def _real_pgrep(token):
    for candidate in ("/usr/bin/pgrep", "/bin/pgrep"):
        if os.access(candidate, os.X_OK):
            # the unguarded Popen: the guard refuses the system pgrep to tests
            with mcs_test_guard._ORIGINAL_POPEN([candidate, "-f", token], stdout=subprocess.PIPE, text=True) as process:
                return {int(line) for line in process.communicate(timeout=30)[0].split()}
    pytest.skip("pgrep is not installed")


def _open_writer(fifo):
    deadline = time.monotonic() + 10
    while True:
        try:
            return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:      # no reader yet
            if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


@pytest.fixture
def processes(tmp_path):
    """An owned child and a decoy outside this run, both named by one token.

    The decoy is started by a shell that leads its own session and exits at
    once: the decoy is re-parented away from this run and its process group
    has no leader any more."""
    token = "mcs-guard-" + uuid.uuid4().hex
    fifo = str(tmp_path / "decoy-control")
    os.mkfifo(fifo, 0o600)
    child = subprocess.Popen([sys.executable, "-c", _SLEEP, token], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    writer = None
    try:
        shell = subprocess.run(
            ["/bin/sh", "-c", '"$0" -c "$1" "$2" "$3" </dev/null >/dev/null 2>&1 & echo "$! $$"',
             sys.executable, _DECOY, token, fifo],
            capture_output=True, text=True, timeout=30, start_new_session=True)
        decoy, group = (int(value) for value in shell.stdout.split())
        writer = _open_writer(fifo)   # the decoy is running and now waits for this end to close
        deadline = time.monotonic() + 10
        while mcs_test_guard._in_test_tree(decoy) and time.monotonic() < deadline:
            time.sleep(0.05)
        table = mcs_test_guard._process_table()
        assert decoy in table and table[decoy][1] == group and group not in table
        assert not mcs_test_guard._in_test_tree(decoy, table)
        assert child.poll() is None and table[child.pid][0] == os.getpid()
        yield token, child, decoy, group
    finally:
        if writer is not None:
            os.close(writer)          # end of input: the decoy exits by itself
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_pgrep_returns_only_processes_of_this_run(processes):
    token, child, decoy, _group = processes
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
    token, child, decoy, _group = processes
    child.terminate()
    child.wait(timeout=10)
    assert decoy in _real_pgrep(token)
    found = subprocess.run(["pgrep", "-f", token], capture_output=True, text=True)
    assert (found.returncode, found.stdout) == (1, "")


@pytest.mark.parametrize("ps_body,root", [
    ("exit 1\n", None),               # ps fails
    ("exit 2\n", None),               # ps fails without output
    ("exit 0\n", None),               # ps succeeds but lists nothing
    ("echo not-a-pid\n", None),       # ps answers something that is not a pid
    (None, "x"),                       # the run's root is unknown
    (None, "9999999"),                 # the run's root is a number that is not a live process
])
def test_pgrep_that_cannot_verify_is_not_a_no_match(tmp_path, ps_body, root):
    # update_services treats exit 1 as "none left" and anything above as unverifiable
    tools = tmp_path / "bin"
    tools.mkdir()
    if ps_body is not None:
        (tools / "ps").write_text("#!/bin/sh\n" + ps_body)
        (tools / "ps").chmod(0o755)
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])
    if root is not None:
        env["MCS_TEST_ROOT_PID"] = root
    found = subprocess.run(["pgrep", "-f", "mcs-guard-none"], env=env, capture_output=True, text=True)
    assert found.returncode == 3 and found.stdout == ""


@pytest.mark.parametrize("listing", [
    "exit 2",                                   # the listing after the search fails
    "exit 0",                                   # ... or is empty
    'echo "$MCS_TEST_ROOT_PID 1"',              # ... or does not show a live match
])
def test_pgrep_with_a_match_it_cannot_place_is_unverified(tmp_path, processes, listing):
    token, child, _decoy, _group = processes
    tools = tmp_path / "bin"
    tools.mkdir()
    # the root check (`-p`) answers; only the full listing after the search is broken
    (tools / "ps").write_text('#!/bin/sh\ncase " $* " in *" -A "*) ' + listing
                              + ' ;; *) echo "$MCS_TEST_ROOT_PID" ;; esac\n')
    (tools / "ps").chmod(0o755)
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])
    found = subprocess.run(["pgrep", "-f", token], env=env, capture_output=True, text=True)
    assert child.poll() is None
    assert found.returncode == 3 and found.stdout == ""


def test_system_process_matchers_cannot_be_started_directly():
    for command in (["/usr/bin/pgrep", "-f", "x"], ["pkill", "-f", "mcs-guard-none"], ["killall", "mcs-guard-none"]):
        if os.sep in command[0]:
            with pytest.raises(RuntimeError, match="disabled in MCS tests"):
                subprocess.run(command, capture_output=True)
        else:
            assert subprocess.run(command, capture_output=True).returncode == 97


def test_signal_reaches_only_processes_of_this_run(processes):
    _token, child, decoy, group = processes
    # the single process, and the leaderless group it lives in, are both refused
    assert not mcs_test_guard._signal_allowed(decoy)
    assert not mcs_test_guard._signal_allowed(-group)
    with pytest.raises(PermissionError, match="disabled in MCS tests"):
        os.kill(decoy, signal.SIGTERM)
    with pytest.raises(PermissionError, match="disabled in MCS tests"):
        os.killpg(group, signal.SIGTERM)
    with pytest.raises(PermissionError, match="disabled in MCS tests"):
        os.kill(-group, signal.SIGTERM)
    time.sleep(0.2)
    assert _exists(decoy)

    os.kill(decoy, 0)                        # probing stays allowed
    child.terminate()                        # an owned child is stopped as before
    assert child.wait(timeout=10) == -signal.SIGTERM
    with pytest.raises(ProcessLookupError):  # an absent pid keeps the kernel's answer
        os.kill(child.pid, signal.SIGTERM)


def test_group_signal_needs_every_member_inside_this_run():
    # The decisions only: sending these for real would signal the test run itself.
    assert not mcs_test_guard._signal_allowed(-1)
    # this run's own group also holds the shell that started pytest
    table = mcs_test_guard._process_table()
    own = [pid for pid, (_parent, group) in table.items() if group == os.getpgrp()]
    expected = all(mcs_test_guard._in_test_tree(pid, table) for pid in own)
    assert mcs_test_guard._signal_allowed(0) is expected
    assert mcs_test_guard._signal_allowed(-os.getpgrp()) is expected


def test_group_started_by_a_test_can_be_stopped():
    # the usual cleanup of a session a test started itself
    leader = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & wait"], start_new_session=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            table = mcs_test_guard._process_table()
            if sum(group == leader.pid for _parent, group in table.values()) >= 2:
                break
            time.sleep(0.02)
        os.killpg(leader.pid, signal.SIGTERM)
        assert leader.wait(timeout=10) == -signal.SIGTERM
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait(timeout=10)


def test_unreadable_process_table_refuses_instead_of_allowing(monkeypatch):
    monkeypatch.setattr(mcs_test_guard, "_process_table", lambda: None)
    assert not mcs_test_guard._signal_allowed(os.getppid())
    assert not mcs_test_guard._signal_allowed(0)
    assert mcs_test_guard._signal_allowed(os.getpid())


def _recording_signals(monkeypatch, probe):
    """Replace both real senders: record every call, answer signal 0 with `probe`."""
    sent = []

    def kill(pid, sig):
        sent.append(("kill", pid, sig))
        if sig == 0:
            probe()

    def killpg(group, sig):
        sent.append(("killpg", group, sig))
        if sig == 0:
            probe()

    monkeypatch.setattr(mcs_test_guard, "_ORIGINAL_KILL", kill)
    monkeypatch.setattr(mcs_test_guard, "_ORIGINAL_KILLPG", killpg)
    return sent


def _raise(error):
    def probe():
        raise error
    return probe


@pytest.mark.parametrize("send,target", [
    (os.kill, 4242), (os.kill, -4242), (os.killpg, 4242)])
def test_target_missing_from_the_process_table_is_never_signalled(monkeypatch, send, target):
    # a readable table that does not show the target proves nothing about who owns it
    table = {os.getpid(): (os.getppid(), os.getpgrp())}
    monkeypatch.setattr(mcs_test_guard, "_process_table", lambda: table)

    # it exists after all: refused
    sent = _recording_signals(monkeypatch, lambda: None)
    with pytest.raises(PermissionError, match="disabled in MCS tests"):
        send(target, signal.SIGTERM)
    assert [call[2] for call in sent] == [0]

    # it does not exist: the kernel's own answer, and still nothing is sent
    sent = _recording_signals(monkeypatch, _raise(ProcessLookupError(errno.ESRCH, "No such process")))
    with pytest.raises(ProcessLookupError):
        send(target, signal.SIGTERM)
    assert [call[2] for call in sent] == [0]

    # any other failure of the probe is not turned into permission to send
    sent = _recording_signals(monkeypatch, _raise(OSError(errno.EIO, "Input/output error")))
    with pytest.raises(OSError) as raised:
        send(target, signal.SIGTERM)
    assert raised.value.errno == errno.EIO and [call[2] for call in sent] == [0]
