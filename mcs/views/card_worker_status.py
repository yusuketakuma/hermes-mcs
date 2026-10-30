"""配送workerの起動証拠を非機密の状態とコード版で照合する。"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
import time

import environment_diagnostics
from safe_io import atomic_write

VERSION = '2026-10-30.1'
REASONS = frozenset({'worker_started', 'profile_mismatch', 'owner_unverified',
                     'application_mismatch', 'standalone_owned', 'worker_start_failed',
                     'route_flags_unready'})
TRANSPORTS = ('slack', 'discord', 'lineworks')


def _path(root, transport, profile):
    if transport not in TRANSPORTS:
        raise ValueError('worker_transport_invalid')
    key = hashlib.sha256(str(profile).encode()).hexdigest()[:24]
    return Path(root) / 'worker_status' / f'{transport}-{key}.json'


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
    pid = os.getpid()
    try:
        started_at = environment_diagnostics.process_started_at(pid, deadline=time.monotonic() + 1)
    except (OSError, ValueError, TypeError, OverflowError):
        started_at = None
    value = {'version': VERSION, 'reason': reason, 'pid': pid,
             'at': time.time(), 'started_at': started_at}
    atomic_write(str(target), lambda f: json.dump(value, f), mode=0o600)


def status(root, transport, profile):
    """Read startup evidence; missing, old-version and dead-process records fail closed."""
    try:
        p = _path(root, transport, profile)
        if p.is_symlink() or p.stat().st_size > 2048:
            raise ValueError('invalid')
        value = json.loads(p.read_text())
        if not isinstance(value, dict) or value.get('reason') not in REASONS:
            raise ValueError('invalid')
        if value.get('version') != VERSION:
            return {'state': 'unknown', 'reason': 'worker_restart_required'}
        pid = value.get('pid')
        if type(pid) is not int or pid <= 0:
            raise ValueError('invalid')
        started_at = value.get('started_at')
        if (type(started_at) not in (int, float) or not math.isfinite(started_at)
                or started_at <= 0):
            raise ValueError('invalid')
        os.kill(pid, 0)
        if environment_diagnostics.process_started_at(pid, deadline=time.monotonic() + 1) != started_at:
            raise ValueError('invalid')
        return {'state': 'started' if value['reason'] == 'worker_started' else 'disabled',
                'reason': value['reason']}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return {'state': 'unknown', 'reason': 'worker_start_unverified'}


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
