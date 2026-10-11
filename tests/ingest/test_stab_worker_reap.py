"""A killed MCS worker whose pipe is still held by a descendant must not
block the caller past the reap bound. Local Python children only."""
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress

import mcs_worker


def test_reap_is_bounded_when_a_descendant_holds_the_pipe(monkeypatch):
    monkeypatch.setattr(mcs_worker, "REAP_TIMEOUT_S", 0.5)
    code = ("import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, start_new_session=True)
    try:
        time.sleep(0.5)
        proc.kill()
        started = time.monotonic()
        mcs_worker._reap(proc)
        assert time.monotonic() - started < 5
        assert proc.stdout.closed
    finally:
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)


def test_reap_collects_a_cooperative_child_normally():
    proc = subprocess.Popen([sys.executable, "-c", "print('x')"],
                            stdout=subprocess.PIPE)
    mcs_worker._reap(proc)
    assert proc.returncode == 0
