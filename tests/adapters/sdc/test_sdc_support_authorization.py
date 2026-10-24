"""Independent synthetic scope/actor/epoch binding, without I/O or signature execution."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import support_authorization as binding

SHARED = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/support'
VECTORS = json.loads((SHARED / 'signature-vectors.json').read_text())
NOW = VECTORS['now']
OPERATION = json.loads(VECTORS['vectors'][0]['payload_canonical'])
APPROVAL = VECTORS['approval']
GRANT = json.loads(VECTORS['vectors'][1]['payload_canonical'])


def digest(value, omitted):
    # Independent oracle, never a candidate contract/hash helper.
    core = {key: child for key, child in value.items() if key not in omitted}
    return hashlib.sha256(json.dumps(core, ensure_ascii=True, sort_keys=True,
                                    separators=(',', ':')).encode('ascii')).hexdigest()


def operation_hash(value):
    return {**value, 'payload_hash': digest(value, {'payload_hash'})}


def approval_hash(value):
    return {**value, 'approval_hash': digest(value, {'approval_hash'})}


def preview(**changes):
    # Align the fixed operation scope/clock explicitly. The shared preview is from
    # a different clock and is not itself an authorization for this operation.
    value = {key: OPERATION[key] for key in (
        'product', 'audience', 'tenant', 'installation', 'device_id', 'environment',
        'case_id', 'purpose_code', 'capability_revision', 'consent_revision',
        'realm_epoch', 'policy_epoch', 'device_epoch', 'case_epoch', 'local_consent_epoch')}
    value.update(schema_version='sdc.support.consent-preview.v1',
                 preview_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                 claim_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
                 key_id='cccccccc-cccc-4ccc-8ccc-cccccccccccc',
                 diagnostic_ids=['runtime.status.v1'], operator_refs=[OPERATION['requested_by']],
                 approved_by='f' * 64, issued_at=NOW, not_before=NOW,
                 expires_at=NOW + 300000, consent_expires_at=NOW + 1800000)
    value.update(changes)
    value['consent_binding_hash'] = digest(value, {
        'preview_id', 'issued_at', 'not_before', 'expires_at', 'consent_binding_hash'})
    return value


def match_operation(operation=OPERATION, approval=APPROVAL, consent=None, now=NOW):
    return binding.match_operation(operation, approval, consent_preview=preview() if consent is None else consent, now_ms=now)


def match_grant(grant=GRANT, operation=OPERATION, approval=APPROVAL, **changes):
    expected = {key: GRANT[key] for key in ('grant_request_id', 'receipt_id', 'admission_id', 'owner_generation')}
    return binding.match_grant(grant, operation, approval, now_ms=NOW, **{**expected, **changes})


def test_shared_operation_approval_and_grant_match_independent_local_consent():
    assert match_operation() == OPERATION
    assert preview()['approved_by'] != APPROVAL['approved_by']
    assert match_grant() == GRANT
    assert digest(OPERATION, {'payload_hash'}) == OPERATION['payload_hash']
    assert digest(APPROVAL, {'approval_hash'}) == APPROVAL['approval_hash']


def test_consent_outlives_display_window_and_does_not_require_operation_after_preview():
    assert match_operation(now=NOW + 300001) == OPERATION
    later_preview = preview(issued_at=NOW + 500, not_before=NOW + 500, expires_at=NOW + 300500)
    assert match_operation(consent=later_preview, now=NOW + 1000) == OPERATION


@pytest.mark.parametrize('field,value', [
    ('tenant', 'other'), ('installation', 'other'), ('device_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'),
    ('environment', 'other'), ('case_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'),
    ('audience', 'other'), ('realm_epoch', 'other'), ('policy_epoch', 2), ('device_epoch', 2),
    ('case_epoch', 2), ('local_consent_epoch', 2), ('purpose_code', 'CONFIG_MISMATCH'),
    ('requested_by', '0' * 64), ('params', {'diagnostic_id': 'notification.health.v1'})])
def test_validly_hashed_operation_cannot_change_trusted_consent_scope(field, value):
    changed = operation_hash({**OPERATION, field: value})
    with pytest.raises(ValueError, match='^SUPPORT_OPERATION_BINDING_MISMATCH$'):
        match_operation(changed)


@pytest.mark.parametrize('field,value', [('approval_kind', 'preauthorized'),
    ('approval_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'), ('intent_hash', '0' * 64),
    ('approved_by', '0' * 64), ('realm_epoch', 'other'), ('policy_epoch', 2),
    ('device_epoch', 2), ('case_epoch', 2), ('local_consent_epoch', 2),
    ('issued_at', NOW - 1), ('expires_at', OPERATION['expires_at'] + 1)])
def test_individually_valid_approval_must_bind_to_exact_operation(field, value):
    changed = approval_hash({**APPROVAL, field: value})
    op = operation_hash({**OPERATION, 'approval_hash': changed['approval_hash']})
    with pytest.raises(ValueError, match='^SUPPORT_OPERATION_BINDING_MISMATCH$'):
        match_operation(op, changed)


def test_rehashed_foreign_approval_actor_cannot_replace_signed_operation_actor():
    changed = approval_hash({**APPROVAL, 'approved_by': '0' * 64})
    with pytest.raises(ValueError):
        match_operation(approval=changed)
    with pytest.raises(ValueError):
        match_operation(approval={**APPROVAL, 'approval_hash': '0' * 64})


@pytest.mark.parametrize('consent,now', [
    (preview(consent_expires_at=NOW + 300000), NOW + 300000),
    (preview(consent_expires_at=NOW + 300000), NOW),
    (preview(not_before=NOW + 1), NOW)])
def test_expired_or_not_yet_valid_consent_and_operation_beyond_consent_are_rejected(consent, now):
    with pytest.raises(ValueError, match='^SUPPORT_OPERATION_BINDING_MISMATCH$'):
        match_operation(consent=consent, now=now)


@pytest.mark.parametrize('changes', [
    {'grant_request_id': 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'},
    {'receipt_id': 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'},
    {'admission_id': 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'},
    {'owner_generation': 'd' * 32}, {'owner_generation': True}])
def test_grant_matches_only_persisted_request_receipt_and_live_generation(changes):
    with pytest.raises(ValueError, match='^SUPPORT_GRANT_BINDING_MISMATCH$'):
        match_grant(**changes)


@pytest.mark.parametrize('field,value', [
    ('tenant', 'other'), ('installation', 'other'), ('device_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'),
    ('environment', 'other'), ('case_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'),
    ('operation_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'), ('payload_hash', '0' * 64),
    ('approval_hash', '0' * 64), ('realm_epoch', 'other'), ('policy_epoch', 2),
    ('device_epoch', 2), ('case_epoch', 2), ('local_consent_epoch', 2)])
def test_other_grant_scope_epoch_or_operation_cannot_be_substituted(field, value):
    with pytest.raises(ValueError, match='^SUPPORT_GRANT_BINDING_MISMATCH$'):
        match_grant({**GRANT, field: value})


def test_grant_half_open_deadline_and_operation_expiry_are_both_required():
    expected = {key: GRANT[key] for key in ('grant_request_id', 'receipt_id', 'admission_id', 'owner_generation')}
    assert binding.match_grant(GRANT, OPERATION, APPROVAL, now_ms=GRANT['expires_at'] - 1, **expected) == GRANT
    with pytest.raises(ValueError):
        binding.match_grant(GRANT, OPERATION, APPROVAL, now_ms=GRANT['expires_at'], **expected)
    short_operation = operation_hash({**OPERATION, 'expires_at': NOW + 1000})
    with pytest.raises(ValueError):
        match_grant(operation=short_operation)


def test_raw_verified_input_is_not_authorization_and_errors_never_echo_values():
    for bad in (None, True, {'secret': 'SYNTHETIC_SECRET'}, {'not_before': False}):
        with pytest.raises(ValueError, match='^SUPPORT_OPERATION_BINDING_MISMATCH$'):
            binding.match_operation(OPERATION, APPROVAL, consent_preview=bad, now_ms=NOW)
    result = match_operation()
    result['params']['diagnostic_id'] = 'changed'
    assert OPERATION['params']['diagnostic_id'] == 'runtime.status.v1'
    assert match_grant() is not GRANT


def test_changed_or_malformed_preview_hash_is_not_accepted():
    consent = copy.deepcopy(preview())
    consent['operator_refs'].append('f' * 64)
    with pytest.raises(ValueError):
        match_operation(consent=consent)


def test_grant_must_not_outlive_shorter_individual_approval():
    approval = approval_hash({**APPROVAL, 'expires_at': NOW + 15000})
    operation = operation_hash({**OPERATION, 'approval_hash': approval['approval_hash']})
    grant = {**GRANT, 'payload_hash': operation['payload_hash'], 'approval_hash': approval['approval_hash']}
    # The central publication contract accepts this still-live shorter approval.
    assert match_operation(operation, approval) == operation
    with pytest.raises(ValueError, match='^SUPPORT_GRANT_BINDING_MISMATCH$'):
        match_grant(grant, operation, approval)
    bounded = {**grant, 'expires_at': approval['expires_at']}
    assert match_grant(bounded, operation, approval) == bounded
    expected = {key: grant[key] for key in ('grant_request_id', 'receipt_id', 'admission_id', 'owner_generation')}
    assert binding.match_grant(bounded, operation, approval, now_ms=NOW + 14999, **expected) == bounded
    with pytest.raises(ValueError, match='^SUPPORT_GRANT_BINDING_MISMATCH$'):
        binding.match_grant(bounded, operation, approval, now_ms=NOW + 15000, **expected)


def test_grant_rechecks_approval_actor_kind_intent_hash_and_epochs():
    for field, value in [('approval_kind', 'preauthorized'), ('approved_by', '0' * 64),
                         ('intent_hash', '0' * 64), ('device_epoch', 2)]:
        approval = approval_hash({**APPROVAL, field: value})
        operation = operation_hash({**OPERATION, 'approval_hash': approval['approval_hash']})
        grant = {**GRANT, 'payload_hash': operation['payload_hash'], 'approval_hash': approval['approval_hash']}
        with pytest.raises(ValueError, match='^SUPPORT_GRANT_BINDING_MISMATCH$'):
            match_grant(grant, operation, approval)
