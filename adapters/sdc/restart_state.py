"""再起動専用の変更同意・不変accept・停止を私有領域の一件の記録に保存する。"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid

from . import restart_contract as contract
from .restart_control import ACCEPT, validate_response
from .storage import RESTART_STATE_MAX_BYTES
from .support_contract import MAX_SAFE_INTEGER, canonical, parse_canonical

NAME = 'restart-state.json'
SCHEMA = 'sdc.support.restart-local-state.v1'
_IDENTITY = ('claim_id', 'device_id', 'key_id')
_BINDING = {'root_fingerprint', 'monitor_endpoint_hash', 'support_endpoint_hash', 'pins_sha256', 'secret_sha256', *_IDENTITY}
_FROM_PREVIEW = ('product', *contract.EXPECTED_FIELDS, 'capability_revision', 'consent_revision',
                 'operation_id', 'intent_hash', 'change_consent_id', 'change_binding_hash', 'preview_id')
_ACCEPT_FIELDS = ('preview_id', 'change_consent_id', 'change_binding_hash', 'local_change_receipt_id', 'preview_payload_hash')


class RestartStateError(ValueError):
    """Expose only fixed local-state errors, never input or credential values."""
    def __init__(self, code='INVALID'):
        allowed = {'INVALID', 'MISSING', 'CONFLICT', 'UNCERTAIN', 'BINDING_UNAVAILABLE', 'HUMAN_REQUIRED', 'EXPIRED'}
        super().__init__('SUPPORT_RESTART_STATE_' + (code if code in allowed else 'INVALID'))


def _check(ok, code='INVALID'):
    if not ok:
        raise RestartStateError(code)


def _integer(value):
    return type(value) is int and 0 <= value <= MAX_SAFE_INTEGER


def _hex(value, length=64):
    return type(value) is str and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False))


def _bounded(value):
    return parse_canonical(canonical(value))


def _binding(value):
    value = _bounded(value)
    _check(type(value) is dict and set(value) in (_BINDING, _BINDING | {'profile'}))
    _check(all(_hex(value[k]) for k in _BINDING - set(_IDENTITY)))
    _check(all(type(value[k]) is str and re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', value[k]) for k in _IDENTITY))
    if 'profile' in value:
        from .support_state import _profile
        _profile(value['profile'], value['support_endpoint_hash'])
    return value


def _current(supplier):
    try:
        return _binding(supplier())
    except TimeoutError:
        raise
    except Exception:
        raise RestartStateError('BINDING_UNAVAILABLE') from None


def _expected(value, preview):
    value = _bounded(value)
    _check(type(value) is dict and set(value) == set(contract.EXPECTED_FIELDS))
    _check(all(canonical(value[k]) == canonical(preview[k]) for k in value), 'CONFLICT')
    return value


def _accept_body(receipt):
    return {'schema_version': 'sdc.support.restart-control.v1',
            **{k: receipt[k] for k in _ACCEPT_FIELDS}, 'accept_id': receipt['change_accept_id'],
            'local_change_receipt_hash': receipt['receipt_hash']}


def _ack(value, state, now_ms):
    ack = contract.validate_restart_acceptance(value, now_ms=now_ms, allow_expired=True)
    receipt, preview = state['receipt'], state['preview']
    _check(all(ack[k] == v for k, v in _accept_body(receipt).items() if k != 'schema_version'), 'CONFLICT')
    _check(ack['operation_id'] == receipt['operation_id']
           and receipt['confirmed_at'] <= ack['accepted_at'] < preview['expires_at']
           and ack['expires_at'] <= receipt['expires_at'], 'CONFLICT')
    return ack


def _validated(state):
    try:
        fields = {'schema_version', 'root_fingerprint', 'owner_generation', 'disabled',
                  'expected', 'binding', 'preview', 'receipt', 'accept'}
        _check(type(state) is dict and set(state) == fields and state['schema_version'] == SCHEMA)
        _check(_hex(state['root_fingerprint']) and _hex(state['owner_generation'], 32) and type(state['disabled']) is bool)
        values = tuple(state[k] for k in ('expected', 'binding', 'preview', 'receipt', 'accept'))
        if state['receipt'] is None:
            _check(all(v is None for v in values))
        else:
            receipt = state['receipt']
            at = receipt['confirmed_at']
            receipt = contract.validate_restart_record('local_receipt', receipt, now_ms=at)
            preview = contract.validate_typed_payload('restart-consent-preview', state['preview'], now_ms=at)
            _expected(state['expected'], preview)
            binding = _binding(state['binding'])
            _check(binding['root_fingerprint'] == state['root_fingerprint'] and all(binding[k] == receipt[k] for k in _IDENTITY))
            _check(all(canonical(receipt[k]) == canonical(preview[k]) for k in _FROM_PREVIEW))
            _check(receipt['approval_hash'] == preview['approval']['approval_hash']
                   and receipt['preview_payload_hash'] == contract.hash_restart_preview(preview)
                   and receipt['receipt_hash'] == contract.hash_restart_local_receipt(receipt)
                   and receipt['expires_at'] == min(preview['intent']['expires_at'], preview['approval']['expires_at']))
            accept = state['accept']
            _check(type(accept) is dict and set(accept) == {'request', 'ack'})
            request = accept['request']
            _check(type(request) is dict and set(request) == {'raw', 'body_hash', 'admitted_at'})
            raw = canonical(_accept_body(receipt), max_bytes=16384)
            _check(request['raw'] == raw and request['body_hash'] == hashlib.sha256(raw.encode('ascii')).hexdigest())
            admitted = request['admitted_at']
            _check(admitted is None or (_integer(admitted) and at <= admitted < preview['expires_at']))
            if accept['ack'] is not None:
                _check(admitted is not None)
                _ack(accept['ack'], state, accept['ack']['accepted_at'])
        _check(len(json.dumps(state, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')) <= RESTART_STATE_MAX_BYTES)
        return _copy(state)
    except RestartStateError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise RestartStateError() from None


def _read(store):
    try:
        root = store.root_fingerprint()
        state = store.read(NAME)
        _check(state is not None, 'MISSING')
        state = _validated(state)
        _check(state['root_fingerprint'] == root, 'CONFLICT')
        return state
    except (RestartStateError, TimeoutError):
        raise
    except (OSError, ValueError, TypeError):
        raise RestartStateError() from None


def _certain(store):
    _check(not getattr(store, '_restart_state_uncertain', False), 'UNCERTAIN')


def _write(store, state):
    _certain(store)
    state = _validated(state)
    try:
        _check(store.root_fingerprint() == state['root_fingerprint'], 'CONFLICT')
        store.write(NAME, state)
        _check(store.root_fingerprint() == state['root_fingerprint'], 'CONFLICT')
    except BaseException as error:
        # A failed replace/directory fsync cannot be treated as a safe retry admission.
        store._restart_state_uncertain = True
        if isinstance(error, TimeoutError) or not isinstance(error, Exception):
            raise
        raise RestartStateError('UNCERTAIN') from None


def _view(store, state, now_ms):
    from .support_state import _profile_expiry
    receipt = state['receipt']
    uncertain = bool(getattr(store, '_restart_state_uncertain', False))
    ack = state['accept']['ack'] if state['accept'] is not None else None
    enabled = bool(not state['disabled'] and not uncertain and receipt is not None
                   and receipt['confirmed_at'] <= now_ms < min(receipt['expires_at'], _profile_expiry(state['binding']))
                   and (ack is None or now_ms < ack['expires_at']))
    return {**_copy(state), 'enabled': enabled, 'uncertain': uncertain}


def initialize(store):
    """Explicit first provisioning only; never use this to repair a lost/rolled-back state.

    The caller must establish a fresh provision externally. Managed initialization
    requires the same Store's one-use import callback; missing files prove no freshness.
    This one-record component cannot replace, retire or re-enable a prior consent.
    """
    with store.restart_state_lock():
        from .provision import initialization_allowed
        _check(initialization_allowed(store, 'restart'), 'CONFLICT')
        _certain(store)
        _check(store.read(NAME) is None, 'CONFLICT')
        state = {'schema_version': SCHEMA, 'root_fingerprint': store.root_fingerprint(),
                 'owner_generation': secrets.token_hex(16), 'disabled': False,
                 **dict.fromkeys(('expected', 'binding', 'preview', 'receipt', 'accept'))}
        _write(store, state)


def prepare(store, *, binding_supplier, now_ms):
    """Snapshot local CAS/binding under state then tick lock, without HTTP or a prompt.

    Supplier must return the latest root/endpoints/restart-pins/actual secret hash
    and current claim/device/key IDs. It must not prompt or perform HTTP and must
    finish within the caller's deadline. This API verifies no signature, pin
    distribution, current registration, central authority or human action.
    """
    _check(_integer(now_ms))
    with store.restart_state_lock(), store.lock():
        _certain(store)
        state = _read(store)
        binding = _current(binding_supplier)
        _check(binding['root_fingerprint'] == state['root_fingerprint'], 'CONFLICT')
        result = _view(store, state, now_ms)
        result['current_binding'] = binding
        result['enabled'] &= state['binding'] == binding
        return result


def confirm_consent(store, verified_preview, *, expected, expected_owner_generation, expected_binding,
                    binding_supplier, now_ms, confirm_human=False):
    """Persist one caller-verified, human-confirmed change; signatures remain caller-owned.

    Before this call the caller must verify restart-purpose signature/pins, obtain
    independent expected15/current registration, display this exact preview and
    consent expiry=min(intent, approval), and obtain explicit human confirmation.
    No lock spans that prompt. Both receipt and accept IDs are fixed here before
    hashing; this function does not admit HTTP, grant, SPENT or Runtime execution.
    now_ms and the finite binding supplier are owned by the integrating caller.
    """
    _check(confirm_human is True, 'HUMAN_REQUIRED')
    _check(_integer(now_ms) and _hex(expected_owner_generation, 32))
    preview = contract.validate_typed_payload('restart-consent-preview', verified_preview, now_ms=now_ms)
    expected = _expected(expected, preview)
    with store.restart_state_lock(), store.lock():
        _certain(store)
        state = _read(store)
        _check(not state['disabled'] and state['receipt'] is None
               and state['owner_generation'] == expected_owner_generation, 'CONFLICT')
        binding = _current(binding_supplier)
        _check(binding == _binding(expected_binding) and binding['root_fingerprint'] == state['root_fingerprint']
               and all(binding[k] == expected[k] for k in _IDENTITY), 'CONFLICT')
        if 'profile' in binding:
            _check(now_ms < binding['profile']['expires_at'], 'EXPIRED')
            _check(all(expected[k] == binding['profile']['endpoint'][k] for k in ('audience', 'environment')), 'CONFLICT')
        receipt = {'schema_version': 'sdc.support.restart-local-change-receipt.v1',
                   **{k: preview[k] for k in _FROM_PREVIEW}, 'approval_hash': preview['approval']['approval_hash'],
                   'preview_payload_hash': contract.hash_restart_preview(preview),
                   'local_change_receipt_id': str(uuid.uuid4()), 'change_accept_id': str(uuid.uuid4()),
                   'confirmed_at': now_ms, 'expires_at': min(preview['intent']['expires_at'], preview['approval']['expires_at']),
                   'receipt_hash': '0' * 64}
        receipt['receipt_hash'] = contract.hash_restart_local_receipt(receipt)
        raw = canonical(_accept_body(receipt), max_bytes=16384)
        state.update(owner_generation=secrets.token_hex(16), expected=expected, binding=binding, preview=preview,
                     receipt=receipt, accept={'request': {'raw': raw, 'body_hash': hashlib.sha256(raw.encode('ascii')).hexdigest(),
                                                        'admitted_at': None}, 'ack': None})
        _write(store, state)
        return _view(store, state, now_ms)


def begin_accept(store, *, expected_owner_generation, binding_supplier, now_ms):
    """Persist admission before each send; a replay keeps the original ID/body/deadline.

    A replay after preview expiry requires prior durable admission. Current central
    case/authority checks remain mandatory; this record alone cannot authorize I/O.
    One admitted request may finish after disable, but no next admission may start.
    """
    _check(_integer(now_ms))
    with store.restart_state_lock(), store.lock():
        _certain(store)
        state = _read(store)
        _check(not state['disabled'] and state['owner_generation'] == expected_owner_generation
               and state['receipt'] is not None, 'CONFLICT')
        _check(_current(binding_supplier) == state['binding'], 'CONFLICT')
        receipt, accept = state['receipt'], state['accept']
        if 'profile' in state['binding']:
            _check(now_ms < state['binding']['profile']['expires_at'], 'EXPIRED')
        _check(receipt['confirmed_at'] <= now_ms < receipt['expires_at'], 'EXPIRED')
        _check(accept['ack'] is None, 'CONFLICT')
        request = accept['request']
        if request['admitted_at'] is None:
            _check(now_ms < state['preview']['expires_at'], 'EXPIRED')
            request['admitted_at'] = now_ms
        else:
            _check(request['admitted_at'] <= now_ms, 'EXPIRED')
        _write(store, state)
        return _copy(request)


def record_accept_ack(store, request, response, *, now_ms):
    """Save the primary immutable ACK, even after disable; never re-enable consent."""
    _check(_integer(now_ms))
    _check(type(request) is dict and set(request) == {'raw', 'body_hash', 'admitted_at'}
           and type(request['raw']) is str and _hex(request['body_hash']) and _integer(request['admitted_at']))
    with store.restart_state_lock():
        _certain(store)
        state = _read(store)
        accept = state['accept']
        _check(accept is not None and accept['request'] == request and request['admitted_at'] is not None, 'CONFLICT')
        response = validate_response(ACCEPT, response, request=parse_canonical(request['raw'], max_bytes=16384), now_ms=now_ms)
        ack = _ack({k: v for k, v in response.items() if k != 'request_id'}, state, now_ms)
        _check(accept['ack'] is None or accept['ack'] == ack, 'CONFLICT')
        accept['ack'] = ack
        _write(store, state)
        return _view(store, state, now_ms)


def require_primary_acceptance(store, acceptance, *, now_ms):
    """Compare all later acceptance fields with the primary ACK; grant no current authority."""
    _check(_integer(now_ms))
    with store.restart_state_lock():
        _certain(store)
        state = _read(store)
        _check(state['accept'] is not None and state['accept']['ack'] is not None, 'CONFLICT')
        ack = _ack(acceptance, state, now_ms)
        _check(ack == state['accept']['ack'], 'CONFLICT')
        return _copy(ack)


def status(store, *, now_ms):
    """Read local evidence only, without current keys, endpoints, network or initialization."""
    _check(_integer(now_ms))
    with store.restart_state_lock():
        return _view(store, _read(store), now_ms)


def disable(store):
    """Durably stop new admissions using state lock alone; preserve all pending/ACK evidence."""
    with store.restart_state_lock():
        _certain(store)
        state = _read(store)
        if not state['disabled']:
            state.update(disabled=True, owner_generation=secrets.token_hex(16))
            _write(store, state)
        return {'disabled': True, 'owner_generation': state['owner_generation']}
