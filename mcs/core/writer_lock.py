"""Own writer-lock descriptors and reject stale generations after transport or waiting."""
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar

from mcs_util import HOME
from safe_io import open_lock_file, read_regular_nofollow

RUN_LOCK = os.path.join(HOME, "data", "run.lock")
UPDATE_MARKER_NAME = "update_in_progress.marker"
RESTART_INTENT_NAME = "gateway_restart_intent.json"
_RESTART_INTENT_SCHEMA = "gateway-restart-intent/1"
# ps is a subprocess: one verdict covers a few polls of the same helper.
_RESTART_HELPER_CHECK_S = 3.0
_restart_helper_check = {"key": None, "verdict": False, "at": 0.0}
_MCS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_LOCK_REACQUIRE_S = 150.0
RUN_LOCK_POLL_S = 0.2
_writer_refusal = ContextVar("writer_refusal", default=None)
_WRITER_REFUSAL_STAGES = {
    "writer_refusal_unclassified": frozenset({"writer_guard", "writer_fence"}),
    "update_marker_present": frozenset({"update_restore_fence"}),
    "restore_hold_present": frozenset({"update_restore_fence"}),
    "restore_state_unverifiable": frozenset({"update_restore_fence"}),
    "initial_writer_blocked": frozenset({"writer_fence"}),
    "inference_source_changed": frozenset({"writer_fence"}),
    "inference_version_changed": frozenset({"writer_fence"}),
    "inference_source_unavailable": frozenset({"writer_fence"}),
    "source_generation_changed": frozenset({"generation_fence"}),
    "run_lock_not_acquired": frozenset({"write_lock", "ledger_lock"}),
    "resident_sources_changed": frozenset({"startup_source_fence"}),
    "loaded_sources_unverified": frozenset({"startup_source_fence"}),
    "resident_sources_unavailable": frozenset({"startup_source_fence"}),
    "startup_deadline_reached": frozenset({"ledger_startup", "resident_startup"}),
    "restart_intent_active": frozenset({"write_lock"}),
}


@contextmanager
def capture_writer_refusal():
    """Capture fixed refusal codes from the checks actually performed in this scope."""
    diagnostic = {"writer_reason": "writer_refusal_unclassified", "writer_stage": "writer_guard"}
    token = _writer_refusal.set(diagnostic)
    try:
        yield diagnostic
    finally:
        _writer_refusal.reset(token)


def record_writer_refusal(reason: str, stage: str) -> None:
    """Record a fixed code without repeating a filesystem or lock check."""
    diagnostic = _writer_refusal.get()
    if diagnostic is not None:
        if (not isinstance(reason, str) or not isinstance(stage, str)
                or stage not in _WRITER_REFUSAL_STAGES.get(reason, ())):
            reason, stage = "writer_refusal_unclassified", "writer_guard"
        diagnostic.update(writer_reason=reason, writer_stage=stage)


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
        record_writer_refusal("update_marker_present", "update_restore_fence")
        return True
    try:
        from notify_spool import restore_writer_hold
        hold = restore_writer_hold(data)
        blocked = hold is not None
        if blocked:
            reason = ("restore_state_unverifiable" if isinstance(hold, dict) and hold.get("unreadable")
                      else "restore_hold_present")
            record_writer_refusal(reason, "update_restore_fence")
        return blocked
    except Exception:
        record_writer_refusal("restore_state_unverifiable", "update_restore_fence")
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


def _helper_process_started_at(pid: int, *, deadline=None) -> float | None:
    """Indirect the ps probe so tests can pin the helper's liveness."""
    import environment_diagnostics
    return environment_diagnostics.process_started_at(pid, deadline=deadline)


def _restart_helper_alive(pid: int, started_at: int, deadline=None) -> bool:
    """Liveness verdict for one intent's helper, cached briefly per (pid, start)."""
    key = (pid, started_at)
    checked = time.monotonic()
    if (_restart_helper_check.get("key") == key
            and checked - _restart_helper_check["at"] < _RESTART_HELPER_CHECK_S):
        return _restart_helper_check["verdict"]
    try:
        birth = _helper_process_started_at(pid, deadline=deadline)
        # Exact match rejects PID reuse, same as health_recovery's gateway check.
        verdict = type(birth) in (int, float) and birth == started_at
    except Exception:
        verdict = False
    _restart_helper_check.update(key=key, verdict=verdict, at=checked)
    return verdict


def restart_intent_active(data_dir: str | None = None, *, now: float | None = None,
                          deadline=None) -> bool:
    """True while a restart helper's published intent is provably live.

    The file alone never suffices: the schema must match, helper_pid must be
    a live process whose start time equals helper_started_at, and not_after
    must be in the future. Anything unreadable, stale or forged fails closed
    to a normal wait. ``deadline`` (monotonic) bounds the ps probe."""
    path = os.path.join(data_dir if data_dir is not None else os.path.join(HOME, "data"),
                        RESTART_INTENT_NAME)
    try:
        row = json.loads(read_regular_nofollow(
            path, 4096, lambda: ValueError("restart_intent_unreadable")))
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return False
    if not isinstance(row, dict) or row.get("schema") != _RESTART_INTENT_SCHEMA:
        return False
    now = time.time() if now is None else now
    if (type(now) not in (int, float)
            or type(row.get("helper_pid")) is not int or row["helper_pid"] <= 0
            or type(row.get("helper_started_at")) is not int
            or not 0 < row["helper_started_at"] <= now
            or type(row.get("not_after")) not in (int, float)
            or not now < row["not_after"]
            or type(row.get("request_id")) is not str
            or re.fullmatch(r"[0-9a-f]{32}", row["request_id"]) is None
            or ("expected_pid" in row
                and (type(row["expected_pid"]) is not int or row["expected_pid"] <= 0))):
        return False
    return _restart_helper_alive(row["helper_pid"], row["helper_started_at"],
                                 deadline=deadline)


def wait_run_lock(path: str, *, wait_s: float, expected_code_stamp: int,
                  marker_path: str) -> int | None:
    """Poll the run lock until ``wait_s`` elapses — short polls so a
    holder that releases between batches is won, not missed. A lock won
    right after an updater released it is given back: this process
    loaded the OLD code before waiting, and running it (with modules
    imported later from the new tree) would mix generations. A live
    gateway-restart intent ends the wait early: the job has not started
    its work yet, so yielding opens a restart window instead of
    delaying it for the whole budget."""
    until = time.monotonic() + wait_s
    intent_dir = os.path.dirname(marker_path)
    while time.monotonic() < until:
        if restart_intent_active(intent_dir, deadline=until):
            record_writer_refusal("restart_intent_active", "write_lock")
            return None
        time.sleep(RUN_LOCK_POLL_S)
        fd = acquire_run_lock(path)
        if fd is not None:
            marked = os.path.exists(marker_path)
            if marked or code_stamp() != expected_code_stamp \
                    or restart_intent_active(intent_dir, deadline=until):
                if marked:
                    reason, stage = "update_marker_present", "update_restore_fence"
                elif code_stamp() != expected_code_stamp:
                    reason, stage = "source_generation_changed", "generation_fence"
                else:
                    reason, stage = "restart_intent_active", "write_lock"
                record_writer_refusal(reason, stage)
                os.close(fd)
                return None
            return fd
    record_writer_refusal("run_lock_not_acquired", "write_lock")
    return None
