"""R4a契約とハッシュを純粋に検証する（署名・同意・runner接続は別）。"""
from __future__ import annotations

import hashlib
import json
import re

MAX_SAFE_INTEGER = 2**53 - 1
MAX_BODY = 16384
SUPPORT_CONSENT = 'sdc-support-consent-v1'
CAPABILITY_REVISION = 'sdc-support-capabilities-v1'
DIAGNOSTIC_IDS = ('runtime.status.v1', 'notification.health.v1', 'connectivity.status.v1')
PURPOSE_CODES = ('NOTIFICATION_FAILURE', 'PROCESS_STALL', 'CONFIG_MISMATCH', 'UPDATE_FAILURE', 'HEALTH_CHECK')
_SCOPE = ('tenant', 'installation', 'device_id', 'environment', 'case_id')
_EPOCHS = ('realm_epoch', 'policy_epoch', 'device_epoch', 'local_consent_epoch', 'case_epoch')
_TIMES = ('issued_at', 'not_before', 'expires_at')
_OPERATION = ('schema_version', 'operation_id', 'product', 'audience', *_SCOPE, 'type', 'params',
              'purpose_code', 'capability_revision', 'requested_by', 'approved_by', 'approval_id',
              'approval_hash', 'consent_revision', *_EPOCHS, *_TIMES, 'payload_hash')
_APPROVAL = ('schema_version', 'approval_id', 'intent_hash', 'approved_by', 'approval_kind',
             'consent_revision', *_EPOCHS, *_TIMES, 'approval_hash')
_GRANT = ('schema_version', 'grant_id', 'grant_request_id', 'operation_id', 'payload_hash', *_SCOPE,
          'receipt_id', 'admission_id', 'owner_generation', 'approval_hash', *_EPOCHS, *_TIMES)
_OMITTED_INTENT = frozenset(('payload_hash', 'approval_id', 'approval_hash', 'approved_by'))
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'


def _check(ok, code='SUPPORT_INVALID_CONTRACT'):
    if not ok:
        raise ValueError(code)


def canonical(value, max_bytes=8192):
    """Return bounded ASCII CJSON text; accept plain JSON types without aliases to output."""
    _check(type(max_bytes) is int and 0 < max_bytes <= MAX_BODY)
    active = set()

    def encode(item, depth):
        _check(depth <= 8)
        kind = type(item)
        if item is None:
            return 'null'
        if kind is bool:
            return 'true' if item else 'false'
        if kind is int:
            _check(-MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER)
            return str(item)
        if kind is str:
            _check(len(item) <= MAX_BODY and re.fullmatch(r'[\x20-\x7e]*', item) is not None)
            return json.dumps(item, ensure_ascii=True)
        _check(kind in (dict, list) and id(item) not in active)
        active.add(id(item))
        try:
            if kind is list:
                _check(len(item) <= 1024)
                pieces, size = [], 2
                for child in item:
                    part = encode(child, depth + 1)
                    size += len(part) + (1 if pieces else 0)
                    _check(size <= MAX_BODY)
                    pieces.append(part)
                result = '[' + ','.join(pieces) + ']'
            else:
                _check(len(item) <= 64 and all(type(key) is str and 0 < len(key) <= MAX_BODY and re.fullmatch(r'[\x20-\x7e]+', key) for key in item))
                pieces, size = [], 2
                for key in sorted(item):
                    _check(len(key) <= MAX_BODY)
                    part = json.dumps(key, ensure_ascii=True) + ':' + encode(item[key], depth + 1)
                    size += len(part) + (1 if pieces else 0)
                    _check(size <= MAX_BODY)
                    pieces.append(part)
                result = '{' + ','.join(pieces) + '}'
        finally:
            active.remove(id(item))
        _check(len(result) <= MAX_BODY)
        return result

    result = encode(value, 0)
    _check(len(result) <= max_bytes)
    return result


def parse_canonical(raw, max_bytes=8192):
    """Parse exact canonical ASCII bytes/text with duplicate, float and negative-zero rejection."""
    _check(type(max_bytes) is int and 0 < max_bytes <= MAX_BODY)
    _check(type(raw) in (bytes, str) and len(raw) <= max_bytes)

    def pairs(items):
        result = {}
        for key, value in items:
            _check(key not in result)
            result[key] = value
        return result

    def integer(text):
        _check(text != '-0' and len(text) <= 17)
        result = int(text)
        _check(-MAX_SAFE_INTEGER <= result <= MAX_SAFE_INTEGER)
        return result

    def denied(_):
        raise ValueError('SUPPORT_INVALID_CONTRACT')

    try:
        text = raw.decode('ascii') if type(raw) is bytes else raw
        _check(text.isascii())
        value = json.loads(text, object_pairs_hook=pairs, parse_int=integer,
                           parse_float=denied, parse_constant=denied)
        _check(canonical(value, max_bytes=max_bytes) == text)
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError('SUPPORT_INVALID_CONTRACT') from None


def _input(value):
    if type(value) in (bytes, str):
        return parse_canonical(value)
    # A canonical round-trip snapshots mutable input and rejects subclasses/foreign objects.
    return parse_canonical(canonical(value))


def _fields(value, names):
    _check(type(value) is dict and set(value) == set(names))


def _matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _hex(value):
    return _matches(value, r'[0-9a-f]{64}')


def _scope(value):
    for key in ('tenant', 'installation'):
        _check(_matches(value[key], r'[a-z0-9][a-z0-9_-]{0,63}'))
    for key in ('device_id', 'case_id'):
        _check(_matches(value[key], _UUID))
    _check(_matches(value['environment'], r'[a-z0-9_-]{1,32}'))


def _epochs(value):
    _check(_matches(value['realm_epoch'], r'[A-Za-z0-9][A-Za-z0-9:_-]{0,127}'))
    for key in _EPOCHS[1:]:
        _check(type(value[key]) is int and 1 <= value[key] <= MAX_SAFE_INTEGER)


def _lifetime(value, limit, now_ms):
    for key in _TIMES:
        _check(type(value[key]) is int and 0 <= value[key] <= MAX_SAFE_INTEGER)
    _check(value['issued_at'] <= value['not_before'] < value['expires_at']
           and value['expires_at'] - value['issued_at'] <= limit)
    if now_ms is not None:
        _check(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER)
        _check(value['not_before'] <= now_ms < value['expires_at'], 'SUPPORT_EXPIRED_OR_NOT_YET_VALID')


def _operation_core(value, now_ms):
    _check(value['schema_version'] == 'sdc.support.operation.v1' and _matches(value['operation_id'], _UUID)
           and value['product'] == 'hermes-mcs')
    _scope(value)
    _epochs(value)
    _lifetime(value, 1800000, now_ms)
    _check(_matches(value['audience'], r'[A-Za-z0-9:_-]{1,100}') and value['type'] == 'diagnostics.read')
    _fields(value['params'], ('diagnostic_id',))
    _check(value['params']['diagnostic_id'] in DIAGNOSTIC_IDS and value['purpose_code'] in PURPOSE_CODES
           and value['capability_revision'] == CAPABILITY_REVISION
           and value['consent_revision'] == SUPPORT_CONSENT and _hex(value['requested_by']))


def _operation_shape(value, now_ms):
    _fields(value, _OPERATION)
    _operation_core(value, now_ms)
    _check(_matches(value['approval_id'], _UUID) and _hex(value['approved_by'])
           and _hex(value['approval_hash']) and _hex(value['payload_hash']))


def _approval_shape(value, now_ms):
    _fields(value, _APPROVAL)
    _check(value['schema_version'] == 'sdc.support.approval.v1' and _matches(value['approval_id'], _UUID)
           and _hex(value['intent_hash']) and _hex(value['approved_by']) and _hex(value['approval_hash']))
    _check(value['approval_kind'] in ('individual', 'preauthorized') and value['consent_revision'] == SUPPORT_CONSENT)
    _epochs(value)
    _lifetime(value, 1800000, now_ms)


def _digest(value):
    return hashlib.sha256(canonical(value).encode('ascii')).hexdigest()


def hash_payload(value):
    """Recompute payload hash excluding its own field, without checking current time."""
    value = _input(value)
    _check(type(value) is dict)
    _operation_shape({**value, 'payload_hash': '0' * 64}, None)
    return _digest({key: child for key, child in value.items() if key != 'payload_hash'})


def hash_intent(value):
    """Hash the closed intent subset of an operation, without current-time checks."""
    value = _input(value)
    _check(type(value) is dict and set(value) <= set(_OPERATION))
    core = {key: child for key, child in value.items() if key not in _OMITTED_INTENT}
    _fields(core, set(_OPERATION) - _OMITTED_INTENT)
    _operation_core(core, None)
    return _digest(core)


def hash_approval(value):
    """Recompute approval hash excluding its own field, without current-time checks."""
    value = _input(value)
    _check(type(value) is dict)
    _approval_shape({**value, 'approval_hash': '0' * 64}, None)
    return _digest({key: child for key, child in value.items() if key != 'approval_hash'})


def validate_operation(value, *, now_ms):
    """Validate structure, lifetime and payload hash; establish no signature or consent."""
    _check(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER)
    value = _input(value)
    _operation_shape(value, now_ms)
    _check(value['payload_hash'] == hash_payload(value), 'SUPPORT_PAYLOAD_HASH_MISMATCH')
    return value


def validate_approval(value, *, now_ms):
    """Validate approval shape/hash; preauthorized shape alone does not authorize R4a."""
    _check(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER)
    value = _input(value)
    _approval_shape(value, now_ms)
    _check(value['approval_hash'] == hash_approval(value), 'SUPPORT_APPROVAL_HASH_MISMATCH')
    return value


def validate_grant(value, *, now_ms):
    """Validate grant shape/time only; caller must bind it to its operation and receipt."""
    _check(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER)
    value = _input(value)
    _fields(value, _GRANT)
    _check(value['schema_version'] == 'sdc.support.execution-grant.v1')
    for key in ('grant_id', 'grant_request_id', 'operation_id', 'receipt_id', 'admission_id'):
        _check(_matches(value[key], _UUID))
    _check(_hex(value['payload_hash']) and _hex(value['approval_hash'])
           and _matches(value['owner_generation'], r'[0-9a-f]{32}'))
    _scope(value)
    _epochs(value)
    _lifetime(value, 30000, now_ms)
    return value


# Consent-preview fields are fixed by the vendored central contract.
_PREVIEW = ('schema_version', 'preview_id', 'product', 'audience', *_SCOPE, 'claim_id', 'key_id',
            'purpose_code', 'capability_revision', 'diagnostic_ids', 'consent_revision', *_EPOCHS,
            'operator_refs', 'approved_by', *_TIMES, 'consent_expires_at',
            'consent_binding_hash')
_PREVIEW_UNBOUND = frozenset(('preview_id', 'issued_at', 'not_before', 'expires_at', 'consent_binding_hash'))


def _ordered_unique(value, maximum, predicate):
    _check(type(value) is list and 1 <= len(value) <= maximum and all(predicate(item) for item in value))
    _check(value == sorted(set(value)))


def _preview_shape(value, now_ms):
    _fields(value, _PREVIEW)
    _check(value['schema_version'] == 'sdc.support.consent-preview.v1' and value['product'] == 'hermes-mcs')
    for key in ('preview_id', 'claim_id', 'key_id'):
        _check(_matches(value[key], _UUID))
    _scope(value)
    _epochs(value)
    _check(_matches(value['audience'], r'[A-Za-z0-9:_-]{1,100}')
           and value['purpose_code'] in PURPOSE_CODES and value['capability_revision'] == CAPABILITY_REVISION
           and value['consent_revision'] == SUPPORT_CONSENT and _hex(value['approved_by'])
           and _hex(value['consent_binding_hash']))
    _ordered_unique(value['diagnostic_ids'], len(DIAGNOSTIC_IDS),
                    lambda item: type(item) is str and item in DIAGNOSTIC_IDS)
    _ordered_unique(value['operator_refs'], 16, _hex)
    _lifetime(value, 300000, now_ms)
    expiry = value['consent_expires_at']
    _check(type(expiry) is int and value['expires_at'] <= expiry <= MAX_SAFE_INTEGER
           and expiry - value['issued_at'] <= 1800000)


def hash_consent_binding(value):
    """Hash fixed preview scope including ordered lists/consent expiry; establish no consent."""
    value = _input(value)
    _check(type(value) is dict)
    _preview_shape({**value, 'consent_binding_hash': '0' * 64}, None)
    return _digest({key: child for key, child in value.items() if key not in _PREVIEW_UNBOUND})


def validate_preview(value, *, now_ms):
    """Validate the fixed preview contract/hash only, never signature or local consent."""
    _check(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER)
    value = _input(value)
    _preview_shape(value, now_ms)
    _check(value['consent_binding_hash'] == hash_consent_binding(value), 'SUPPORT_CONSENT_BINDING_MISMATCH')
    return value
