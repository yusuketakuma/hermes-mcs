"""Collect short-lived I/O workers without letting inherited pipes extend deadlines."""
import subprocess
from contextlib import suppress

REAP_TIMEOUT_S = 5.0


def reap_worker(process) -> None:
    """Drain a stopped worker within a bound, then close pipes still held elsewhere.

    communicate() after kill waits for EOF, which never comes while a
    descendant still holds a pipe. Killing the worker remains with its caller.
    """
    try:
        process.communicate(timeout=REAP_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with suppress(OSError):
                    stream.close()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.1)
