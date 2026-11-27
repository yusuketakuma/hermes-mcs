"""配送workerの起動証拠を非機密の状態とコード版で照合する。"""
from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
import time

import environment_diagnostics
import mcs_util
from safe_io import atomic_write

VERSION = '2026-10-30.1'
REASONS = frozenset({'worker_started', 'profile_mismatch', 'owner_unverified',
                     'application_mismatch', 'standalone_owned', 'worker_start_failed',
                     'route_flags_unready'})
TRANSPORTS = ('slack', 'discord', 'lineworks')
# The updater's detached gateway restart gives up after 660 s; one collection
# interval on top keeps a restart that is still on its way out of the alarm.
RESTART_GRACE_S = 1200


def _path(root, transport, profile):
    if transport not in TRANSPORTS:
        raise ValueError('worker_transport_invalid')
    key = hashlib.sha256(str(profile).encode()).hexdigest()[:24]
    return Path(root) / 'worker_status' / f'{transport}-{key}.json'


@contextmanager
def _write_fence(directory, deadline=None):
    """Serialize writers without letting a held lock consume the caller's probe budget."""
    deadline = time.monotonic() + 1 if deadline is None else deadline
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('worker_status_lock_timeout') from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def record(root, transport, profile, reason):
    """Write a bounded status witness; never store profile names or identifiers."""
    if reason not in REASONS:
        raise ValueError('worker_reason_invalid')
    target = _path(root, transport, profile)
    if not Path(root).is_dir():
        return
    target.parent.mkdir(mode=0o700, exist_ok=True)
    if target.parent.is_symlink():
        raise ValueError('worker_status_path_invalid')
    try:
        with _write_fence(target.parent, time.monotonic() + 1):
            pid = os.getpid()
            try:
                started_at = environment_diagnostics.process_started_at(pid, deadline=time.monotonic() + 1)
            except (OSError, ValueError, TypeError, OverflowError):
                started_at = None
            value = {'version': VERSION, 'reason': reason, 'pid': pid,
                     'at': time.time(), 'started_at': started_at}
            atomic_write(str(target), lambda f: json.dump(value, f), mode=0o600)
    except TimeoutError:
        return


def _read_witness(p):
    if p.is_symlink() or p.stat().st_size > 2048:
        raise ValueError('invalid')
    value = json.loads(p.read_text())
    if not isinstance(value, dict):
        raise ValueError('invalid')
    return value


def _valid_identity(value):
    pid = value.get('pid')
    recorded_at = value.get('at')
    return (value.get('version') == VERSION and value.get('reason') in REASONS
            and type(pid) is int and pid > 0
            and type(recorded_at) in (int, float) and math.isfinite(recorded_at) and recorded_at > 0)


def _birth(pid, deadline):
    os.kill(pid, 0)
    birth = environment_diagnostics.process_started_at(pid, deadline=deadline)
    if type(birth) not in (int, float) or not math.isfinite(birth) or birth <= 0:
        return None
    return birth


def _repair_missing_started_at(p, deadline):
    """Repair one missing witness under the writer fence; return (repaired, probed birth)."""
    value = _read_witness(p)
    if not _valid_identity(value):
        return False, None
    if value.get('started_at') is not None:
        return False, None
    birth = _birth(value['pid'], deadline)
    if birth is None or birth > value['at'] or time.monotonic() >= deadline:
        return False, birth
    current = _read_witness(p)
    if current != value:
        return False, birth
    atomic_write(str(p), lambda f: json.dump({**value, 'started_at': birth}, f), mode=0o600)
    return True, birth


def repair_missing_started_at(path, *, deadline=None):
    """Re-acquire a missing started_at once for the same live record pid.

    A process born after the record cannot be its writer (PID reuse), and a
    malformed present stamp is evidence rather than a value to overwrite."""
    try:
        p = Path(path)
        deadline = time.monotonic() + 1 if deadline is None else deadline
        if p.is_symlink() or p.stat().st_size > 2048:
            return False
        with _write_fence(p.parent, deadline):
            repaired, _birth_value = _repair_missing_started_at(p, deadline)
            return repaired
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return False


def status(root, transport, profile, *, deadline=None):
    """Read startup evidence; missing, old-version and dead-process records fail closed."""
    try:
        p = _path(root, transport, profile)
        deadline = time.monotonic() + 1 if deadline is None else deadline
        if p.is_symlink() or p.stat().st_size > 2048:
            raise ValueError('invalid')
        birth = None
        with _write_fence(p.parent, deadline):
            repaired, birth = _repair_missing_started_at(p, deadline)
            value = _read_witness(p)
        if value.get('reason') not in REASONS:
            raise ValueError('invalid')
        if value.get('version') != VERSION:
            return {'state': 'unknown', 'reason': 'worker_restart_required'}
        if not _valid_identity(value):
            raise ValueError('invalid')
        started_at = value.get('started_at')
        if (type(started_at) not in (int, float) or not math.isfinite(started_at)
                or started_at <= 0 or started_at > value['at']):
            raise ValueError('invalid')
        if birth is None or not repaired:
            birth = _birth(value['pid'], deadline)
        if time.monotonic() >= deadline or birth != started_at or _read_witness(p) != value:
            raise ValueError('invalid')
        return {'state': 'started' if value['reason'] == 'worker_started' else 'disabled',
                'reason': value['reason']}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return {'state': 'unknown', 'reason': 'worker_start_unverified'}


def restart_overdue(root, cfg, now, *, checkout=None) -> bool:
    """A started Hermes worker that still predates the sources its gateway loads.

    A restart request that failed or was lost, or an edit applied without one,
    leaves the old code delivering; its start witness alone looks healthy."""
    notify = cfg.get('notify')
    transport = notify.get('interactive') if isinstance(notify, dict) else None
    if cfg.get('runtime_mode') == 'standalone' or transport not in ('slack', 'discord'):
        return False    # the standalone host and the LINE WORKS adapter load other sources
    scope = notify.get(transport)
    profile = (scope.get('profile') if isinstance(scope, dict) else None) or 'default'
    if status(root, transport, profile)['state'] != 'started':
        return False    # every other state already reports its own reason
    try:
        started = json.loads(_path(root, transport, profile).read_text())['started_at']
        newest = environment_diagnostics.gateway_sources_mtime(checkout or mcs_util.REPO)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False
    return (type(started) in (int, float) and newest is not None
            and started < newest <= now - RESTART_GRACE_S)


def diagnose(root, cfg, *, probe_data=False):
    """Report the configured delivery worker (Hermes profile or LINE WORKS adapter) and restore hold."""
    notify = cfg.get('notify')
    notify = {} if notify is None else notify
    transport = notify.get('interactive') if isinstance(notify, dict) else None
    scope = notify.get(transport) if transport in TRANSPORTS else None
    scope = {} if scope is None else scope
    if not isinstance(notify, dict) or not isinstance(scope, dict):
        # a malformed block cannot name the owner: unverified, never "not ours"
        result = {'state': 'unknown', 'reason': 'worker_start_unverified'}
    elif cfg.get('runtime_mode') == 'standalone' or transport not in TRANSPORTS:
        result = {'state': 'not_applicable', 'reason': 'worker_not_hermes_owned'}
    else:
        # LINE WORKS has its own adapter process (not a Hermes profile); it records under 'default'.
        profile = 'default' if transport == 'lineworks' else scope.get('profile') or 'default'
        result = status(root, transport, profile)
    try:
        os.lstat(Path(root) / 'restore_pending.json')
        hold = True
    except FileNotFoundError:
        hold = False
    except OSError:
        hold = True
    if not hold and not probe_data:
        hold = None
    elif not hold:
        try:
            ledger = Path(root) / 'ledger.db'
            if ledger.is_symlink():
                raise ValueError('invalid')
            with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True,
                                         timeout=1)) as db:
                db.set_progress_handler(lambda: 1, 100_000)
                table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                   "AND name='notification_restore_holds'").fetchone()
                hold = bool(table and db.execute(
                    "SELECT 1 FROM notification_restore_holds WHERE released_at IS NULL LIMIT 1"
                ).fetchone())
        except (OSError, ValueError, sqlite3.Error):
            hold = None
    return {**result, 'restore_delivery_held': hold,
            'recovery_scope': 'collect_only' if hold else None}
