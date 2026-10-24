"""固定の構造化healthと受動witnessを、同意後の匿名診断観測へ射影します。"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType

from safe_io import fdopen_owned

from adapters.sdc.diagnostic_contract import COMPONENTS, LIMITS

MAX_SAFE_INTEGER = 2**53 - 1
MAX_HEALTH_BYTES = 1024 * 1024  # health_policy.STATUS_MAX_BYTES; no log/config reads.
HEALTH_COMPONENTS = ('scheduler', 'notification', 'connectivity', 'storage', 'config')
# health_report.build_health: these are producer codes, not text or queue counts.
# A failed/deferred delivery has no known cause: UNKNOWN does not infer a timeout.
HEALTH_REASONS = MappingProxyType({
    'run_failed': ('scheduler', 'run', 'UNKNOWN', 'error'),
    'stage_errors': ('scheduler', 'run', 'UNKNOWN', 'warning'),
    'run_deadline_exceeded': ('scheduler', 'run', 'TIMEOUT', 'error'),
    'collection_incomplete': ('scheduler', 'receive', 'UNKNOWN', 'warning'),
    'session_expired': ('connectivity', 'receive', 'REJECTED', 'warning'),
    'notification_failed': ('notification', 'send', 'UNKNOWN', 'error'),
    'notification_deferred': ('notification', 'send', 'UNKNOWN', 'info'),
    'notification_parked': ('notification', 'send', 'UNKNOWN', 'warning'),
    'notification_held': ('notification', 'send', 'UNKNOWN', 'warning'),
    'notification_pending': ('notification', 'send', 'UNKNOWN', 'info'),
    'disk_low': ('storage', 'run', 'RESOURCE_LIMIT', 'warning'),
    'ledger_relation_violations': ('storage', 'verify', 'INTEGRITY_FAILURE', 'error'),
    'backup_not_verified': ('storage', 'verify', 'UNKNOWN', 'warning'),
    'backup_deferred_deadline': ('storage', 'verify', 'TIMEOUT', 'warning'),
})
# Only Connector._transition's fixed results and explicit resume are accepted.
# Root supplies their existing transition time; this provider never reads a Store.
CONNECTOR_CODES = MappingProxyType({
    'SDC_ACCEPTED': None,
    'SDC_EXPLICIT_RESUME': None,
    'SDC_RETRY_LATER': ('send', 'UNKNOWN', 'warning'),
    'SDC_REQUEST_DENIED': ('send', 'REJECTED', 'error'),
    'SDC_TIME_SKEW': ('send', 'INVALID_STATE', 'warning'),
})
SOURCE_COVERAGE = MappingProxyType({
    'runtime': 'standalone owner presence only; Hermes is unobserved',
    'scheduler': 'health.json fixed run/collection reasons only',
    'notification': 'health.json fixed delivery reasons/state only',
    'connectivity': 'health.json session_expired only; no network probe',
    'update': 'unobserved; code_changed is not an update outcome',
    'storage': 'health.json fixed disk/ledger/backup fault observations only',
    'config': 'health.json backup configuration classification only',
    'connector': 'optional caller-injected fixed Connector observation only',
})
_UNKNOWN = ('unknown', 'UNKNOWN', 'warning')


def _require(ok):
    if not ok:
        raise ValueError('SDC_DIAGNOSTIC_PROVIDER_INVALID')


def _millisecond(value):
    return type(value) is int and 0 <= value <= MAX_SAFE_INTEGER


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= MAX_SAFE_INTEGER / 1000:
        return None
    result = int(value * 1000)
    return result if _millisecond(result) else None


def _read_health(root):
    """Open only root/data/health.json, bounding bytes and refusing each fixed link."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        data = os.open('data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            fd = os.open('health.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=data)
            with fdopen_owned(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_HEALTH_BYTES:
                    raise ValueError('SDC_DIAGNOSTIC_SOURCE_UNAVAILABLE')
                raw = stream.read(MAX_HEALTH_BYTES + 1)
                after = os.fstat(stream.fileno())
                if (len(raw) != before.st_size or len(raw) > MAX_HEALTH_BYTES
                        or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise ValueError('SDC_DIAGNOSTIC_SOURCE_UNAVAILABLE')
        finally:
            os.close(data)
    finally:
        os.close(directory)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('SDC_DIAGNOSTIC_SOURCE_UNAVAILABLE')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('SDC_DIAGNOSTIC_SOURCE_UNAVAILABLE')

    # Unselected fields are neither projected nor serialized/hashed again.
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def _health(value):
    """Project before hashing: never inspect errors, run_id, patients or counters."""
    if type(value) is not dict:
        return {}
    at = _seconds(value.get('at'))
    if at is None:
        return {}
    reasons = value.get('state_reasons')
    if type(reasons) is not list or len(reasons) > 64:
        return {}  # No truncation, guessed count or completed source mark.
    groups = {component: set() for component in HEALTH_COMPONENTS}
    unknown = False
    for reason in reasons:
        if type(reason) is not str or len(reason) > 128:
            return {}
        row = HEALTH_REASONS.get(reason)
        if row is None and reason != 'code_changed':
            unknown = True
        elif row is not None:
            groups[row[0]].add(row[1:])
    if (value.get('overall') not in ('ok', 'degraded', 'failed')
            or value.get('run_status') not in ('ok', 'partial', 'failed', 'session_expired')
            or unknown):
        groups['scheduler'].add(_UNKNOWN)
    # These producer fields cover missing reason entries without making up causes.
    if value.get('run_status') == 'failed':
        groups['scheduler'].add(('run', 'UNKNOWN', 'error'))
    if value.get('run_status') == 'session_expired':
        groups['connectivity'].add(('receive', 'REJECTED', 'warning'))
    if value.get('disk_low') is True:
        groups['storage'].add(('run', 'RESOURCE_LIMIT', 'warning'))
    observed = {'scheduler'}
    notify = value.get('notify')
    if type(notify) is dict and 'state' in notify:
        observed.add('notification')
        if notify['state'] in ('incomplete', 'parked') and not groups['notification']:
            groups['notification'].add(('send', 'UNKNOWN', 'warning'))
        elif notify['state'] not in ('ok', 'incomplete', 'parked'):
            groups['notification'].add(_UNKNOWN)
    backup = value.get('backup')
    if type(backup) is dict:
        observed.add('config')
    if type(backup) is dict and backup.get('state') == 'invalid':
        codes = backup.get('reasons')
        if (type(codes) is list and len(codes) <= 64
                and all(type(code) is str and len(code) <= 128 for code in codes)
                and 'backup_config_invalid' in codes):
            groups['config'].add(('verify', 'INVALID_STATE', 'error'))
        else:
            groups['config'].add(_UNKNOWN)
    elif type(backup) is dict and backup.get('state') not in ('disabled', 'ok', 'degraded', 'failed'):
        groups['config'].add(_UNKNOWN)
    # No session failure does not prove connectivity was observed (jobs-only is
    # possible); disk_low=False can mean no disk measurement. Neither gets a mark.
    return {component: (at, sorted(rows)) for component, rows in groups.items() if rows or component in observed}


def _runtime(root, mode, now, collector):
    if mode != 'standalone':
        return None
    if collector is None:
        from sdc_support_diagnostics import collect_diagnostic
        collector = collect_diagnostic
    result = collector('runtime.status.v1', generated_at_ms=now, root=root, mode=mode)
    if (type(result) is not dict or result.get('schema_version') != 'sdc.support.diagnostic-result.v1'
            or result.get('diagnostic_id') != 'runtime.status.v1' or not _millisecond(result.get('observed_at'))):
        return None
    from mcs_standalone.service import STATUS_TTL
    if not 0 <= now - result['observed_at'] <= STATUS_TTL * 1000:
        return None
    data = result.get('data')
    # The existing collector validates service.status's owner timestamp/TTL/generation.
    # Neither the source's generation nor a commit/PID enters a mark or event.
    rows = [] if (type(data) is dict and data.get('mode') == mode
                  and data.get('process') == 'running' and result.get('status') in ('observed', 'partial')) else [_UNKNOWN]
    return result['observed_at'], rows


def _connector(value):
    if type(value) is not dict or not _millisecond(value.get('observed_at')):
        return None
    code = value.get('code')
    row = CONNECTOR_CODES.get(code, _UNKNOWN) if type(code) is str else _UNKNOWN
    return value['observed_at'], [] if row is None else [row]


def collect_events(*, trusted_root, mode, previous_marks, since_ms, now_ms,
                   health_reader=None, diagnostic_collector=None, connector_observation=None):
    """Collect consent-bounded observations, not inferred failure occurrence counts.

    count=1 means one component/classification in one producer observation; equal
    classifications from several reasons collapse to one DTO row. No patient/source
    counter is copied. dropped_events counts omitted DTO rows, never Event.count.
    observed_at is assembly time, not a global completed-source watermark. Marks
    contain only each producer's time and sorted safe classifications. Unavailable
    sources advance no mark; same-time conflicts and rollback remain partial/held.

    Root/mode are trusted local caller inputs, never remote params. Only bounded
    health.json and the existing passive standalone collector are read; injection
    hooks are fixed trusted/test providers, not paths/commands/config discovery.
    update has no accepted source, so this partial observer never claims all eight
    components were observed, even when no fault rows are returned.
    """
    _require(type(trusted_root) is str or isinstance(trusted_root, Path))
    root = Path(trusted_root)
    _require(root.is_absolute() and '..' not in root.parts and type(mode) is str and mode in ('hermes', 'standalone'))
    _require(_millisecond(since_ms) and _millisecond(now_ms) and since_ms <= now_ms)
    _require(type(previous_marks) is dict and set(previous_marks) <= set(COMPONENTS))
    previous = {}
    for component, mark in previous_marks.items():
        _require(type(mark) is dict and set(mark) == {'observed_at', 'signature'})
        _require(_millisecond(mark['observed_at']) and since_ms <= mark['observed_at'] <= now_ms)
        _require(type(mark['signature']) is str and re.fullmatch(r'[0-9a-f]{64}', mark['signature']))
        previous[component] = dict(mark)
    _require(health_reader is None or callable(health_reader))
    _require(diagnostic_collector is None or callable(diagnostic_collector))

    observations = {}
    try:
        observations.update(_health((health_reader or _read_health)(root)))
    except TimeoutError:
        raise
    except Exception:
        pass  # Fixed missing-source outcome; no arbitrary error is emitted or hashed.
    try:
        runtime = _runtime(root, mode, now_ms, diagnostic_collector)
    except TimeoutError:
        raise
    except Exception:
        runtime = None
    if runtime is not None:
        observations['runtime'] = runtime
    connector = _connector(connector_observation)
    if connector is not None:
        observations['connector'] = connector

    events, marks, dropped = [], {}, 0
    for component in COMPONENTS:
        if component not in observations:
            continue
        at, classifications = observations[component]
        # Old consent history and future/rolled-back sources never receive a mark.
        if not since_ms <= at <= now_ms:
            continue
        prior = previous.get(component)
        if prior is not None and at <= prior['observed_at']:
            continue  # Same occurrence, including conflicting rewrites, is not new.
        safe = {'component': component, 'observed_at': at, 'classifications': classifications}
        signature = hashlib.sha256(json.dumps(safe, sort_keys=True, ensure_ascii=True,
                                             separators=(',', ':')).encode('ascii')).hexdigest()
        marks[component] = {'observed_at': at, 'signature': signature}
        bucket = at // LIMITS['bucket_ms'] * LIMITS['bucket_ms']
        if bucket < now_ms - LIMITS['max_age_ms']:
            dropped += len(classifications)
            continue
        for stage, code, severity in classifications:
            events.append({'component': component, 'stage': stage, 'code': code,
                           'severity': severity, 'bucket_start': bucket, 'count': 1})
    _require(len(events) <= LIMITS['events_per_batch'] and dropped <= LIMITS['dropped_max'])
    return {'events': events, 'dropped_events': dropped, 'partial': True,
            'observed_at': now_ms, 'source_marks': marks}
