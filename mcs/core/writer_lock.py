"""Own writer-lock descriptors and reject stale generations after transport or waiting."""
import fcntl
import os
import time
from contextlib import contextmanager, suppress

from mcs_util import HOME
from safe_io import open_lock_file

RUN_LOCK = os.path.join(HOME, "data", "run.lock")
UPDATE_MARKER_NAME = "update_in_progress.marker"
_MCS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_LOCK_REACQUIRE_S = 150.0
RUN_LOCK_POLL_S = 0.2


def acquire_run_lock(path: str | None = None) -> int | None:
    """Non-blocking exclusive flock on the run lockfile.

    Returns the open fd — the lock lives as long as the fd stays open
    (hold it in main() scope; process exit releases it). Returns None
    when another writer holds it. Every entry point that writes the
    ledger must hold this lock so a manual CLI can never interleave
    writes with a scheduled tick. The one documented exception is the
    resident extract_llm drainer, which takes it per result write and
    leaves its fenced bookkeeping writes lockless (see
    ``extract_llm._write_lock``)."""
    path = path or RUN_LOCK
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = open_lock_file(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def code_stamp() -> int:
    """Newest mtime of the mcs/ sources — changes when an update merges."""
    newest = 0
    for base, _dirs, files in os.walk(_MCS_ROOT):
        for name in files:
            if name.endswith(".py"):
                with suppress(OSError):
                    newest = max(newest, os.stat(
                        os.path.join(base, name)).st_mtime_ns)
    return newest


def code_stamp_checked() -> int:
    """Read the same source stamp, failing if any part is unavailable."""
    def fail(error):
        raise error

    newest = 0
    for base, _dirs, files in os.walk(_MCS_ROOT, onerror=fail):
        for name in files:
            if name.endswith('.py'):
                newest = max(newest, os.stat(os.path.join(base, name)).st_mtime_ns)
    if not newest:
        raise OSError('source_stamp_unavailable')
    return newest


class RunLockLost(BaseException):
    """The run lock could not be safely retaken after unlocked transport.

    ``held`` tells whether the fd holds the lock again (an updater ran in
    the window: old code must not keep writing) or not (bounded wait
    expired: no DB write is allowed at all). BaseException on purpose:
    the generic ``except Exception`` retry paths must not charge an
    attempt for it — the drain catches it explicitly."""

    def __init__(self, held: bool, updated: bool = False):
        super().__init__("run_lock_held" if held
                         else "run_lock_timeout_updated" if updated
                         else "run_lock_timeout")
        self.held = held
        # an updater ran while the bounded wait was expiring: the lock
        # is not held, but this code is stale and must not stay resident
        self.updated = updated


def writer_blocked() -> bool:
    """Block writers for update/restore holds or an unverifiable restore state."""
    data = os.path.join(HOME, "data")
    if os.path.lexists(os.path.join(data, UPDATE_MARKER_NAME)):
        return True
    try:
        from notify_spool import restore_writer_hold
        return restore_writer_hold(data) is not None
    except Exception:
        return True


def updater_state() -> tuple:
    return (writer_blocked(), code_stamp())


@contextmanager
def unlocked_transport(db, lock_fd, wait_s: float | None = None,
                       deadline: float | None = None):
    """Release the run lock during transport and reacquire before DB work.

    The reacquire is bounded (``wait_s``, default RUN_LOCK_REACQUIRE_S,
    never past the caller's absolute ``deadline``) and refuses a lock won
    right after an updater — same rule as wait_run_lock: an
    update marker that appeared or a code stamp that moved during the
    window raises RunLockLost(held=True); an expired wait raises
    RunLockLost(held=False), with ``updated`` set when the same check
    fails during the expiry."""
    if lock_fd is None:
        yield
        return
    if db.in_transaction:
        raise RuntimeError("transport_inside_db_transaction")
    before = updater_state()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    try:
        yield
    finally:
        until = time.monotonic() + (
            RUN_LOCK_REACQUIRE_S if wait_s is None else wait_s)
        if deadline is not None:
            until = min(until, deadline)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise RunLockLost(
                        held=False, updated=updater_state() != before) from None
                time.sleep(RUN_LOCK_POLL_S)
        if updater_state() != before:
            raise RunLockLost(held=True)


def wait_run_lock(path: str, *, wait_s: float, expected_code_stamp: int,
                  marker_path: str) -> int | None:
    """Poll the run lock until ``wait_s`` elapses — short polls so a
    holder that releases between batches is won, not missed. A lock won
    right after an updater released it is given back: this process
    loaded the OLD code before waiting, and running it (with modules
    imported later from the new tree) would mix generations."""
    until = time.monotonic() + wait_s
    while time.monotonic() < until:
        time.sleep(RUN_LOCK_POLL_S)
        fd = acquire_run_lock(path)
        if fd is not None:
            if os.path.exists(marker_path) or code_stamp() != expected_code_stamp:
                os.close(fd)
                return None
            return fd
    return None
