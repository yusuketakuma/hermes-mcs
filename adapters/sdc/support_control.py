"""R4a操作5POSTの閉じたDTOだけを検証する（署名・認可・送信は別）。"""
from __future__ import annotations

import base64
import binascii
import re

from .support_contract import (CAPABILITY_REVISION, DIAGNOSTIC_IDS, MAX_SAFE_INTEGER,
                               canonical, parse_canonical, validate_approval)

POLL = '/v2/support/requests/poll'
ADMIT = '/v2/support/operations/admit'
GRANT = '/v2/support/operations/grant'
REPORT = '/v2/support/operations/report'
STATUS = '/v2/support/operations/status'
_REQUESTS = {
    POLL: ('poll_id', 'case_id', 'capability_revision', 'local_consent_epoch'),
    ADMIT: ('admission_id', 'operation_id', 'payload_hash', 'receipt_id', 'local_consent_epoch', 'owner_generation'),
    GRANT: ('grant_request_id', 'operation_id', 'payload_hash', 'receipt_id', 'admission_id'),
    REPORT: ('report_id', 'operation_id', 'payload_hash', 'receipt_id', 'state_revision', 'state', 'observed_at', 'result'),
    STATUS: ('status_id', 'operation_id', 'payload_hash', 'receipt_id', 'report_id'),
}
_RESPONSES = {
    POLL: ('poll_id', 'operation', 'approval', 'request_id'),
    ADMIT: ('operation_id', 'receipt_id', 'state', 'request_id'),
    GRANT: ('operation_id', 'state', 'grant', 'request_id'),
    REPORT: ('operation_id', 'receipt_id', 'state', 'state_revision', 'request_id'),
    STATUS: ('operation_id', 'receipt_id', 'report_id', 'report', 'operation', 'request_id'),
}
_UUID_FIELDS = frozenset(('poll_id', 'case_id', 'admission_id', 'operation_id', 'receipt_id',
                          'grant_request_id', 'report_id', 'status_id', 'request_id'))
_REPORT_STATES = ('running', 'succeeded', 'failed', 'unknown')
_OPERATION_STATES = ('proposed', 'approved', 'queued', 'locally_admitted', 'grant_consumed',
                     'running', 'succeeded', 'failed', 'unknown', 'canceled', 'expired', 'denied')
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
_VERSION = r'(?:[0-9]{1,8}!)?[0-9]{1,8}(?:\.[0-9]{1,8}){0,7}(?:(?:a|b|rc)[0-9]{1,8})?(?:\.post[0-9]{1,8})?(?:\.dev[0-9]{1,8})?'


def _check(ok):
    if not ok:
        raise ValueError('SDC_SUPPORT_CONTROL_INVALID')


def _integer(value, minimum=0, maximum=MAX_SAFE_INTEGER):
    return type(value) is int and minimum <= value <= maximum


def _matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _fields(value, names):
    _check(type(value) is dict and set(value) == set(names))


def _input(value):
    return parse_canonical(value if type(value) in (str, bytes) else canonical(value, max_bytes=16384), max_bytes=16384)


def _common(value, fields):
    _fields(value, ('schema_version', *fields))
    _check(value['schema_version'] == 'sdc.support.v1')
    for name in _UUID_FIELDS & value.keys():
        _check(_matches(value[name], _UUID))
    if 'payload_hash' in value:
        _check(_matches(value['payload_hash'], r'[0-9a-f]{64}'))


def _envelope(value, purpose):
    _fields(value, ('alg', 'kid', 'purpose', 'payload', 'signature'))
    _check(value['alg'] == 'PS256' and value['purpose'] == purpose
           and _matches(value['kid'], r'[A-Za-z0-9_-]{1,64}')
           and _matches(value['payload'], r'[A-Za-z0-9_-]{1,10923}')
           and _matches(value['signature'], r'[A-Za-z0-9_-]{512}'))
    raw = base64.b64decode(value['payload'] + '=' * (-len(value['payload']) % 4), altchars=b'-_', validate=True)
    _check(base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') == value['payload'])
    _check(type(parse_canonical(raw, max_bytes=8192)) is dict)
    # Shape/encoding only: neither cryptography nor operation/grant authorization.


def _diagnostic(value):
    _fields(value, ('schema_version', 'diagnostic_id', 'generated_at', 'observed_at',
                    'status', 'duration_ms', 'error_code', 'data'))
    _check(value['schema_version'] == 'sdc.support.diagnostic-result.v1'
           and value['diagnostic_id'] in DIAGNOSTIC_IDS and _integer(value['generated_at']))
    _check(value['observed_at'] is None or _integer(value['observed_at']) and value['observed_at'] <= value['generated_at'])
    _check(value['status'] in ('observed', 'partial', 'unknown', 'failed')
           and value['error_code'] in ('NONE', 'SOURCE_UNKNOWN', 'SOURCE_STALE', 'SOURCE_UNAVAILABLE', 'DIAGNOSTIC_FAILED')
           and (value['status'] == 'observed') == (value['error_code'] == 'NONE'))
    _check(value['duration_ms'] is None or _integer(value['duration_ms'], maximum=1800000))
    data = value['data']
    if value['diagnostic_id'] == 'runtime.status.v1':
        _fields(data, ('mode', 'process', 'installed_commit', 'loaded_commit', 'installed_version', 'loaded_version'))
        _check(data['mode'] in ('hermes', 'standalone', 'unknown') and data['process'] in ('running', 'stopped', 'unknown'))
        for name in ('installed_commit', 'loaded_commit'):
            _check(data[name] is None or _matches(data[name], r'[0-9a-f]{40}'))
        for name in ('installed_version', 'loaded_version'):
            _check(data[name] is None or type(data[name]) is str and len(data[name]) <= 128 and _matches(data[name], _VERSION))
    elif value['diagnostic_id'] == 'notification.health.v1':
        _fields(data, ('service', 'credential_configured', 'authentication', 'retry'))
        _check(data['service'] in ('running', 'stopped', 'unknown')
               and data['credential_configured'] in ('configured', 'unconfigured', 'unknown')
               and data['authentication'] in ('valid', 'reauth_required', 'unknown') and data['retry'] in ('backoff', 'unknown'))
    else:
        _fields(data, ('target_id', 'connection', 'duration_ms'))
        _check(data['target_id'] == 'sdc-central' and data['connection'] in ('reachable', 'timeout', 'tls_failed', 'unknown'))
        _check(data['duration_ms'] is None or _integer(data['duration_ms'], maximum=1800000))


def validate_request(path, value):
    """Return a detached bounded request DTO; current authorization/idempotency remain external."""
    try:
        _check(type(path) is str and path in _REQUESTS)
        value = _input(value)
        _common(value, _REQUESTS[path])
        if path == POLL:
            _check(value['capability_revision'] == CAPABILITY_REVISION and _integer(value['local_consent_epoch'], 1))
        elif path == ADMIT:
            _check(_integer(value['local_consent_epoch'], 1) and _matches(value['owner_generation'], r'[0-9a-f]{32}'))
        elif path == REPORT:
            _check(_integer(value['state_revision'], 1) and value['state'] in _REPORT_STATES and _integer(value['observed_at'], 1))
            if value['result'] is not None:
                _check(value['state'] == 'succeeded')
                _diagnostic(value['result'])
                _check(value['result']['generated_at'] == value['observed_at'])
            # succeeded/null is valid receipt-only reconciliation after revocation.
            # A currently authorized succeeded report needs content; only runner knows that context.
        return value
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, binascii.Error):
        raise ValueError('SDC_SUPPORT_CONTROL_REQUEST_INVALID') from None


def validate_response(path, value, *, request):
    """Validate fixed response shape/echo IDs; null status report remains unknown evidence.

    Approval validation uses its own not_before for historical shape/hash only.
    Signature, current time, full operation binding and receipt authorization are
    separate runner checks. Server request_id is not the client's idempotency ID.
    """
    try:
        request = validate_request(path, request)
        value = _input(value)
        _common(value, _RESPONSES[path])
        for name in ('poll_id', 'operation_id', 'receipt_id', 'report_id'):
            if name in value:
                _check(value[name] == request[name])
        if path == POLL:
            _check((value['operation'] is None) == (value['approval'] is None))
            if value['operation'] is not None:
                _envelope(value['operation'], 'operation')
                approval = validate_approval(value['approval'], now_ms=value['approval']['not_before'])
                _check(approval['approval_kind'] == 'individual')
        elif path == ADMIT:
            _check(value['state'] == 'locally_admitted')
        elif path == GRANT:
            _check(value['state'] in _OPERATION_STATES)
            if value['grant'] is not None:
                _envelope(value['grant'], 'execution-grant')
        elif path == REPORT:
            _check(value['state'] in _REPORT_STATES and _integer(value['state_revision'], 1)
                   and value['state'] == request['state'] and value['state_revision'] == request['state_revision'])
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
        raise ValueError('SDC_SUPPORT_CONTROL_RESPONSE_INVALID') from None
