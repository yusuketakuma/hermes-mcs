"""release.apply の閉じたwire契約・hash・相互束縛だけを検査する。"""
from __future__ import annotations

import hashlib

from adapters.sdc import release_contract as release
from adapters.sdc.support_contract import canonical, PURPOSE_CODES

EXPECTED_FIELDS = ('audience', 'tenant', 'installation', 'device_id', 'environment', 'trust_domain', 'stream_id', 'case_id', 'claim_id', 'key_id', 'realm_epoch', 'policy_epoch', 'device_epoch', 'case_epoch', 'local_consent_epoch', 'trust_digest', 'root_version', 'metadata_epoch', 'release_id', 'manifest_digest', 'target_code_seq', 'plan_id', 'plan_hash')
PURPOSES = ('release-apply-consent-preview', 'release-apply-operation', 'release-apply-execution-grant')
CAPABILITY_REVISION = 'sdc-release-apply-capabilities-v1'
CONSENT_REVISION = 'sdc-release-apply-consent-v1'
COMPLETION_TIMEOUT_MS = 3600000
ERROR_CODES = ('NONE', 'PRECHECK_FAILED', 'START_WINDOW_EXPIRED', 'APPLY_FAILED', 'VERIFY_FAILED', 'RESULT_UNKNOWN')
VERSIONS = {key: 'sdc.release.apply-' + name + '.v1' for key, name in (
    ('intent', 'intent'), ('approval', 'approval'), ('preview', 'consent-preview'), ('operation', 'operation'),
    ('receipt', 'local-change-receipt'), ('acceptance', 'acceptance'), ('admission', 'admission'),
    ('grant', 'execution-grant'), ('result', 'result'), ('binding', 'change-binding'), ('control', 'control'),
    ('admin', 'admin'), ('policy', 'policy'), ('state', 'state-witness'))}
COMMON = ('product', *EXPECTED_FIELDS, 'capability_revision', 'consent_revision')
TIMES = ('issued_at', 'not_before', 'expires_at')
INTENT = (*COMMON, 'operation_id', 'type', 'purpose_code', 'requested_by', 'plan_report_id', 'plan_report_hash', 'window', 'canary_stage', 'completion_timeout_ms', *TIMES)
APPROVAL = (*COMMON, 'approval_id', 'intent_hash', 'approved_by', 'approval_kind', *TIMES, 'approval_hash')
OP_ADDED = ('intent_hash', 'approval_id', 'approval_hash', 'approved_by', 'change_consent_id', 'change_binding_hash', 'local_change_receipt_id', 'change_accept_id', 'preview_id', 'preview_payload_hash', 'local_change_receipt_hash', 'payload_hash')
PREVIEW = (*COMMON, 'operation_id', 'preview_id', 'intent', 'approval', 'intent_hash', 'change_consent_id', 'change_binding_hash', *TIMES)
BOUND = ('operation_id', 'intent_hash', 'payload_hash', 'approval_hash', 'change_consent_id', 'change_binding_hash', 'local_change_receipt_id', 'change_accept_id', 'preview_id', 'preview_payload_hash', 'local_change_receipt_hash')
GRANT = (*COMMON, *BOUND, 'grant_id', 'grant_request_id', 'receipt_id', 'admission_id', 'owner_generation', 'preflight_sha256', 'state_sha256', *TIMES)
LOCAL_RECEIPT = (*COMMON, 'operation_id', 'intent_hash', 'approval_hash', 'change_consent_id', 'change_binding_hash', 'preview_id', 'preview_payload_hash', 'local_change_receipt_id', 'change_accept_id', 'confirmed_at', 'expires_at', 'receipt_hash')
ACCEPTANCE = ('accept_id', 'preview_id', 'operation_id', 'change_consent_id', 'change_binding_hash', 'local_change_receipt_id', 'preview_payload_hash', 'local_change_receipt_hash', 'state', 'accepted_at', 'expires_at')
ADMISSION = ('admission_id', 'operation_id', 'payload_hash', 'receipt_id', 'owner_generation', 'admitted_at')
RESULT = ('operation_id', 'payload_hash', 'receipt_id', 'grant_id', 'release_id', 'manifest_digest', 'target_code_seq', 'generated_at', 'start_receipt_id', 'started_at', 'installed_release_id', 'installed_code_seq', 'verified_at', 'readiness', 'error_code')
FACTS = ('start_receipt_id', 'started_at', 'verified_at', 'installed_release_id', 'installed_code_seq')
OWNER = ('product', 'owner_id', 'owner_revision', 'owner_evidence_sha256')
REGISTRATION = ('registration_id', 'product', 'tenant', 'installation', 'device_id', 'environment', 'claim_id', 'key_id', 'realm_epoch', 'policy_epoch', 'device_epoch', 'owner_id', 'owner_revision', 'capability_revision', 'verified_result_supported', 'device_evidence_sha256', 'not_before', 'expires_at')
CONTROLS = dict(plan_report=('report_id', 'case_id', 'local_planning_consent_id', 'state', 'plan'),
    preview=('preview_id', 'operation_id'),
    accept=('accept_id', 'preview_id', 'change_consent_id', 'change_binding_hash', 'local_change_receipt_id', 'preview_payload_hash', 'local_change_receipt_hash'),
    poll=('poll_id', 'case_id'), admit=('admission_id', 'operation_id', 'payload_hash', 'receipt_id', 'owner_generation'),
    grant=('grant_request_id', 'operation_id', 'payload_hash', 'receipt_id', 'admission_id', 'preconditions', 'state'),
    report=('report_id', 'operation_id', 'payload_hash', 'receipt_id', 'grant_id', 'sequence', 'state', 'observed_at', 'result'),
    status=('status_id', 'operation_id', 'payload_hash', 'receipt_id', 'report_id'))
ADMIN = dict(propose=('proposal_id', 'operation_id', 'plan_report_id'), approve=('approval_id', 'operation_id', 'intent_hash'), revoke=('revoke_id', 'operation_id'), read=('case_id', 'device_id'))
HASH_FIELDS = ('payload_hash', 'change_binding_hash', 'preview_payload_hash', 'local_change_receipt_hash', 'intent_hash')
ZERO = '0' * 64


class ContractError(release.ContractError):
    """固定されたrelease.apply拒否コードだけを公開する。"""
    def __init__(self, code='RELEASE_APPLY_INVALID_CONTRACT', *, hold=False):
        self.hold = hold
        super().__init__(code)


def check(ok, code='RELEASE_APPLY_INVALID_CONTRACT'):
    if not ok:
        raise ContractError(code)


def conflict(ok):
    check(ok, 'RELEASE_APPLY_BINDING_CONFLICT')


def _input(v, limit=8192):
    try:
        return release.input_record(v, limit, plain_integers=False)
    except ValueError:
        raise ContractError() from None


def closed(v, names):
    check(type(v) is dict and set(v) == set(names))


def fields(v, names):
    closed(v, ('schema_version', *names))


def same(a, b):
    return canonical(a) == canonical(b)


def selected(v, names):
    return {k: v[k] for k in names}


def _common(v):
    check(v['product'] == 'hermes-mcs' and v['tenant'] == 'self' and release.slug(v['installation']) and release.uuid(v['device_id']) and release.match(v['environment'], r'[a-z0-9_-]{1,32}'))
    check(release.token(v['trust_domain']) and release.token(v['stream_id']) and release.uuid(v['case_id']) and release.match(v['audience'], r'[A-Za-z0-9:_-]{1,100}') and release.uuid(v['claim_id']) and release.uuid(v['key_id']) and release.realm(v['realm_epoch']))
    for k in ('policy_epoch', 'device_epoch', 'case_epoch', 'local_consent_epoch', 'root_version', 'metadata_epoch', 'target_code_seq'):
        check(release.positive(v[k]))
    check(all(release.hex64(v[k]) for k in ('trust_digest', 'release_id', 'manifest_digest', 'plan_hash')))
    check(release.uuid(v['plan_id']) and v['capability_revision'] == CAPABILITY_REVISION and v['consent_revision'] == CONSENT_REVISION)


def _lifetime(v, limit, *, now_ms=None, allow_expired=False, check_time=True):
    check(type(allow_expired) is bool and type(check_time) is bool and all(release.integer(v[k]) for k in TIMES))
    check(v['issued_at'] <= v['not_before'] < v['expires_at'] and v['expires_at'] - v['issued_at'] <= limit)
    if check_time:
        check(release.integer(now_ms) and v['not_before'] <= now_ms)
        check(allow_expired or now_ms < v['expires_at'], 'RELEASE_APPLY_EXPIRED')


def _window(v):
    closed(v, ('starts_at', 'ends_at'))
    check(release.integer(v['starts_at']) and release.integer(v['ends_at']) and v['starts_at'] < v['ends_at'])


def intent_from(v):
    return dict(schema_version=VERSIONS['intent'], **selected(v, INTENT))


def _intent(v, options):
    _common(v)
    check(release.uuid(v['operation_id']) and v['type'] == 'release.apply' and v['purpose_code'] in PURPOSE_CODES and release.hex64(v['requested_by']))
    check(release.uuid(v['plan_report_id']) and release.hex64(v['plan_report_hash']) and v['canary_stage'] == 'self_test' and type(v['completion_timeout_ms']) is int and v['completion_timeout_ms'] == COMPLETION_TIMEOUT_MS)
    _window(v['window'])
    _lifetime(v, 1800000, **options)
    check(v['expires_at'] <= v['window']['ends_at'])


def validate_apply_intent(value, **options):
    v = _input(value)
    fields(v, INTENT)
    check(v['schema_version'] == VERSIONS['intent'])
    _intent(v, options)
    return v


def validate_apply_approval(value, **options):
    v = _input(value)
    fields(v, APPROVAL)
    check(v['schema_version'] == VERSIONS['approval'])
    _common(v)
    _lifetime(v, 1800000, **options)
    check(release.uuid(v['approval_id']) and all(release.hex64(v[k]) for k in ('intent_hash', 'approved_by', 'approval_hash')) and v['approval_kind'] == 'individual')
    return v


def _approval_binding(intent, approval):
    for k in COMMON:
        conflict(same(intent[k], approval[k]))
    conflict(approval['issued_at'] >= intent['issued_at'] and approval['not_before'] >= intent['not_before'] and approval['expires_at'] <= intent['expires_at'])


def validate_apply_preview(value, **options):
    v = _input(value)
    fields(v, PREVIEW)
    check(v['schema_version'] == VERSIONS['preview'])
    _common(v)
    _lifetime(v, 300000, **options)
    check(all(release.uuid(v[k]) for k in ('operation_id', 'preview_id', 'change_consent_id')) and release.hex64(v['intent_hash']) and release.hex64(v['change_binding_hash']))
    intent, approval = validate_apply_intent(v['intent'], **options), validate_apply_approval(v['approval'], **options)
    _approval_binding(intent, approval)
    for k in COMMON:
        conflict(same(v[k], intent[k]))
    conflict(v['operation_id'] == intent['operation_id'] and v['intent_hash'] == approval['intent_hash'] and v['issued_at'] >= approval['issued_at'] and v['not_before'] >= approval['not_before'] and v['expires_at'] <= min(intent['expires_at'], approval['expires_at']))
    return v


def validate_apply_operation(value, **options):
    v = _input(value)
    fields(v, (*INTENT, *OP_ADDED))
    check(v['schema_version'] == VERSIONS['operation'])
    _intent(v, options)
    check(all(release.uuid(v[k]) for k in ('approval_id', 'change_consent_id', 'local_change_receipt_id', 'change_accept_id', 'preview_id')))
    check(all(release.hex64(v[k]) for k in ('intent_hash', 'approval_hash', 'approved_by', 'change_binding_hash', 'payload_hash', 'preview_payload_hash', 'local_change_receipt_hash')))
    return v


def validate_apply_grant(value, **options):
    v = _input(value)
    fields(v, GRANT)
    check(v['schema_version'] == VERSIONS['grant'])
    _common(v)
    _lifetime(v, 30000, **options)
    check(all(release.uuid(v[k]) for k in ('operation_id', 'change_consent_id', 'local_change_receipt_id', 'change_accept_id', 'preview_id', 'grant_id', 'grant_request_id', 'receipt_id', 'admission_id')))
    check(all(release.hex64(v[k]) for k in ('intent_hash', 'payload_hash', 'approval_hash', 'change_binding_hash', 'preview_payload_hash', 'local_change_receipt_hash', 'preflight_sha256', 'state_sha256')))
    check(release.match(v['owner_generation'], r'[0-9a-f]{32}'))
    return v


def validate_apply_local_receipt(value, *, now_ms=None, allow_expired=False, check_time=True):
    v = _input(value)
    fields(v, LOCAL_RECEIPT)
    check(v['schema_version'] == VERSIONS['receipt'])
    _common(v)
    check(all(release.uuid(v[k]) for k in ('operation_id', 'change_consent_id', 'preview_id', 'local_change_receipt_id', 'change_accept_id')))
    check(all(release.hex64(v[k]) for k in ('intent_hash', 'approval_hash', 'change_binding_hash', 'preview_payload_hash', 'receipt_hash')))
    check(release.integer(v['confirmed_at']) and release.integer(v['expires_at']) and v['confirmed_at'] < v['expires_at'] and type(allow_expired) is bool and type(check_time) is bool)
    if check_time:
        check(release.integer(now_ms) and v['confirmed_at'] <= now_ms)
        check(allow_expired or now_ms < v['expires_at'], 'RELEASE_APPLY_EXPIRED')
    return v


def validate_apply_acceptance(value, *, now_ms=None, allow_expired=False, check_time=True):
    v = _input(value)
    fields(v, ACCEPTANCE)
    check(v['schema_version'] == VERSIONS['acceptance'] and v['state'] == 'accepted' and release.integer(v['accepted_at']) and release.positive(v['expires_at']) and v['accepted_at'] < v['expires_at'])
    check(all(release.uuid(v[k]) for k in ('accept_id', 'preview_id', 'operation_id', 'change_consent_id', 'local_change_receipt_id')))
    check(all(release.hex64(v[k]) for k in ('change_binding_hash', 'preview_payload_hash', 'local_change_receipt_hash')) and type(allow_expired) is bool and type(check_time) is bool)
    if check_time:
        check(release.integer(now_ms) and v['accepted_at'] <= now_ms)
        check(allow_expired or now_ms < v['expires_at'], 'RELEASE_APPLY_EXPIRED')
    return v


def validate_apply_admission(value, *, now_ms=None, check_time=True):
    v = _input(value)
    fields(v, ADMISSION)
    check(v['schema_version'] == VERSIONS['admission'] and all(release.uuid(v[k]) for k in ('admission_id', 'operation_id', 'receipt_id')))
    check(release.hex64(v['payload_hash']) and release.match(v['owner_generation'], r'[0-9a-f]{32}') and release.integer(v['admitted_at']) and type(check_time) is bool)
    if check_time:
        check(release.integer(now_ms) and v['admitted_at'] <= now_ms)
    return v


def complete_apply_result(v):
    return v['error_code'] == 'NONE' and v['readiness'] == 'ready' and all(v[k] is not None for k in FACTS) and v['installed_release_id'] == v['release_id'] and v['installed_code_seq'] == v['target_code_seq']


def validate_apply_result(value, *, now_ms=None, check_time=True):
    v = _input(value, 16384)
    fields(v, RESULT)
    check(v['schema_version'] == VERSIONS['result'] and all(release.uuid(v[k]) for k in ('operation_id', 'receipt_id', 'grant_id')))
    check(all(release.hex64(v[k]) for k in ('payload_hash', 'release_id', 'manifest_digest')) and release.positive(v['target_code_seq']) and release.integer(v['generated_at']) and type(check_time) is bool)
    if check_time:
        check(release.integer(now_ms) and v['generated_at'] <= now_ms)
    check(v['start_receipt_id'] is None or release.uuid(v['start_receipt_id']))
    check((v['started_at'] is None) == (v['start_receipt_id'] is None))
    check((v['installed_release_id'] is None) == (v['installed_code_seq'] is None) and (v['installed_release_id'] is None or release.hex64(v['installed_release_id']) and release.positive(v['installed_code_seq'])))
    previous = 0
    for k in ('started_at', 'verified_at'):
        if v[k] is not None:
            check(release.integer(v[k]) and previous <= v[k] <= v['generated_at'])
            previous = v[k]
    check(v['verified_at'] is None or v['started_at'] is not None)
    check(v['readiness'] in ('unknown', 'ready', 'not_ready') and v['error_code'] in ERROR_CODES)
    if v['error_code'] == 'NONE':
        check(complete_apply_result(v))
    if v['error_code'] in ('PRECHECK_FAILED', 'START_WINDOW_EXPIRED'):
        check(all(v[k] is None for k in ('started_at', 'installed_release_id', 'verified_at')))
    return v


def validate_apply_preconditions(value, *, now_ms=None):
    c = _input(value)
    closed(c, release.PRECONDITIONS)
    check(release.match(c['current_commit'], r'[0-9a-f]{40}') and type(c['hand_edited']) is bool and release.hex64(c['installed_lock_sha256']) and release.integer(c['free_bytes']) and release.int_tuple(c['updater'], 3) and release.positive(c['installed_schema']))
    check(type(c['backup_verified']) is bool and release.hex64(c['backup_sha256']) and release.integer(c['observed_at']))
    closed(c['platform'], ('os', 'cpu', 'python'))
    check(release.slug(c['platform']['os']) and release.slug(c['platform']['cpu']) and release.int_tuple(c['platform']['python'], 2))
    if now_ms is not None:
        check(release.integer(now_ms))
    return c


def validate_apply_control(kind, value, *, now_ms):
    check(type(kind) is str and kind in CONTROLS)
    v = _input(value, 16384)
    fields(v, CONTROLS[kind])
    check(v['schema_version'] == VERSIONS['control'])
    for k in CONTROLS[kind]:
        if k.endswith('_id'):
            check(release.uuid(v[k]))
        if k in HASH_FIELDS:
            check(release.hex64(v[k]))
    if kind == 'plan_report':
        release.validate_release_state(v['state'])
        check(type(v['plan']) is dict)
    if kind == 'admit':
        check(release.match(v['owner_generation'], r'[0-9a-f]{32}'))
    if kind == 'grant':
        validate_apply_preconditions(v['preconditions'], now_ms=now_ms)
        release.validate_release_state(v['state'])
    if kind == 'report':
        check(release.positive(v['sequence']) and v['state'] in ('running', 'succeeded', 'failed', 'unknown') and release.integer(v['observed_at']) and release.integer(now_ms) and v['observed_at'] <= now_ms)
        if v['result'] is not None:
            r = validate_apply_result(v['result'], now_ms=now_ms)
            conflict(all(same(r[k], v[k]) for k in ('operation_id', 'payload_hash', 'receipt_id', 'grant_id')) and r['generated_at'] == v['observed_at'])
            if r['error_code'] == 'NONE':
                check(v['state'] == 'succeeded')
        if v['state'] == 'succeeded':
            check(v['result'] is not None and complete_apply_result(v['result']))
    return v


def validate_apply_admin(kind, value):
    check(type(kind) is str and kind in ADMIN)
    v = _input(value)
    fields(v, ADMIN[kind])
    check(v['schema_version'] == VERSIONS['admin'])
    for k in ADMIN[kind]:
        check(release.uuid(v[k]) if k.endswith('_id') else release.hex64(v[k]))
    return v


def validate_apply_policy(value):
    v = _input(value, 16384)
    fields(v, ('policy_revision', 'owners', 'device_registrations'))
    check(v['schema_version'] == VERSIONS['policy'] and release.positive(v['policy_revision']))
    check(type(v['owners']) is list and len(v['owners']) <= 1 and type(v['device_registrations']) is list and len(v['device_registrations']) <= 16)
    for o in v['owners']:
        closed(o, OWNER)
        check(o['product'] == 'hermes-mcs' and o['owner_id'] == 'mcs.updater' and release.positive(o['owner_revision']) and release.hex64(o['owner_evidence_sha256']))
    devices, ids = set(), set()
    for r in v['device_registrations']:
        closed(r, REGISTRATION)
        check(r['product'] == 'hermes-mcs' and r['tenant'] == 'self' and release.slug(r['installation']) and release.uuid(r['device_id']) and release.match(r['environment'], r'[a-z0-9_-]{1,32}'))
        check(all(release.uuid(r[k]) for k in ('registration_id', 'claim_id', 'key_id')) and release.realm(r['realm_epoch']) and release.positive(r['policy_epoch']) and release.positive(r['device_epoch']))
        check(r['capability_revision'] == CAPABILITY_REVISION and type(r['verified_result_supported']) is bool and release.hex64(r['device_evidence_sha256']) and release.integer(r['not_before']) and release.integer(r['expires_at']) and r['not_before'] < r['expires_at'])
        check(len(v['owners']) == 1 and r['owner_id'] == v['owners'][0]['owner_id'] and same(r['owner_revision'], v['owners'][0]['owner_revision']))
        check(r['device_id'] not in devices and r['registration_id'] not in ids)
        devices.add(r['device_id'])
        ids.add(r['registration_id'])
    return v


def _digest(v):
    return hashlib.sha256(canonical(v).encode('ascii')).hexdigest()


def hash_apply_intent(v):
    return _digest(validate_apply_intent(v, check_time=False))


def hash_apply_approval(v):
    v = validate_apply_approval({**_input(v), 'approval_hash': ZERO}, check_time=False)
    del v['approval_hash']
    return _digest(v)


def hash_apply_change_binding(v):
    v = _input(v)
    validator = validate_apply_preview if v['schema_version'] == VERSIONS['preview'] else validate_apply_operation
    v = validator({**v, 'change_binding_hash': ZERO}, check_time=False)
    return _digest(dict(schema_version=VERSIONS['binding'], intent_hash=v['intent_hash'],
        approval_hash=v['approval']['approval_hash'] if v['schema_version'] == VERSIONS['preview'] else v['approval_hash'],
        change_consent_id=v['change_consent_id'], claim_id=v['claim_id'], key_id=v['key_id']))


def hash_apply_payload(v):
    v = validate_apply_operation({**_input(v), 'payload_hash': ZERO}, check_time=False)
    del v['payload_hash']
    return _digest(v)


def hash_apply_preview(v):
    return _digest(validate_apply_preview(v, check_time=False))


def hash_apply_local_receipt(v):
    v = validate_apply_local_receipt({**_input(v), 'receipt_hash': ZERO}, check_time=False)
    del v['receipt_hash']
    return _digest(v)


def hash_apply_state(v):
    return _digest(dict(schema_version=VERSIONS['state'], state=release.validate_release_state(v)))


def hash_apply_report(v, *, now_ms):
    return _digest(validate_apply_control('report', v, now_ms=now_ms))


def validate_apply_typed_payload(purpose, value, **options):
    if purpose == PURPOSES[1]:
        v = validate_apply_operation(value, **options)
        conflict(v['intent_hash'] == hash_apply_intent(intent_from(v)) and v['change_binding_hash'] == hash_apply_change_binding(v) and v['payload_hash'] == hash_apply_payload(v))
        return v
    if purpose == PURPOSES[0]:
        v = validate_apply_preview(value, **options)
        conflict(v['intent_hash'] == hash_apply_intent(v['intent']) and v['approval']['approval_hash'] == hash_apply_approval(v['approval']) and v['change_binding_hash'] == hash_apply_change_binding(v))
        return v
    if purpose == PURPOSES[2]:
        return validate_apply_grant(value, **options)
    raise ContractError('SUPPORT_SIGNATURE_PURPOSE_DENIED')


def validate_apply_authorization(*, operation, approval, preview, local_receipt, acceptance, registration, expected, now_ms, grant=None, admission=None):
    """独立した記録を相互照合し、永続同意や実行権限の存在は判定しない。"""
    op = validate_apply_typed_payload(PURPOSES[1], operation, now_ms=now_ms)
    p = validate_apply_typed_payload(PURPOSES[0], preview, now_ms=now_ms, allow_expired=True)
    a, ack = validate_apply_approval(approval, now_ms=now_ms), validate_apply_acceptance(acceptance, now_ms=now_ms)
    local = validate_apply_local_receipt(local_receipt, now_ms=now_ms)
    reg, expected = _input(registration), _input(expected)
    reg = validate_apply_policy(dict(schema_version=VERSIONS['policy'], policy_revision=1, owners=[dict(product='hermes-mcs', owner_id=reg['owner_id'], owner_revision=reg['owner_revision'], owner_evidence_sha256=ZERO)], device_registrations=[reg]))['device_registrations'][0]
    check(type(expected) is dict and set(EXPECTED_FIELDS) <= set(expected))
    for k, value in expected.items():
        conflict(k in op and same(op[k], value))
    conflict(same(intent_from(op), p['intent']) and same(a, p['approval']))
    _approval_binding(p['intent'], a)
    conflict(op['intent_hash'] == a['intent_hash'] and a['approval_hash'] == hash_apply_approval(a) and all(op[k] == a[k] for k in ('approval_id', 'approval_hash', 'approved_by')))
    conflict(all(op[k] == p[k] for k in ('preview_id', 'change_consent_id', 'change_binding_hash')))
    conflict(op['preview_payload_hash'] == hash_apply_preview(p) and op['local_change_receipt_hash'] == hash_apply_local_receipt(local) == local['receipt_hash'])
    for k in (*COMMON, 'operation_id', 'intent_hash', 'approval_hash', 'change_consent_id', 'change_binding_hash', 'preview_id', 'preview_payload_hash', 'local_change_receipt_id', 'change_accept_id'):
        conflict(same(op[k], local[k]))
    for left, right in (('change_accept_id', 'accept_id'), *[(k, k) for k in ('preview_id', 'operation_id', 'change_consent_id', 'change_binding_hash', 'local_change_receipt_id', 'preview_payload_hash', 'local_change_receipt_hash')]):
        conflict(same(op[left], ack[right]))
    conflict(p['not_before'] <= local['confirmed_at'] <= ack['accepted_at'] < p['expires_at'] and local['expires_at'] <= min(op['expires_at'], a['expires_at']) and ack['expires_at'] <= local['expires_at'])
    for k in ('product', 'tenant', 'installation', 'device_id', 'environment', 'claim_id', 'key_id', 'realm_epoch', 'policy_epoch', 'device_epoch'):
        conflict(same(op[k], reg[k]))
    conflict(reg['not_before'] <= now_ms < reg['expires_at'])
    g, n = None, None
    if grant is not None:
        g = validate_apply_grant(grant, now_ms=now_ms)
        for k in (*COMMON, *BOUND):
            conflict(same(g[k], op[k]))
        tracking = ack['accepted_at'] + op['completion_timeout_ms']
        check(release.integer(tracking))
        conflict(g['issued_at'] >= ack['accepted_at'] and g['expires_at'] <= min(op['expires_at'], a['expires_at'], ack['expires_at'], reg['expires_at'], tracking))
        n = validate_apply_admission(admission, now_ms=now_ms)
        conflict(n['operation_id'] == op['operation_id'] and n['payload_hash'] == op['payload_hash'] and ack['accepted_at'] <= n['admitted_at'] <= g['issued_at'])
        for k in ('receipt_id', 'admission_id', 'owner_generation'):
            conflict(g[k] == n[k])
    return dict(operation=op, approval=a, preview=p, localReceipt=local, acceptance=ack, registration=reg, grant=g, admission=n)


def validate_apply_grant_witness(*, plan, attestation, trust, preconditions, state, now_ms):
    """grant直前の宣言を固定計画と比較し、実ownerの状態は保証しない。"""
    check(release.integer(now_ms))
    c, s = validate_apply_preconditions(preconditions, now_ms=now_ms), release.validate_release_state(state)
    plan, attestation, trust = _input(plan), _input(attestation), _input(trust)
    check(type(plan) is dict and type(plan.get('preconditions')) is dict)
    for k in release.PRECONDITIONS:
        if k not in ('observed_at', 'free_bytes'):
            check(same(c[k], plan['preconditions'][k]), 'RELEASE_PLAN_STALE')
    check(c['observed_at'] <= now_ms + trust['policy']['max_clock_skew_ms'] and now_ms - c['observed_at'] <= trust['policy']['preflight_ttl_ms'], 'RELEASE_PLAN_STALE')
    for k in (*release.EXPECTED_FIELDS, 'tenant', 'installation', 'device_id', 'realm_epoch', 'trust_digest', 'root_version', 'metadata_epoch', *release.STATE_POINTS):
        check(same(s[k], plan[k]), 'RELEASE_PLAN_STALE')
    check(s['unresolved'] is None and not release.release_plan_blockers({**plan, 'preconditions': c}, attestation, trust, s, now_ms=now_ms), 'RELEASE_PLAN_STALE')
    return dict(preflight_sha256=release.hash_release_preconditions(c), state_sha256=hash_apply_state(s))


def validate_apply_outcome(report, *, operation, grant, admission, now_ms):
    r = validate_apply_control('report', report, now_ms=now_ms)
    op = validate_apply_typed_payload(PURPOSES[1], operation, now_ms=now_ms, allow_expired=True)
    g, n = validate_apply_grant(grant, now_ms=now_ms, allow_expired=True), validate_apply_admission(admission, now_ms=now_ms)
    for k in (*COMMON, *BOUND):
        conflict(same(g[k], op[k]))
    for k in ('receipt_id', 'admission_id', 'owner_generation'):
        conflict(g[k] == n[k])
    conflict(n['operation_id'] == op['operation_id'] and n['payload_hash'] == op['payload_hash'] and n['admitted_at'] <= g['issued_at'])
    conflict(all(r[k] == op[k] for k in ('operation_id', 'payload_hash')) and r['receipt_id'] == g['receipt_id'] and r['grant_id'] == g['grant_id'])
    if r['result'] is not None:
        conflict(all(same(r['result'][k], op[k]) for k in ('release_id', 'manifest_digest', 'target_code_seq')))
        if r['result']['started_at'] is not None:
            conflict(g['not_before'] <= r['result']['started_at'] < g['expires_at'])
    return r


NEXT = dict(none=('running', 'unknown', 'failed', 'succeeded'), running=('running', 'unknown', 'failed', 'succeeded'), unknown=('unknown', 'failed', 'succeeded'), succeeded=(), failed=())


def decide_apply_report(*, report, report_hash, stored, verified_result_supported):
    """既知事実を保った報告遷移を返し、矛盾はhold付きで拒否する。"""
    check(release.hex64(report_hash) and type(verified_result_supported) is bool and type(stored) is dict)
    released = stored.get('released_prestart', False)
    check(type(released) is bool)
    facts = {**dict.fromkeys(FACTS), **(stored.get('facts') or {})}
    latest, r = stored.get('latest'), report['result']

    def held():
        raise ContractError('RELEASE_APPLY_REPORT_CONFLICT', hold=True)

    for k in FACTS:
        value = r[k] if r is not None else None
        if value is None:
            continue
        if facts[k] is not None and not same(facts[k], value):
            held()
        facts[k] = value
    if released and any(facts[k] is not None for k in FACTS):
        held()
    adopted = ('succeeded' if verified_result_supported else 'unknown') if report['state'] == 'succeeded' else report['state']
    if latest is not None:
        check(release.positive(latest['sequence']) and release.hex64(latest['hash']) and latest['adopted'] in NEXT and latest['adopted'] != 'none')
        if report['sequence'] == latest['sequence']:
            if report_hash != latest['hash']:
                held()
            return dict(disposition='duplicate', adopted=latest['adopted'], reported=report['state'], facts=facts, releaseSlot=False)
        if report['sequence'] < latest['sequence']:
            return dict(disposition='history', adopted=latest['adopted'], reported=report['state'], facts=facts, releaseSlot=False)
    if adopted not in NEXT[latest['adopted'] if latest else 'none']:
        held()
    prestart = adopted == 'failed' and r is not None and r['error_code'] in ('PRECHECK_FAILED', 'START_WINDOW_EXPIRED') and all(facts[k] is None for k in FACTS)
    return dict(disposition='advanced', adopted=adopted, reported=report['state'], facts=facts, releaseSlot=adopted == 'succeeded' or prestart, releasedPrestart=prestart)
