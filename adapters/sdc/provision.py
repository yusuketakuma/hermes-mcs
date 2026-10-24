"""初回公開設定の私有保存・R1登録開始・同じdeviceによるACK照合を所有する。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import uuid

from . import provision_contract as contract
from .protocol import endpoint
from .support_contract import canonical

NAME = 'provision-state.json'
SCHEMA = 'sdc.provision.local.v1'
ACK = '/v2/provisioning/ack'
STATUS = '/v2/provisioning/status'
# Per slot: three escaped <=16KiB raws, two <=16KiB responses and <=8KiB metadata.
# Three slots plus <=8KiB root metadata; below Store's separate 1MiB read ceiling.
STATE_MAX_BYTES = 3 * (3 * 2 * 16384 + 2 * 16384 + 8192) + 8192
PROFILES = ('monitor', 'support', 'restart')
_CONSUMERS = {'support': ('support-state.json',),
              'restart': ('restart-state.json', 'restart-planning.json')}
EXPECTED_FIELDS = frozenset(('console_origin', 'bundle_hash', 'tenant', 'installation',
                             'environment', 'enrollment_id', 'profile_id', 'origin', 'audience'))
_LEGACY = ('endpoint.json', 'support-bootstrap.json', 'state.json', 'credential',
           'credential-store.json', 'enrollment-consent.json', 'support-state.json',
           'restart-state.json', 'restart-planning.json', 'restart-execution.json')


class ProvisionError(ValueError):
    """Return fixed local failure codes without raw inputs or credential errors."""

    def __init__(self, code='INVALID'):
        allowed = ('INVALID', 'MISSING', 'CONFLICT', 'UNCERTAIN', 'EXPIRED', 'CONSENT_REQUIRED',
                   'UNSUPPORTED', 'REGISTRATION_HELD', 'ATTEMPTS_EXHAUSTED')
        super().__init__('SDC_PROVISION_' + (code if code in allowed else 'INVALID'))


def _check(ok, code='INVALID'):
    if not ok:
        raise ProvisionError(code)


def _public(function):
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ProvisionError, BlockingIOError, TimeoutError):
            raise
        except Exception:
            raise ProvisionError() from None
    call.__name__, call.__doc__ = function.__name__, function.__doc__
    return call


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False))


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _record_hash(value):
    return _hash(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii'))


def _fields(value, names):
    _check(type(value) is dict and set(value) == set(names))


def _time(value):
    _check(type(value) is int and 0 <= value <= contract.MAX_SAFE_INTEGER)


def _reason(value):
    _check(type(value) is str and re.fullmatch(r'[\x20-\x7e]{1,128}', value)
           and bool(value.strip()), 'CONSENT_REQUIRED')


def _human(confirm, reason):
    _check(confirm is True, 'CONSENT_REQUIRED')
    _reason(reason)


def _uuid(value):
    contract.validate_provision('UUID', value, now_ms=0)


def _expected(value):
    _fields(value, EXPECTED_FIELDS)
    kinds = {'console_origin': 'Origin', 'bundle_hash': 'Hash', 'tenant': 'Identifier',
             'installation': 'Identifier', 'environment': 'Environment', 'enrollment_id': 'UUID',
             'profile_id': 'ProfileID', 'origin': 'Origin', 'audience': 'Audience'}
    for field, kind in kinds.items():
        contract.validate_provision(kind, value[field], now_ms=0)
    return _copy(value)


def _request(body):
    raw = canonical(body, max_bytes=16384)
    return {'raw': raw, 'body_hash': _hash(raw.encode('ascii'))}


def _body(record, definition, now):
    _fields(record, ('raw', 'body_hash'))
    _check(type(record['raw']) is str and len(record['raw']) <= 16384)
    raw = record['raw'].encode('ascii')
    _check(_hash(raw) == record['body_hash'])
    value = contract.parse_provision(definition, raw, now_ms=now)
    _check(canonical(value, max_bytes=16384) == record['raw'])
    return value


def _bundle(slot):
    return contract.parse_provision('Bundle', slot['raw'], now_ms=slot['receipt']['applied_at'])


def _identity(value):
    _fields(value, ('device_id', 'claim_id', 'key_id'))
    for item in value.values():
        _uuid(item)


def _receipt_matches(slot, receipt, identity=None):
    contract.validate_provision('Receipt', receipt, now_ms=slot['receipt']['applied_at'])
    request = _body(slot['ack_request'], 'AckRequest', slot['receipt']['applied_at'])
    _check(all(receipt[field] == value for field, value in request.items() if field != 'schema_version'), 'CONFLICT')
    bundle = _bundle(slot)
    _check(receipt['generation'] == bundle['generation']
           and bundle['issued_at'] <= receipt['accepted_at'] < bundle['import_expires_at']
           and receipt['applied_at'] <= receipt['accepted_at'] + 300000, 'CONFLICT')
    if identity is not None:
        _check(all(receipt[field] == value for field, value in identity.items()), 'CONFLICT')
    if bundle['profile_id'] != 'monitor':
        _check(all(receipt[field] == bundle[field] for field in ('device_id', 'claim_id')), 'CONFLICT')


def _cohort(state, bundle):
    """Compare installed public baselines, without inventing R1 device epochs or policy epochs."""
    if bundle['profile_id'] == 'monitor':
        return
    monitor = state['profiles']['monitor']
    _check(monitor is not None and state['registration'] is not None, 'REGISTRATION_HELD')
    baseline = _bundle(monitor)
    fields = ('root_id', 'enrollment_id', 'tenant', 'installation', 'environment', 'realm_epoch')
    _check(all(bundle[k] == baseline[k] for k in fields), 'CONFLICT')
    for profile in _CONSUMERS:
        other = state['profiles'][profile]
        if other is not None:
            previous = _bundle(other)
            _check(all(bundle[k] == previous[k] for k in (*fields, 'device_id', 'claim_id', 'device_epoch')), 'CONFLICT')
    if bundle['profile_id'] == 'restart':
        _check(state['profiles']['support'] is not None, 'MISSING')


def _validated(state):
    _fields(state, ('schema_version', 'root_id', 'root_fingerprint', 'prepared_at', 'reason', 'profiles', 'registration'))
    _check(state['schema_version'] == SCHEMA)
    _uuid(state['root_id'])
    contract.validate_provision('Hash', state['root_fingerprint'], now_ms=0)
    _time(state['prepared_at'])
    _reason(state['reason'])
    _fields(state['profiles'], PROFILES)
    registration = state['registration']
    if registration is not None:
        _fields(registration, ('registration_id', 'started_at'))
        _uuid(registration['registration_id'])
        _time(registration['started_at'])
    for profile, slot in state['profiles'].items():
        if slot is None:
            continue
        _fields(slot, ('raw', 'bundle_hash', 'expected', 'receipt', 'ack_request', 'status_request',
                       'ack_attempts', 'ack_denied', 'ack', 'last_status', 'last_result'))
        _fields(slot['receipt'], ('local_receipt_id', 'ack_id', 'applied_at', 'reason'))
        local = slot['receipt']
        _uuid(local['local_receipt_id'])
        _uuid(local['ack_id'])
        _time(local['applied_at'])
        _reason(local['reason'])
        _check(state['prepared_at'] <= local['applied_at'])
        _check(type(slot['raw']) is str and len(slot['raw']) <= 16384)
        bundle = _bundle(slot)
        contract.validate_import_bundle(bundle, now_ms=local['applied_at'])
        _check(canonical(bundle, max_bytes=16384) == slot['raw'])
        _check(_hash(slot['raw'].encode('ascii')) == slot['bundle_hash'])
        expected = _expected(slot['expected'])
        _check(expected['bundle_hash'] == slot['bundle_hash'])
        _check(all(bundle[key] == value for key, value in expected.items() if key != 'bundle_hash'))
        _check(bundle['root_id'] == state['root_id'] and bundle['profile_id'] == profile
               and bundle['generation'] == 1 and bundle['previous_provision_id'] is None)
        _cohort(state, bundle)
        endpoint({key: bundle[key] for key in ('origin', 'audience', 'environment')})
        ack = _body(slot['ack_request'], 'AckRequest', local['applied_at'])
        status_request = _body(slot['status_request'], 'StatusRequest', local['applied_at'])
        common = {'provision_id': bundle['provision_id'], 'root_id': state['root_id'],
                  'bundle_hash': slot['bundle_hash'], 'local_receipt_id': local['local_receipt_id']}
        _check(ack == {'schema_version': 'sdc.provision.ack.v1', **common,
                       'ack_id': local['ack_id'], 'applied_at': local['applied_at']})
        _check(status_request == {'schema_version': 'sdc.provision.status.v1', **common})
        _check(type(slot['ack_attempts']) is list and len(slot['ack_attempts']) <= 4)
        _check(type(slot['ack_denied']) is bool)
        for attempt in slot['ack_attempts']:
            _fields(attempt, ('sent_at', 'reason', 'identity'))
            _time(attempt['sent_at'])
            _reason(attempt['reason'])
            _identity(attempt['identity'])
            _check(local['applied_at'] <= attempt['sent_at'] < bundle['expires_at'])
        if slot['ack_attempts']:
            _check(slot['ack_attempts'][0]['sent_at'] < bundle['import_expires_at'])
        if slot['ack'] is not None:
            _receipt_matches(slot, slot['ack'])
        if slot['last_status'] is not None:
            contract.validate_provision('StatusResponse', slot['last_status'], now_ms=local['applied_at'])
            _status_matches(slot, slot['last_status']['status'])
        _check(slot['last_result'] in ('not-sent', 'unknown', 'denied', 'acknowledged', 'status-observed'))
        if profile == 'monitor' and registration is not None:
            _check(local['applied_at'] <= registration['started_at'])
        elif profile != 'monitor':
            _check(registration['started_at'] <= local['applied_at'])
    if state['profiles']['monitor'] is None:
        _check(registration is None)
    _check(len(_encoded(state)) <= STATE_MAX_BYTES)
    return _copy(state)


def _certain(store):
    _check(not getattr(store, '_provision_uncertain', False), 'UNCERTAIN')


def _read(store, *, optional=False):
    _certain(store)
    root = store.root_fingerprint()
    value = store._read_file(NAME)
    if value is None:
        _check(not getattr(store, '_provision_seen', False), 'CONFLICT')
        _check(optional, 'MISSING')
        return None
    store._provision_seen = True
    value = _validated(value)
    _check(value['root_fingerprint'] == root == store.root_fingerprint(), 'CONFLICT')
    _check(store._read_file('endpoint.json') is None and store._read_file('support-bootstrap.json') is None, 'CONFLICT')
    return value


def _write(store, state):
    _certain(store)
    state = _validated(state)
    try:
        _check(store.root_fingerprint() == state['root_fingerprint'], 'CONFLICT')
        store._write_file(NAME, state)
        _check(store.root_fingerprint() == state['root_fingerprint'] and store._read_file(NAME) == state, 'CONFLICT')
        store._provision_seen = True
    except BaseException as error:
        store._provision_uncertain = True
        if isinstance(error, TimeoutError) or not isinstance(error, Exception):
            raise
        raise ProvisionError('UNCERTAIN') from None


class _Permit:
    def __init__(self, store, state, marker):
        self.store, self.fd, self.root = store, store.fd, state['root_fingerprint']
        self.marker = marker

    def __reduce__(self):
        raise TypeError('SDC_PROVISION_CONFLICT')


def _permit(store, value, state, marker):
    return (type(value) is _Permit and value.store is store and value.fd == store.fd
            and value.root == state['root_fingerprint'] and value.marker == marker)


def _summary(state, profile='monitor'):
    if state is None:
        return {'state': 'legacy', 'root_id': None, 'profile': None, 'local_receipt_id': None,
                'registration_started': False, 'ack_attempts': 0, 'acknowledged': False}
    slot = state['profiles'][profile]
    return {'state': ('prepared' if slot is None else 'acknowledged' if slot['ack'] is not None
                      else 'denied' if slot['ack_denied'] else slot['last_result'] if slot['last_result'] != 'not-sent'
                      else 'registration-started' if state['registration'] else 'imported'),
            'root_id': state['root_id'], 'profile': profile if slot else None,
            'local_receipt_id': slot['receipt']['local_receipt_id'] if slot else None,
            'registration_started': state['registration'] is not None,
            'ack_attempts': len(slot['ack_attempts']) if slot else 0,
            'acknowledged': slot is not None and slot['ack'] is not None}


@_public
def status(store, *, now_ms, profile='monitor'):
    """Read bounded local evidence only; no credential, central freshness or authority claim."""
    _time(now_ms)
    _check(profile in PROFILES, 'UNSUPPORTED')
    with store.lock():
        return _summary(_read(store, optional=True), profile)


@_public
def prepare_root(store, *, confirm_human, reason, now_ms):
    """Persist a UUID only in an explicitly created new directory; not a restore witness."""
    _human(confirm_human, reason)
    _time(now_ms)
    with store.lock():
        _certain(store)
        _check(getattr(store, 'created_new', False) is True, 'CONFLICT')
        _check(set(os.listdir(store.fd)) <= {'tick.lock'}, 'CONFLICT')
        state = {'schema_version': SCHEMA, 'root_id': str(uuid.uuid4()),
                 'root_fingerprint': store.root_fingerprint(), 'prepared_at': now_ms, 'reason': reason,
                 'profiles': dict.fromkeys(('monitor', 'support', 'restart')), 'registration': None}
        try:
            fd = store._open(NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            os.close(fd)
            os.fsync(store.fd)
            _write(store, state)
        except BaseException as error:
            store._provision_uncertain = True
            if isinstance(error, TimeoutError) or not isinstance(error, Exception):
                raise
            raise ProvisionError('UNCERTAIN') from None
        return _summary(state)


@_public
def prepare_import(store, raw, *, expected, now_ms):
    """Bind independently supplied Console values to exact public bytes; never authenticate their source."""
    _time(now_ms)
    expected = _expected(expected)
    _check(type(raw) in (bytes, str))
    _check(len(raw) <= 16384)
    raw = raw.encode('utf-8') if type(raw) is str else raw
    bundle = contract.parse_provision('Bundle', raw, now_ms=now_ms)
    contract.validate_import_bundle(bundle, now_ms=now_ms)
    _check(raw == canonical(bundle, max_bytes=16384).encode('ascii')
           and _hash(raw) == expected['bundle_hash'], 'CONFLICT')
    _check(all(bundle[key] == value for key, value in expected.items() if key != 'bundle_hash'), 'CONFLICT')
    _check(bundle['generation'] == 1 and bundle['previous_provision_id'] is None, 'CONFLICT')
    try:
        endpoint({key: bundle[key] for key in ('origin', 'audience', 'environment')})
    except ValueError:
        raise ProvisionError('UNSUPPORTED') from None
    with store.lock():
        state = _read(store)
        _check(bundle['root_id'] == state['root_id'], 'CONFLICT')
        profile = bundle['profile_id']
        slot = state['profiles'][profile]
        _check(slot is None or slot['raw'] == raw.decode('ascii') and slot['expected'] == expected, 'CONFLICT')
        if profile == 'monitor':
            _check(state['registration'] is None, 'REGISTRATION_HELD')
        else:
            _profile_current(store, state, bundle, now_ms)
            if slot is None:
                _check(all(store._read_file(name) is None for name in _CONSUMERS[profile]), 'CONFLICT')
                if profile == 'restart':
                    _check(store._read_file('restart-execution.json') is None, 'CONFLICT')
        return {'bundle': bundle, 'raw': raw.decode('ascii'), 'expected': expected, 'state_hash': _record_hash(state)}


@_public
def commit_import(store, plan, *, confirm_human, reason, now_ms):
    """Fsync immutable profile evidence before issuing its distinct same-Store initial permit."""
    _human(confirm_human, reason)
    _fields(plan, ('bundle', 'raw', 'expected', 'state_hash'))
    fresh = prepare_import(store, plan['raw'], expected=plan['expected'], now_ms=now_ms)
    _check(fresh == plan, 'CONFLICT')
    with store.lock():
        state = _read(store)
        _check(_record_hash(state) == plan['state_hash'], 'CONFLICT')
        profile = plan['bundle']['profile_id']
        slot = state['profiles'][profile]
        first = slot is None
        if profile == 'monitor':
            _check(all(store._read_file(name) is None for name in _LEGACY), 'CONFLICT')
        else:
            _profile_current(store, state, plan['bundle'], now_ms)
            permits = getattr(store, '_provision_profile_permits', {})
            permits.pop(profile, None)  # Reapproval never renews even an unused initial permit.
            store._provision_profile_permits = permits
            if first:
                _check(all(store._read_file(name) is None for name in _CONSUMERS[profile]), 'CONFLICT')
                if profile == 'restart':
                    _check(store._read_file('restart-execution.json') is None, 'CONFLICT')
        if first:
            local = {'local_receipt_id': str(uuid.uuid4()), 'ack_id': str(uuid.uuid4()),
                     'applied_at': now_ms, 'reason': reason}
            common = {'provision_id': plan['bundle']['provision_id'], 'root_id': state['root_id'],
                      'bundle_hash': plan['expected']['bundle_hash'], 'local_receipt_id': local['local_receipt_id']}
            slot = {'raw': plan['raw'], 'bundle_hash': common['bundle_hash'], 'expected': plan['expected'],
                    'receipt': local, 'ack_request': _request({'schema_version': 'sdc.provision.ack.v1', **common,
                                                            'ack_id': local['ack_id'], 'applied_at': now_ms}),
                    'status_request': _request({'schema_version': 'sdc.provision.status.v1', **common}),
                    'ack_attempts': [], 'ack_denied': False, 'ack': None, 'last_status': None, 'last_result': 'not-sent'}
            state['profiles'][profile] = slot
        # Reapproval re-fsyncs the same receipt/bytes/IDs/time, never renews them.
        _write(store, state)
        if profile == 'monitor':
            store._provision_import_permit = _Permit(store, state, slot['bundle_hash'])
        elif first:
            store._provision_profile_permits[profile] = _Permit(store, state, _profile_marker(profile, slot))
        return _copy(slot['receipt'])


@_public
def monitor_endpoint(store):
    """Project the one stored monitor endpoint without consulting pin expiry, ACK or credentials."""
    state = _read(store, optional=True)
    if state is None:
        return None
    slot = state['profiles']['monitor']
    _check(slot is not None, 'MISSING')
    bundle = _bundle(slot)
    return {key: bundle[key] for key in ('origin', 'audience', 'environment')}


def _profile_marker(profile, slot):
    return profile, slot['bundle_hash'], slot['receipt']['local_receipt_id']


def _profile_current(store, state, bundle, now_ms):
    _cohort(state, bundle)
    _check(bundle['issued_at'] <= now_ms < bundle['expires_at'], 'EXPIRED')
    _check(bundle['profile_id'] not in getattr(store, '_provision_profile_failed', set()), 'UNCERTAIN')
    monitor = store._read_file('state.json')
    _check(state['registration'] is not None and _registration_state(monitor, active_only=True)
           and 'rotation_id' not in monitor, 'REGISTRATION_HELD')
    if bundle['profile_id'] != 'monitor':
        _check(all(bundle[k] == monitor[k] for k in ('device_id', 'claim_id')), 'CONFLICT')
    slot = state['profiles'][bundle['profile_id']]
    if slot is not None:
        _check(not slot['ack_denied'], 'CONFLICT')


def _profile_snapshot(state, profile):
    slot = state['profiles'][profile]
    _check(slot is not None, 'MISSING')
    monitor = state['profiles']['monitor']
    _check(monitor is not None, 'MISSING')
    monitor_bundle = _bundle(monitor)
    target = {k: monitor_bundle[k] for k in ('origin', 'audience', 'environment')}
    return {'bundle': _bundle(slot), 'root_fingerprint': state['root_fingerprint'],
            'monitor_endpoint_hash': _record_hash(target), 'bundle_hash': slot['bundle_hash'],
            'local_receipt_id': slot['receipt']['local_receipt_id']}


def _consumer_records(store, state, profile):
    for name in _CONSUMERS[profile]:
        value = store._read_file(name)
        _check(type(value) is dict and value.get('root_fingerprint') == state['root_fingerprint'], 'CONFLICT')


@_public
def profile_snapshot(store, *, profile, now_ms):
    """Read current local profile metadata, never a secret, HTTP or central-head proof.

    The caller holds any required state locks. Do not reacquire tick.lock here:
    suppliers use this during their existing support -> restart -> tick sections.
    """
    _check(profile in PROFILES, 'UNSUPPORTED')
    _time(now_ms)
    state = _read(store)
    result = _profile_snapshot(state, profile)
    _profile_current(store, state, result['bundle'], now_ms)
    if profile in _CONSUMERS and not _initialization_active(store, state, profile):
        _consumer_records(store, state, profile)
    _check(store.root_fingerprint() == result['root_fingerprint'], 'CONFLICT')
    return _copy(result)


@_public
def support_bootstrap(store):
    """Project saved support bytes; consumers must separately recheck current profile_snapshot."""
    state = _read(store, optional=True)
    if state is None:
        return None
    authority = _profile_snapshot(state, 'support')
    bundle = authority['bundle']
    return {'schema_version': 'sdc.support.bootstrap.v1',
            'root_fingerprint': authority['root_fingerprint'],
            'monitor_endpoint_hash': authority['monitor_endpoint_hash'],
            'support_endpoint': {k: bundle[k] for k in ('origin', 'audience', 'environment')},
            'pins': _copy(bundle['pins']), 'authority': authority}


def _initialization_active(store, state, profile):
    slot = state['profiles'][profile]
    active = getattr(store, '_provision_initializing', None)
    return (slot is not None and _permit(store, active, state, _profile_marker(profile, slot))
            and getattr(store, '_provision_initializing_thread', None) == threading.get_ident()
            and profile not in getattr(store, '_provision_profile_failed', set()))


@_public
def initialization_allowed(store, profile):
    """Permit only explicit legacy initialization or this same-Store callback's active scope."""
    _check(profile in _CONSUMERS, 'UNSUPPORTED')
    state = _read(store, optional=True)
    if state is None:
        return True  # Existing caller still owns its explicit legacy fresh-root contract.
    return _initialization_active(store, state, profile)


@_public
def initialize_profile(store, *, profile, now_ms, initializer):
    """Consume one newly imported profile's RAM permission, then invoke its initializer unlocked."""
    _check(profile in _CONSUMERS and callable(initializer), 'UNSUPPORTED')
    _time(now_ms)
    with store.lock():
        state = _read(store)
        slot = state['profiles'][profile]
        permission = getattr(store, '_provision_profile_permits', {}).pop(profile, None)
        _check(slot is not None and _permit(store, permission, state, _profile_marker(profile, slot)), 'CONFLICT')
        _check(getattr(store, '_provision_initializing', None) is None, 'CONFLICT')
        _profile_current(store, state, _bundle(slot), now_ms)
        _check(all(store._read_file(name) is None for name in _CONSUMERS[profile]), 'CONFLICT')
        if profile == 'restart':
            _check(store._read_file('restart-execution.json') is None, 'CONFLICT')
        store._provision_initializing = permission
        store._provision_initializing_thread = threading.get_ident()
    try:
        initializer()
        with store.lock():
            current = _read(store)
            _check(_permit(store, permission, current, _profile_marker(profile, current['profiles'][profile])), 'CONFLICT')
            _profile_current(store, current, _bundle(current['profiles'][profile]), now_ms)
            _consumer_records(store, current, profile)
            return _copy(slot['receipt'])
    except BaseException:
        failed = getattr(store, '_provision_profile_failed', set())
        failed.add(profile)
        store._provision_profile_failed = failed
        raise
    finally:
        store._provision_initializing = None
        store._provision_initializing_thread = None


def _registration_state(state, *, active_only=False):
    if (type(state) is not dict or state.get('state') not in ('pending', 'active')
            or type(state.get('enabled')) is not bool
            or state['state'] == 'pending' and state['enabled'] is not False
            or active_only and (state['state'] != 'active' or state['enabled'] is not True)
            or any(state.get(key) for key in ('disconnected', 'central_suspended', 'enrollment_hold', 'rotation_id'))):
        return False
    try:
        _uuid(state.get('claim_id'))
        if state.get('device_id'):
            _uuid(state['device_id'])
            _uuid(state.get('key_id'))
        elif state['state'] != 'pending' or state.get('key_id'):
            return False
    except ValueError:
        return False
    return True


def _registration_values(state, credential, *, active_only=False):
    _check(_registration_state(state, active_only=active_only) and type(credential) is dict, 'REGISTRATION_HELD')
    _check(type(credential.get('key')) is str and re.fullmatch(r'[0-9a-f]{64}', credential['key'])
           and not any(key in credential for key in ('next_key', 'next_key_id', 'rotation_id')), 'REGISTRATION_HELD')
    if state.get('device_id'):
        _check(credential.get('key_id') == state['key_id'], 'REGISTRATION_HELD')
    else:
        # Connector saves the assigned key ID before its device/state file. A
        # partial save must replay the same claim/key, never mint new identities.
        if credential.get('key_id') is not None:
            _uuid(credential['key_id'])


@_public
def guard_registration(store, state, credential):
    """Guard Connector's actual reads under its tick lock; consume first-call RAM permission once."""
    saved = _read(store, optional=True)
    if saved is None:
        return
    marker = saved['registration']
    _check(marker is not None and saved['profiles']['monitor'] is not None, 'REGISTRATION_HELD')
    _check(type(state) is dict and type(credential) is dict, 'REGISTRATION_HELD')
    permission = getattr(store, '_provision_registration_permit', None)
    store._provision_registration_permit = None
    if not state and not credential:
        _check(_permit(store, permission, saved, marker['registration_id']), 'REGISTRATION_HELD')
    else:
        _registration_values(state, credential)


@_public
def begin_registration(store, *, now_ms):
    """Fsync the start before customer/Connector creation; reopening is never fresh enrollment."""
    _time(now_ms)
    with store.lock():
        state = _read(store, optional=True)
        if state is None:
            return _summary(None)
        slot = state['profiles']['monitor']
        _check(slot is not None, 'MISSING')
        if state['registration'] is None:
            _check(_permit(store, getattr(store, '_provision_import_permit', None), state, slot['bundle_hash']), 'REGISTRATION_HELD')
            store._provision_import_permit = None
            _check(all(store._read_file(name) is None for name in _LEGACY), 'CONFLICT')
            state['registration'] = {'registration_id': str(uuid.uuid4()), 'started_at': now_ms}
            _write(store, state)
            store._provision_registration_permit = _Permit(store, state, state['registration']['registration_id'])
            return _summary(state)
    # Customer native reads remain outside the new short lock. Connector's hook
    # checks its own actual reads again; this component promises no native deadline.
    monitor = store._read_file('state.json')
    _check(_registration_state(monitor), 'REGISTRATION_HELD')
    guard_registration(store, monitor, store.read('credential'))
    return _summary(state)


def _current_identity(store):
    state = store._read_file('state.json')
    _check(_registration_state(state, active_only=True), 'REGISTRATION_HELD')
    credential = store.read('credential')
    _registration_values(state, credential, active_only=True)
    identity = {key: state[key] for key in ('device_id', 'claim_id', 'key_id')}
    _identity(identity)
    return identity, credential['key']


def _status_matches(slot, value):
    bundle = _bundle(slot)
    _check(all(value[key] == bundle[key] for key in ('provision_id', 'root_id', 'generation', 'expires_at'))
           and value['bundle_hash'] == slot['bundle_hash'], 'CONFLICT')
    if value['state'] == 'current':
        _check(value['current_generation'] == bundle['generation']
               and value['current_provision_id'] == bundle['provision_id']
               and value['realm_epoch'] == bundle['realm_epoch']
               and value['policy_revision'] == bundle['policy_revision'], 'CONFLICT')
    if value['receipt'] is not None:
        _receipt_matches(slot, value['receipt'])


def _binding(state, profile='monitor'):
    slot = state['profiles'][profile]
    return (state['root_id'], state['root_fingerprint'], state['registration']['registration_id'],
            profile, slot['raw'], slot['bundle_hash'], _record_hash(slot['receipt']),
            state['profiles']['monitor']['bundle_hash'])


@_public
def ack_once(store, *, profile, transport_factory, clock, action='ack', confirm_human=False, reason=None):
    """Send one admitted ACK or metadata status; no loop, discovery, automatic enable or trust adoption."""
    _check(profile in PROFILES, 'UNSUPPORTED')
    _check(action in ('ack', 'status'))
    if action == 'ack':
        _human(confirm_human, reason)
    now = int(clock() * 1000)
    _time(now)
    with store.lock():
        state = _read(store)
        slot = state['profiles'][profile]
        _check(slot is not None and state['registration'] is not None, 'REGISTRATION_HELD')
        if action == 'ack' and slot['ack'] is not None:
            return _summary(state, profile)
        binding = _binding(state, profile)
        bundle = _bundle(slot)
        if profile != 'monitor':
            current = store._read_file('state.json')
            _check(_registration_state(current, active_only=True)
                   and all(current.get(k) == bundle[k] for k in ('device_id', 'claim_id')), 'REGISTRATION_HELD')
        deadline = None
        if action == 'ack':
            _check(not slot['ack_denied'], 'CONFLICT')
            _check(len(slot['ack_attempts']) < 4, 'ATTEMPTS_EXHAUSTED')
            deadline = bundle['import_expires_at']
            _check(now < deadline, 'EXPIRED')
        record = _copy(slot['ack_request' if action == 'ack' else 'status_request'])
        # The fixed provision routes authenticate at ingest for every profile.
        # Target-profile origin/audience are only for its later support/restart calls.
        monitor_bundle = _bundle(state['profiles']['monitor'])
        config = {k: monitor_bundle[k] for k in ('origin', 'audience', 'environment')}
    path, raw = (ACK if action == 'ack' else STATUS), record['raw'].encode('ascii')
    from .protocol import sign_provision
    identity, key = _current_identity(store)
    client = None
    dispatched = False
    try:
        if profile != 'monitor':
            _check(all(identity[k] == bundle[k] for k in ('device_id', 'claim_id')), 'CONFLICT')
        # Signing and the fixed transport constructor perform no HTTP. Their
        # known failures must not consume the durable send-reservation budget.
        headers = sign_provision(config, path, raw, identity, key, now=clock(), nonce=secrets.token_hex(16))
        client = transport_factory(config)
        with store.lock():
            state = _read(store)
            slot = state['profiles'][profile]
            _check(binding == _binding(state, profile), 'CONFLICT')
            _check(record == slot['ack_request' if action == 'ack' else 'status_request'], 'CONFLICT')
            monitor = store._read_file('state.json')
            _check(_registration_state(monitor, active_only=True)
                   and all(monitor.get(k) == v for k, v in identity.items()), 'CONFLICT')
            now = int(clock() * 1000)
            _time(now)
            if action == 'ack':
                if slot['ack'] is not None:
                    return _summary(state, profile)
                _check(not slot['ack_denied'], 'CONFLICT')
                _check(len(slot['ack_attempts']) < 4, 'ATTEMPTS_EXHAUSTED')
                _check(now < deadline, 'EXPIRED')
                slot['ack_attempts'].append({'sent_at': now, 'reason': reason, 'identity': identity})
            slot['last_result'] = 'unknown'
            _write(store, state)
        if deadline is not None:
            _check(int(clock() * 1000) < deadline, 'EXPIRED')
        store.guard_credential()  # Before dispatch; a held change never rolls back the reservation.
        dispatched = True
        response = client.send(path, raw, headers)
    except BaseException as error:
        if not dispatched:
            raise  # A persisted reservation is not proof of transmission; never roll it back.
        # Clock skew (401) and rate limits (429) may clear within the original
        # ACK window; they never prove acceptance or replenish send attempts.
        denied = any(type(value) is int and 400 <= value <= 499 and value not in (401, 429) for value in
                     (getattr(error, 'status', None), getattr(client, 'refusal_status', None)))
        interrupted = isinstance(error, TimeoutError) or not isinstance(error, Exception)
        if interrupted and not denied:
            raise  # The already durable unknown remains; cancellation is not refusal evidence.
        try:
            with store.lock():
                state = _read(store)
                slot = state['profiles'][profile]
                _check(binding == _binding(state, profile), 'CONFLICT')
                if action == 'ack' and denied:
                    slot['ack_denied'] = True
                slot['last_result'] = ('acknowledged' if slot['ack'] is not None else
                                       'denied' if slot['ack_denied'] or denied else 'unknown')
                _write(store, state)
        except BaseException:
            if interrupted:
                store._provision_uncertain = True
                raise error from None
            raise
        if interrupted:
            raise
        return _summary(state, profile)
    finally:
        key = None
    checked = contract.validate_provision('AckResponse' if action == 'ack' else 'StatusResponse', response, now_ms=int(clock() * 1000))
    with store.lock():
        state = _read(store)
        slot = state['profiles'][profile]
        _check(binding == _binding(state, profile), 'CONFLICT')
        _check(record == slot['ack_request' if action == 'ack' else 'status_request'], 'CONFLICT')
        receipt = checked['receipt'] if action == 'ack' else checked['status']['receipt']
        if action == 'status':
            _status_matches(slot, checked['status'])
            if checked['status']['state'] != 'current':
                slot['ack_denied'] = True
        if receipt is not None:
            _receipt_matches(slot, receipt, identity)
            _check(slot['ack'] is None or slot['ack'] == receipt, 'CONFLICT')
            slot['ack'] = receipt
        if action == 'status':
            slot['last_status'] = checked
        slot['last_result'] = 'acknowledged' if slot['ack'] is not None else 'denied' if slot['ack_denied'] else 'unknown'
        _write(store, state)
        return _summary(state, profile)
