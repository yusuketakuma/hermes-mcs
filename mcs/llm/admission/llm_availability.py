"""Own shared LLM availability files, circuit locking and disk admission checks."""
import fcntl
import json
import math
import os
import subprocess
import sys
import time
from contextlib import contextmanager, suppress

import safe_io
from mcs_util import load_config
from stats_math import finite_number

# ---------- LLM lane stability gates ----------
# Shared by extract_llm and semantic_drain so both lanes defer under the
# same conditions instead of each inventing a gate.  The breaker state is
# a JSON file beside the db (independent of the DB's own health — a
# corrupted db must not hide an outage); once the failure streak hits
# _CIRCUIT_TRIP the lane stays closed for a cooldown that doubles per
# streak (cap 30min), so an outage degrades to one file read per drain.
# The disk floor guards the artifact writers: a nearly-full volume turns
# every guarded INSERT into an I/O error storm — rows stay pending and
# the drain reports disk_free_mb instead of burning attempts.
_CIRCUIT_TRIP = 3
_CIRCUIT_BASE_S = 120.0
_CIRCUIT_MAX_S = 1800.0
_CIRCUIT_MAX_STREAK = math.ceil(math.log2(_CIRCUIT_MAX_S / _CIRCUIT_BASE_S))


def _db_file(db) -> str:
    return db.execute("PRAGMA database_list").fetchone()[2]


def circuit_state_path(db):
    from pathlib import Path
    return Path(_db_file(db)).with_name("llm_circuit.json")


def _circuit_state(db) -> dict:
    return load_config(circuit_state_path(db))


def circuit_open_s(db) -> float:
    """Seconds the breaker stays open; 0 when closed. An unreadable or
    corrupt state file fails closed-open — it never blocks the lane."""
    # A negative stamp reads as unknown here and as already past before: 0 either way.
    until = _finite_ts(_circuit_state(db).get("open_until"))
    remaining = 0.0 if until is None else until - time.time()
    # No cooldown is ever written longer than _CIRCUIT_MAX_S: a longer one is a
    # stepped-back clock or a damaged file, and no call would run to clear it.
    return remaining if 0.0 < remaining <= _CIRCUIT_MAX_S else 0.0


@contextmanager
def _circuit_lock(path):
    """Serialize the breaker/health read-modify-write across the extract
    workers, the semantic lanes and the tick. Blocking but brief (one
    small JSON rewrite); an unopenable lock file fails open so the guard
    never stops the lane it protects."""
    fd = None
    try:
        try:
            fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            pass
        yield
    finally:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)   # closing releases the flock; keep the inode


# Health marker beside the breaker file: when the endpoint last answered
# and since when it has been observed down. The breaker file is deleted
# on success (absence = closed), so the marker lives apart. Stall checks
# read it: an outage keeps the job queues' updated_at fresh (every free
# defer rewrites it), so queue age alone never fires.
_HEALTH_SUCCESS_REFRESH_S = 60.0


def llm_health_path(db):
    return circuit_state_path(db).with_name("llm_health.json")


def _finite_ts(value) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def llm_health(db) -> dict:
    """{"last_success": ts|None, "down_since": ts|None} in wall-clock
    seconds. Missing/corrupt state reads as unknown, never as an outage."""
    state = load_config(llm_health_path(db))
    return {"last_success": _finite_ts(state.get("last_success")),
            "down_since": _finite_ts(state.get("down_since"))}


def llm_down_since(db) -> float | None:
    """Start of the current endpoint-down/timeout streak, or None."""
    return llm_health(db)["down_since"]


def llm_down_for_s(db, now: float | None = None) -> float | None:
    """Seconds the endpoint has been observed down with no answered call
    since; None when it is not currently observed down."""
    since = llm_down_since(db)
    now = time.time() if now is None else now
    if since is None or since > now:    # a future stamp is unknown, never "down for 0 s"
        return None
    return now - since


def _health_write(path, state: dict) -> None:
    payload = json.dumps(state)
    with suppress(OSError):
        safe_io.atomic_write(str(path), lambda f: f.write(payload))


def _mark_llm_success(db) -> None:
    now = time.time()
    current = llm_health(db)
    if current["down_since"] is None and current["last_success"] is not None \
            and 0 <= now - current["last_success"] < _HEALTH_SUCCESS_REFRESH_S:
        return   # one rewrite per minute on a busy healthy lane
    _health_write(llm_health_path(db), {"last_success": now})


def _mark_llm_down(db) -> None:
    current = llm_health(db)
    now = time.time()
    if current["down_since"] is not None and current["down_since"] <= now:
        return
    state = {"down_since": now}    # also replaces a stamp left ahead of a stepped-back clock
    if current["last_success"] is not None:
        state["last_success"] = current["last_success"]
    _health_write(llm_health_path(db), state)


# Resident semantic turns and whether the run lock was busy, kept for the
# stall alert only (observation; never read by admission).
SEMANTIC_TURNS_NAME = "semantic_turns.json"   # beside the ledger, like llm_health.json
SEMANTIC_TURNS_WINDOW_S = 86400.0
_SEMANTIC_TURNS_MAX = 2000


def semantic_turns_path(db):
    return circuit_state_path(db).with_name(SEMANTIC_TURNS_NAME)


def _semantic_turns(path, now: float) -> list:
    turns = load_config(path).get("turns")
    return [t for t in turns if isinstance(t, list) and len(t) == 2
            and _finite_ts(t[0]) is not None and 0 <= now - t[0] < SEMANTIC_TURNS_WINDOW_S
            and t[1] in (0, 1)] if isinstance(turns, list) else []


def record_semantic_turn(path, lock_busy: bool) -> None:
    """Append one resident semantic turn; an unwritable file only loses the sample."""
    with _circuit_lock(path):
        now = time.time()
        turns = _semantic_turns(path, now) + [[now, 1 if lock_busy else 0]]
        _health_write(path, {"turns": turns[-_SEMANTIC_TURNS_MAX:]})


def semantic_turns_24h(db, now: float | None = None) -> dict | None:
    """{"attempts", "lock_busy"} over the last 24 h, or None when none was recorded."""
    turns = _semantic_turns(semantic_turns_path(db), time.time() if now is None else now)
    return ({"attempts": len(turns), "lock_busy": sum(t[1] for t in turns)}
            if turns else None)


def circuit_success(db) -> None:
    """Any answered call proves the endpoint is alive — reset state."""
    path = circuit_state_path(db)
    with _circuit_lock(path):
        with suppress(OSError):
            path.unlink(missing_ok=True)
        _mark_llm_success(db)


def circuit_streak(db) -> int:
    """Trips of the current outage (0 = not tripped since the last
    answered call). Corrupt state reads as 0."""
    streak = _circuit_state(db).get("streak")
    return streak if type(streak) is int and streak >= 0 else 0


def circuit_failure(db) -> None:
    """Endpoint-down observation: count consecutive probes and open the
    breaker on a doubling cooldown once the streak trips. The read-
    modify-write holds a flock so concurrent lanes never lose a count."""
    path = circuit_state_path(db)
    with _circuit_lock(path):
        state = _circuit_state(db)
        fails, streak = state.get("failures"), state.get("streak")
        fails = min(fails, _CIRCUIT_TRIP - 1) \
            if type(fails) is int and fails >= 0 else 0
        streak = min(streak, _CIRCUIT_MAX_STREAK) \
            if type(streak) is int and streak >= 0 else 0
        fails += 1
        out = {"failures": fails, "streak": streak}
        if fails >= _CIRCUIT_TRIP:
            out["streak"] = min(streak + 1, _CIRCUIT_MAX_STREAK)
            out["open_until"] = time.time() + min(
                _CIRCUIT_MAX_S, _CIRCUIT_BASE_S * 2 ** out["streak"])
            out["failures"] = 0
        _health_write(path, out)
        _mark_llm_down(db)


def disk_free_mb(db) -> float | None:
    """Free space (MiB) on the volume holding the db; None when
    unmeasurable — an unreadable fs must not gate the lane."""
    try:
        import shutil
        return shutil.disk_usage(_db_file(db)).free / (1024 * 1024)
    except (OSError, TypeError, IndexError):
        return None


def disk_floor_mb() -> float:
    """MiB floor under which the LLM lane defers; 0 disables the guard.
    config.json local_llm.disk_guard_mb, else the legacy MCS_DISK_GUARD_MB."""
    ll = load_config().get("local_llm")
    configured = ll.get("disk_guard_mb") if isinstance(ll, dict) else None
    if finite_number(configured) and configured >= 0:
        return float(configured)
    try:
        value = float(os.environ.get("MCS_DISK_GUARD_MB", "512"))
        return value if math.isfinite(value) and value >= 0 else 512.0
    except ValueError:
        return 512.0


# kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical.
_PRESSURE_CRITICAL = 4
_pressure_critical_last = False


def memory_pressure_critical() -> bool:
    """True only while macOS reports critical memory pressure; an unreadable
    level adds no deferral (the free-memory reserve still decides)."""
    global _pressure_critical_last
    if sys.platform != "darwin":
        return False
    try:
        level = int(subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=2, check=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin", "LC_ALL": "C"}).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    critical = level == _PRESSURE_CRITICAL
    if critical != _pressure_critical_last:
        # One stderr event per transition records why background work waits.
        _pressure_critical_last = critical
        print(json.dumps({"event": "memory_pressure_deferral" if critical else "memory_pressure_cleared",
                          "reason": "memory_pressure_critical", "level": level, "pid": os.getpid()}),
              file=sys.stderr, flush=True)
    return critical
