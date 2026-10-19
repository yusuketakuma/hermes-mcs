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

from safe_io import publish_tmp


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
_SNAPSHOT_VALIDATION_CODE = (
    "import sys; sys.path.insert(0, sys.argv[1]); import _mcs_path; "
    "from ledger import valid_mcs_db; "
    "sys.exit(0 if valid_mcs_db(sys.argv[2], deadline=float(sys.argv[3])) else 1)"
)


def _valid_snapshot_candidate(path: str, *, deadline: float) -> bool:
    """Isolate candidate validation so disk waits cannot hold the publisher."""
    if monotonic() >= deadline:
        return False
    try:
        fd = os.open(os.path.join(os.path.dirname(path), '.validation.lock'),
                     os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            child = subprocess.Popen(
                [sys.executable, '-I', '-c', _SNAPSHOT_VALIDATION_CODE,
                 str(Path(__file__).resolve().parents[1]), path, str(deadline)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, pass_fds=(fd,))
            try:
                return child.wait(timeout=max(0, deadline - monotonic())) == 0
            except subprocess.TimeoutExpired:
                child.kill()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    # The inherited lock fences new validators until even an
                    # uninterruptible disk wait exits and the OS closes it.
                    pass
                return False
    except OSError:
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
    if deadline is not None and monotonic() >= deadline:
        return None

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
        verify_by = monotonic() + snapshot_verify_timeout_s(tmp)
        if deadline is not None:
            verify_by = min(verify_by, deadline)
        if not _valid_snapshot_candidate(tmp, deadline=verify_by):
            return None
        publish_tmp(tmp, dest)
        # A reader keeps its old inode or opens the complete new generation.
        return dest
    finally:
        with suppress(OSError):
            os.unlink(tmp)
