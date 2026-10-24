"""Synthetic fixed-preview and public-signature oracles; no customer acceptance."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import support_contract as contract
import sdc_support_signatures as signatures

NOW = 1792663200000
EXPECTED_BINDING = "1973d68236d22a3437ddb1f8bb93e40f76d73a0c40b4802d1b4fc66389d2e277"
PREVIEW = {'product': 'hermes-mcs',
 'audience': 'sdc:synthetic',
 'tenant': 'tenant-a',
 'installation': 'installation-a',
 'device_id': '22222222-2222-4222-8222-222222222222',
 'environment': 'synthetic',
 'case_id': '33333333-3333-4333-8333-333333333333',
 'purpose_code': 'HEALTH_CHECK',
 'capability_revision': 'sdc-support-capabilities-v1',
 'consent_revision': 'sdc-support-consent-v1',
 'realm_epoch': 'synthetic-realm-1',
 'policy_epoch': 1,
 'case_epoch': 1,
 'local_consent_epoch': 1,
 'schema_version': 'sdc.support.consent-preview.v1',
 'preview_id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
 'claim_id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
 'key_id': 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
 'device_epoch': 1,
 'diagnostic_ids': ['connectivity.status.v1', 'notification.health.v1', 'runtime.status.v1'],
 'operator_refs': ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                   'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
 'approved_by': 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
 'issued_at': 1792663200000,
 'not_before': 1792663200000,
 'expires_at': 1792663500000,
 'consent_expires_at': 1792665000000}
PREVIEW['consent_binding_hash'] = EXPECTED_BINDING


def test_binding_matches_independent_fixed_expected_and_closed_projection():
    value = copy.deepcopy(PREVIEW)
    assert contract.hash_consent_binding(value) == EXPECTED_BINDING
    assert contract.validate_preview(value, now_ms=NOW) == value
    # Independent hash oracle lists the fields excluded by the draft.
    core = {key: child for key, child in value.items()
            if key not in ('preview_id', 'issued_at', 'not_before', 'expires_at', 'consent_binding_hash')}
    assert hashlib.sha256(json.dumps(core, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest() == EXPECTED_BINDING
    parsed = contract.validate_preview(contract.canonical(value).encode('ascii'), now_ms=NOW)
    parsed['operator_refs'].append('d' * 64)
    assert value['operator_refs'] == ['a' * 64, 'b' * 64]


@pytest.mark.parametrize('field,value', [
    ('tenant', 'other'), ('installation', 'other'), ('device_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'),
    ('environment', 'other'), ('audience', 'other'), ('case_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'),
    ('claim_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'), ('key_id', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'),
    ('policy_epoch', 2), ('realm_epoch', 'other'), ('case_epoch', 2), ('local_consent_epoch', 2),
    ('device_epoch', 2), ('approved_by', 'd' * 64), ('operator_refs', ['a' * 64, 'b' * 64, 'd' * 64]),
    ('diagnostic_ids', ['runtime.status.v1']), ('consent_expires_at', NOW + 1700000)])
def test_binding_scope_changes_are_not_accepted_with_old_hash(field, value):
    with pytest.raises(ValueError, match='^SUPPORT_CONSENT_BINDING_MISMATCH$'):
        contract.validate_preview({**PREVIEW, field: value}, now_ms=NOW)


@pytest.mark.parametrize('field,value', [
    ('unknown', 'SYNTHETIC_SECRET'), ('product', 'other'), ('schema_version', 'other'),
    ('purpose_code', 'RUN_COMMAND'), ('capability_revision', 'other'), ('consent_revision', 'other'),
    ('device_epoch', True), ('policy_epoch', 0), ('preview_id', 'other'),
    ('claim_id', None), ('key_id', True), ('operator_refs', []), ('operator_refs', ['a' * 64] * 2),
    ('operator_refs', ['b' * 64, 'a' * 64]), ('operator_refs', [str(n).zfill(64) for n in range(17)]),
    ('operator_refs', ['secret']), ('diagnostic_ids', []),
    ('diagnostic_ids', ['runtime.status.v1', 'runtime.status.v1']),
    ('diagnostic_ids', ['runtime.status.v1', 'connectivity.status.v1']),
    ('diagnostic_ids', ['filesystem.read']), ('expires_at', NOW + 300001),
    ('consent_expires_at', NOW + 1800001), ('consent_expires_at', NOW + 299999),
    ('consent_expires_at', True), ('consent_binding_hash', None)])
def test_closed_shape_limits_and_list_order_reject(field, value):
    with pytest.raises(ValueError):
        contract.validate_preview({**PREVIEW, field: value}, now_ms=NOW)


def test_half_open_preview_window_and_refresh_without_consent_scope_change():
    assert contract.validate_preview(PREVIEW, now_ms=NOW + 299999) == PREVIEW
    for now in (NOW - 1, NOW + 300000, True, None, 2**53):
        with pytest.raises(ValueError):
            contract.validate_preview(PREVIEW, now_ms=now)
    refreshed = {**PREVIEW, 'preview_id': 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
                 'issued_at': NOW + 1, 'not_before': NOW + 1, 'expires_at': NOW + 300001}
    assert contract.hash_consent_binding(refreshed) == EXPECTED_BINDING
    assert contract.validate_preview(refreshed, now_ms=NOW + 1) == refreshed


def test_each_unknown_or_missing_field_is_rejected():
    for key in PREVIEW:
        value = {name: child for name, child in PREVIEW.items() if name != key}
        with pytest.raises(ValueError):
            contract.validate_preview(value, now_ms=NOW)


def test_preview_uses_existing_cjson_duplicate_and_unicode_rejection():
    raw = contract.canonical(PREVIEW)
    for text in (raw.replace('{', '{"preview_id":"other",', 1),
                 raw.replace('tenant-a', r'\u00e9'), raw + '\n'):
        with pytest.raises(ValueError):
            contract.validate_preview(text.encode('ascii'), now_ms=NOW)


def test_old_pin_purposes_never_authorize_preview_before_crypto(monkeypatch):
    fixtures = Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/support/signature-vectors.json'
    vector = json.loads(fixtures.read_text())
    envelope = dict(vector['vectors'][0]['envelope'])
    envelope['purpose'] = 'consent-preview'
    envelope['payload'] = base64.urlsafe_b64encode(contract.canonical(PREVIEW).encode('ascii')).rstrip(b'=').decode('ascii')
    def no_crypto(*_args):
        pytest.fail('old purpose pins must reject before crypto')
    monkeypatch.setattr(signatures, '_verify_pss', no_crypto)
    with pytest.raises(signatures.SignatureError):
        signatures.verify_envelope(json.dumps(envelope).encode('ascii'), purpose='consent-preview',
                                   pins=vector['pinned_keys'], now_ms=NOW)


def test_cross_purpose_preview_is_rejected_before_crypto(monkeypatch):
    def no_crypto(*_args):
        pytest.fail('purpose mismatch must reject before crypto')
    monkeypatch.setattr(signatures, '_verify_pss', no_crypto)
    fixtures = Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/support/signature-vectors.json'
    vector = json.loads(fixtures.read_text())
    pins = copy.deepcopy(vector['pinned_keys'])
    pins[0]['purposes'].append('consent-preview')
    raw = json.dumps(vector['vectors'][0]['envelope']).encode('ascii')
    with pytest.raises(signatures.SignatureError):
        signatures.verify_envelope(raw, purpose='consent-preview', pins=pins, now_ms=NOW)


def test_explicit_preview_pin_is_accepted_only_for_its_declared_use():
    fixtures = Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/support/signature-vectors.json'
    vector = json.loads(fixtures.read_text())
    pin = copy.deepcopy(vector['pinned_keys'][0])
    pin['purposes'] = ['consent-preview']
    assert signatures._pin([pin], pin['kid'], 'consent-preview', NOW)
    with pytest.raises(signatures.SignatureError):
        signatures._pin([pin], pin['kid'], 'operation', NOW)
    pin['purposes'] = ['consent-preview', 'unknown']
    with pytest.raises(signatures.SignatureError):
        signatures._pin([pin], pin['kid'], 'consent-preview', NOW)


SHARED = Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/support'


def test_fixed_preview_schema_matches_the_closed_fields_and_binding_definition():
    schema = json.loads((SHARED / 'consent-preview.schema.json').read_text())
    vectors = json.loads((SHARED / 'consent-preview-vectors.json').read_text())
    assert schema['additionalProperties'] is False
    assert len(schema['required']) == 27
    assert set(PREVIEW) == set(schema['required']) == set(schema['properties'])
    assert schema['properties']['diagnostic_ids']['maxItems'] == 3
    assert schema['properties']['operator_refs']['maxItems'] == 16
    assert set(vectors['binding_excluded_fields']) == {
        'preview_id', 'issued_at', 'not_before', 'expires_at', 'consent_binding_hash'}
    for vector in vectors['vectors']:
        payload = vector['payload']
        assert contract.validate_preview(payload, now_ms=vectors['now']) == payload
        assert contract.canonical(payload) == vector['payload_canonical']
        assert contract.hash_consent_binding(payload) == payload['consent_binding_hash']
        core = {key: child for key, child in payload.items() if key not in vectors['binding_excluded_fields']}
        assert contract.canonical(core) == vector['binding_canonical']
        assert hashlib.sha256(vector['binding_canonical'].encode('ascii')).hexdigest() == payload['consent_binding_hash']


def test_shared_preview_signature_vector_uses_real_ps256_with_explicit_pin():
    vectors = json.loads((SHARED / 'consent-preview-vectors.json').read_text())
    for vector in vectors['vectors']:
        raw = json.dumps(vector['envelope'], separators=(',', ':')).encode('ascii')
        result = signatures.verify_envelope(raw, purpose='consent-preview',
                                           pins=vectors['pinned_keys'], now_ms=vectors['now'])
        assert result == vector['payload']
        assert contract.validate_preview(result, now_ms=vectors['now']) == vector['payload']


def test_shared_preview_signature_cannot_validate_tampered_scope_or_binding():
    vectors = json.loads((SHARED / 'consent-preview-vectors.json').read_text())
    vector = vectors['vectors'][0]
    for field, value in [('tenant', 'other'), ('device_epoch', 2), ('operator_refs', ['f' * 64]),
                         ('consent_expires_at', vector['payload']['consent_expires_at'] + 1)]:
        payload = {**vector['payload'], field: value}
        envelope = {**vector['envelope'], 'payload': base64.urlsafe_b64encode(
            contract.canonical(payload).encode('ascii')).rstrip(b'=').decode('ascii')}
        with pytest.raises(signatures.SignatureError):
            signatures.verify_envelope(json.dumps(envelope).encode('ascii'), purpose='consent-preview',
                                       pins=vectors['pinned_keys'], now_ms=vectors['now'])


def test_control_preview_response_uses_the_shared_signed_payload():
    vectors = json.loads((SHARED / 'control-vectors.json').read_text())
    previews = json.loads((SHARED / 'consent-preview-vectors.json').read_text())
    found = 0
    for vector in vectors['response_vectors']:
        body = vector['body']
        if 'preview' in body:
            found += 1
            assert body['preview'] == previews['vectors'][0]['envelope']
            raw = base64.urlsafe_b64decode(body['preview']['payload'] + '=' * (-len(body['preview']['payload']) % 4))
            payload = contract.validate_preview(raw, now_ms=vector['now'])
            assert body['preview_id'] == payload['preview_id']
    assert found == 1


def test_device_epoch_is_mandatory_in_all_fixed_payloads_and_hash_coverage():
    vectors = json.loads((SHARED / 'signature-vectors.json').read_text())
    operation = json.loads(vectors['vectors'][0]['payload_canonical'])
    grant = json.loads(vectors['vectors'][1]['payload_canonical'])
    for value, validator in [(operation, contract.validate_operation), (grant, contract.validate_grant),
                             (vectors['approval'], contract.validate_approval)]:
        for bad in (None, True, 0, -1, 2**53):
            with pytest.raises(ValueError):
                validator({**value, 'device_epoch': bad}, now_ms=vectors['now'])
        missing = {key: child for key, child in value.items() if key != 'device_epoch'}
        with pytest.raises(ValueError):
            validator(missing, now_ms=vectors['now'])
    assert contract.hash_payload({**operation, 'device_epoch': 2}) != operation['payload_hash']
    assert contract.hash_intent({**operation, 'device_epoch': 2}) != vectors['approval']['intent_hash']
    assert contract.hash_approval({**vectors['approval'], 'device_epoch': 2}) != vectors['approval']['approval_hash']


def test_relevant_negative_control_approval_vectors_remain_rejected():
    vectors = json.loads((SHARED / 'control-vectors.json').read_text())
    selected = 0
    for vector in vectors['negative_response_vectors']:
        if vector['name'] in ('poll-operation-with-null-approval', 'poll-approval-missing-device-epoch'):
            selected += 1
            with pytest.raises(ValueError):
                contract.validate_approval(vector['body']['approval'], now_ms=vectors['now'])
    assert selected == 2
