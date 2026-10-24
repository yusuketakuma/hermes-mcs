"""親案件に束縛した一件の再起動準備・実owner観測・固定要求を保存する。"""
from contextlib import contextmanager
import hashlib
import json
import secrets
import uuid

from . import restart_contract as contract
from . import restart_control as control
from . import restart_state as change
from . import support_state as support
from . import support_client
from . import provision
from .protocol import endpoint
from .storage import RESTART_PLANNING_MAX_BYTES
from .support_contract import canonical

NAME = 'restart-planning.json'
SCHEMA = 'sdc.support.restart-planning.v1'
EXPECTED = tuple(k for k in contract.EXPECTED_FIELDS if k not in ('params', 'service_revision'))
PHASES = ('sample', 'refresh', 'confirm')


class RestartPlanningError(ValueError):
    def __init__(self, code='INVALID'):
        allowed = {'INVALID', 'MISSING', 'CONFLICT', 'EXPIRED', 'UNCERTAIN', 'HUMAN_REQUIRED'}
        super().__init__('SUPPORT_RESTART_PLANNING_' + (code if code in allowed else 'INVALID'))


def _check(ok, code='INVALID'):
    if not ok:
        raise RestartPlanningError(code)


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False))


def _hash(value):
    return hashlib.sha256(canonical(value, max_bytes=16384).encode('ascii')).hexdigest()


def _wire(path, body, now_ms):
    body = control.validate_request(path, body, now_ms=now_ms)
    return {'raw': canonical(body, max_bytes=16384), 'body_hash': _hash(body), 'created_at': now_ms}


def _body(path, wire):
    _check(type(wire) is dict and set(wire) == {'raw', 'body_hash', 'created_at'} and change._integer(wire['created_at']))
    body = control.validate_request(path, wire['raw'], now_ms=wire['created_at'])
    _check(_hash(body) == wire['body_hash'])
    return body


def _registration(value, parent, now_ms, original=None):
    reg = contract.validate_restart_registration(value, now_ms=now_ms)
    _check(reg['state'] == 'active' and reg['product'] == 'hermes-mcs')
    _check(all(reg[k] == parent['expected'][k] for k in ('tenant', 'installation', 'device_id', 'environment',
                                                       'realm_epoch', 'policy_epoch', 'device_epoch')), 'CONFLICT')
    if original is not None:
        _check(all(reg[k] == original[k] for k in reg if k not in ('not_before', 'expires_at')), 'CONFLICT')
    return reg


def _expected(plan):
    _check(plan['registration'] is not None and plan['generation'] is not None, 'CONFLICT')
    return {**_target_expected(plan), 'service_revision': plan['registration']['value']['service_revision'],
            'params': {'service_id': 'mcs.standalone', 'effects_profile': 'mcs.standalone.restart.v1',
                       'expected_runtime_generation': plan['generation']}}


def _target_expected(plan):
    expected = _copy(plan['parent']['expected'])
    if 'profile' in plan['binding']:
        expected['audience'] = plan['binding']['profile']['endpoint']['audience']
    return expected


def _preflight(plan, value, now_ms):
    generation = plan['generation'] or value.get('runtime_generation')
    return contract._preflight(value, now_ms, expected=_target_expected(plan),
                               params={'expected_runtime_generation': generation})


def _validated(state):
    try:
        _check(type(state) is dict and set(state) == {'schema_version', 'root_fingerprint', 'owner_generation', 'disabled', 'plan'})
        _check(state['schema_version'] == SCHEMA and change._hex(state['root_fingerprint'])
               and change._hex(state['owner_generation'], 32) and type(state['disabled']) is bool)
        p = state['plan']
        if p is not None:
            _check(type(p) is dict and set(p) == {'parent', 'planning_owner', 'restart_owner', 'binding', 'planning_consent_id', 'created_at', 'expires_at',
                   'read', 'registration', 'samples', 'generation', 'inspection_consent', 'inspection', 'inspection_ack', 'inspection_ack_at',
                   'preview', 'verified_preview', 'verified_at'})
            parent = p['parent']
            _check(type(parent) is dict and set(parent) == {'owner_generation', 'local_receipt_id', 'accepted_at', 'expires_at',
                                                         'binding', 'expected', 'acceptance_hash'})
            support._binding(parent['binding'])
            canonical(parent['expected'])
            _check(change._hex(parent['owner_generation'], 32) and support._uuid(parent['local_receipt_id'])
                   and change._hex(parent['acceptance_hash']) and set(parent['expected']) == set(EXPECTED))
            _check(all(support._uuid(parent['expected'][k]) for k in ('case_id', 'claim_id', 'device_id', 'key_id'))
                   and all(support._integer(parent['expected'][k], 1) for k in ('policy_epoch', 'device_epoch', 'case_epoch', 'local_consent_epoch')))
            _check(change._integer(parent['accepted_at']) and change._integer(parent['expires_at'])
                   and parent['accepted_at'] < parent['expires_at'] and change._hex(p['restart_owner'], 32) and change._hex(p['planning_owner'], 32)
                   and support._uuid(p['planning_consent_id']))
            _check(state['disabled'] or state['owner_generation'] == p['planning_owner'], 'CONFLICT')
            binding = change._binding(p['binding'])
            _check(binding['root_fingerprint'] == parent['binding']['root_fingerprint'] == state['root_fingerprint'])
            _check(('profile' in binding) == ('profile' in parent['binding']))
            _check(change._integer(p['created_at']) and change._integer(p['expires_at'])
                   and parent['accepted_at'] <= p['created_at'] < p['expires_at'] <= min(parent['expires_at'], support._profile_expiry(binding)))
            read = p['read']
            _check(type(read) is dict and set(read) == {'request', 'attempts', 'blocked'} and type(read['attempts']) is int
                   and 0 <= read['attempts'] <= 4 and type(read['blocked']) is bool)
            body = _body(control.REGISTRATION_READ, read['request'])
            _check(body['case_id'] == parent['expected']['case_id'] and body['local_receipt_id'] == parent['local_receipt_id'])
            reg = p['registration']
            if reg is not None:
                _check(type(reg) is dict and set(reg) == {'value', 'request_id', 'fetched_at'} and support._uuid(reg['request_id'])
                       and change._integer(reg['fetched_at']) and read['attempts'] > 0)
                _registration(reg['value'], parent, reg['fetched_at'])
                _check(p['expires_at'] <= reg['value']['expires_at'])
            _check(p['generation'] is None or change._hex(p['generation'], 32))
            _check(type(p['samples']) is dict and set(p['samples']) == set(PHASES))
            pending = 0
            for phase, sample in p['samples'].items():
                if sample is None:
                    continue
                _check(reg is not None and type(sample) is dict and set(sample) == {'request_id', 'requested_at', 'expires_at', 'preflight'}
                       and support._uuid(sample['request_id']) and change._integer(sample['requested_at'])
                       and change._integer(sample['expires_at']) and sample['requested_at'] < sample['expires_at'] <= parent['expires_at']
                       and sample['expires_at'] <= sample['requested_at'] + 300000)
                preflight = sample['preflight']
                if preflight is None:
                    pending += 1
                else:
                    _check(preflight['observed_at'] >= sample['requested_at'])
                    _preflight(p, preflight, preflight['observed_at'])
                if phase != 'sample':
                    _check(p['generation'] is not None and p['samples']['sample'] is not None
                           and p['samples']['sample']['preflight'] is not None)
            _check(pending <= 1 and (p['generation'] is None) == (p['samples']['sample'] is None or p['samples']['sample']['preflight'] is None))
            consent = p['inspection_consent']
            if consent is not None:
                _check(type(consent) is dict and set(consent) == {'consent_id', 'inspection_id', 'confirmed_at'}
                       and support._uuid(consent['consent_id']) and support._uuid(consent['inspection_id'])
                       and change._integer(consent['confirmed_at']) and p['generation'] is not None)
            if p['inspection'] is not None:
                _check(consent is not None)
                body = _body(control.INSPECTION, p['inspection'])
                _check(body['inspection_id'] == consent['inspection_id'] and body['local_planning_consent_id'] == consent['consent_id']
                       and body['case_id'] == parent['expected']['case_id'] and body['service_revision'] == reg['value']['service_revision'])
                _preflight(p, body['preflight'], p['inspection']['created_at'])
                if p['inspection_ack'] is not None:
                    _check(change._integer(p['inspection_ack_at']) and p['inspection_ack_at'] >= p['inspection']['created_at'])
                    control.validate_response(control.INSPECTION, p['inspection_ack'], request=body, now_ms=p['inspection_ack_at'])
            else:
                _check(p['inspection_ack'] is None)
            _check((p['inspection_ack'] is None) == (p['inspection_ack_at'] is None))
            if p['preview'] is not None:
                _check(p['inspection_ack'] is not None)
                body = _body(control.PREVIEW, p['preview'])
                if p['verified_preview'] is not None:
                    preview = contract.validate_typed_payload('restart-consent-preview', p['verified_preview'], now_ms=p['verified_at'])
                    _check(all(preview[k] == v for k, v in _expected(p).items()))
                    _check(preview['preview_id'] == body['preview_id'] and preview['operation_id'] == body['operation_id']
                           and preview['intent']['inspection_id'] == consent['inspection_id']
                           and preview['intent']['inspection_hash'] == p['inspection']['body_hash'])
                else:
                    _check(p['verified_at'] is None)
            else:
                _check(p['verified_preview'] is None and p['verified_at'] is None)
        _check(len(json.dumps(state, ensure_ascii=True, separators=(',', ':')).encode('ascii')) <= RESTART_PLANNING_MAX_BYTES)
        return _copy(state)
    except RestartPlanningError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, AttributeError):
        raise RestartPlanningError() from None


def _read(store):
    try:
        root = store.root_fingerprint()
        state = store.read(NAME)
        _check(state is not None, 'MISSING')
        state = _validated(state)
        _check(state['root_fingerprint'] == root, 'CONFLICT')
        return state
    except (RestartPlanningError, TimeoutError):
        raise
    except (OSError, ValueError, TypeError):
        raise RestartPlanningError() from None


def _certain(store):
    _check(not getattr(store, '_restart_planning_uncertain', False), 'UNCERTAIN')
    change._certain(store)


def _write(store, state):
    _certain(store)
    state = _validated(state)
    try:
        _check(store.root_fingerprint() == state['root_fingerprint'], 'CONFLICT')
        store.write(NAME, state)
        _check(store.root_fingerprint() == state['root_fingerprint'], 'CONFLICT')
    except BaseException as error:
        store._restart_planning_uncertain = True
        if isinstance(error, TimeoutError) or not isinstance(error, Exception):
            raise
        raise RestartPlanningError('UNCERTAIN') from None


@contextmanager
def _locks(store):
    with store.support_state_lock(), store.restart_state_lock(), store.lock():
        yield


def _local_profile(store, binding, profile, expected, now_ms):
    """Recheck stored public Bundle metadata, without injected providers or signature verification."""
    snapshot = provision.profile_snapshot(store, profile=profile, now_ms=now_ms)
    bundle = snapshot['bundle']
    _check(binding['profile'] == support_client._profile_binding(snapshot, now_ms=now_ms)
           and binding['root_fingerprint'] == snapshot['root_fingerprint']
           and binding['monitor_endpoint_hash'] == snapshot['monitor_endpoint_hash']
           and binding['pins_sha256'] == _hash(bundle['pins'])
           and all(bundle[k] == expected[k] for k in support_client._PROFILE_SCOPE), 'CONFLICT')


def _local_frame(store, now_ms):
    """Metadata only under three locks; no keys, injected providers, HTTP or signature verification."""
    _check(change._integer(now_ms))
    _certain(store)
    state, restart = _read(store), change._read(store)
    _check(not state['disabled'] and not restart['disabled'] and store.read('restart-execution.json') is None, 'CONFLICT')
    parent_state = support._read(store)
    accept = parent_state.get('client', {}).get('accept')
    _check(parent_state['enabled'] and parent_state['receipt'] is not None and accept is not None and accept['ack'] is not None, 'CONFLICT')
    parent = support._planning_parent(store, binding_supplier=lambda: parent_state['receipt']['binding'], now_ms=now_ms)
    monitor = store._read_file('state.json')
    target = endpoint(store.read('endpoint.json'))
    _check(type(monitor) is dict and monitor.get('state') == 'active' and monitor.get('enabled') is True
           and not any(monitor.get(k) for k in ('disconnected', 'central_suspended', 'enrollment_hold'))
           and 'rotation_id' not in monitor
           and all(monitor.get(k) == parent['expected'][k] for k in ('claim_id', 'device_id', 'key_id'))
           and store._fingerprint(target) == parent['binding']['monitor_endpoint_hash']
           and target['environment'] == parent['expected']['environment'], 'CONFLICT')
    if 'profile' in parent['binding']:
        _local_profile(store, parent['binding'], 'support', parent['expected'], now_ms)
    else:
        _check(target['audience'] == parent['expected']['audience'], 'CONFLICT')
    p = state['plan']
    if p is None:
        _check(restart['receipt'] is None, 'CONFLICT')
    else:
        _check(parent == p['parent'], 'CONFLICT')
        _check(p['created_at'] <= now_ms < p['expires_at'], 'EXPIRED')
        if 'profile' in p['binding']:
            _local_profile(store, p['binding'], 'restart', _target_expected(p), now_ms)
        if p['registration'] is not None:
            _registration(p['registration']['value'], parent, now_ms)
        if restart['receipt'] is None:
            _check(restart['owner_generation'] == p['restart_owner'], 'CONFLICT')
        else:
            _check(p['verified_preview'] is not None and restart['preview'] == p['verified_preview']
                   and restart['expected'] == _expected(p) and restart['binding'] == p['binding'], 'CONFLICT')
    return {'state': state, 'parent': parent, 'binding': p['binding'] if p else None, 'restart': restart}


def _access(store, supplier, now_ms):
    """Add actual current credentials/pins to the local frame for an authoritative boundary."""
    frame = _local_frame(store, now_ms)
    bindings = supplier()
    _check(type(bindings) is dict and set(bindings) == {'parent_binding', 'binding'})
    parent = frame['parent']
    binding = change._binding(bindings['binding'])
    _check(('profile' in binding) == ('profile' in parent['binding']) and now_ms < support._profile_expiry(binding), 'CONFLICT')
    _check(bindings['parent_binding'] == parent['binding'] and binding['root_fingerprint'] == frame['state']['root_fingerprint']
           and all(binding[k] == parent['binding'][k] for k in ('root_fingerprint', 'monitor_endpoint_hash', 'secret_sha256', 'key_id'))
           and all(binding[k] == parent['expected'][k] for k in ('claim_id', 'device_id', 'key_id')), 'CONFLICT')
    _check(frame['state']['plan'] is None or binding == frame['state']['plan']['binding'], 'CONFLICT')
    frame['binding'] = binding
    return frame


def initialize(store):
    """Explicit externally established fresh provision only; never repair a missing state."""
    with store.restart_state_lock():
        _check(provision.initialization_allowed(store, 'restart'), 'CONFLICT')
        _check(store.read(NAME) is None, 'CONFLICT')
        _write(store, {'schema_version': SCHEMA, 'root_fingerprint': store.root_fingerprint(),
                      'owner_generation': secrets.token_hex(16), 'disabled': False, 'plan': None})


def snapshot(store, *, binding_supplier, now_ms):
    with _locks(store):
        return _copy(_access(store, binding_supplier, now_ms))


def local_snapshot(store, *, now_ms):
    """Observe pending metadata without provider/credential access; never authorize a send."""
    with _locks(store):
        return _copy(_local_frame(store, now_ms))


def begin(store, snapshot, *, binding_supplier, now_ms, confirm_human=False):
    _check(confirm_human is True, 'HUMAN_REQUIRED')
    with _locks(store):
        current = _access(store, binding_supplier, now_ms)
        _check(current == snapshot and current['state']['plan'] is None, 'CONFLICT')
        parent, state = current['parent'], current['state']
        read = _wire(control.REGISTRATION_READ, {'schema_version': 'sdc.support.restart-registration-read.v1',
                     'case_id': parent['expected']['case_id'], 'local_receipt_id': parent['local_receipt_id']}, now_ms)
        state['plan'] = {'parent': parent, 'planning_owner': state['owner_generation'], 'restart_owner': current['restart']['owner_generation'], 'binding': current['binding'],
            'planning_consent_id': str(uuid.uuid4()),
            'created_at': now_ms, 'expires_at': min(parent['expires_at'], support._profile_expiry(current['binding'])),
            'read': {'request': read, 'attempts': 0, 'blocked': False},
            'registration': None, 'samples': dict.fromkeys(PHASES), 'generation': None, 'inspection_consent': None,
            'inspection': None, 'inspection_ack': None, 'inspection_ack_at': None, 'preview': None, 'verified_preview': None, 'verified_at': None}
        _write(store, state)


def read_attempt(store, *, binding_supplier, now_ms):
    with _locks(store):
        state = _access(store, binding_supplier, now_ms)['state']
        p = state['plan']
        _check(p is not None and p['registration'] is None and not p['read']['blocked'] and p['read']['attempts'] < 4, 'CONFLICT')
        p['read']['attempts'] += 1
        _write(store, state)
        return _copy(p['read']['request'])


def block_read(store, request):
    """Keep terminal read evidence even after local stop; never re-enable."""
    with store.restart_state_lock():
        state = _read(store)
        _check(state['plan'] is not None and state['plan']['read']['request'] == request, 'CONFLICT')
        state['plan']['read']['blocked'] = True
        _write(store, state)


def record_registration(store, request, response, *, binding_supplier, now_ms):
    response = control.validate_response(control.REGISTRATION_READ, response, request=_body(control.REGISTRATION_READ, request), now_ms=now_ms)
    with _locks(store):
        state = _access(store, binding_supplier, now_ms)['state']
        p = state['plan']
        _check(p is not None and p['read']['request'] == request and p['read']['attempts'] > 0
               and not p['read']['blocked'] and p['registration'] is None, 'CONFLICT')
        reg = _registration(response['registration'], p['parent'], now_ms)
        p['registration'] = {'value': reg, 'request_id': response['request_id'], 'fetched_at': now_ms}
        p['expires_at'] = min(p['expires_at'], reg['expires_at'])
        _write(store, state)


def request_sample(store, phase, *, binding_supplier, now_ms, expires_at=None):
    _check(phase in PHASES)
    with _locks(store):
        state = _access(store, binding_supplier, now_ms)['state']
        p = state['plan']
        _check(p is not None and p['registration'] is not None, 'CONFLICT')
        _check(not any(v is not None and v['preflight'] is None for v in p['samples'].values()), 'CONFLICT')
        _check(p['samples'][phase] is None and (phase == 'sample' or p['generation'] is not None), 'CONFLICT')
        _check(phase != 'refresh' or p['inspection_consent'] is not None, 'CONFLICT')
        _check(phase != 'confirm' or p['verified_preview'] is not None, 'CONFLICT')
        deadline = min(p['expires_at'], now_ms + 300000, expires_at if expires_at is not None else now_ms + 300000)
        _check(change._integer(deadline) and now_ms < deadline, 'EXPIRED')
        p['samples'][phase] = {'request_id': str(uuid.uuid4()), 'requested_at': now_ms, 'expires_at': deadline, 'preflight': None}
        _write(store, state)


def respond_once(store, *, sample, now):
    """Observe outside all locks, then compare the same pending request and all parent CAS."""
    with store.restart_state_lock():
        candidate = _read(store)
        p = candidate['plan']
        if candidate['disabled'] or p is None or not any(v is not None and v['preflight'] is None and now() < v['expires_at'] for v in p['samples'].values()):
            return False
    with _locks(store):
        state = _local_frame(store, now())['state']
        _check(state == candidate, 'CONFLICT')
        p = state['plan']
        phase = next(k for k, v in p['samples'].items() if v is not None and v['preflight'] is None)
        request = {'request_id': p['samples'][phase]['request_id'], 'planning_owner': state['owner_generation'], 'phase': phase,
                   'expected_generation': p['generation'], 'expires_at': min(p['expires_at'], p['samples'][phase]['expires_at']), 'expected': _target_expected(p)}
        _check(now() < request['expires_at'], 'EXPIRED')
    try:
        preflight = sample(request)
    except TimeoutError:
        raise
    except Exception:
        raise RestartPlanningError('CONFLICT') from None
    with _locks(store):
        latest = _local_frame(store, now())['state']
        _check(latest == state, 'CONFLICT')
        _check(now() < request['expires_at'], 'EXPIRED')
        p = latest['plan']
        preflight = _preflight(p, preflight, now())
        _check(preflight['observed_at'] >= p['samples'][phase]['requested_at'], 'CONFLICT')
        p['generation'] = preflight['runtime_generation']
        p['samples'][phase]['preflight'] = preflight
        _write(store, latest)
    return True


def confirm_inspection(store, *, binding_supplier, now_ms, confirm_human=False):
    _check(confirm_human is True, 'HUMAN_REQUIRED')
    with _locks(store):
        state = _access(store, binding_supplier, now_ms)['state']
        p = state['plan']
        _check(p is not None and p['generation'] is not None and p['inspection_consent'] is None, 'CONFLICT')
        p['inspection_consent'] = {'consent_id': str(uuid.uuid4()), 'inspection_id': str(uuid.uuid4()), 'confirmed_at': now_ms}
        _write(store, state)


def begin_inspection(store, *, binding_supplier, now_ms):
    with _locks(store):
        state = _access(store, binding_supplier, now_ms)['state']
        p = state['plan']
        _check(p is not None and p['inspection_consent'] is not None and p['inspection'] is None, 'CONFLICT')
        selected = p['samples']['refresh'] or p['samples']['sample']
        _check(selected is not None and selected['preflight'] is not None, 'CONFLICT')
        preflight = _preflight(p, selected['preflight'], now_ms)
        consent = p['inspection_consent']
        p['inspection'] = _wire(control.INSPECTION, {'schema_version': 'sdc.support.restart-control.v1',
            'inspection_id': consent['inspection_id'], 'local_planning_consent_id': consent['consent_id'],
            'case_id': p['parent']['expected']['case_id'], 'service_revision': p['registration']['value']['service_revision'],
            'preflight': preflight}, now_ms)
        _write(store, state)
        return _copy(p['inspection'])


def record_inspection(store, request, response, *, binding_supplier, now_ms):
    # ACK evidence may arrive after stop; saving it never admits the next step.
    response = control.validate_response(control.INSPECTION, response, request=_body(control.INSPECTION, request), now_ms=now_ms)
    with _locks(store):
        state = _read(store)
        p = state['plan']
        _check(p is not None and p['inspection'] == request and p['inspection_ack'] is None, 'CONFLICT')
        _registration(response['registration'], p['parent'], response['registration']['not_before'], p['registration']['value'])
        p['expires_at'] = min(p['expires_at'], response['expires_at'], response['registration']['expires_at'])
        p['inspection_ack'] = response
        p['inspection_ack_at'] = now_ms
        _write(store, state)


def begin_preview(store, operation_id, *, binding_supplier, now_ms):
    _check(support._uuid(operation_id))
    with _locks(store):
        state = _access(store, binding_supplier, now_ms)['state']
        p = state['plan']
        _check(p is not None and p['inspection_ack'] is not None and p['preview'] is None, 'CONFLICT')
        p['preview'] = _wire(control.PREVIEW, {'schema_version': 'sdc.support.restart-control.v1',
                                             'preview_id': str(uuid.uuid4()), 'operation_id': operation_id}, now_ms)
        _write(store, state)
        return _copy(p['preview'])


def record_preview(store, request, verified_preview, registration, *, binding_supplier, now_ms):
    preview = contract.validate_typed_payload('restart-consent-preview', verified_preview, now_ms=now_ms)
    with _locks(store):
        state = _access(store, binding_supplier, now_ms)['state']
        p = state['plan']
        _check(p is not None and p['preview'] == request and p['verified_preview'] is None, 'CONFLICT')
        _registration(registration, p['parent'], now_ms, p['registration']['value'])
        p.update(verified_preview=preview, verified_at=now_ms,
                 expires_at=min(p['expires_at'], preview['intent']['expires_at'], preview['approval']['expires_at'], registration['expires_at']))
        _write(store, state)  # Validation binds expected15 and the exact inspection ID/hash.


def disable(store):
    with store.restart_state_lock():
        state = _read(store)
        if not state['disabled']:
            state.update(disabled=True, owner_generation=secrets.token_hex(16))
            _write(store, state)


def status(store):
    """Local evidence only; no credential, endpoint, supplier or Runtime proof is obtained."""
    with store.restart_state_lock():
        state = _read(store)
        p = state['plan']
        return {'disabled': state['disabled'], 'uncertain': bool(getattr(store, '_restart_planning_uncertain', False)),
                'case_id': p['parent']['expected']['case_id'] if p else None, 'expires_at': p['expires_at'] if p else None,
                'read_attempts': p['read']['attempts'] if p else 0, 'registration_recorded': bool(p and p['registration']),
                'runtime_generation': p['generation'] if p else None, 'inspection_recorded': bool(p and p['inspection_ack']),
                'preview_recorded': bool(p and p['verified_preview'])}
