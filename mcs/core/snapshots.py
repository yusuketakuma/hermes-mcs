"""Publish verified snapshot files and own their temporary connections and validator process."""

import fcntl
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from time import monotonic
import uuid
from contextlib import suppress

from safe_io import fdopen_owned, publish_tmp


SNAPSHOT_VERIFY_TIMEOUT_S = 30.0
# quick_check time grows with the file: the verify budget adds one second
# per SNAPSHOT_VERIFY_RATE_BPS of snapshot, capped, so a ledger that has
# outgrown the floor is not refused on every publish (H4)
SNAPSHOT_VERIFY_RATE_BPS = 64 * 1024 * 1024
SNAPSHOT_VERIFY_EXTRA_MAX_S = 150.0


def snapshot_verify_timeout_s(path: str) -> float:
    """Verify budget for a snapshot candidate of this size."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return SNAPSHOT_VERIFY_TIMEOUT_S + min(
        SNAPSHOT_VERIFY_EXTRA_MAX_S, size / SNAPSHOT_VERIFY_RATE_BPS)
# Why the latest publish returned None: a fixed code plus elapsed seconds,
# never paths or database content. Reset at the start of every publish.
_failure: dict = {}


def last_failure() -> dict:
    """Bounded reason and timings of the most recent unpublished snapshot."""
    return dict(_failure)


def _fail(reason: str, **extra) -> None:
    _failure.update(reason=reason, **extra)


# Exit 1 is only an explicit validation refusal; a failed bootstrap/import
# exits 3, an exception inside the validator 4 and running out of its own
# deadline 5, so the parent can tell them apart without reading the child's
# output. Success is still exit 0 only.
_SNAPSHOT_VALIDATION_CODE = (
    "import sys\n"
    "try:\n"
    "    sys.path.insert(0, sys.argv[1]); import _mcs_path; from ledger import valid_mcs_db\n"
    "except BaseException:\n"
    "    sys.exit(3)\n"
    "try:\n"
    "    ok = valid_mcs_db(sys.argv[2], deadline=float(sys.argv[3]))\n"
    "except BaseException:\n"
    "    sys.exit(4)\n"
    "import time\n"
    "sys.exit(0 if ok is True else 5 if time.monotonic() >= float(sys.argv[3]) else 1)\n"
)
_EXIT_REASONS = {1: "validator_rejected", 3: "validator_bootstrap_error", 4: "validator_exception",
                 5: "validator_timeout"}


def _valid_snapshot_candidate(path: str, *, deadline: float) -> bool:
    """Isolate candidate validation so disk waits cannot hold the publisher."""
    if monotonic() >= deadline:
        _fail("verify_deadline_at_start")
        return False
    started = monotonic()
    try:
        fd = os.open(os.path.join(os.path.dirname(path), '.validation.lock'),
                     os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with fdopen_owned(fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _fail("validation_lock_busy")
                return False
            child = subprocess.Popen(
                [sys.executable, '-I', '-c', _SNAPSHOT_VALIDATION_CODE,
                 str(Path(__file__).resolve().parents[1]), path, str(deadline)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, pass_fds=(fd,))
            try:
                code = child.wait(timeout=max(0, deadline - monotonic()))
                if code != 0:
                    _fail(_EXIT_REASONS.get(code, "validator_exit"),
                          exit_code=code, verify_s=round(monotonic() - started, 3))
                return code == 0
            except subprocess.TimeoutExpired:
                _fail("validator_timeout", verify_s=round(monotonic() - started, 3),
                      budget_s=round(deadline - started, 3))
                child.kill()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    # The inherited lock fences new validators until even an
                    # uninterruptible disk wait exits and the OS closes it.
                    pass
                return False
            except BaseException:
                # Cleanup must not replace the original interruption/wait error.
                # An unreaped child keeps the inherited lock as a validator fence.
                with suppress(BaseException):
                    child.kill()
                with suppress(BaseException):
                    child.wait(timeout=1)
                raise
    except OSError as error:
        _fail("validator_oserror", errno=error.errno)
        return False


class _SnapshotDeadline(Exception):
    """The copy ran into the caller's hard deadline."""


def publish_snapshot(db_path: str, dest_dir: str, *,
                     deadline: float | None = None) -> str | None:
    """Verified point-in-time copy for read-only consumers.
    backup -> tmp -> quick_check -> atomic rename; consumers never see a
    half-written snapshot. Returns the published path or None.
    ``deadline`` (monotonic) is the caller's hard stop, e.g. its armed
    watchdog: the copy is abandoned and the verify budget capped there,
    so a large ledger fails this publish instead of getting the process
    killed mid-copy on every tick."""
    _failure.clear()
    if deadline is not None and monotonic() >= deadline:
        _fail("deadline_before_copy")
        return None
    started = monotonic()

    def check_deadline(*_):
        if deadline is not None and monotonic() >= deadline:
            raise _SnapshotDeadline
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    dest = os.path.join(dest_dir, "ledger-snapshot.db")
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".snapshot.", suffix=".tmp")
    os.close(fd)  # private from creation, including throughout the backup
    try:
        src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                              uri=True)
        try:
            dst = sqlite3.connect(tmp)
            try:
                # one read snapshot across the steps: a write by another
                # connection would otherwise restart the copy every step
                src.execute("BEGIN")
                src.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
                try:
                    src.backup(dst, pages=4096, progress=check_deadline)
                except _SnapshotDeadline:
                    _fail("copy_deadline", copy_s=round(monotonic() - started, 3))
                    return None
                with dst:
                    dst.execute("INSERT OR REPLACE INTO snapshot_meta("
                                "singleton,generation_id,generated_at) "
                                "VALUES(1,?,?)",
                                (str(uuid.uuid4()), time.time()))
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        copied = monotonic()
        # Verification reads the same bytes the copy just wrote: when the disk is
        # slow enough to stretch the copy, the size-based budget cannot hold, so
        # the budget follows the measured copy (same cap, still under the deadline).
        verify_by = copied + max(snapshot_verify_timeout_s(tmp), min(
            SNAPSHOT_VERIFY_TIMEOUT_S + SNAPSHOT_VERIFY_EXTRA_MAX_S, 1.5 * (copied - started)))
        if deadline is not None:
            verify_by = min(verify_by, deadline)
        if not _valid_snapshot_candidate(tmp, deadline=verify_by):
            _failure.setdefault("reason", "validator_false")
            _fail(_failure["reason"], copy_s=round(copied - started, 3),
                  capped_by_deadline=deadline is not None and verify_by == deadline)
            return None
        publish_tmp(tmp, dest)
        # A reader keeps its old inode or opens the complete new generation.
        return dest
    finally:
        with suppress(OSError):
            os.unlink(tmp)
