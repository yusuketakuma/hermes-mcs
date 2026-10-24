"""遠隔支援の明示同意・停止・失効証拠を単一の私有recordで保存する。"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid

from .support_contract import MAX_SAFE_INTEGER, canonical, parse_canonical, validate_preview

NAME = 'support-state.json'
SCHEMA = 'sdc.support.local-consent.v1'
MAX_TOMBSTONES = 128
MAX_BYTES = 65536
_BINDING = {'root_fingerprint', 'monitor_endpoint_hash', 'support_endpoint_hash', 'pins_sha256', 'secret_sha256', 'key_id'}
_FAMILY = ('tenant', 'installation', 'device_id', 'environment', 'case_id', 'realm_epoch')


class SupportStateError(ValueError):
    """Expose fixed local-state failure codes without source values."""
    def __init__(self, code='SUPPORT_STATE_INVALID'):
        allowed = {'SUPPORT_STATE_INVALID', 'SUPPORT_STATE_MISSING', 'SUPPORT_STATE_CONFLICT',
                   'SUPPORT_BINDING_UNAVAILABLE', 'SUPPORT_HUMAN_REQUIRED', 'SUPPORT_EPOCH_RETIRED',
                   'SUPPORT_TOMBSTONE_FULL', 'SUPPORT_CONSENT_EXPIRED'}
        super().__init__(code if code in allowed else 'SUPPORT_STATE_INVALID')


def _check(condition, code='SUPPORT_STATE_INVALID'):
    if not condition:
        raise SupportStateError(code)


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_SAFE_INTEGER


def _hex(value, length=64):
    return type(value) is str and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


def _uuid(value):
    try:
        parsed = uuid.UUID(value) if type(value) is str else None
    except ValueError:
        parsed = None
    return parsed is not None and parsed.version == 4 and str(parsed) == value


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False))


def _binding(value):
    _check(type(value) is dict and set(value) in (_BINDING, _BINDING | {'profile'}))
    _check(all(_hex(value[key]) for key in _BINDING - {'key_id'}))
    _check(_uuid(value['key_id']))
    if 'profile' in value:
        _profile(value['profile'], value['support_endpoint_hash'])
    return _copy(value)


def _profile(value, endpoint_hash):
    from .protocol import endpoint
    _check(type(value) is dict and set(value) == {'bundle_hash', 'expires_at', 'endpoint'})
    _check(_hex(value['bundle_hash']) and _integer(value['expires_at']))
    target = endpoint(value['endpoint'])
    _check(hashlib.sha256(canonical(target).encode('ascii')).hexdigest() == endpoint_hash)
    return _copy(value)


def _profile_expiry(binding):
    return binding.get('profile', {}).get('expires_at', MAX_SAFE_INTEGER)


def _current_binding(supplier):
    try:
        return _binding(supplier())
    except TimeoutError:
        raise
    except Exception:
        raise SupportStateError('SUPPORT_BINDING_UNAVAILABLE') from None


def _epoch(preview):
    family = hashlib.sha256(canonical({key: preview[key] for key in _FAMILY}).encode('ascii')).hexdigest()
    return {'scope_sha256': family, 'case_epoch': preview['case_epoch'],
            'local_consent_epoch': preview['local_consent_epoch']}


def _size(state):
    return len(json.dumps(state, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii'))


def _validated(state):
    try:
        fields = {'schema_version', 'root_fingerprint', 'owner_generation', 'enabled', 'receipt', 'tombstones'}
        _check(type(state) is dict and set(state) in (fields, fields | {'client'}, fields | {'runner'}, fields | {'client', 'runner'}))
        _check(state['schema_version'] == SCHEMA and _hex(state['root_fingerprint'])
               and _hex(state['owner_generation'], 32) and type(state['enabled']) is bool)
        rows = state['tombstones']
        _check(type(rows) is list and len(rows) <= MAX_TOMBSTONES)
        seen = set()
        for row in rows:
            _check(type(row) is dict and set(row) == {'scope_sha256', 'case_epoch', 'local_consent_epoch'})
            _check(_hex(row['scope_sha256']) and _integer(row['case_epoch'], 1) and _integer(row['local_consent_epoch'], 1))
            identity = row['scope_sha256'], row['case_epoch'], row['local_consent_epoch']
            _check(identity not in seen)
            seen.add(identity)
        receipt = state['receipt']
        if receipt is None:
            _check(state['enabled'] is False)
        else:
            _check(type(receipt) is dict and set(receipt) == {'local_receipt_id', 'accepted', 'accepted_at', 'preview', 'binding'})
            _check(_uuid(receipt['local_receipt_id']) and receipt['accepted'] is True and _integer(receipt['accepted_at']))
            binding = _binding(receipt['binding'])
            preview = validate_preview(receipt['preview'], now_ms=receipt['accepted_at'])
            _check(binding['root_fingerprint'] == state['root_fingerprint'] and preview['key_id'] == binding['key_id'])
            epoch = _epoch(preview)
            if state['enabled']:
                _check(epoch not in rows and len(rows) < MAX_TOMBSTONES)
                for row in rows:
                    if row['scope_sha256'] == epoch['scope_sha256']:
                        _check(epoch['case_epoch'] > row['case_epoch'] and epoch['local_consent_epoch'] > row['local_consent_epoch'])
            else:
                _check(epoch in rows)
        if 'client' in state:
            _validate_client(state['client'], state)
        if 'runner' in state:
            _validate_runner(state['runner'], state)
        _check(_size(state) <= MAX_BYTES)
        return _copy(state)
    except SupportStateError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise SupportStateError() from None


def _read(store):
    try:
        root_fingerprint = store.root_fingerprint()
        state = store.read(NAME)
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError):
        raise SupportStateError() from None
    _check(state is not None, 'SUPPORT_STATE_MISSING')
    state = _validated(state)
    _check(state['root_fingerprint'] == root_fingerprint, 'SUPPORT_STATE_CONFLICT')
    return state


def _snapshot(state, binding, now_ms):
    receipt = state['receipt']
    enabled = (state['enabled'] and receipt is not None and receipt['binding'] == binding
               and receipt['accepted_at'] <= now_ms < min(receipt['preview']['consent_expires_at'], _profile_expiry(binding)))
    return {'owner_generation': state['owner_generation'], 'binding': _copy(binding),
            'enabled': bool(enabled), 'receipt': _copy(receipt), 'tombstone_count': len(state['tombstones'])}


def prepare(store, *, binding_supplier, now_ms, initialize=False):
    """Read a CAS snapshot; explicit first provisioning alone may create disabled state.

    Never use initialize to repair a previously provisioned root whose file vanished:
    lost tombstones require a new central epoch and on-site approval. No local file
    can prove continuity after deletion or complete rollback of the same root.
    The borrowed Store is not closed. Acquire support-state.lock before tick.lock;
    both are held only for this local operation, never for a prompt or HTTP.
    """
    _check(_integer(now_ms) and type(initialize) is bool)
    with store.support_state_lock(), store.lock():
        if initialize:
            from .provision import initialization_allowed
            _check(initialization_allowed(store, 'support'), 'SUPPORT_STATE_CONFLICT')
        binding = _current_binding(binding_supplier)
        _check(binding['root_fingerprint'] == store.root_fingerprint(), 'SUPPORT_STATE_CONFLICT')
        try:
            state = _read(store)
        except SupportStateError as error:
            if not initialize or str(error) != 'SUPPORT_STATE_MISSING':
                raise
            state = {'schema_version': SCHEMA, 'root_fingerprint': binding['root_fingerprint'],
                     'owner_generation': secrets.token_hex(16), 'enabled': False, 'receipt': None, 'tombstones': []}
            store.write(NAME, _validated(state))
        _check(state['root_fingerprint'] == binding['root_fingerprint'], 'SUPPORT_STATE_CONFLICT')
        return _snapshot(state, binding, now_ms)


def confirm_consent(store, preview, *, expected_owner_generation, expected_binding,
                    binding_supplier, now_ms, confirm_human=False):
    """Save explicit consent after owner-generation and current-binding CAS.

    Caller verifies signature/central identity and obtains human approval outside the
    locks. Acquire support-state.lock before tick.lock. binding_supplier is called
    only after both locks are held and must supply the latest
    root fingerprint (via Store.enrollment_binding), monitor/support endpoint hashes,
    pins hash, actual Connector.secret full SHA256 and its key_id. Never return the
    secret itself. This component starts no HTTP, human prompt or Keychain operation;
    supplier failure saves nothing. Scope/epoch/current-authority checks by the remote
    runner remain separate from this local receipt and are not implied by its presence.
    local_receipt_id is stable for this consent; owner_generation is local CAS only,
    not the future operation runner's wire continuation generation.
    """
    _check(type(confirm_human) is bool and confirm_human is True, 'SUPPORT_HUMAN_REQUIRED')
    _check(_integer(now_ms) and _hex(expected_owner_generation, 32))
    with store.support_state_lock(), store.lock():
        state = _read(store)
        _check(state['owner_generation'] == expected_owner_generation, 'SUPPORT_STATE_CONFLICT')
        binding = _current_binding(binding_supplier)
        _check(binding == _binding(expected_binding) and binding['root_fingerprint'] == state['root_fingerprint'], 'SUPPORT_STATE_CONFLICT')
        _check(now_ms < _profile_expiry(binding), 'SUPPORT_CONSENT_EXPIRED')
        try:
            preview = validate_preview(preview, now_ms=now_ms)
        except (ValueError, TypeError):
            raise SupportStateError() from None
        _check(preview['key_id'] == binding['key_id'], 'SUPPORT_STATE_CONFLICT')
        epoch = _epoch(preview)
        previous = state['receipt']
        if previous is not None and _epoch(previous['preview']) == epoch:
            _check(state['enabled'], 'SUPPORT_EPOCH_RETIRED')
            _check(now_ms < previous['preview']['consent_expires_at'], 'SUPPORT_CONSENT_EXPIRED')
            _check(previous['binding'] == binding and previous['preview']['consent_binding_hash'] == preview['consent_binding_hash'], 'SUPPORT_STATE_CONFLICT')
            return _snapshot(state, binding, now_ms)
        _check(state.get('runner', {}).get('active') is None, 'SUPPORT_STATE_CONFLICT')
        history = list(state['tombstones'])
        if previous is not None and state['enabled']:
            history.append(_epoch(previous['preview']))
        for row in history:
            if row['scope_sha256'] == epoch['scope_sha256']:
                _check(epoch['case_epoch'] > row['case_epoch'] and epoch['local_consent_epoch'] > row['local_consent_epoch'], 'SUPPORT_EPOCH_RETIRED')
        _check(len(history) < MAX_TOMBSTONES, 'SUPPORT_TOMBSTONE_FULL')
        state.update(enabled=True, owner_generation=secrets.token_hex(16), tombstones=history,
                     receipt={'local_receipt_id': str(uuid.uuid4()), 'accepted': True, 'accepted_at': now_ms, 'preview': preview, 'binding': binding})
        if 'client' in state:
            state['client']['accept'] = None
        if 'runner' in state:
            state['runner']['poll'] = None
        state = _validated(state)
        # Reserve a complete disable record now; never evict revocation evidence later.
        disabled = {**state, 'enabled': False, 'tombstones': history + [epoch]}
        _check(_size(disabled) <= MAX_BYTES, 'SUPPORT_TOMBSTONE_FULL')
        _validated(disabled)
        store.write(NAME, state)
        return _snapshot(state, binding, now_ms)


def disable(store, *, now_ms):
    """Revoke locally using only support-state.lock, without monitoring I/O or keys."""
    _check(_integer(now_ms))
    with store.support_state_lock():
        state = _read(store)
        if state['receipt'] is not None and state['enabled']:
            state['tombstones'].append(_epoch(state['receipt']['preview']))
        state.update(enabled=False, owner_generation=secrets.token_hex(16))
        store.write(NAME, _validated(state))
        return {'enabled': False, 'owner_generation': state['owner_generation']}


_ACK = {'schema_version', 'accept_id', 'preview_id', 'case_id', 'local_receipt_id', 'local_consent_epoch',
        'consent_binding_hash', 'state', 'expires_at', 'request_id'}


def _ack(value, request, deadline):
    _check(type(value) is dict and set(value) == _ACK and value['schema_version'] == 'sdc.support.v1')
    _check(value['state'] == 'accepted' and _uuid(value['request_id']) and _uuid(value['case_id']) and _integer(value['local_consent_epoch'], 1))
    _check(_integer(value['expires_at'], 1) and value['expires_at'] <= deadline)
    _check(all(value[key] == request[key] for key in ('accept_id', 'preview_id', 'local_receipt_id', 'local_consent_epoch', 'consent_binding_hash')))
    return _copy(value)


def _raw_request(record, fields):
    _check(type(record['raw']) is str and len(record['raw']) <= 16384)
    body = parse_canonical(record['raw'], max_bytes=16384)
    _check(type(body) is dict and set(body) == fields and body['schema_version'] == 'sdc.support.v1')
    _check(hashlib.sha256(record['raw'].encode('ascii')).hexdigest() == record['body_hash'])
    _check(_hex(record['owner_generation'], 32) and _integer(record['started_at']) and _integer(record['attempts'], 1))
    _binding(record['binding'])
    return body


def _validate_client(client, state):
    _check(type(client) is dict and set(client) == {'preview', 'accept'})
    p, a = client['preview'], client['accept']
    if p is not None:
        _check(type(p) is dict and set(p) == {'request_id', 'case_id', 'raw', 'body_hash', 'binding', 'owner_generation', 'started_at', 'retry_until', 'attempts', 'payload', 'verified_at'})
        body = _raw_request(p, {'schema_version', 'preview_id', 'case_id'})
        _check(_uuid(p['request_id']) and _uuid(p['case_id']) and body['preview_id'] == p['request_id'] and body['case_id'] == p['case_id'])
        _check(_integer(p['retry_until']) and p['started_at'] < p['retry_until'] <= p['started_at'] + 300000)
        _check(p['binding']['root_fingerprint'] == state['root_fingerprint'])
        if p['payload'] is None:
            _check(p['verified_at'] is None)
        else:
            value = validate_preview(p['payload'], now_ms=p['verified_at'])
            _check(value['preview_id'] == p['request_id'] and value['case_id'] == p['case_id'] and value['key_id'] == p['binding']['key_id'] and p['retry_until'] <= value['expires_at'])
    if a is not None:
        _check(type(a) is dict and set(a) == {'request_id', 'raw', 'body_hash', 'binding', 'owner_generation', 'started_at', 'attempts', 'preview_deadline', 'deadline', 'ack'})
        body = _raw_request(a, {'schema_version', 'accept_id', 'preview_id', 'consent_binding_hash', 'local_consent_epoch', 'local_receipt_id'})
        receipt = state['receipt']
        _check(receipt is not None and _uuid(a['request_id']) and body['accept_id'] == a['request_id'])
        preview = receipt['preview']
        _check(_integer(body['local_consent_epoch'], 1))
        _check(body['preview_id'] == preview['preview_id'] and body['consent_binding_hash'] == preview['consent_binding_hash']
               and body['local_consent_epoch'] == preview['local_consent_epoch'] and body['local_receipt_id'] == receipt['local_receipt_id'])
        _check(a['binding'] == receipt['binding'] and a['preview_deadline'] == preview['expires_at'] and a['deadline'] == preview['consent_expires_at'])
        _check(receipt['accepted_at'] <= a['started_at'] < a['preview_deadline'])
        if a['ack'] is not None:
            ack = _ack(a['ack'], body, a['deadline'])
            _check(ack['case_id'] == preview['case_id'])


def _write_client(store, state):
    state = _validated(state)
    if state['enabled'] and state['receipt'] is not None:
        disabled = {**state, 'enabled': False, 'tombstones': state['tombstones'] + [_epoch(state['receipt']['preview'])]}
        _check(_size(disabled) <= MAX_BYTES, 'SUPPORT_TOMBSTONE_FULL')
        _validated(disabled)
    store.write(NAME, state)


def client_status(store, *, now_ms):
    """Read local receipt/ACK evidence only; never probe credentials or central authority."""
    _check(_integer(now_ms))
    with store.support_state_lock():
        state = _read(store)
        return {'owner_generation': state['owner_generation'], 'enabled': state['enabled'],
                'receipt': _copy(state['receipt']), 'client': _copy(state.get('client', {'preview': None, 'accept': None})),
                'runner': _copy(state.get('runner', {'poll': None, 'active': None, 'terminal': []}))}


def begin_preview(store, case_id, *, expected_owner_generation, binding_supplier, now_ms):
    """Persist one preview request admission before I/O; retry its exact bytes only."""
    _check(_uuid(case_id) and _integer(now_ms))
    with store.support_state_lock(), store.lock():
        state = _read(store)
        _check(state['owner_generation'] == expected_owner_generation, 'SUPPORT_STATE_CONFLICT')
        binding = _current_binding(binding_supplier)
        _check(binding['root_fingerprint'] == state['root_fingerprint'], 'SUPPORT_STATE_CONFLICT')
        client = state.setdefault('client', {'preview': None, 'accept': None})
        p = client['preview']
        if p is None or p['case_id'] != case_id:
            receipt = state['receipt']
            _check(not state['enabled'] or receipt is None or now_ms >= receipt['preview']['consent_expires_at'], 'SUPPORT_STATE_CONFLICT')
            identifier = str(uuid.uuid4())
            raw = canonical({'schema_version': 'sdc.support.v1', 'preview_id': identifier, 'case_id': case_id})
            p = {'request_id': identifier, 'case_id': case_id, 'raw': raw, 'body_hash': hashlib.sha256(raw.encode('ascii')).hexdigest(),
                 'binding': binding, 'owner_generation': state['owner_generation'], 'started_at': now_ms,
                 'retry_until': min(now_ms + 300000, _profile_expiry(binding)), 'attempts': 1, 'payload': None, 'verified_at': None}
            client['preview'] = p
        else:
            _check(p['owner_generation'] == state['owner_generation'] and p['binding'] == binding, 'SUPPORT_STATE_CONFLICT')
            _check(p['started_at'] <= now_ms < p['retry_until'], 'SUPPORT_CONSENT_EXPIRED')
            p['attempts'] += 1
        _write_client(store, state)
        return _copy(p)


def record_preview(store, request, preview, *, now_ms):
    """Save already verified preview metadata only while its local owner still matches."""
    with store.support_state_lock():
        state = _read(store)
        p = state.get('client', {}).get('preview')
        _check(p is not None and p['request_id'] == request['request_id'] and p['raw'] == request['raw']
               and state['owner_generation'] == request['owner_generation'], 'SUPPORT_STATE_CONFLICT')
        value = validate_preview(preview, now_ms=now_ms)
        _check(value['preview_id'] == p['request_id'] and value['case_id'] == p['case_id'], 'SUPPORT_STATE_CONFLICT')
        _check(p['payload'] is None or p['payload'] == value, 'SUPPORT_STATE_CONFLICT')
        p.update(payload=value, verified_at=now_ms, retry_until=min(p['retry_until'], value['expires_at']))
        _write_client(store, state)


def begin_accept(store, *, expected_owner_generation, binding_supplier, now_ms):
    """Persist receipt-bound accept UUID/body and start evidence before any send."""
    _check(_integer(now_ms))
    with store.support_state_lock(), store.lock():
        state = _read(store)
        _check(state['owner_generation'] == expected_owner_generation and state['enabled'], 'SUPPORT_STATE_CONFLICT')
        receipt = state['receipt']
        binding = _current_binding(binding_supplier)
        _check(receipt is not None and receipt['binding'] == binding, 'SUPPORT_STATE_CONFLICT')
        value = receipt['preview']
        _check(receipt['accepted_at'] <= now_ms < min(value['consent_expires_at'], _profile_expiry(binding)), 'SUPPORT_CONSENT_EXPIRED')
        client = state.setdefault('client', {'preview': None, 'accept': None})
        a = client['accept']
        if a is None:
            _check(now_ms < value['expires_at'], 'SUPPORT_CONSENT_EXPIRED')
            identifier = str(uuid.uuid4())
            raw = canonical({'schema_version': 'sdc.support.v1', 'accept_id': identifier, 'preview_id': value['preview_id'],
                             'consent_binding_hash': value['consent_binding_hash'], 'local_consent_epoch': value['local_consent_epoch'],
                             'local_receipt_id': receipt['local_receipt_id']})
            a = {'request_id': identifier, 'raw': raw, 'body_hash': hashlib.sha256(raw.encode('ascii')).hexdigest(),
                 'owner_generation': state['owner_generation'], 'binding': binding, 'started_at': now_ms, 'attempts': 1,
                 'preview_deadline': value['expires_at'], 'deadline': value['consent_expires_at'], 'ack': None}
            client['accept'] = a
        else:
            _check(a['owner_generation'] == state['owner_generation'] and a['binding'] == binding, 'SUPPORT_STATE_CONFLICT')
            _check(now_ms >= a['started_at'] and (a['ack'] is None or now_ms < a['ack']['expires_at']), 'SUPPORT_CONSENT_EXPIRED')
            a['attempts'] += 1
        _write_client(store, state)
        return _copy(a)


def record_accept_ack(store, request, response):
    """Keep matching ACK evidence even after stop; never re-enable or clear tombstones."""
    with store.support_state_lock():
        state = _read(store)
        a = state.get('client', {}).get('accept')
        _check(a is not None and a['request_id'] == request['request_id'] and a['raw'] == request['raw'], 'SUPPORT_STATE_CONFLICT')
        body = parse_canonical(a['raw'])
        ack = _ack(response, body, a['deadline'])
        _check(ack['case_id'] == state['receipt']['preview']['case_id'], 'SUPPORT_STATE_CONFLICT')
        if a['ack'] is not None:
            _check({k: v for k, v in a['ack'].items() if k != 'request_id'} == {k: v for k, v in ack.items() if k != 'request_id'}, 'SUPPORT_STATE_CONFLICT')
        else:
            a['ack'] = ack
        _write_client(store, state)
        return {'enabled': state['enabled'], 'expires_at': a['ack']['expires_at']}


MAX_OPERATION_RECEIPTS = 32
# Store JSON escapes every quote/backslash in the ASCII report string. Reserving
# twice the wire limit plus fixed bookkeeping also leaves the disable tombstone.
REPORT_STORAGE_RESERVE = 2 * 16384 + 4096
_RUNNER_PHASES = ('ADMITTING', 'GRANTING', 'STARTED', 'RESULT', 'REPORTING', 'ACKED')


def _wire(path, body, now_ms):
    from .support_control import validate_request
    body = validate_request(path, body)
    raw = canonical(body, max_bytes=16384)
    return {'path': path, 'raw': raw, 'body_hash': hashlib.sha256(raw.encode('ascii')).hexdigest(), 'started_at': now_ms}


def _wire_body(record, path):
    from .support_control import validate_request
    _check(type(record) is dict and set(record) == {'path', 'raw', 'body_hash', 'started_at'}
           and record['path'] == path and _integer(record['started_at']))
    body = validate_request(path, record['raw'])
    _check(hashlib.sha256(record['raw'].encode('ascii')).hexdigest() == record['body_hash'])
    return body


def _validate_runner(runner, state):
    from .support_control import POLL, ADMIT, GRANT, REPORT, STATUS
    from .support_authorization import match_operation
    _check(type(runner) is dict and set(runner) == {'poll', 'active', 'terminal'})
    terminal = runner['terminal']
    _check(type(terminal) is list and len(terminal) <= MAX_OPERATION_RECEIPTS)
    seen = set()
    for row in terminal:
        _check(type(row) is dict)
        if row.get('kind') == 'pregrant_terminal':
            _check(set(row) == {'kind', 'operation_id', 'payload_hash', 'receipt_id', 'admission_id', 'admit_body_hash',
                                'state', 'state_revision', 'not_started', 'status_id', 'status_body_hash', 'ack_request_id'})
            _check(all(_uuid(row[k]) for k in ('operation_id', 'receipt_id', 'admission_id', 'status_id', 'ack_request_id'))
                   and all(_hex(row[k]) for k in ('payload_hash', 'admit_body_hash', 'status_body_hash'))
                   and row['state'] in ('canceled', 'expired') and type(row['state_revision']) is int and row['state_revision'] == 0
                   and row['not_started'] is True)
        else:
            _check(set(row) == {'operation_id', 'payload_hash', 'receipt_id', 'report_id', 'body_hash', 'report_state', 'ack_request_id', 'ack_source'})
            _check(all(_uuid(row[k]) for k in ('operation_id', 'receipt_id', 'report_id', 'ack_request_id'))
                   and _hex(row['payload_hash']) and _hex(row['body_hash']) and row['report_state'] in ('succeeded', 'failed', 'unknown')
                   and row['ack_source'] in ('report', 'status'))
        _check(row['operation_id'] not in seen)
        seen.add(row['operation_id'])
    if runner['poll'] is not None:
        _wire_body(runner['poll'], POLL)
    active = runner['active']
    if active is None:
        return
    _check(type(active) is dict and set(active) == {'operation', 'approval', 'verified_at', 'consent_owner', 'binding',
                                                 'alive_owner', 'phase', 'admit', 'grant', 'report', 'status'})
    _check(active['phase'] in _RUNNER_PHASES and _integer(active['verified_at'])
           and _hex(active['consent_owner'], 32) and _hex(active['alive_owner'], 32))
    _binding(active['binding'])
    receipt = state['receipt']
    _check(receipt is not None and active['binding'] == receipt['binding'])
    op = match_operation(active['operation'], active['approval'], consent_preview=receipt['preview'], now_ms=active['verified_at'])
    _check(op['operation_id'] not in seen)
    admit = _wire_body(active['admit'], ADMIT)
    _check(admit['operation_id'] == op['operation_id'] and admit['payload_hash'] == op['payload_hash']
           and admit['owner_generation'] == active['alive_owner'] and admit['local_consent_epoch'] == op['local_consent_epoch'])
    for field, path in (('grant', GRANT), ('report', REPORT), ('status', STATUS)):
        if active[field] is not None:
            body = _wire_body(active[field], path)
            _check(body['operation_id'] == op['operation_id'] and body['payload_hash'] == op['payload_hash']
                   and body['receipt_id'] == admit['receipt_id'])
            if field == 'grant':
                _check(body['admission_id'] == admit['admission_id'])
            elif field == 'report':
                _check(body['state_revision'] == 1 and body['state'] in ('succeeded', 'failed', 'unknown'))
                if body['result'] is not None:
                    _check(body['result']['diagnostic_id'] == op['params']['diagnostic_id'])
            elif active['report'] is not None:
                _check(body['report_id'] == parse_canonical(active['report']['raw'], max_bytes=16384)['report_id'])
    _check(active['phase'] == 'ADMITTING' or active['grant'] is not None)
    _check(active['phase'] not in ('RESULT', 'REPORTING', 'ACKED') or active['report'] is not None)


def _runner_access(store, expected_owner_generation, binding_supplier, now_ms, *, live=True):
    """Called only under support-state.lock then tick.lock; no I/O lifetime implied."""
    state = _read(store)
    receipt = state['receipt']
    _check(state['owner_generation'] == expected_owner_generation and state['enabled'] and receipt is not None, 'SUPPORT_STATE_CONFLICT')
    binding = _current_binding(binding_supplier)
    _check(binding == receipt['binding'], 'SUPPORT_STATE_CONFLICT')
    accept = state.get('client', {}).get('accept')
    _check(accept is not None and accept['ack'] is not None, 'SUPPORT_STATE_CONFLICT')
    if live:
        _check(receipt['accepted_at'] <= now_ms < min(receipt['preview']['consent_expires_at'], accept['ack']['expires_at'],
                                                    _profile_expiry(binding)), 'SUPPORT_CONSENT_EXPIRED')
    state.setdefault('runner', {'poll': None, 'active': None, 'terminal': []})
    return state


def _planning_parent(store, *, binding_supplier, now_ms):
    """Snapshot current parent consent under support -> restart -> tick locks owned by caller.

    Original R4a pins/binding are required. This metadata identifies a case only;
    it gives no planning or change permission and writes no runner state.
    """
    from .restart_contract import EXPECTED_FIELDS
    state = _read(store)
    state = _runner_access(store, state['owner_generation'], binding_supplier, now_ms)
    receipt, ack = state['receipt'], state['client']['accept']['ack']
    return {'owner_generation': state['owner_generation'], 'local_receipt_id': receipt['local_receipt_id'],
            'accepted_at': receipt['accepted_at'], 'expires_at': min(receipt['preview']['consent_expires_at'], ack['expires_at'],
                                                                    _profile_expiry(receipt['binding'])),
            'binding': receipt['binding'], 'expected': {k: receipt['preview'][k] for k in EXPECTED_FIELDS if k not in ('params', 'service_revision')},
            'acceptance_hash': hashlib.sha256(canonical({k: v for k, v in ack.items() if k != 'request_id'}).encode('ascii')).hexdigest()}


def runner_poll(store, *, expected_owner_generation, binding_supplier, now_ms):
    """Persist one fixed poll request; accepted consent/current identity remain mandatory."""
    from .support_control import POLL
    with store.support_state_lock(), store.lock():
        state = _runner_access(store, expected_owner_generation, binding_supplier, now_ms)
        runner = state['runner']
        _check(runner['active'] is None and len(runner['terminal']) < MAX_OPERATION_RECEIPTS, 'SUPPORT_STATE_CONFLICT')
        if runner['poll'] is None:
            preview = state['receipt']['preview']
            runner['poll'] = _wire(POLL, {'schema_version': 'sdc.support.v1', 'poll_id': str(uuid.uuid4()),
                                         'case_id': preview['case_id'], 'capability_revision': preview['capability_revision'],
                                         'local_consent_epoch': preview['local_consent_epoch']}, now_ms)
        else:
            body = _wire_body(runner['poll'], POLL)
            _check(body['case_id'] == state['receipt']['preview']['case_id']
                   and body['local_consent_epoch'] == state['receipt']['preview']['local_consent_epoch'], 'SUPPORT_STATE_CONFLICT')
        _write_client(store, state)
        return _copy(runner['poll'])


def runner_empty(store, *, expected_owner_generation, binding_supplier, now_ms):
    """Finish an observed empty poll without inventing operation success."""
    with store.support_state_lock(), store.lock():
        state = _runner_access(store, expected_owner_generation, binding_supplier, now_ms)
        _check(state['runner']['active'] is None, 'SUPPORT_STATE_CONFLICT')
        state['runner']['poll'] = None
        _write_client(store, state)


def runner_admit(store, operation, approval, *, alive_owner, expected_owner_generation, binding_supplier, now_ms):
    """Reserve a single operation and durable admission IDs before any admit HTTP."""
    from .support_control import ADMIT
    from .support_authorization import match_operation
    with store.support_state_lock(), store.lock():
        state = _runner_access(store, expected_owner_generation, binding_supplier, now_ms)
        runner = state['runner']
        _check(runner['active'] is None and len(runner['terminal']) < MAX_OPERATION_RECEIPTS, 'SUPPORT_STATE_CONFLICT')
        operation = match_operation(operation, approval, consent_preview=state['receipt']['preview'], now_ms=now_ms)
        _check(not any(row['operation_id'] == operation['operation_id'] for row in runner['terminal']), 'SUPPORT_STATE_CONFLICT')
        body = {'schema_version': 'sdc.support.v1', 'operation_id': operation['operation_id'], 'payload_hash': operation['payload_hash'],
                'admission_id': str(uuid.uuid4()), 'receipt_id': str(uuid.uuid4()),
                'local_consent_epoch': operation['local_consent_epoch'], 'owner_generation': alive_owner}
        runner['active'] = {'operation': operation, 'approval': _copy(approval), 'verified_at': now_ms,
                            'consent_owner': state['owner_generation'], 'binding': _copy(state['receipt']['binding']),
                            'alive_owner': alive_owner, 'phase': 'ADMITTING', 'admit': _wire(ADMIT, body, now_ms),
                            'grant': None, 'report': None, 'status': None}
        runner['poll'] = None
        _check(_size(state) + REPORT_STORAGE_RESERVE + 1024 <= MAX_BYTES, 'SUPPORT_TOMBSTONE_FULL')
        _write_client(store, state)
        return _copy(runner['active'])


def _active_owner(state, alive_owner, phase):
    active = state['runner']['active']
    _check(active is not None and active['alive_owner'] == alive_owner and active['phase'] in phase
           and active['consent_owner'] == state['owner_generation'], 'SUPPORT_STATE_CONFLICT')
    return active


def runner_grant(store, *, alive_owner, expected_owner_generation, binding_supplier, now_ms):
    """Persist the first grant ID only for this live admission continuation."""
    from .support_control import GRANT
    with store.support_state_lock(), store.lock():
        state = _runner_access(store, expected_owner_generation, binding_supplier, now_ms)
        active = _active_owner(state, alive_owner, ('ADMITTING',))
        admit = parse_canonical(active['admit']['raw'])
        body = {key: admit[key] for key in ('schema_version', 'operation_id', 'payload_hash', 'receipt_id', 'admission_id')}
        body['grant_request_id'] = str(uuid.uuid4())
        active.update(phase='GRANTING', grant=_wire(GRANT, body, now_ms))
        _write_client(store, state)
        return _copy(active)


def runner_started(store, *, alive_owner, expected_owner_generation, binding_supplier, now_ms, check_deadline):
    """Save STARTED once; no process restart may use this token to execute again."""
    with store.support_state_lock(), store.lock():
        state = _runner_access(store, expected_owner_generation, binding_supplier, now_ms)
        active = _active_owner(state, alive_owner, ('GRANTING',))
        check_deadline()
        _check(_size(state) + REPORT_STORAGE_RESERVE + 1024 <= MAX_BYTES, 'SUPPORT_TOMBSTONE_FULL')
        active['phase'] = 'STARTED'
        _write_client(store, state)
        check_deadline()  # fsync consumes the same budget; an elapsed STARTED never retries.


def runner_result(store, report, *, alive_owner):
    """Save exact result/report bytes after provider, even when stop won meanwhile."""
    from .support_control import REPORT
    with store.support_state_lock():
        state = _read(store)
        active = state.get('runner', {}).get('active')
        _check(active is not None and active['alive_owner'] == alive_owner and active['phase'] == 'STARTED', 'SUPPORT_STATE_CONFLICT')
        active.update(phase='RESULT', report=_wire(REPORT, report, report['observed_at']))
        _write_client(store, state)
        return _copy(active)


def runner_report(store, *, alive_owner, expected_owner_generation, binding_supplier, now_ms):
    """Admit one content report; its started bytes are never blindly retried."""
    with store.support_state_lock(), store.lock():
        state = _runner_access(store, expected_owner_generation, binding_supplier, now_ms)
        active = _active_owner(state, alive_owner, ('RESULT',))
        active['phase'] = 'REPORTING'
        _write_client(store, state)
        return _copy(active['report'])


def runner_status(store, *, expected_owner_generation, binding_supplier, now_ms):
    """Admit metadata-only reconciliation, including expiry; local stop still forbids it."""
    from .support_control import STATUS
    with store.support_state_lock(), store.lock():
        state = _runner_access(store, expected_owner_generation, binding_supplier, now_ms, live=False)
        active = state['runner']['active']
        _check(active is not None and active['phase'] in ('ADMITTING', 'GRANTING', 'STARTED', 'RESULT', 'REPORTING'), 'SUPPORT_STATE_CONFLICT')
        # Even a crash before result assembly needs a stable, non-content report ID.
        # Do not invent a result or report body to fit a status response.
        if active['status'] is None:
            admit = parse_canonical(active['admit']['raw'])
            body = {key: admit[key] for key in ('schema_version', 'operation_id', 'payload_hash', 'receipt_id')}
            body.update(status_id=str(uuid.uuid4()), report_id=parse_canonical(active['report']['raw'], max_bytes=16384)['report_id'] if active['report'] else str(uuid.uuid4()))
            active['status'] = _wire(STATUS, body, now_ms)
        _write_client(store, state)
        return _copy(active['status'])


def runner_ack(store, request, *, state_code, ack_request_id, ack_source):
    """Archive only exact report acceptance evidence; never infer diagnostic health."""
    with store.support_state_lock():
        state = _read(store)
        runner = state.get('runner', {})
        active = runner.get('active')
        _check(active is not None and active['report'] is not None and active['report'] == request, 'SUPPORT_STATE_CONFLICT')
        body = parse_canonical(request['raw'], max_bytes=16384)
        _check(state_code == body['state'] and state_code in ('succeeded', 'failed', 'unknown')
               and _uuid(ack_request_id) and ack_source in ('report', 'status'))
        runner['terminal'].append({'operation_id': body['operation_id'], 'payload_hash': body['payload_hash'],
                                   'receipt_id': body['receipt_id'], 'report_id': body['report_id'],
                                   'body_hash': request['body_hash'], 'report_state': state_code,
                                   'ack_request_id': ack_request_id, 'ack_source': ack_source})
        runner['active'] = None
        _write_client(store, state)


def runner_execution(store, *, alive_owner, expected_owner_generation, binding_supplier, now_ms, check_deadline):
    """Recheck STARTED owner/binding immediately before the one provider admission."""
    with store.support_state_lock(), store.lock():
        state = _runner_access(store, expected_owner_generation, binding_supplier, now_ms)
        _active_owner(state, alive_owner, ('STARTED',))
        check_deadline()


def runner_pregrant_terminal(store, request, response, *, expected_owner_generation, binding_supplier, now_ms):
    """Release only durable never-granted admission plus matching central revision-zero terminal evidence."""
    from .support_control import STATUS, validate_response
    with store.support_state_lock(), store.lock():
        state = _runner_access(store, expected_owner_generation, binding_supplier, now_ms, live=False)
        runner = state['runner']
        active = runner['active']
        _check(active is not None and active['phase'] == 'ADMITTING' and active['grant'] is None and active['report'] is None
               and active['consent_owner'] == state['owner_generation'] and active['status'] == request, 'SUPPORT_STATE_CONFLICT')
        body = _wire_body(active['status'], STATUS)
        response = validate_response(STATUS, response, request=body)
        operation = response['operation']
        _check(response['report'] is None and operation['state'] in ('canceled', 'expired')
               and type(operation['state_revision']) is int and operation['state_revision'] == 0, 'SUPPORT_STATE_CONFLICT')
        admit = parse_canonical(active['admit']['raw'])
        runner['terminal'].append({'kind': 'pregrant_terminal', 'operation_id': admit['operation_id'],
                                   'payload_hash': admit['payload_hash'], 'receipt_id': admit['receipt_id'],
                                   'admission_id': admit['admission_id'], 'admit_body_hash': active['admit']['body_hash'],
                                   'state': operation['state'], 'state_revision': 0, 'not_started': True,
                                   'status_id': body['status_id'], 'status_body_hash': request['body_hash'],
                                   'ack_request_id': response['request_id']})
        runner['active'] = None
        _write_client(store, state)
