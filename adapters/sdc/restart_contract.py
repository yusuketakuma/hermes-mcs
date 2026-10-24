"""再起動候補の型と束縛を純粋に検証する（署名・現在権限・実行は別）。

独立したtrusted expected、永続した本人同意、現在登録、owner由来の因果証拠を
呼出元が別途検証する。内部APIの入力はplain dictまたは厳密なASCII CJSONだけで、
HTTP一般JSONのparserではない。時刻は明示入力だけを使い、I/Oや有効化は行わない。
"""
from __future__ import annotations

import hashlib
import re

from adapters.sdc.support_contract import MAX_SAFE_INTEGER, PURPOSE_CODES, canonical, parse_canonical

RESTART_PURPOSES = ('restart-operation', 'restart-execution-grant', 'restart-consent-preview')
EXPECTED_FIELDS = ('audience', 'tenant', 'installation', 'device_id', 'environment', 'case_id',
                   'claim_id', 'key_id', 'realm_epoch', 'policy_epoch', 'device_epoch',
                   'case_epoch', 'local_consent_epoch', 'service_revision', 'params')
_COMMON = ('product', *EXPECTED_FIELDS, 'capability_revision', 'consent_revision')
_SCOPE = ('tenant', 'installation', 'device_id', 'environment', 'case_id')
_EPOCHS = ('realm_epoch', 'policy_epoch', 'device_epoch', 'case_epoch', 'local_consent_epoch', 'service_revision')
_TIMES = ('issued_at', 'not_before', 'expires_at')
_INTENT = (*_COMMON, 'operation_id', 'type', 'purpose_code', 'requested_by', 'inspection_id',
           'inspection_hash', 'completion_timeout_ms', *_TIMES)
_APPROVAL = (*_COMMON, 'approval_id', 'intent_hash', 'approved_by', 'approval_kind', *_TIMES, 'approval_hash')
_OP_ADDED = ('intent_hash', 'approval_id', 'approval_hash', 'approved_by', 'change_consent_id',
             'change_binding_hash', 'local_change_receipt_id', 'change_accept_id', 'preview_id',
             'preview_payload_hash', 'local_change_receipt_hash', 'payload_hash')
_PREVIEW = (*_COMMON, 'operation_id', 'preview_id', 'intent', 'approval', 'intent_hash',
            'change_consent_id', 'change_binding_hash', *_TIMES)
_GRANT_BINDING = (*_COMMON, 'operation_id', 'intent_hash', 'payload_hash', 'approval_hash',
                  'change_consent_id', 'change_binding_hash', 'local_change_receipt_id',
                  'change_accept_id', 'preview_id', 'preview_payload_hash', 'local_change_receipt_hash')
_GRANT = (*_GRANT_BINDING, 'grant_id', 'grant_request_id', 'receipt_id', 'admission_id', 'owner_generation', *_TIMES)
_PREFLIGHT = ('product', *_SCOPE, 'service_id', 'runtime_generation', 'started_at', 'observed_at',
              'owner_alive', 'update_in_progress', 'command_intake_in_progress', 'restarting')
_REGISTRATION = ('product', 'tenant', 'installation', 'device_id', 'environment', 'service_id',
                 'effects_profile', 'service_revision', 'state', 'realm_epoch', 'policy_epoch',
                 'device_epoch', 'causal_evidence_supported', 'not_before', 'expires_at')
_RESULT = ('operation_id', 'payload_hash', 'receipt_id', 'grant_id', 'service_revision', 'params',
           'generated_at', 'start_receipt_id', 'started_at', 'drain_completed_at', 'old_owner_exit_at',
           'handoff_receipt_id', 'handoff_parent_receipt_id', 'successor_runtime_generation',
           'supervisor_launch_id', 'successor_launch_id', 'ready_observed_at', 'readiness', 'error_code')
_ACCEPTANCE = ('accept_id', 'preview_id', 'operation_id', 'change_consent_id', 'change_binding_hash',
               'local_change_receipt_id', 'preview_payload_hash', 'local_change_receipt_hash',
               'state', 'accepted_at', 'expires_at')
_LOCAL_RECEIPT = (*_COMMON, 'operation_id', 'intent_hash', 'approval_hash', 'change_consent_id',
                  'change_binding_hash', 'preview_id', 'preview_payload_hash', 'local_change_receipt_id',
                  'change_accept_id', 'confirmed_at', 'expires_at', 'receipt_hash')
_CONTROLS = {
    'inspection': ('inspection_id', 'case_id', 'service_revision', 'local_planning_consent_id', 'preflight'),
    'preview': ('preview_id', 'operation_id'),
    'accept': ('accept_id', 'preview_id', 'change_consent_id', 'change_binding_hash',
               'local_change_receipt_id', 'preview_payload_hash', 'local_change_receipt_hash'),
    'poll': ('poll_id', 'case_id'),
    'admit': ('admission_id', 'operation_id', 'payload_hash', 'receipt_id', 'owner_generation'),
    'grant': ('grant_request_id', 'operation_id', 'payload_hash', 'receipt_id', 'admission_id', 'preflight'),
    'report': ('report_id', 'operation_id', 'payload_hash', 'receipt_id', 'state_revision', 'state', 'observed_at', 'result'),
    'status': ('status_id', 'operation_id', 'payload_hash', 'receipt_id', 'report_id'),
}
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
_GENERATION = r'[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}'
_REG_BINDING = ('product', 'tenant', 'installation', 'device_id', 'environment', 'realm_epoch',
                'policy_epoch', 'device_epoch', 'service_revision')
_ADMISSION = ('receipt_id', 'admission_id', 'owner_generation')
_EVIDENCE = ('start_receipt_id', 'started_at', 'drain_completed_at', 'old_owner_exit_at',
             'handoff_receipt_id', 'handoff_parent_receipt_id', 'successor_runtime_generation',
             'supervisor_launch_id', 'successor_launch_id', 'ready_observed_at')
_ZERO = '0' * 64


def _check(ok, code='SUPPORT_INVALID_RESTART_CONTRACT'):
    if not ok:
        raise ValueError(code)


def _conflict(ok):
    _check(ok, 'SUPPORT_RESTART_BINDING_CONFLICT')


def _match(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _integer(value):
    return type(value) is int and 0 <= value <= MAX_SAFE_INTEGER


def _positive(value):
    return _integer(value) and value > 0


def _hex(value):
    return _match(value, r'[0-9a-f]{64}')


def _input(value, limit=8192):
    try:
        v = parse_canonical(value, max_bytes=limit) if type(value) in (bytes, str) else parse_canonical(canonical(value, max_bytes=limit), max_bytes=limit)
        _check(type(v) is dict)
        return v
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError('SUPPORT_INVALID_RESTART_CONTRACT') from None


def _fields(v, names):
    _check(type(v) is dict and set(v) == {'schema_version', *names})


def _record(value, names, schema, limit=8192):
    v = _input(value, limit)
    _fields(v, names)
    _check(v['schema_version'] == 'sdc.support.restart-' + schema + '.v1')
    return v


def _same(a, b):
    return canonical(a, max_bytes=16384) == canonical(b, max_bytes=16384)


def _bind(a, b, keys):
    for key in keys:
        _conflict(_same(a[key], b[key]))


def _scope(v, case=True):
    for key in ('tenant', 'installation'):
        _check(_match(v[key], r'[a-z0-9][a-z0-9_-]{0,63}'))
    _check(_match(v['device_id'], _UUID) and _match(v['environment'], r'[a-z0-9_-]{1,32}'))
    if case:
        _check(_match(v['case_id'], _UUID))


def _params(v):
    _check(type(v) is dict and set(v) == {'service_id', 'expected_runtime_generation', 'effects_profile'})
    _check(v['service_id'] == 'mcs.standalone' and v['effects_profile'] == 'mcs.standalone.restart.v1'
           and _match(v['expected_runtime_generation'], _GENERATION))


def _common(v):
    _scope(v)
    _params(v['params'])
    _check(v['product'] == 'hermes-mcs' and _match(v['audience'], r'[A-Za-z0-9:_-]{1,100}')
           and _match(v['claim_id'], _UUID) and _match(v['key_id'], _UUID))
    _check(_match(v['realm_epoch'], r'[A-Za-z0-9][A-Za-z0-9:_-]{0,127}'))
    for key in _EPOCHS[1:]:
        _check(_positive(v[key]))
    _check(v['capability_revision'] == 'sdc-support-restart-capabilities-v1'
           and v['consent_revision'] == 'sdc-support-restart-consent-v1')


def _window(start, end, now_ms, allow_expired):
    _check(type(allow_expired) is bool)
    if now_ms is not None:  # Only private hash projections omit the current-time check.
        _check(_integer(now_ms) and start <= now_ms)
        _check(allow_expired or now_ms < end, 'SUPPORT_RESTART_EXPIRED')


def _lifetime(v, limit, now_ms, allow_expired):
    _check(all(_integer(v[k]) for k in _TIMES))
    _check(v['issued_at'] <= v['not_before'] < v['expires_at'] and v['expires_at'] - v['issued_at'] <= limit)
    _window(v['not_before'], v['expires_at'], now_ms, allow_expired)


def _intent_core(v, now_ms, allow_expired):
    _common(v)
    _check(_match(v['operation_id'], _UUID) and v['type'] == 'service.restart'
           and v['purpose_code'] in PURPOSE_CODES and _hex(v['requested_by']))
    _check(_match(v['inspection_id'], _UUID) and _hex(v['inspection_hash']) and v['completion_timeout_ms'] == 900000)
    _lifetime(v, 1800000, now_ms, allow_expired)


def _validate(kind, value, now_ms, allow_expired=False):
    names = {'intent': _INTENT, 'approval': _APPROVAL, 'preview': _PREVIEW,
             'operation': (*_INTENT, *_OP_ADDED), 'grant': _GRANT,
             'registration': _REGISTRATION, 'acceptance': _ACCEPTANCE, 'local_receipt': _LOCAL_RECEIPT}
    schemas = {'preview': 'consent-preview', 'grant': 'execution-grant', 'local_receipt': 'local-change-receipt'}
    v = _record(value, names[kind], schemas.get(kind, kind))
    if kind in ('intent', 'operation'):
        _intent_core(v, now_ms, allow_expired)
    elif kind in ('approval', 'preview', 'grant'):
        _common(v)
        _lifetime(v, {'approval': 1800000, 'preview': 300000, 'grant': 30000}[kind], now_ms, allow_expired)
    if kind == 'approval':
        _check(_match(v['approval_id'], _UUID) and all(_hex(v[k]) for k in ('intent_hash', 'approved_by', 'approval_hash')) and v['approval_kind'] == 'individual')
    elif kind == 'preview':
        _check(all(_match(v[k], _UUID) for k in ('operation_id', 'preview_id', 'change_consent_id')) and _hex(v['intent_hash']) and _hex(v['change_binding_hash']))
        intent = _validate('intent', v['intent'], now_ms, allow_expired)
        approval = _validate('approval', v['approval'], now_ms, allow_expired)
        _approval_binding(intent, approval)
        _bind(v, intent, _COMMON)
        _conflict(v['operation_id'] == intent['operation_id'] and v['intent_hash'] == approval['intent_hash']
                  and v['issued_at'] >= approval['issued_at'] and v['not_before'] >= approval['not_before']
                  and v['expires_at'] <= min(intent['expires_at'], approval['expires_at']))
    elif kind in ('operation', 'grant'):
        ids = ('approval_id', 'change_consent_id', 'local_change_receipt_id', 'change_accept_id', 'preview_id') if kind == 'operation' else ('operation_id', 'change_consent_id', 'local_change_receipt_id', 'change_accept_id', 'preview_id', 'grant_id', 'grant_request_id', 'receipt_id', 'admission_id')
        hashes = ('intent_hash', 'approval_hash', 'change_binding_hash', 'payload_hash', 'preview_payload_hash', 'local_change_receipt_hash')
        _check(all(_match(v[k], _UUID) for k in ids) and all(_hex(v[k]) for k in hashes))
        _check(_hex(v['approved_by']) if kind == 'operation' else _match(v['owner_generation'], r'[0-9a-f]{32}'))
    elif kind == 'registration':
        _scope(v, False)
        _check(v['product'] == 'hermes-mcs' and v['service_id'] == 'mcs.standalone' and v['effects_profile'] == 'mcs.standalone.restart.v1'
               and v['state'] in ('active', 'revoked') and type(v['causal_evidence_supported']) is bool)
        _check(all(_positive(v[k]) for k in ('service_revision', 'policy_epoch', 'device_epoch')) and _match(v['realm_epoch'], r'[A-Za-z0-9][A-Za-z0-9:_-]{0,127}'))
        _check(_integer(v['not_before']) and _integer(v['expires_at']) and v['not_before'] < v['expires_at'])
        _window(v['not_before'], v['expires_at'], now_ms, allow_expired)
    elif kind in ('acceptance', 'local_receipt'):
        if kind == 'local_receipt':
            _common(v)
        ids = ('accept_id', 'preview_id', 'operation_id', 'change_consent_id', 'local_change_receipt_id') if kind == 'acceptance' else ('operation_id', 'change_consent_id', 'preview_id', 'local_change_receipt_id', 'change_accept_id')
        hashes = ('change_binding_hash', 'preview_payload_hash', 'local_change_receipt_hash') if kind == 'acceptance' else ('intent_hash', 'approval_hash', 'change_binding_hash', 'preview_payload_hash', 'receipt_hash')
        _check(all(_match(v[k], _UUID) for k in ids) and all(_hex(v[k]) for k in hashes))
        if kind == 'acceptance':
            _check(v['state'] == 'accepted')
        start = v['accepted_at'] if kind == 'acceptance' else v['confirmed_at']
        _check(_integer(start) and _integer(v['expires_at']) and start < v['expires_at'])
        _window(start, v['expires_at'], now_ms, allow_expired)
    return v


def _approval_binding(intent, approval):
    _bind(intent, approval, _COMMON)
    _conflict(approval['issued_at'] >= intent['issued_at'] and approval['not_before'] >= intent['not_before'] and approval['expires_at'] <= intent['expires_at'])


def validate_restart_record(kind, value, *, now_ms, allow_expired=False):
    """Validate one unsigned record's shape/time only; hashes and authority need separate checks."""
    _check(_integer(now_ms) and type(kind) is str)
    if kind == 'result':
        _check(type(allow_expired) is bool)
        return validate_restart_result(value, now_ms=now_ms)
    _check(kind in ('intent', 'approval', 'preview', 'operation', 'grant', 'registration', 'acceptance', 'local_receipt'))
    return _validate(kind, value, now_ms, allow_expired)


def validate_restart_intent(value, *, now_ms, allow_expired=False):
    """Validate intent shape/time; authenticity and authority remain external."""
    return validate_restart_record('intent', value, now_ms=now_ms, allow_expired=allow_expired)


def validate_restart_approval(value, *, now_ms, allow_expired=False):
    """Validate approval shape/time; authenticity and authority remain external."""
    return validate_restart_record('approval', value, now_ms=now_ms, allow_expired=allow_expired)


def validate_restart_preview(value, *, now_ms, allow_expired=False):
    """Validate preview shape/time; authenticity and authority remain external."""
    return validate_restart_record('preview', value, now_ms=now_ms, allow_expired=allow_expired)


def validate_restart_operation(value, *, now_ms, allow_expired=False):
    """Validate operation shape/time; authenticity and authority remain external."""
    return validate_restart_record('operation', value, now_ms=now_ms, allow_expired=allow_expired)


def validate_restart_grant(value, *, now_ms, allow_expired=False):
    """Validate grant shape/time; authenticity and authority remain external."""
    return validate_restart_record('grant', value, now_ms=now_ms, allow_expired=allow_expired)


def validate_restart_registration(value, *, now_ms, allow_expired=False):
    """Validate registration shape/time; authenticity and authority remain external."""
    return validate_restart_record('registration', value, now_ms=now_ms, allow_expired=allow_expired)


def validate_restart_acceptance(value, *, now_ms, allow_expired=False):
    """Validate acceptance shape/time; authenticity and authority remain external."""
    return validate_restart_record('acceptance', value, now_ms=now_ms, allow_expired=allow_expired)


def validate_restart_local_receipt(value, *, now_ms, allow_expired=False):
    """Validate local receipt shape/time; authenticity and authority remain external."""
    return validate_restart_record('local_receipt', value, now_ms=now_ms, allow_expired=allow_expired)


def _intent_from(v):
    return {'schema_version': 'sdc.support.restart-intent.v1', **{k: v[k] for k in _INTENT}}


def _digest(v):
    return hashlib.sha256(canonical(v).encode('ascii')).hexdigest()


def hash_restart_intent(value):
    """Hash a shape-validated intent without using a clock."""
    return _digest(_validate('intent', value, None))


def _hash_without(kind, value, field):
    v = _input(value)
    v[field] = _ZERO
    v = _validate(kind, v, None)
    del v[field]
    return _digest(v)


def hash_restart_approval(value):
    """Hash approval except its own hash; no signature or current authority is established."""
    return _hash_without('approval', value, 'approval_hash')


def hash_restart_payload(value):
    """Hash operation except its own payload_hash."""
    return _hash_without('operation', value, 'payload_hash')


def hash_restart_preview(value):
    """Hash the entire verified-shape preview payload, including its transient ID and window."""
    return _digest(_validate('preview', value, None))


def hash_restart_local_receipt(value):
    """Hash local receipt except receipt_hash; persistence and human confirmation remain external."""
    return _hash_without('local_receipt', value, 'receipt_hash')


def hash_restart_change_binding(value):
    """Hash the noncircular stable intent/approval/change/credential-ID projection."""
    v = _input(value)
    v['change_binding_hash'] = _ZERO
    v = _validate('preview' if v.get('schema_version') == 'sdc.support.restart-consent-preview.v1' else 'operation', v, None)
    return _digest({'schema_version': 'sdc.support.restart-change-binding.v1', 'intent_hash': v['intent_hash'],
                    'approval_hash': v['approval']['approval_hash'] if 'approval' in v else v['approval_hash'],
                    'change_consent_id': v['change_consent_id'], 'claim_id': v['claim_id'], 'key_id': v['key_id']})


def validate_typed_payload(purpose, value, *, now_ms, allow_expired=False):
    """Validate the closed restart signature purpose and hash DAG, not signature or permission."""
    _check(type(purpose) is str and purpose in RESTART_PURPOSES, 'SUPPORT_SIGNATURE_PURPOSE_DENIED')
    _check(_integer(now_ms))
    kind = {'restart-operation': 'operation', 'restart-execution-grant': 'grant', 'restart-consent-preview': 'preview'}[purpose]
    v = _validate(kind, value, now_ms, allow_expired)
    if kind == 'operation':
        _conflict(v['intent_hash'] == hash_restart_intent(_intent_from(v)))
        _conflict(v['change_binding_hash'] == hash_restart_change_binding(v))
        _conflict(v['payload_hash'] == hash_restart_payload(v))
    elif kind == 'preview':
        _conflict(v['intent_hash'] == hash_restart_intent(v['intent']))
        _conflict(v['approval']['approval_hash'] == hash_restart_approval(v['approval']))
        _conflict(v['change_binding_hash'] == hash_restart_change_binding(v))
    return v


def _complete_result(v):
    return v['error_code'] == 'NONE' and v['readiness'] == 'ready' and all(v[k] is not None for k in _EVIDENCE)


def validate_restart_result(value, *, now_ms):
    """Validate result structure only; matching IDs are not authentic native causal evidence."""
    v = _record(value, _RESULT, 'result', 16384)
    _check(_match(v['operation_id'], _UUID) and _hex(v['payload_hash']) and _match(v['receipt_id'], _UUID)
           and _match(v['grant_id'], _UUID) and _positive(v['service_revision']))
    _params(v['params'])
    _check(_integer(v['generated_at']) and _integer(now_ms) and v['generated_at'] <= now_ms)
    for key in ('start_receipt_id', 'handoff_receipt_id', 'handoff_parent_receipt_id', 'supervisor_launch_id', 'successor_launch_id'):
        _check(v[key] is None or _match(v[key], _UUID))
    previous = 0
    for key in ('started_at', 'drain_completed_at', 'old_owner_exit_at', 'ready_observed_at'):
        if v[key] is not None:
            _check(_integer(v[key]) and previous <= v[key] <= v['generated_at'])
            previous = v[key]
    _check(v['started_at'] is None or v['start_receipt_id'] is not None)
    _check(v['successor_runtime_generation'] is None or (_match(v['successor_runtime_generation'], _GENERATION)
           and v['successor_runtime_generation'] != v['params']['expected_runtime_generation']))
    _check((v['handoff_receipt_id'] is None) == (v['handoff_parent_receipt_id'] is None))
    if v['handoff_receipt_id'] is not None:
        _check(v['handoff_parent_receipt_id'] == v['start_receipt_id'] and v['successor_runtime_generation'] is not None)
    if v['successor_launch_id'] is not None:
        _check(v['successor_launch_id'] == v['supervisor_launch_id'] and v['successor_runtime_generation'] is not None)
    _check(v['ready_observed_at'] is None or v['successor_runtime_generation'] is not None)
    _check(v['readiness'] in ('unknown', 'ready', 'not_ready') and v['error_code'] in
           ('NONE', 'IN_PROGRESS', 'PRECHECK_FAILED', 'START_WINDOW_EXPIRED', 'DRAIN_INCOMPLETE', 'HANDOFF_UNKNOWN', 'READINESS_FAILED', 'RESULT_UNKNOWN'))
    _check(v['error_code'] != 'NONE' or _complete_result(v))
    return v


def _preflight(value, now_ms, *, expected=None, params=None):
    v = _record(value, _PREFLIGHT, 'preflight')
    _scope(v)
    _check(v['product'] == 'hermes-mcs' and v['service_id'] == 'mcs.standalone' and _match(v['runtime_generation'], _GENERATION))
    _check(_integer(v['started_at']) and _integer(v['observed_at']) and _integer(now_ms))
    for k in ('owner_alive', 'update_in_progress', 'command_intake_in_progress', 'restarting'):
        _check(type(v[k]) is bool)
    if expected is not None:
        _check(all(v[k] == expected[k] for k in _SCOPE) and v['runtime_generation'] == params['expected_runtime_generation'], 'SUPPORT_RESTART_TARGET_CONFLICT')
    _check(v['started_at'] <= v['observed_at'] <= now_ms and now_ms - v['observed_at'] <= 15000,
           'SUPPORT_RESTART_STALE' if expected is not None else 'SUPPORT_INVALID_RESTART_CONTRACT')
    if expected is not None:
        _check(v['owner_alive'] and not any(v[k] for k in ('update_in_progress', 'command_intake_in_progress', 'restarting')), 'SUPPORT_RESTART_NOT_READY')
    return v


def validate_restart_control(kind, value, *, now_ms):
    """Validate eight closed request shapes; current owner/DB permission and CAS are external."""
    _check(type(kind) is str and kind in _CONTROLS and _integer(now_ms))
    v = _record(value, _CONTROLS[kind], 'control', 16384)
    for key in _CONTROLS[kind]:
        if key.endswith('_id'):
            _check(_match(v[key], _UUID))
        if key in ('payload_hash', 'change_binding_hash', 'preview_payload_hash', 'local_change_receipt_hash'):
            _check(_hex(v[key]))
    if kind in ('inspection', 'grant'):
        _preflight(v['preflight'], now_ms)
    if kind == 'inspection':
        _check(_positive(v['service_revision']))
        _conflict(v['case_id'] == v['preflight']['case_id'])
    if kind == 'admit':
        _check(_match(v['owner_generation'], r'[0-9a-f]{32}'))
    if kind == 'report':
        _check(_positive(v['state_revision']) and v['state'] in ('running', 'succeeded', 'failed', 'unknown')
               and _integer(v['observed_at']) and v['observed_at'] <= now_ms)
        if v['result'] is not None:
            result = validate_restart_result(v['result'], now_ms=now_ms)
            _bind(result, v, ('operation_id', 'payload_hash', 'receipt_id'))
            _conflict(result['generated_at'] == v['observed_at'])
            _check(result['error_code'] != 'NONE' or v['state'] == 'succeeded')
        _check(v['state'] != 'succeeded' or (v['result'] is not None and _complete_result(v['result'])))
    return v


def _admission(value, grant):
    v = _input(value)
    _check(set(v) == set(_ADMISSION) and _match(v['receipt_id'], _UUID) and _match(v['admission_id'], _UUID) and _match(v['owner_generation'], r'[0-9a-f]{32}'))
    _bind(grant, v, _ADMISSION)


def validate_restart_authorization(*, operation, approval, preview, local_receipt, acceptance,
                                   registration, preflight, expected, now_ms, grant=None, admission=None):
    """Join independent trusted records; no signature, current permission or human ACK is proven here."""
    op = validate_typed_payload('restart-operation', operation, now_ms=now_ms)
    p = validate_typed_payload('restart-consent-preview', preview, now_ms=now_ms, allow_expired=True)
    a = _validate('approval', approval, now_ms)
    ack = _validate('acceptance', acceptance, now_ms)
    local = _validate('local_receipt', local_receipt, now_ms)
    reg = _validate('registration', registration, now_ms)
    e = _input(expected)
    _check(all(k in e for k in EXPECTED_FIELDS))
    for key, value in e.items():
        _conflict(key in op and _same(op[key], value))
    _conflict(_same(_intent_from(op), p['intent']) and _same(a, p['approval']))
    _approval_binding(p['intent'], a)
    _conflict(op['intent_hash'] == a['intent_hash'] and a['approval_hash'] == hash_restart_approval(a)
              and op['approval_id'] == a['approval_id'] and op['approval_hash'] == a['approval_hash'] and op['approved_by'] == a['approved_by'])
    _bind(op, p, ('preview_id', 'change_consent_id', 'change_binding_hash'))
    _conflict(op['preview_payload_hash'] == hash_restart_preview(p) and op['local_change_receipt_hash'] == hash_restart_local_receipt(local)
              and local['receipt_hash'] == op['local_change_receipt_hash'])
    _bind(op, local, (*_COMMON, 'operation_id', 'intent_hash', 'approval_hash', 'change_consent_id',
                      'change_binding_hash', 'preview_id', 'preview_payload_hash', 'local_change_receipt_id', 'change_accept_id'))
    _conflict(op['change_accept_id'] == ack['accept_id'])
    _bind(op, ack, ('preview_id', 'operation_id', 'change_consent_id', 'change_binding_hash',
                   'local_change_receipt_id', 'preview_payload_hash', 'local_change_receipt_hash'))
    _conflict(p['not_before'] <= local['confirmed_at'] <= ack['accepted_at'] < p['expires_at']
              and local['expires_at'] <= min(op['expires_at'], a['expires_at']) and ack['expires_at'] <= local['expires_at'])
    _conflict(reg['state'] == 'active')
    _bind(op, reg, _REG_BINDING)
    _conflict(reg['service_id'] == op['params']['service_id'] and reg['effects_profile'] == op['params']['effects_profile'])
    witness = _preflight(preflight, now_ms, expected=e, params=op['params'])
    g = None
    if grant is not None:
        g = _validate('grant', grant, now_ms)
        _bind(g, op, _GRANT_BINDING)
        tracking_deadline = ack['accepted_at'] + op['completion_timeout_ms']
        _check(_integer(tracking_deadline))
        _conflict(g['issued_at'] >= ack['accepted_at'] and g['expires_at'] <= min(op['expires_at'], a['expires_at'], ack['expires_at'], reg['expires_at'], tracking_deadline))
        _admission(admission, g)
    return {'operation': op, 'approval': a, 'preview': p, 'local_receipt': local, 'acceptance': ack,
            'registration': reg, 'preflight': witness, 'grant': g}


def validate_restart_outcome(value, *, operation, grant, registration, admission, now_ms):
    """Bind result to a prior operation/grant; native supervisor proof must still come from its owner."""
    result = validate_restart_result(value, now_ms=now_ms)
    op = validate_typed_payload('restart-operation', operation, now_ms=now_ms, allow_expired=True)
    g = _validate('grant', grant, now_ms, True)
    reg = _validate('registration', registration, now_ms, True)
    _bind(g, op, _GRANT_BINDING)
    _admission(admission, g)
    _bind(result, op, ('operation_id', 'payload_hash', 'service_revision', 'params'))
    _bind(result, g, ('receipt_id', 'grant_id'))
    if _complete_result(result):
        _conflict(reg['causal_evidence_supported'] and reg['state'] == 'active' and reg['not_before'] <= now_ms < reg['expires_at'])
        _bind(op, reg, _REG_BINDING)
        _conflict(reg['service_id'] == op['params']['service_id'] and reg['effects_profile'] == op['params']['effects_profile']
                  and g['not_before'] <= result['started_at'] < g['expires_at'])
    return result
