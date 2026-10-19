"""配送workerの起動証拠を非機密の状態とコード版で照合する。"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import time

from mcs_util import atomic_write

VERSION = '2026-10-17.1'
REASONS = frozenset({'worker_started', 'profile_mismatch', 'owner_unverified',
                     'application_mismatch', 'standalone_owned', 'worker_start_failed'})


def _path(root, transport, profile):
    if transport not in ('slack', 'discord'):
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
    value = {'version': VERSION, 'reason': reason, 'pid': os.getpid(),
             'at': time.time()}
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
        os.kill(pid, 0)
        return {'state': 'started' if value['reason'] == 'worker_started' else 'disabled',
                'reason': value['reason']}
    except (OSError, ValueError, TypeError, RecursionError):
        return {'state': 'unknown', 'reason': 'worker_start_unverified'}


def diagnose(root, cfg, *, probe_data=False):
    """Report the configured Hermes owner and restore hold without changing either."""
    notify = cfg.get('notify') or {}
    transport = notify.get('interactive') if isinstance(notify, dict) else None
    scope = (notify.get(transport) or {}) if transport in ('slack', 'discord') else {}
    if not isinstance(notify, dict) or not isinstance(scope, dict):
        # a malformed block cannot name the owner: unverified, never "not ours"
        result = {'state': 'unknown', 'reason': 'worker_start_unverified'}
    elif cfg.get('runtime_mode') == 'standalone' or transport not in ('slack', 'discord'):
        result = {'state': 'not_applicable', 'reason': 'worker_not_hermes_owned'}
    else:
        result = status(root, transport, scope.get('profile') or 'default')
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
