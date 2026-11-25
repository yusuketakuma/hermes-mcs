"""v3 owner tick driver: open the control store, build deps and run one bounded tick.

The scheduler boundary owns no update semantics. It opens the v3 control Store,
constructs the dependency surface through ``release_auto_deps.build_deps`` and
invokes the existing owner/health leaf operations. Dependency failures are
reported as fixed hold codes; they never become a successful update and never
fall back to the legacy updater.
"""

import fcntl
import os
from contextlib import contextmanager

import release_auto_deps
import release_auto_health
import release_auto_owner
from adapters.sdc.storage import Store

RESULT_SCHEMA = 'mcs.release-auto-tick-result.v1'
DRIVER_LOCK_NAME = 'release-auto-driver.lock'


class DriverBusy(RuntimeError):
    """Another scheduler child already owns this device's tick slice."""

    code = 'DRIVER_BUSY'


@contextmanager
def driver_mutex(path):
    """Hold one process-local mutex for a whole tick, outside the owner lock DAG.

    Owner mutation locks are intentionally short and leaf-scoped; they cannot
    serialize a network-bound tick. This lock is held only by the scheduler
    driver, so it cannot deadlock the G/U/Q/R/C/A/M/O acquisition order.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DriverBusy('DRIVER_BUSY') from exc
        yield
    finally:
        os.close(fd)


def _driver_busy():
    return {'schema_version': RESULT_SCHEMA, 'owner': None, 'health': None,
            'action': 'busy', 'code': 'DRIVER_BUSY'}


def _code(exc, fallback):
    code = getattr(exc, 'code', None)
    return code if type(code) is str and code else fallback


def _hold(stage, exc, fallback):
    return {'schema_version': RESULT_SCHEMA, 'owner': None, 'health': None,
            'action': 'hold', 'stage': stage, 'code': _code(exc, fallback)}


def _run_tick_unlocked(control_root, *, store_factory=Store,
                       deps_builder=release_auto_deps.build_deps,
                       owner_tick=None,
                       health_report=release_auto_health.report_health):
    """Run one owner tick plus one health report attempt.

    ``owner.tick`` already bounds each stage and preserves partial state. This
    boundary catches dependency/contract exceptions because wire and runtime
    providers expose their own error types rather than OwnerError. Unexpected
    exceptions are also converted to a fixed hold code: a scheduler child must
    not crash-loop or reinterpret an unknown state as success.
    """
    try:
        store = store_factory(control_root)
    except Exception as exc:
        return _hold('open_store', exc, 'STORE_UNAVAILABLE')
    try:
        try:
            deps = deps_builder(store=store)
        except Exception as exc:
            return _hold('build_deps', exc, 'DEPS_UNAVAILABLE')
        try:
            if owner_tick is None:
                owner_result = release_auto_owner.tick(store, deps)
            else:
                owner_result = owner_tick(store, deps)
        except Exception as exc:
            return _hold('owner_tick', exc, 'OWNER_TICK_FAILED')
        try:
            health_result = health_report(store, deps)
        except Exception as exc:
            health_result = {'action': 'hold', 'code': _code(exc, 'HEALTH_FAILED')}
        return {'schema_version': RESULT_SCHEMA,
                'owner': owner_result, 'health': health_result,
                'action': owner_result.get('action', 'unknown')
                if isinstance(owner_result, dict) else 'unknown'}
    finally:
        try:
            store.close()
        except Exception:
            pass


def run_tick(control_root, *, mutex_path=None, **kwargs):
    """Run one bounded tick, refusing overlap with another scheduler child."""
    if mutex_path is None:
        return _run_tick_unlocked(control_root, **kwargs)
    try:
        with driver_mutex(mutex_path):
            return _run_tick_unlocked(control_root, **kwargs)
    except DriverBusy:
        return _driver_busy()
    except Exception as exc:
        return _hold('driver_mutex', exc, 'DRIVER_MUTEX_UNAVAILABLE')


def public_result(result):
    """Project a tick result to fixed public fields for CLI/status output."""
    if not isinstance(result, dict):
        return {'action': 'hold', 'code': 'TICK_RESULT_INVALID'}
    out = {'action': result.get('action'), 'schema_version': result.get('schema_version')}
    for key in ('stage', 'code'):
        if result.get(key) is not None:
            out[key] = result[key]
    owner = result.get('owner')
    if isinstance(owner, dict):
        out['owner'] = {k: owner.get(k) for k in ('action', 'phase', 'code', 'operation_id')
                        if owner.get(k) is not None}
    health = result.get('health')
    if isinstance(health, dict):
        out['health'] = {k: health.get(k) for k in ('action', 'code', 'pending')
                         if health.get(k) is not None}
    return out
