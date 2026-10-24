"""再起動と初回登録読取りの閉じた要求・応答を検証する（現在権限・実行は別）。

内部APIはplain dictまたは厳密ASCII CJSONを受け取る。HTTP request_idは
サーバーの追跡IDであり、冪等IDや本人同意の証拠ではない。通信や有効化は行わない。
"""
from __future__ import annotations

import base64
import binascii
import re

from . import restart_contract as restart
from .support_contract import MAX_SAFE_INTEGER, canonical, parse_canonical

INSPECTION = '/v2/support/restarts/inspections/report'
PREVIEW = '/v2/support/restarts/cases/preview'
ACCEPT = '/v2/support/restarts/cases/accept'
POLL = '/v2/support/restarts/requests/poll'
ADMIT = '/v2/support/restarts/operations/admit'
GRANT = '/v2/support/restarts/operations/grant'
REPORT = '/v2/support/restarts/operations/report'
STATUS = '/v2/support/restarts/operations/status'
REGISTRATION_READ = '/v2/support/restarts/services/read'
_KINDS = dict(zip((INSPECTION, PREVIEW, ACCEPT, POLL, ADMIT, GRANT, REPORT, STATUS),
                  ('inspection', 'preview', 'accept', 'poll', 'admit', 'grant', 'report', 'status')))
PATHS = (*_KINDS, REGISTRATION_READ)
_RESPONSES = {
    INSPECTION: ('inspection_id', 'state', 'expires_at', 'registration'),
    PREVIEW: ('preview_id', 'preview', 'registration'),
    ACCEPT: ('accept_id', 'preview_id', 'operation_id', 'change_consent_id', 'change_binding_hash',
             'local_change_receipt_id', 'state', 'expires_at', 'accepted_at', 'preview_payload_hash', 'local_change_receipt_hash'),
    POLL: ('operation', 'approval', 'acceptance', 'registration'),
    ADMIT: ('operation_id', 'receipt_id', 'state'),
    GRANT: ('grant',),
    REPORT: ('operation_id', 'receipt_id', 'state', 'state_revision'),
    STATUS: ('operation_id', 'receipt_id', 'report_id', 'report', 'operation'),
    REGISTRATION_READ: ('case_id', 'local_receipt_id', 'registration'),
}
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
_REPORT_STATES = ('running', 'succeeded', 'failed', 'unknown')
_OPERATION_STATES = ('proposed', 'approved', 'queued', 'locally_admitted', 'grant_consumed',
                     'running', 'succeeded', 'failed', 'unknown', 'canceled', 'expired', 'denied')


def _check(ok):
    if not ok:
        raise ValueError('SDC_RESTART_CONTROL_INVALID')


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_SAFE_INTEGER


def _matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _fields(value, names):
    _check(type(value) is dict and set(value) == set(names))


def _input(value):
    return parse_canonical(value if type(value) in (str, bytes) else canonical(value, max_bytes=16384), max_bytes=16384)


def _bind(left, right, keys):
    for key in keys:
        _check(canonical(left[key]) == canonical(right[key]))


def _base64(value):
    _check(_matches(value, r'[A-Za-z0-9_-]+'))
    raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    _check(base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') == value)
    return raw


def _envelope(value, purpose, now_ms):
    _fields(value, ('alg', 'kid', 'purpose', 'payload', 'signature'))
    _check(value['alg'] == 'PS256' and value['purpose'] == purpose
           and _matches(value['kid'], r'[A-Za-z0-9_-]{1,64}')
           and type(value['payload']) is str and 1 <= len(value['payload']) <= 10923
           and _matches(value['signature'], r'[A-Za-z0-9_-]{512}'))
    _check(len(_base64(value['signature'])) == 384)
    payload = parse_canonical(_base64(value['payload']), max_bytes=8192)
    return restart.validate_typed_payload(purpose, payload, now_ms=now_ms)


def _registration(value, now_ms):
    return restart.validate_restart_registration(value, now_ms=now_ms, allow_expired=True)


def _registered_target(registration, payload):
    # Mutual consistency of server records is not independent current authority.
    _bind(registration, payload, ('product', 'tenant', 'installation', 'device_id', 'environment',
                                  'realm_epoch', 'policy_epoch', 'device_epoch', 'service_revision'))
    _check(registration['service_id'] == payload['params']['service_id']
           and registration['effects_profile'] == payload['params']['effects_profile'])


def validate_request(path, value, *, now_ms, allow_historical=False):
    """Validate a new request, or explicitly inspect immutable historical request bytes.

    allow_historical only relaxes elapsed preflight freshness, never future time.
    The caller must prove identical persisted ID/raw bytes and current authority;
    this option itself grants no replay or execution permission. Business guards
    such as rejecting failed/null reports remain central-handler responsibilities.
    """
    try:
        _check(type(path) is str and path in PATHS and _integer(now_ms) and type(allow_historical) is bool)
        value = _input(value)
        if path == REGISTRATION_READ:
            _fields(value, ('schema_version', 'case_id', 'local_receipt_id'))
            _check(value['schema_version'] == 'sdc.support.restart-registration-read.v1'
                   and all(_matches(value[k], _UUID) for k in ('case_id', 'local_receipt_id')))
            return value
        validation_time = now_ms
        if allow_historical and path in (INSPECTION, GRANT):
            observed = value['preflight']['observed_at']
            _check(_integer(observed) and observed <= now_ms)
            validation_time = observed
        return restart.validate_restart_control(_KINDS[path], value, now_ms=validation_time)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ValueError('SDC_RESTART_CONTROL_REQUEST_INVALID') from None


def validate_response(path, value, *, request, now_ms):
    """Validate response shape, typed hashes and request echoes without verifying signatures.

    HTTP request_id is preserved. For immutable stored accept ACK, remove only
    request_id and pass that record to validate_restart_acceptance. No status
    report=null observation establishes execution, failure or permission to retry.
    Original request preflight freshness and immutable accept ACK/registration
    expiry are historical shape checks. Future timestamps remain rejected.
    Signed preview/operation/grant and approval windows are checked at now_ms;
    even a currently valid DTO is not independent current permission.
    Registration read requires a current active registration. Its service_revision
    is an input for the caller's initial baseline comparison, never an automatic
    migration of expected values or evidence of planning/change consent.
    """
    try:
        request = validate_request(path, request, now_ms=now_ms, allow_historical=True)
        value = _input(value)
        _fields(value, ('schema_version', 'request_id', *_RESPONSES[path]))
        _check(_matches(value['request_id'], _UUID))
        schema = {ACCEPT: 'sdc.support.restart-acceptance.v1',
                  REGISTRATION_READ: 'sdc.support.restart-registration-response.v1'}
        _check(value['schema_version'] == schema.get(path, 'sdc.support.restart-response.v1'))
        for key in _RESPONSES[path]:
            if key.endswith('_id'):
                _check(_matches(value[key], _UUID))
                if key in request:
                    _check(value[key] == request[key])
        if path == REGISTRATION_READ:
            registration = restart.validate_restart_registration(value['registration'], now_ms=now_ms)
            _check(registration['state'] == 'active')
        elif path == INSPECTION:
            _check(value['state'] == 'recorded' and _integer(value['expires_at']))
            reg = _registration(value['registration'], now_ms)
            _check(reg['service_revision'] == request['service_revision'])
            _bind(reg, request['preflight'], ('product', 'tenant', 'installation', 'device_id', 'environment', 'service_id'))
        elif path == PREVIEW:
            payload = _envelope(value['preview'], 'restart-consent-preview', now_ms)
            _bind(payload, request, ('preview_id', 'operation_id'))
            _registered_target(_registration(value['registration'], now_ms), payload)
        elif path == ACCEPT:
            ack = restart.validate_restart_acceptance({k: v for k, v in value.items() if k != 'request_id'}, now_ms=now_ms, allow_expired=True)
            _bind(ack, request, ('accept_id', 'preview_id', 'change_consent_id', 'change_binding_hash',
                                 'local_change_receipt_id', 'preview_payload_hash', 'local_change_receipt_hash'))
        elif path == POLL:
            records = tuple(value[k] for k in ('operation', 'approval', 'acceptance', 'registration'))
            _check(all(v is None for v in records) or all(v is not None for v in records))
            if value['operation'] is not None:
                op = _envelope(value['operation'], 'restart-operation', now_ms)
                _check(op['case_id'] == request['case_id'])
                approval = restart.validate_restart_approval(value['approval'], now_ms=now_ms)
                _check(approval['approval_hash'] == restart.hash_restart_approval(approval))
                _bind(op, approval, (*restart.EXPECTED_FIELDS, 'product', 'capability_revision', 'consent_revision',
                                     'approval_id', 'approval_hash', 'intent_hash', 'approved_by'))
                ack = restart.validate_restart_acceptance(value['acceptance'], now_ms=now_ms, allow_expired=True)
                _check(op['change_accept_id'] == ack['accept_id'])
                _bind(op, ack, ('operation_id', 'preview_id', 'change_consent_id', 'change_binding_hash',
                                'local_change_receipt_id', 'preview_payload_hash', 'local_change_receipt_hash'))
                _registered_target(_registration(value['registration'], now_ms), op)
        elif path == ADMIT:
            _check(value['state'] == 'locally_admitted')
        elif path == GRANT:
            grant = _envelope(value['grant'], 'restart-execution-grant', now_ms)
            _bind(grant, request, ('grant_request_id', 'operation_id', 'payload_hash', 'receipt_id', 'admission_id'))
            _bind(grant, request['preflight'], ('product', 'tenant', 'installation', 'device_id', 'environment', 'case_id'))
            _check(grant['params']['expected_runtime_generation'] == request['preflight']['runtime_generation'])
        elif path == REPORT:
            _check(value['state'] in _REPORT_STATES and _integer(value['state_revision'], 1))
            _bind(value, request, ('state', 'state_revision'))
        else:
            operation = value['operation']
            _fields(operation, ('state', 'state_revision'))
            _check(operation['state'] in _OPERATION_STATES and _integer(operation['state_revision']))
            if value['report'] is not None:
                report = value['report']
                _fields(report, ('body_hash', 'state', 'state_revision'))
                _check(_matches(report['body_hash'], r'[0-9a-f]{64}') and report['state'] in _REPORT_STATES
                       and _integer(report['state_revision'], 1))
        return value
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, binascii.Error):
        raise ValueError('SDC_RESTART_CONTROL_RESPONSE_INVALID') from None
