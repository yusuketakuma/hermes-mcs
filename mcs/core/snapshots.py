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

from safe_io import fdopen_owned, fsync_dir


SNAPSHOT_VERIFY_TIMEOUT_S = 30.0
# quick_check time grows with the file: the verify budget adds one second
# per SNAPSHOT_VERIFY_RATE_BPS of snapshot, capped, so a ledger that has
# outgrown the floor is not refused on every publish (H4). quick_check on a
# 281 MB ledger took 12.4 s (~23 MB/s) beside the live workers and timed
# out past 34 s under heavier load while the copy took 2 s, so the rate
# leaves several times the measured read speed as headroom.
SNAPSHOT_VERIFY_RATE_BPS = 4 * 1024 * 1024
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


# Copy/verify seconds and the verify budget of the latest publish that
# succeeded, so the remaining margin stays visible without a failure.
_timing: dict = {}


def last_timing() -> dict:
    """Bounded timings of the most recent published snapshot."""
    return dict(_timing)


def _reset_status() -> None:
    _failure.clear()
    _timing.clear()


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


def _valid_snapshot_candidate(path: str, *, deadline: float,
                              fence_fd: int | None = None, state: dict | None = None) -> bool:
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
                stderr=subprocess.DEVNULL, pass_fds=(fd,) if fence_fd is None else (fd, fence_fd))
            try:
                code = child.wait(timeout=max(0, deadline - monotonic()))
                if code != 0:
                    _fail(_EXIT_REASONS.get(code, "validator_exit"),
                          exit_code=code, verify_s=round(monotonic() - started, 3))
                return code == 0
            except subprocess.TimeoutExpired:
                _fail("validator_timeout", verify_s=round(monotonic() - started, 3),
                      budget_s=round(deadline - started, 3))
                with suppress(BaseException):
                    child.kill()
                try:
                    child.wait(timeout=1)
                except BaseException:
                    if state is not None:
                        state["unreaped"] = True
                    _failure["child_unreaped"] = True
                    # The inherited lock fences new validators until even an
                    # uninterruptible disk wait exits and the OS closes it.
                    pass
                return False
            except BaseException:
                # Cleanup must not replace the original interruption/wait error.
                # An unreaped child keeps the inherited lock as a validator fence.
                with suppress(BaseException):
                    child.kill()
                try:
                    child.wait(timeout=1)
                except BaseException:
                    if state is not None:
                        state["unreaped"] = True
                    _failure["child_unreaped"] = True
                raise
    except OSError as error:
        _fail("validator_oserror", errno=error.errno)
        return False


class _SnapshotDeadline(Exception):
    pass


def _prepare_snapshot(db_path: str, tmp: str, deadline: float | None) -> None:
    def check_deadline(*_):
        if deadline is not None and monotonic() >= deadline:
            raise _SnapshotDeadline

    check_deadline()
    timeout = 5.0 if deadline is None else min(5.0, max(0, deadline - monotonic()))
    src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                          uri=True, timeout=timeout)
    try:
        dst = sqlite3.connect(tmp, timeout=timeout)
        try:
            src.execute("BEGIN")
            src.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            check_deadline()
            src.backup(dst, pages=4096, progress=check_deadline, sleep=0.0)
            check_deadline()
            with dst:
                dst.execute("INSERT OR REPLACE INTO snapshot_meta("
                            "singleton,generation_id,generated_at) VALUES(1,?,?)",
                            (str(uuid.uuid4()), time.time()))
            dst.execute("PRAGMA journal_mode=DELETE")
            check_deadline()
        finally:
            dst.close()
    finally:
        src.close()


_PREPARE_CODE = (
    "import sys\n"
    "try:\n"
    "    sys.path.insert(0, sys.argv[1]); import _mcs_path; import snapshots\n"
    "except BaseException: sys.exit(3)\n"
    "try:\n"
    "    snapshots._prepare_snapshot(sys.argv[2], sys.argv[3], "
    "None if sys.argv[4] == '' else float(sys.argv[4]))\n"
    "except snapshots._SnapshotDeadline: sys.exit(5)\n"
    "except BaseException: sys.exit(4)\n"
)
_CLEANUP_CODE = (
    "import os, sys\n"
    "for suffix in ('', '-journal', '-wal', '-shm'):\n"
    "    try: os.unlink(sys.argv[1] + suffix)\n"
    "    except FileNotFoundError: pass\n"
)


def _prepare_in_child(db_path: str, tmp: str, deadline: float | None, fd: int, state: dict) -> bool:
    try:
        child = subprocess.Popen(
            [sys.executable, '-I', '-c', _PREPARE_CODE,
             str(Path(__file__).resolve().parents[1]), db_path, tmp,
             '' if deadline is None else str(deadline)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, pass_fds=(fd,))
    except OSError as error:
        _fail("copy_oserror", errno=error.errno)
        return False
    try:
        code = child.wait(timeout=None if deadline is None else max(0, deadline - monotonic()))
    except BaseException as error:
        with suppress(BaseException):
            child.kill()
        try:
            child.wait(timeout=1)
        except BaseException:
            state["unreaped"] = True
        if isinstance(error, subprocess.TimeoutExpired):
            _fail("copy_timeout", child_unreaped=state.get("unreaped", False))
            return False
        if isinstance(error, OSError):
            _fail("copy_oserror", errno=error.errno,
                  child_unreaped=state.get("unreaped", False))
            return False
        raise
    if code != 0:
        _fail({3: "copy_bootstrap_error", 4: "copy_exception", 5: "copy_deadline"}.get(
            code, "copy_exit"), exit_code=code)
        return False
    return True


def _cleanup_candidate(tmp: str, fd: int) -> None:
    try:
        child = subprocess.Popen([sys.executable, '-I', '-c', _CLEANUP_CODE, tmp],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, pass_fds=(fd,))
        try:
            if child.wait(timeout=1) != 0:
                _failure["cleanup_failed"] = True
        except subprocess.TimeoutExpired:
            # Its inherited fence remains held until the unlink finishes.
            _failure["cleanup_pending"] = True
    except OSError:
        _failure["cleanup_failed"] = True


def publish_snapshot(db_path: str, dest_dir: str, *,
                     deadline: float | None = None) -> str | None:
    """Publish a verified point-in-time copy for read-only consumers.

    Preparation and validation run in fenced children. The parent checks the
    deadline before atomic publication; native publication I/O can still stall.
    """
    _reset_status()
    if deadline is not None and monotonic() >= deadline:
        _fail("deadline_before_copy")
        return None
    started = monotonic()
    try:
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        os.chmod(dest_dir, 0o700)
        fd = os.open(os.path.join(dest_dir, '.snapshot.lock'),
                     os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        _fail("snapshot_setup_oserror", errno=error.errno)
        return None
    with fdopen_owned(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("snapshot_lock_busy")
            return None
        except OSError as error:
            _fail("snapshot_lock_error", errno=error.errno)
            return None
        state = {}
        tmp = None
        published = False
        try:
            if deadline is not None and monotonic() >= deadline:
                _fail("deadline_before_copy")
                return None
            try:
                temp_fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".snapshot.", suffix=".tmp")
                os.close(temp_fd)
            except OSError as error:
                _fail("snapshot_setup_oserror", errno=error.errno)
                return None
            if not _prepare_in_child(db_path, tmp, deadline, fd, state):
                _failure.setdefault("copy_s", round(monotonic() - started, 3))
                return None
            copied = monotonic()
            verify_by = copied + max(snapshot_verify_timeout_s(tmp), min(
                SNAPSHOT_VERIFY_TIMEOUT_S + SNAPSHOT_VERIFY_EXTRA_MAX_S, 1.5 * (copied - started)))
            if deadline is not None:
                verify_by = min(verify_by, deadline)
            if not _valid_snapshot_candidate(tmp, deadline=verify_by, fence_fd=fd, state=state):
                _failure.setdefault("reason", "validator_false")
                _fail(_failure["reason"], copy_s=round(copied - started, 3),
                      capped_by_deadline=deadline is not None and verify_by == deadline)
                return None
            verified = monotonic()
            if verified >= verify_by:
                _fail("verify_deadline", copy_s=round(copied - started, 3),
                      verify_s=round(verified - copied, 3))
                return None
            dest = os.path.join(dest_dir, "ledger-snapshot.db")
            try:
                candidate_fd = os.open(tmp, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(candidate_fd)
                finally:
                    os.close(candidate_fd)
                if deadline is not None and monotonic() >= deadline:
                    _fail("publish_deadline", copy_s=round(copied - started, 3),
                          verify_s=round(verified - copied, 3))
                    return None
                os.replace(tmp, dest)
                published = True
                fsync_dir(dest_dir)
            except OSError:
                _fail("publication_uncertain" if published else "publication_failed",
                      copy_s=round(copied - started, 3),
                      verify_s=round(verified - copied, 3))
                return None
            _timing.update(copy_s=round(copied - started, 3),
                           verify_s=round(verified - copied, 3), budget_s=round(verify_by - copied, 3))
            return dest
        finally:
            if tmp is not None and not published and not state.get("unreaped"):
                _cleanup_candidate(tmp, fd)
