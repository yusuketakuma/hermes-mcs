"""Collect allowlisted public distribution metadata in the selected interpreter."""
from importlib import metadata
import hashlib
import json
import math
import platform
import re
import time
import uuid

SCHEMA = 'sdc.inventory.v1'
PAGE_SCHEMA = 'sdc.inventory.page.v1'
MAX_PAGE_BYTES = 16384
MAX_PAGE_ITEMS = 100
MAX_PAGES = 32
# shortcut: inventories above 10,000 distributions stay partial; raise only with a tested collection budget.
MAX_DISTRIBUTIONS = 10000
_EXCLUSIONS = ('not_public', 'direct_source', 'invalid_metadata', 'metadata_error', 'conflicting_distribution')
_FIELDS = {'schema_version', 'runtime', 'packages', 'complete', 'coverage', 'fetched_at', 'source_revision',
           'excluded_counts', 'collection_failed', 'lock_complete', 'lock_discrepancies'}


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,198}[A-Za-z0-9])?', value):
        raise ValueError('SDC_INVALID_PACKAGE_NAME')
    return re.sub(r'[-_.]+', '-', value).lower()


def _version(value):
    # Public canonical subset only; never strip local/private build labels.
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not re.fullmatch(r'(?:[0-9]{1,8}!)?[0-9]{1,8}(?:\.[0-9]{1,8}){0,7}(?:(?:a|b|rc)[0-9]{1,8})?(?:\.post[0-9]{1,8})?(?:\.dev[0-9]{1,8})?', value):
        raise ValueError('SDC_INVALID_PACKAGE_VERSION')
    return value


def _raw(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def collect_inventory(*, public_packages, runtime_mode, source_revision, distributions=None,
                      lock_entries=None, clock=time.time, python_version=None):
    """Use after mcs_runtime has selected and pinned this process's interpreter.

    public_packages is an explicitly approved set of public PyPI names, never
    inferred from installed names. lock_entries maps approved names to exact
    versions; parsing project lock files remains with their existing owner.
    """
    if not isinstance(public_packages, (set, frozenset, list, tuple)) or len(public_packages) > MAX_DISTRIBUTIONS:
        raise ValueError('SDC_INVALID_PUBLIC_ALLOWLIST')
    try:
        approved = {_name(value) for value in public_packages}
    except ValueError:
        raise ValueError('SDC_INVALID_PUBLIC_ALLOWLIST') from None
    stamp = clock()
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not 0 <= stamp < 253402300800:
        raise ValueError('SDC_INVALID_INVENTORY_TIME')
    mode = runtime_mode if isinstance(runtime_mode, str) and runtime_mode in ('hermes', 'standalone') else 'unknown'
    python = platform.python_version() if python_version is None else python_version
    python = python if isinstance(python, str) and re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}', python) else None
    revision = source_revision if isinstance(source_revision, str) and re.fullmatch('[0-9a-f]{40}', source_revision) else None
    excluded = dict.fromkeys(_EXCLUSIONS, 0)
    installed, conflicts = {}, set()
    failed, seen = False, 0
    try:
        for dist in metadata.distributions() if distributions is None else distributions:
            seen += 1
            if seen > MAX_DISTRIBUTIONS:
                failed = True
                break
            try:
                name = _name(dist.metadata.get('Name'))
                if name not in approved:
                    excluded['not_public'] += 1
                    continue
                # Presence alone excludes VCS/local/editable sources; never expose its content.
                if dist.read_text('direct_url.json') is not None:
                    excluded['direct_source'] += 1
                    continue
                version = _version(dist.version)
            except ValueError:
                excluded['invalid_metadata'] += 1
                continue
            except TimeoutError:
                raise
            except Exception:
                excluded['metadata_error'] += 1
                continue
            if name in conflicts:
                excluded['conflicting_distribution'] += 1
            elif name in installed and installed[name] != version:
                del installed[name]
                conflicts.add(name)
                excluded['conflicting_distribution'] += 2
            else:
                installed[name] = version
    except TimeoutError:
        raise
    except Exception:
        failed = True
    lock, lock_complete = {}, isinstance(lock_entries, dict)
    if lock_complete:
        if len(lock_entries) > MAX_DISTRIBUTIONS:
            raise ValueError('SDC_INVALID_INVENTORY_LOCK')
        for key, value in lock_entries.items():
            try:
                name, version = _name(key), _version(value)
                if name not in approved or name in lock and lock[name] != version:
                    lock_complete = False
                    continue
                lock[name] = version
            except ValueError:
                lock_complete = False
    differences = []
    for name in sorted(set(lock) | set(installed)) if isinstance(lock_entries, dict) else ():
        actual, expected = installed.get(name), lock.get(name)
        if actual != expected:
            differences.append({'ecosystem': 'PyPI', 'name': name, 'installed_version': actual,
                                'locked_version': expected, 'kind': 'missing' if actual is None else 'unlocked' if expected is None else 'version_mismatch'})
    complete = bool(seen and installed and not failed and not any(excluded.values()) and mode != 'unknown' and python and revision)
    snapshot = {'schema_version': SCHEMA, 'runtime': {'mode': mode, 'python_version': python},
                'packages': [{'ecosystem': 'PyPI', 'name': name, 'version': version, 'public': True} for name, version in sorted(installed.items())],
                'complete': complete, 'coverage': 'complete' if complete else 'unknown', 'fetched_at': int(stamp * 1000),
                'source_revision': revision, 'excluded_counts': excluded, 'collection_failed': failed,
                'lock_complete': lock_complete, 'lock_discrepancies': differences}
    return {**snapshot, 'hash': hashlib.sha256(_raw(snapshot)).hexdigest()}


def _valid_snapshot(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != _FIELDS | {'hash'}:
        return False
    runtime = snapshot['runtime']
    if not isinstance(runtime, dict) or set(runtime) != {'mode', 'python_version'} or runtime['mode'] not in ('hermes', 'standalone', 'unknown'):
        return False
    if runtime['python_version'] is not None and (not isinstance(runtime['python_version'], str) or not re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}', runtime['python_version'])):
        return False
    if snapshot['schema_version'] != SCHEMA or type(snapshot['fetched_at']) is not int or not 0 <= snapshot['fetched_at'] < 253402300800000:
        return False
    if snapshot['source_revision'] is not None and (not isinstance(snapshot['source_revision'], str) or not re.fullmatch('[0-9a-f]{40}', snapshot['source_revision'])):
        return False
    if any(type(snapshot[key]) is not bool for key in ('complete', 'collection_failed', 'lock_complete')) or snapshot['coverage'] != ('complete' if snapshot['complete'] else 'unknown'):
        return False
    counts = snapshot['excluded_counts']
    if not isinstance(counts, dict) or set(counts) != set(_EXCLUSIONS) or any(type(value) is not int or not 0 <= value <= MAX_DISTRIBUTIONS for value in counts.values()):
        return False
    for field in ('packages', 'lock_discrepancies'):
        if not isinstance(snapshot[field], list):
            return False
        for item in snapshot[field]:
            fields = {'ecosystem', 'name', 'version', 'public'} if field == 'packages' else {'ecosystem', 'name', 'installed_version', 'locked_version', 'kind'}
            if not isinstance(item, dict) or set(item) != fields or item['ecosystem'] != 'PyPI':
                return False
            try:
                if _name(item['name']) != item['name']:
                    return False
                if field == 'packages':
                    _version(item['version'])
                    if item['public'] is not True:
                        return False
                else:
                    for key in ('installed_version', 'locked_version'):
                        if item[key] is not None:
                            _version(item[key])
                    if item['kind'] not in ('missing', 'unlocked', 'version_mismatch'):
                        return False
            except ValueError:
                return False
    return isinstance(snapshot['hash'], str) and re.fullmatch('[0-9a-f]{64}', snapshot['hash']) is not None


def inventory_pages(snapshot, snapshot_id):
    """Return fixed-schema pages of at most 16KiB and 100 combined entries."""
    try:
        valid_id = isinstance(snapshot_id, str) and str(uuid.UUID(snapshot_id)) == snapshot_id
    except ValueError:
        valid_id = False
    if not valid_id or not _valid_snapshot(snapshot):
        raise ValueError('SDC_INVALID_INVENTORY')
    source = {key: value for key, value in snapshot.items() if key != 'hash'}
    if snapshot['hash'] != hashlib.sha256(_raw(source)).hexdigest():
        raise ValueError('SDC_INVALID_INVENTORY')
    header = {key: value for key, value in source.items() if key not in ('packages', 'lock_discrepancies', 'schema_version')}
    pages = []
    page = {**header, 'schema_version': PAGE_SCHEMA, 'snapshot_id': snapshot_id, 'snapshot_hash': snapshot['hash'], 'page': 1,
            'page_count': 1, 'packages': [], 'lock_discrepancies': []}
    # Reserve the largest possible page-number width before splitting.
    count_bound = len(snapshot['packages']) + len(snapshot['lock_discrepancies']) + 1
    page['page'] = page['page_count'] = count_bound
    for field in ('packages', 'lock_discrepancies'):
        for item in snapshot[field]:
            target = {**page, field: page[field] + [item]}
            if len(target['packages']) + len(target['lock_discrepancies']) > MAX_PAGE_ITEMS or len(_raw(target)) > MAX_PAGE_BYTES:
                if not page['packages'] and not page['lock_discrepancies']:
                    raise ValueError('SDC_BODY_TOO_LARGE')
                pages.append(page)
                page = {**page, 'packages': [], 'lock_discrepancies': []}
                target = {**page, field: [item]}
                if len(_raw(target)) > MAX_PAGE_BYTES:
                    raise ValueError('SDC_BODY_TOO_LARGE')
            page = target
    pages.append(page)
    if len(pages) > MAX_PAGES:
        raise ValueError('SDC_INVENTORY_PAGE_LIMIT')
    return [_raw({**item, 'page': index, 'page_count': len(pages)}) for index, item in enumerate(pages, 1)]
