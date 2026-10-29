"""Synthetic standalone CLI identity, exact bytes, EOF and failure contracts."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

import run_log


@pytest.fixture
def cli(tmp_path):
    # Copy only the stdlib module: no package bootstrap or installed project.
    script = tmp_path / "run_log.py"
    script.write_bytes(Path(run_log.__file__).read_bytes())
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    return script, data


def invoke(cli, *args, **kwargs):
    script, data = cli
    return subprocess.run([sys.executable, "-I", str(script), *args[:1],
                           "--data-dir", str(data), *args[1:]],
                          capture_output=True, timeout=20, **kwargs)


def wait_bytes(path, expected):
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if path.exists() and path.read_bytes() == expected:
            return
        time.sleep(0.01)
    assert path.read_bytes() == expected


@pytest.mark.parametrize("command", ["append-stdin", "append-file"])
def test_append_cli_preserves_bytes_and_leaves_notification_stdout(cli, command):
    script, data = cli
    payload = b"x" * (run_log.BLOCK * 3) + b"\nlast\x00partial"
    source = script.parent / "synthetic.log"
    source.write_bytes(payload)
    source.chmod(0o600)
    result = invoke(cli, command, *(["--file", str(source)] if command == "append-file" else []),
                    input=payload if command == "append-stdin" else b"unused synthetic input")
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert (data / run_log.LOG).read_bytes() == payload
    assert source.read_bytes() == payload


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public"])
def test_append_file_rejects_untrusted_sources(cli, kind):
    script, data = cli
    source = script.parent / "source"
    protected = script.parent / "protected"
    protected.write_bytes(b"protected synthetic")
    protected.chmod(0o600)
    if kind == "symlink":
        source.symlink_to(protected)
    elif kind == "hardlink":
        os.link(protected, source)
    elif kind == "fifo":
        os.mkfifo(source, 0o600)
    else:
        source.write_bytes(b"private synthetic")
        source.chmod(0o644)
    result = invoke(cli, "append-file", "--file", str(source))
    assert result.returncode == 1
    assert result.stderr == b"mcs_log_operation_failed\n"
    assert protected.read_bytes() == b"protected synthetic"
    assert (data / run_log.LOG).read_bytes() == b""


def test_exec_preserves_pid_argv_stdin_exit_and_reaps_drainer_at_eof(cli):
    script, data = cli
    child_pid = script.parent / "drainer.pid"
    reaped = script.parent / "reaped.json"
    # The synthetic producer closes its outputs before waitpid, proving that the
    # drainer holds no duplicate writer and really exits at EOF.
    producer = ("import os,sys,json;from pathlib import Path;"
                "assert os.environ['MCS_RUN_LOG_CAPTURED']=='1';"
                "assert os.environ['MCS_RUN_LOG_HELPER']==str(Path(sys.argv[1]).parent/'run_log.py');"
                "assert sys.stdin.buffer.read()==b'synthetic authentication input';"
                "os.write(1,json.dumps([os.getpid(),sys.argv]).encode()+b'\\n');"
                "os.write(2,b'x'*200000+b'final');os.close(1);os.close(2);"
                "pid,status=os.waitpid(int(Path(sys.argv[1]).read_text()),0);"
                "Path(sys.argv[2]).write_text(json.dumps([pid,status]));sys.exit(7)")
    launcher = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import run_log;"
                "original=run_log.subprocess.Popen\n"
                "def launch(*a,**kw):\n"
                " child=original(*a,**kw);Path(sys.argv[3]).write_text(str(child.pid));return child\n"
                "run_log.subprocess.Popen=launch\n"
                "run_log.exec_logged(sys.argv[2],sys.argv[4:])")
    process = subprocess.Popen([sys.executable, "-I", "-c", launcher, str(script.parent), str(data),
                                str(child_pid), sys.executable, "-I", "-c", producer,
                                str(child_pid), str(reaped), "literal;$(no-shell)"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env={**os.environ, "MCS_RUN_LOG_CAPTURED": "synthetic-parent"})
    stdout, stderr = process.communicate(b"synthetic authentication input", timeout=20)
    assert (process.returncode, stdout, stderr) == (7, b"", b"")
    lines = (data / run_log.LOG).read_bytes().split(b"\n", 1)
    identity = json.loads(lines[0])
    assert identity == [process.pid, ["-c", str(child_pid), str(reaped), "literal;$(no-shell)"]]
    assert b"synthetic authentication input" not in (data / run_log.LOG).read_bytes()
    assert lines[1] == b"x" * 200000 + b"final"
    assert json.loads(reaped.read_text()) == [int(child_pid.read_text()), 0]


def test_exec_failure_restores_descriptors_and_reaps_actual_child(cli):
    script, data = cli
    code = ("import sys,os,errno;sys.path.insert(0,sys.argv[1]);import run_log\n"
            "before=[os.fstat(fd) for fd in (1,2)];before_env=dict(os.environ);"
            "pids=[];original=run_log.subprocess.Popen\n"
            "def launch(*a,**kw):\n"
            " child=original(*a,**kw);pids.append(child.pid);return child\n"
            "run_log.subprocess.Popen=launch\n"
            "try:run_log.exec_logged(sys.argv[2],['/synthetic/missing-command'])\n"
            "except FileNotFoundError:pass\n"
            "assert before==[os.fstat(fd) for fd in (1,2)]\n"
            "assert dict(os.environ)==before_env\n"
            "try:os.waitpid(pids[0],os.WNOHANG)\n"
            "except ChildProcessError:pass\n"
            "else:raise AssertionError('drainer not reaped')\n"
            "os.write(1,b'notification preserved');os.write(2,b'failure restored')")
    result = subprocess.run([sys.executable, "-I", "-c", code, str(script.parent), str(data)],
                            capture_output=True, timeout=20)
    assert (result.returncode, result.stdout, result.stderr) == (
        0, b"notification preserved", b"failure restored")
    assert (data / run_log.LOG).read_bytes() == b""


def test_exec_cli_rejects_relative_executable_without_shell(cli):
    result = invoke(cli, "exec", "--", "echo", "synthetic")
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"mcs_log_operation_failed\n"


def test_second_pipe_failure_closes_first_pipe(cli, monkeypatch):
    _script, data = cli
    original = run_log._pipe_above_stdio
    opened = []

    def fail_second():
        if opened:
            raise OSError("synthetic descriptor exhaustion")
        values = original()
        opened.extend(values)
        return values

    monkeypatch.setattr(run_log, "_pipe_above_stdio", fail_second)
    with pytest.raises(OSError, match="synthetic descriptor exhaustion"):
        run_log.exec_logged(data, [sys.executable, "-c", "pass"])
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_drainer_start_failure_is_reported_and_reaped_before_exec(cli, monkeypatch):
    _script, data = cli
    original = run_log.subprocess.Popen
    children = []

    def failing_child(_argv, **kwargs):
        child = original([sys.executable, "-I", "-c", "raise SystemExit(3)"], **kwargs)
        children.append(child)
        return child

    def forbidden_exec(*_):
        raise AssertionError("exec before ready")

    monkeypatch.setattr(run_log.subprocess, "Popen", failing_child)
    monkeypatch.setattr(run_log.os, "execve", forbidden_exec)
    before = [os.fstat(fd) for fd in (1, 2)]
    with pytest.raises(run_log.RunLogError, match="drain_start_failed"):
        run_log.exec_logged(data, [sys.executable, "-c", "pass"])
    assert before == [os.fstat(fd) for fd in (1, 2)]
    assert children[0].returncode == 3
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)


def test_drainer_rejects_regular_fd(cli):
    script, data = cli
    source = script.parent / "not-a-pipe"
    source.write_bytes(b"never read")
    fd = os.open(source, os.O_RDONLY)
    read_fd, write_fd = os.pipe()
    try:
        result = subprocess.run([sys.executable, "-I", str(script), "_drain", "--data-dir",
                                 str(data), str(fd), str(write_fd)],
                                pass_fds=(fd, write_fd), capture_output=True, timeout=10)
        assert result.returncode == 1
        assert result.stderr == b"mcs_log_operation_failed\n"
        assert not (data / run_log.LOG).exists()
    finally:
        for descriptor in (fd, read_fd, write_fd):
            os.close(descriptor)


def test_exec_group_term_preserves_producer_shutdown_output(cli):
    script, data = cli
    code = ("import os,signal,time\n"
            "def done(*_):\n"
            " os.write(2,b'finished\\n');raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,done);os.write(1,b'ready\\n')\n"
            "while True:time.sleep(0.05)")
    process = subprocess.Popen([sys.executable, "-I", str(script), "exec", "--data-dir",
                                str(data), "--", sys.executable, "-I", "-c", code],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        wait_bytes(data / run_log.LOG, b"ready\n")
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert (process.returncode, stdout, stderr) == (0, b"", b"")
        wait_bytes(data / run_log.LOG, b"ready\nfinished\n")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_finished_waits_for_descendant_eof_after_direct_child_exit(cli):
    script, data = cli
    release = script.parent / "release"
    grandchild = ("import os,sys,time;from pathlib import Path\n"
                  "while not Path(sys.argv[1]).exists():time.sleep(0.005)\n"
                  "os.write(1,b'descendant complete\\n')")
    producer = "import subprocess,sys;subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],sys.argv[2]])"
    capture = run_log.PipeCapture(data)
    child = None
    try:
        child = subprocess.Popen([sys.executable, "-I", "-c", producer, grandchild, str(release)],
                                 stdout=capture.write_fd, stderr=capture.write_fd)
        capture.started()
        assert child.wait(timeout=10) == 0
        assert not capture.finished.is_set()
    finally:
        release.touch()
        if child is not None:
            child.wait(timeout=10)
        assert capture.finished.wait(timeout=10)
        capture.close()
    assert (data / run_log.LOG).read_bytes() == b"descendant complete\n"


def test_finished_is_set_after_failed_capture_closes_read_fd(cli, monkeypatch):
    _script, data = cli
    capture = run_log.PipeCapture(data)

    def failed_emit(_record):
        raise OSError("synthetic capacity failure")

    monkeypatch.setattr(capture, "_emit", failed_emit)
    child = None
    try:
        child = subprocess.Popen([sys.executable, "-I", "-c", "import os;os.write(1,b'failed\\n')"],
                                 stdout=capture.write_fd, stderr=capture.write_fd)
        capture.started()
        assert child.wait(timeout=10) == 0
        assert capture.finished.wait(timeout=10)
        assert capture.failed.is_set()
        with pytest.raises(OSError):
            os.fstat(capture._read)
    finally:
        if child is not None:
            child.wait(timeout=10)
        with pytest.raises(run_log.RunLogError, match="capture_failed"):
            capture.close()
