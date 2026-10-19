"""Own command sessions through interruption, termination and bounded reaping."""
import os
import signal
import subprocess
from contextlib import suppress

TERM_GRACE_S = 2  # give Git time to remove its own lock before KILL


def run_owned(argv: list[str], timeout: float, environment: dict,
               stdin=None) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output, text) for one command in its own
    session. On timeout or interruption that session is stopped by
    stop_owned and the exception re-raised. Worst case: timeout +
    TERM_GRACE_S + the bounded reap."""
    child = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=environment,
                             start_new_session=True)
    try:
        out, err = child.communicate(timeout=timeout)
    except BaseException:
        stop_owned(child)
        raise
    return subprocess.CompletedProcess(argv, child.returncode, out, err)


def stop_owned(child) -> None:
    """SIGTERM first so git cleans its lock, SIGKILL after the grace,
    then a bounded reap. Only a still-unreaped leader's group is
    signalled — its pid (= group id) cannot have been reused."""
    if child.returncode is None:
        with suppress(OSError):
            os.killpg(child.pid, signal.SIGTERM)
        with suppress(BaseException):
            child.communicate(timeout=TERM_GRACE_S)
        if child.returncode is None:
            with suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)
    with suppress(BaseException):
        child.communicate(timeout=5)
    if child.returncode is None:
        with suppress(BaseException):
            child.wait(timeout=5)
    for stream in (child.stdout, child.stderr):
        if stream is not None:
            with suppress(OSError):
                stream.close()   # an escaped holder must not leak our fds
