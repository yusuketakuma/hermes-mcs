"""Publish verified snapshot files and own their temporary connections and validator process."""

import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from time import monotonic, perf_counter
import uuid
from contextlib import contextmanager, suppress

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
class _PublicationStatus(threading.local):
    def __init__(self):
        self.failure = {}
        self.timing = {}
        self.locks = {}


_status = _PublicationStatus()


def last_failure() -> dict:
    """Return this thread's latest bounded failure, never paths or source data."""
    return dict(_status.failure)


def last_timing() -> dict:
    """Return this thread's latest published snapshot timings and marker state."""
    return dict(_status.timing)


def _reset_status() -> None:
    _status.failure.clear()
    _status.timing.clear()


def _fail(reason: str, **extra) -> None:
    _status.failure.update(reason=reason, **extra)


def _record_timing(**values) -> None:
    _status.timing.update(values)


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
                    _status.failure["child_unreaped"] = True
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
                    _status.failure["child_unreaped"] = True
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
                _status.failure["cleanup_failed"] = True
        except subprocess.TimeoutExpired:
            # Its inherited fence remains held until the unlink finishes.
            _status.failure["cleanup_pending"] = True
    except OSError:
        _status.failure["cleanup_failed"] = True


def _lock_identity(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError("snapshot_lock_unverified")
    return info.st_dev, info.st_ino


def _verified_publication_lock(dest_dir, fd):
    """Accept only this thread's live, matching, OS-locked publication fence."""
    try:
        if type(fd) is not int or fd < 0:
            raise ValueError("snapshot_lock_unverified")
        identity = _lock_identity(os.fstat(fd))
        if _status.locks.get(fd) != identity:
            raise ValueError("snapshot_lock_unverified")
        path = os.path.join(dest_dir, '.snapshot.lock')
        if _lock_identity(os.stat(path, follow_symlinks=False)) != identity:
            raise ValueError("snapshot_lock_unverified")
        probe = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
        try:
            if _lock_identity(os.fstat(probe)) != identity:
                raise ValueError("snapshot_lock_unverified")
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # A foreign owner's lock must not masquerade as this fd's fence.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise ValueError("snapshot_lock_unverified")
        finally:
            os.close(probe)
        return True
    except (OSError, ValueError):
        _fail("snapshot_lock_unverified")
        return False


@contextmanager
def _publication_lock(dest_dir: str):
    """Own the existing publication fence across freshness checks and marker updates."""
    try:
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        os.chmod(dest_dir, 0o700)
        fd = os.open(os.path.join(dest_dir, '.snapshot.lock'),
                     os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        _fail("snapshot_setup_oserror", errno=error.errno)
        yield None
        return
    with fdopen_owned(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("snapshot_lock_busy")
            yield None
            return
        except OSError as error:
            _fail("snapshot_lock_error", errno=error.errno)
            yield None
            return
        try:
            identity = _lock_identity(os.fstat(fd))
            if _lock_identity(os.stat(os.path.join(dest_dir, '.snapshot.lock'),
                                      follow_symlinks=False)) != identity:
                raise ValueError("snapshot_lock_unverified")
        except (OSError, ValueError):
            _fail("snapshot_lock_unverified")
            yield None
            return
        _status.locks[fd] = identity
        try:
            yield fd
        finally:
            _status.locks.pop(fd, None)


def publish_snapshot(db_path: str, dest_dir: str, *,
                     deadline: float | None = None, verification_context=None) -> str | None:
    """Publish a verified point-in-time copy for read-only consumers.

    Preparation and validation run in fenced children. The parent checks the
    deadline before atomic publication; native publication I/O can still stall.
    """
    _reset_status()
    if deadline is not None and monotonic() >= deadline:
        _fail("deadline_before_copy")
        return None
    started = monotonic()
    total_started = perf_counter()
    with _publication_lock(dest_dir) as fd:
        if fd is None:
            return None
        return _publish_snapshot_locked(db_path, dest_dir, fd, deadline=deadline, started=started,
                                        total_started=total_started,
                                        verification_context=verification_context)


def _publish_snapshot_locked(db_path: str, dest_dir: str, fd: int, *,
                             deadline: float | None = None, started=None, total_started=None,
                             verification_context=None) -> str | None:
    """Copy and validate while the caller retains the publication fence.

    An optional ``verification_context(*, deadline)`` factory gates the
    child-validation window: the caller's run lock may be released while
    the fenced child validates, then reacquired and re-identified before
    publication proceeds."""
    if not _verified_publication_lock(dest_dir, fd):
        return None
    started = monotonic() if started is None else started
    total_started = perf_counter() if total_started is None else total_started
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
        if verification_context is not None:
            # Writer-identity baseline check before the private copy is
            # even attempted — a swapped source path is caught while the
            # lock is still held.
            verification_context(deadline=deadline).precheck()
        if not _prepare_in_child(db_path, tmp, deadline, fd, state):
            _status.failure.setdefault("copy_s", round(monotonic() - started, 3))
            return None
        copied = monotonic()
        verify_by = copied + max(snapshot_verify_timeout_s(tmp), min(
            SNAPSHOT_VERIFY_TIMEOUT_S + SNAPSHOT_VERIFY_EXTRA_MAX_S, 1.5 * (copied - started)))
        if deadline is not None:
            verify_by = min(verify_by, deadline)
        if verification_context is None:
            if not _valid_snapshot_candidate(tmp, deadline=verify_by, fence_fd=fd, state=state):
                _status.failure.setdefault("reason", "validator_false")
                _fail(_status.failure["reason"], copy_s=round(copied - started, 3),
                      capped_by_deadline=deadline is not None and verify_by == deadline)
                return None
            verified = monotonic()
        else:
            with verification_context(deadline=verify_by) as gate:
                inside = _valid_snapshot_candidate(tmp, deadline=verify_by,
                                                   fence_fd=fd, state=state)
                # Sampled inside the unlock scope: the reacquire cost is
                # never charged to verify_s.
                verified = monotonic()
            gate.confirm_fence(fd, dest_dir)
            if not inside:
                _status.failure.setdefault("reason", "validator_false")
                _fail(_status.failure["reason"], copy_s=round(copied - started, 3),
                      capped_by_deadline=deadline is not None and verify_by == deadline)
                return None
        if verified >= verify_by:
            _fail("verify_deadline", copy_s=round(copied - started, 3),
                  verify_s=round(verified - copied, 3))
            return None
        publication_started = perf_counter()
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
        _record_timing(copy_s=round(copied - started, 3),
                       verify_s=round(verified - copied, 3), budget_s=round(verify_by - copied, 3),
                       publication_s=round(perf_counter() - publication_started, 3),
                       total_s=round(perf_counter() - total_started, 3))
        return dest
    finally:
        if tmp is not None and not published and not state.get("unreaped"):
            _cleanup_candidate(tmp, fd)
