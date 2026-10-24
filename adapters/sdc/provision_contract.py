"""固定provision値と局所的な整合性を検証する（配布元の真正性・適用許可は別）。"""
from __future__ import annotations

import hashlib
import json
import math
import re

from .support_contract import MAX_SAFE_INTEGER, canonical, parse_canonical

MAX_BODY = 16384
MAX_GENERATION = 2147483647
CHANNEL = 'authenticated-console-manual-v1'
VALIDATOR = 'sdc.provision.validator.v1'
_PROFILES = ('monitor', 'support', 'restart')
_PURPOSES = {
    'support': frozenset(('operation', 'execution-grant', 'consent-preview')),
    'restart': frozenset(('restart-operation', 'restart-execution-grant', 'restart-consent-preview')),
}
_PATTERNS = {
    'UUID': r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
    'Hash': r'[0-9a-f]{64}',
    'Identifier': r'[a-z0-9][a-z0-9_-]{0,63}',
    'IssuerRef': r'[a-z0-9][a-z0-9_-]{0,63}',
    'Environment': r'[a-z0-9_-]{1,32}',
    'RealmEpoch': r'[A-Za-z0-9][A-Za-z0-9:_-]{0,127}',
    'Audience': r'[A-Za-z0-9:_-]{1,100}',
    'CSRF': r'[0-9]{10}\.[0-9a-f]{64}',
}
_BUNDLE_FIELDS = {
    'provision_id': 'UUID', 'root_id': 'UUID', 'enrollment_id': 'UUID',
    'policy_revision': 'UUID', 'tenant': 'Identifier', 'installation': 'Identifier',
    'environment': 'Environment', 'realm_epoch': 'RealmEpoch', 'profile_id': 'ProfileID',
    'generation': 'Generation', 'origin': 'Origin', 'audience': 'Audience',
    'console_origin': 'Origin', 'issuer_ref': 'IssuerRef', 'channel_ref': 'ChannelRef',
    'issued_by': 'Hash', 'policy_hash': 'Hash', 'issued_at': 'Timestamp',
    'import_expires_at': 'Timestamp', 'expires_at': 'Timestamp',
}
_RECEIPT_FIELDS = {
    'ack_id': 'UUID', 'provision_id': 'UUID', 'root_id': 'UUID', 'bundle_hash': 'Hash',
    'local_receipt_id': 'UUID', 'device_id': 'UUID', 'claim_id': 'UUID', 'key_id': 'UUID',
    'generation': 'Generation', 'accepted_at': 'Timestamp', 'applied_at': 'Timestamp',
}
_STATUS_FIELDS = {
    'provision_id': 'UUID', 'root_id': 'UUID', 'bundle_hash': 'Hash',
    'generation': 'Generation', 'current_generation': 'Generation',
    'current_provision_id': 'UUID', 'realm_epoch': 'RealmEpoch',
    'policy_revision': 'UUID', 'expires_at': 'Timestamp',
}
_REQUEST_FIELDS = {
    'Issue': {'issue_id': 'UUID', 'root_id': 'UUID', 'profile_id': 'ProfileID',
              'enrollment_id': 'UUID', 'expected_generation': 'ExpectedGeneration'},
    'Read': {'provision_id': 'UUID', 'root_id': 'UUID'},
    'Revoke': {'revoke_id': 'UUID', 'provision_id': 'UUID', 'root_id': 'UUID',
               'bundle_hash': 'Hash', 'expected_generation': 'ExpectedGeneration'},
    'Status': {'provision_id': 'UUID', 'root_id': 'UUID', 'bundle_hash': 'Hash',
               'local_receipt_id': 'UUID'},
    'Ack': {'ack_id': 'UUID', 'provision_id': 'UUID', 'root_id': 'UUID',
            'bundle_hash': 'Hash', 'local_receipt_id': 'UUID', 'applied_at': 'Timestamp'},
}
_RESULT_FIELDS = {
    'Issue': {'bundle': 'Bundle', 'bundle_hash': 'Hash'},
    'Read': {'status': 'Status'},
    'Revoke': {'revoke_id': 'UUID', 'provision_id': 'UUID', 'root_id': 'UUID',
               'bundle_hash': 'Hash', 'generation': 'Generation', 'revoked_at': 'Timestamp'},
    'Status': {'status': 'Status'},
    'Ack': {'receipt': 'Receipt'},
}


class ProvisionError(ValueError):
    """Expose a fixed code without input values or dependency errors."""

    def __init__(self):
        super().__init__('SDC_PROVISION_INVALID')


def _check(condition):
    if not condition:
        raise ProvisionError()


def _match(value, pattern):
    _check(type(value) is str and re.fullmatch(pattern, value) is not None)


def _integer(value, minimum=0, maximum=MAX_SAFE_INTEGER):
    _check(type(value) is int and minimum <= value <= maximum)


def _fields(value, fields):
    _check(type(value) is dict and set(value) == set(fields))


def _members(value, fields, now):
    for field, definition in fields.items():
        _validate(definition, value[field], now)


def _nullable(definition, value, now):
    if value is not None:
        _validate(definition, value, now)


def _ipv4_number(label):
    # WHATWG URL's restricted IPv4 number grammar, used only after the fixed
    # ASCII origin check. None means this label is not an IPv4 number.
    if not label:
        return None
    if label.startswith('0x'):
        digits, radix, pattern = label[2:], 16, r'[0-9a-f]*'
    elif len(label) > 1 and label.startswith('0'):
        digits, radix, pattern = label[1:], 8, r'[0-7]*'
    else:
        digits, radix, pattern = label, 10, r'[0-9]*'
    if re.fullmatch(pattern, digits) is None:
        return None
    return int(digits, radix) if digits else 0


def _origin(value):
    # Central provisionOrigin uses WHATWG URL, whose numeric-ending hosts are
    # IPv4 (including short/octal/hex forms). Validate without changing bytes.
    _match(value, r'https://[a-z0-9.-]+(?::443)?')
    host = value[8:].removesuffix(':443')
    labels = host.split('.')
    _check(len(value) <= 260)
    if labels[-1].isdigit() or _ipv4_number(labels[-1]) is not None:
        numbers = [_ipv4_number(label) for label in labels]
        _check(len(numbers) <= 4 and all(number is not None for number in numbers))
        _check(all(number <= 255 for number in numbers[:-1])
               and numbers[-1] < 256 ** (5 - len(numbers)))
        # A valid numeric host normalizes to four labels at the central parser.
    else:
        _check(len(host) <= 253 and len(labels) >= 2)
        for label in labels:
            _match(label, r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')


def _pin(value, profile, now):
    _fields(value, ('kid', 'purposes', 'spki', 'not_before', 'expires_at'))
    _match(value['kid'], r'[A-Za-z0-9_-]{1,64}')
    uses = value['purposes']
    _check(type(uses) is list and 1 <= len(uses) <= 3)
    _check(all(type(use) is str and use in _PURPOSES[profile] for use in uses))
    _check(len(set(uses)) == len(uses))
    _match(value['spki'], r'[A-Za-z0-9_-]{1,2731}')
    _integer(value['not_before'])
    _integer(value['expires_at'])
    _check(value['not_before'] <= now < value['expires_at'])
    # These existing functions inspect canonical base64url and minimal RSA3072
    # e65537 SPKI in Python. They neither verify a signature nor invoke OpenSSL.
    from sdc_support_signatures import validate_pins, validate_restart_pins
    validate = validate_pins if profile == 'support' else validate_restart_pins
    validate([value], purpose=uses[0], now_ms=now)


def _pins(value, profile, now):
    _check(type(value) is list)
    if profile == 'monitor':
        _check(not value)
        return
    _check(1 <= len(value) <= 8)
    seen, purposes = set(), set()
    for pin in value:
        _pin(pin, profile, now)
        _check(pin['kid'] not in seen)
        seen.add(pin['kid'])
        purposes.update(pin['purposes'])
    _check(purposes == _PURPOSES[profile])


def _bundle(value, now):
    _fields(value, (*_BUNDLE_FIELDS, 'schema_version', 'previous_provision_id',
                    'device_id', 'claim_id', 'device_epoch', 'pins', 'validator_revision'))
    _members(value, _BUNDLE_FIELDS, now)
    _check(value['schema_version'] == 'sdc.provision.bundle.v1')
    _check(value['validator_revision'] == VALIDATOR)
    _nullable('UUID', value['previous_provision_id'], now)
    monitor = value['profile_id'] == 'monitor'
    for field, definition in (('device_id', 'UUID'), ('claim_id', 'UUID'), ('device_epoch', 'Epoch')):
        if monitor:
            _check(value[field] is None)
        else:
            _validate(definition, value[field], now)
    _pins(value['pins'], value['profile_id'], now)
    issued, imported, expires = (value[field] for field in ('issued_at', 'import_expires_at', 'expires_at'))
    _check(issued < imported <= min(issued + 1800000, expires))
    _check(expires <= issued + 30 * 86400000)
    for pin in value['pins']:
        _check(pin['not_before'] <= issued and expires <= pin['expires_at'])
    # A read can return an authority-current bundle after its import window.
    # Caller separately checks import eligibility, independent policy/head/root,
    # previous generation, authenticated issuer/channel, and enrollment deadline.


def _request(definition, value, now):
    admin = definition.endswith('AdminRequest')
    kind = definition.removesuffix('AdminRequest' if admin else 'Request')
    _check(kind in _REQUEST_FIELDS and (not admin or kind in ('Issue', 'Read', 'Revoke')))
    fields = _REQUEST_FIELDS[kind]
    extra = ('device_id', 'expected_provision_id') if kind == 'Issue' else ()
    _fields(value, (*fields, 'schema_version', *extra, *(('csrf',) if admin else ())))
    _members(value, fields, now)
    _check(value['schema_version'] == 'sdc.provision.' + kind.lower() + '.v1')
    if admin:
        _validate('CSRF', value['csrf'], now)
    if kind == 'Issue':
        if value['profile_id'] == 'monitor':
            _check(value['device_id'] is None)
        else:
            _validate('UUID', value['device_id'], now)
        if value['expected_generation'] == 0:
            _check(value['expected_provision_id'] is None)
        else:
            _validate('UUID', value['expected_provision_id'], now)
    if kind == 'Revoke':
        _check(value['expected_generation'] > 0)


def _hash(value):
    return hashlib.sha256(canonical(value, max_bytes=MAX_BODY).encode('ascii')).hexdigest()


def _result(definition, value, now):
    response = definition.endswith('Response')
    kind = definition.removesuffix('Response' if response else 'Result')
    _check(kind in _RESULT_FIELDS)
    fields = _RESULT_FIELDS[kind]
    _fields(value, (*fields, 'schema_version', *(('bundle',) if kind == 'Read' else ()),
                    *(('request_id',) if response else ())))
    _members(value, fields, now)
    _check(value['schema_version'] == 'sdc.provision.' + kind.lower() + '-response.v1')
    if response:
        _validate('UUID', value['request_id'], now)
    if kind == 'Issue':
        _check(value['bundle_hash'] == _hash(value['bundle']))
    if kind == 'Read':
        if value['status']['state'] == 'current':
            _bundle(value['bundle'], now)
            _check(value['status']['bundle_hash'] == _hash(value['bundle']))
        else:
            _check(value['bundle'] is None)


def _validate(definition, value, now):
    if definition in _PATTERNS:
        _match(value, _PATTERNS[definition])
    elif definition in ('Timestamp', 'Epoch', 'Generation', 'ExpectedGeneration'):
        _integer(value, 1 if definition == 'Generation' else 0,
                 MAX_GENERATION if definition == 'Generation' else
                 MAX_GENERATION - 1 if definition == 'ExpectedGeneration' else MAX_SAFE_INTEGER)
    elif definition == 'Origin':
        _origin(value)
    elif definition == 'ChannelRef':
        _check(value == CHANNEL)
    elif definition == 'ProfileID':
        _check(type(value) is str and value in _PROFILES)
    elif definition in ('SupportPin', 'RestartPin'):
        _pin(value, definition.removesuffix('Pin').lower(), now)
    elif definition in ('SupportPins', 'RestartPins'):
        _pins(value, definition.removesuffix('Pins').lower(), now)
    elif definition in ('MonitorProfile', 'SupportProfile', 'RestartProfile'):
        profile = definition.removesuffix('Profile').lower()
        _fields(value, ('profile_id', 'origin', 'audience', 'pins'))
        _check(value['profile_id'] == profile)
        _origin(value['origin'])
        _validate('Audience', value['audience'], now)
        _pins(value['pins'], profile, now)
    elif definition == 'Policy':
        _fields(value, ('schema_version', 'issuer_ref', 'policy_revision', 'channel_ref', 'profiles'))
        _check(value['schema_version'] == 'sdc.provision.policy.v1')
        _members(value, {'issuer_ref': 'IssuerRef', 'policy_revision': 'UUID', 'channel_ref': 'ChannelRef'}, now)
        _check(type(value['profiles']) is list and 1 <= len(value['profiles']) <= 3)
        seen = set()
        for profile in value['profiles']:
            _check(type(profile) is dict)
            name = profile.get('profile_id')
            _validate('ProfileID', name, now)
            _check(name not in seen)
            seen.add(name)
            _validate(name.title() + 'Profile', profile, now)
    elif definition == 'Bundle':
        _bundle(value, now)
    elif definition == 'Receipt':
        _fields(value, _RECEIPT_FIELDS)
        _members(value, _RECEIPT_FIELDS, now)
    elif definition == 'Status':
        _fields(value, (*_STATUS_FIELDS, 'state', 'receipt'))
        _members(value, _STATUS_FIELDS, now)
        _check(value['state'] in ('current', 'superseded', 'revoked', 'expired'))
        _nullable('Receipt', value['receipt'], now)
    elif definition == 'ErrorResponse':
        _fields(value, ('code', 'request_id'))
        _match(value['code'], r'[A-Z_]{1,64}')
        _validate('UUID', value['request_id'], now)
    elif definition.endswith('Request'):
        _request(definition, value, now)
    elif definition.endswith(('Result', 'Response')):
        _result(definition, value, now)
    else:
        raise ProvisionError()


def validate_provision(definition, value, *, now_ms):
    """Return a detached fixed-contract value, not authenticated provision authority.

    All fixed schema definitions are supported. Local checks also require every
    supplied pin to be current, strictly decoded RSA3072/e65537, purpose-separated,
    and uniquely named; bundle lifetime caps and embedded bundle hashes are checked.
    SHA256 is the existing signature verifier's fixed algorithm, not a bundle choice.
    Policy/head/CAS, revocation, actual root, import eligibility, request/receipt
    binding, issuer/channel authenticity and human consent remain caller checks.
    Historical Status/Receipt metadata is not made current by this function.
    """
    try:
        _check(type(definition) is str)
        _integer(now_ms)
        snapshot = parse_canonical(canonical(value, max_bytes=MAX_BODY), max_bytes=MAX_BODY)
        _validate(definition, snapshot, now_ms)
        return snapshot
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, ImportError):
        raise ProvisionError() from None


def parse_provision(definition, raw, *, now_ms):
    """Parse bounded strict UTF-8 JSON; whitespace/key order need not be canonical."""
    try:
        _check(type(raw) in (bytes, str))
        _check(len(raw) <= MAX_BODY)
        encoded = raw.encode('utf-8') if type(raw) is str else raw
        _check(len(encoded) <= MAX_BODY)
        text = encoded.decode('utf-8', errors='strict')

        def pairs(items):
            result = {}
            for key, value in items:
                _check(key not in result)
                result[key] = value
            return result

        def integer(number):
            _check(len(number) <= 17)
            value = int(number)
            _check(-MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER)
            return value

        def denied(_):
            raise ProvisionError()

        def number(text):
            # JSON.parse produces binary64 numbers. Preserve its integral-number
            # meaning (1.0/1e0 -> 1). Provision's Number.isSafeInteger checks
            # accept -0 as zero; field minima still apply after normalization.
            value = float(text)
            _check(math.isfinite(value) and value.is_integer()
                   and abs(value) <= MAX_SAFE_INTEGER)
            return int(value)

        value = json.loads(text, object_pairs_hook=pairs, parse_int=integer,
                           parse_float=number, parse_constant=denied)
        return validate_provision(definition, value, now_ms=now_ms)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ProvisionError() from None


def validate_import_bundle(value, *, now_ms):
    """Validate an import candidate's local window, without granting apply authority.

    The caller must still authenticate its independent source, compare the actual
    root/current head/policy and durable receipt, and obtain explicit human consent.
    Successful structural validation or a status response does not supply those facts.
    """
    bundle = validate_provision('Bundle', value, now_ms=now_ms)
    _check(bundle['issued_at'] <= now_ms < bundle['import_expires_at'])
    return bundle
