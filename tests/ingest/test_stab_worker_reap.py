"""A killed MCS worker whose pipe is still held by a descendant must not
block the caller past the reap bound. Local Python children only."""
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress

import worker_process


def test_reap_is_bounded_when_a_descendant_holds_the_pipe(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_process, "REAP_TIMEOUT_S", 0.5)
    ready = tmp_path / "descendant-ready"
    descendant = ("from pathlib import Path; import sys, time; "
                  "Path(sys.argv[1]).write_text('ready'); time.sleep(30)")
    code = ("import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {descendant!r}, {str(ready)!r}]); "
            "time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, start_new_session=True)
    try:
        # The descendant must own the inherited pipe before its parent dies.
        # A fixed sleep can kill a slow-starting parent before it spawns one.
        ready_deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < ready_deadline:
            assert proc.poll() is None, "synthetic parent exited before readiness"
            time.sleep(0.01)
        assert ready.is_file(), "synthetic descendant must have started"
        proc.kill()
        started = time.monotonic()
        worker_process.reap_worker(proc)
        assert time.monotonic() - started < 5
        assert proc.stdout.closed
    finally:
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        finally:
            proc.stdin.close()
            proc.stdout.close()


def test_reap_collects_a_cooperative_child_normally():
    proc = subprocess.Popen([sys.executable, "-c", "print('x')"],
                            stdout=subprocess.PIPE)
    worker_process.reap_worker(proc)
    assert proc.returncode == 0
