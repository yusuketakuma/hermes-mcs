"""Independent fixed-vector and adversarial oracles; no crypto, service or network."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import support_contract as contract

ROOT = Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/support'
VECTORS = json.loads((ROOT / 'signature-vectors.json').read_text())
NOW = VECTORS['now']
OPERATION = json.loads(VECTORS['vectors'][0]['payload_canonical'])
GRANT = json.loads(VECTORS['vectors'][1]['payload_canonical'])
APPROVAL = VECTORS['approval']


def test_all_positive_signature_payload_vectors_have_exact_canonical_and_hashes():
    for vector in VECTORS['vectors']:
        raw = vector['payload_canonical'].encode('ascii')
        value = contract.parse_canonical(raw)
        assert contract.canonical(value) == vector['payload_canonical']
        if vector['purpose'] == 'operation':
            assert contract.validate_operation(raw, now_ms=NOW) == value
            assert contract.hash_payload(value) == value['payload_hash']
            assert contract.hash_intent(value) == hashlib.sha256(VECTORS['intent_canonical'].encode('ascii')).hexdigest()
            assert contract.hash_intent(value) == APPROVAL['intent_hash']
        else:
            assert contract.validate_grant(raw, now_ms=NOW) == value
    assert contract.hash_approval(APPROVAL) == APPROVAL['approval_hash'] == OPERATION['approval_hash']
    assert contract.validate_approval(APPROVAL, now_ms=NOW) == APPROVAL


def test_hash_helpers_accept_unsigned_cores_and_ignore_only_their_own_digest():
    payload = {k: v for k, v in OPERATION.items() if k != 'payload_hash'}
    approval = {k: v for k, v in APPROVAL.items() if k != 'approval_hash'}
    intent = json.loads(VECTORS['intent_canonical'])
    assert contract.hash_payload(payload) == OPERATION['payload_hash']
    assert contract.hash_approval(approval) == APPROVAL['approval_hash']
    assert contract.hash_intent(intent) == APPROVAL['intent_hash']


@pytest.mark.parametrize('validator,original', [
    (contract.validate_operation, OPERATION), (contract.validate_grant, GRANT),
    (contract.validate_approval, APPROVAL)])
def test_validation_detaches_input_and_returned_nested_values(validator, original):
    value = copy.deepcopy(original)
    result = validator(value, now_ms=NOW)
    assert result == value and result is not value
    if 'params' in value:
        assert result['params'] is not value['params']
        result['params']['diagnostic_id'] = 'changed'
        assert value['params'] == original['params']
    value['schema_version'] = 'changed'
    assert result['schema_version'] == original['schema_version']


@pytest.mark.parametrize('validator,original', [
    (contract.validate_operation, OPERATION), (contract.validate_grant, GRANT),
    (contract.validate_approval, APPROVAL)])
@pytest.mark.parametrize('field,value', [
    ('unknown', 'SYNTHETIC_SECRET'), ('schema_version', 'unknown'),
    ('realm_epoch', ''), ('policy_epoch', True), ('local_consent_epoch', 0), ('case_epoch', 2**53),
    ('issued_at', True), ('not_before', -1), ('expires_at', 2**53)])
def test_all_closed_shapes_and_epoch_time_types_reject(validator, original, field, value):
    changed = {**original, field: value}
    with pytest.raises(ValueError, match='^SUPPORT_'):
        validator(changed, now_ms=NOW)


@pytest.mark.parametrize('original,validator', [(OPERATION, contract.validate_operation), (GRANT, contract.validate_grant)])
@pytest.mark.parametrize('field,value', [('tenant', 'Upper'), ('installation', 'x\n'),
    ('device_id', '11111111-1111-1111-8111-111111111111'), ('case_id', True), ('environment', 'x/y')])
def test_operation_and_grant_scope_syntax_reject(original, validator, field, value):
    with pytest.raises(ValueError):
        validator({**original, field: value}, now_ms=NOW)


@pytest.mark.parametrize('validator,original,limit', [
    (contract.validate_operation, OPERATION, 1800000),
    (contract.validate_approval, APPROVAL, 1800000),
    (contract.validate_grant, GRANT, 30000)])
def test_lifetimes_are_half_open_and_bounded(validator, original, limit):
    assert validator(original, now_ms=original['not_before']) == original
    assert validator(original, now_ms=original['expires_at'] - 1) == original
    for now in (original['not_before'] - 1, original['expires_at'], True, None, 2**53):
        with pytest.raises(ValueError):
            validator(original, now_ms=now)
    for times in ({'issued_at': NOW + 1, 'not_before': NOW},
                  {'not_before': original['expires_at']},
                  {'expires_at': original['issued_at'] + limit + 1}):
        with pytest.raises(ValueError):
            validator({**original, **times}, now_ms=NOW)


@pytest.mark.parametrize('field,value', [('product', 'other'), ('type', 'commands.run'),
    ('audience', 'x/y'), ('consent_revision', 'sdc-self-monitoring-v1'),
    ('capability_revision', 'unknown'), ('purpose_code', 'ARBITRARY'),
    ('requested_by', 'x' * 64), ('approved_by', None), ('approval_id', 'unknown'),
    ('params', {'diagnostic_id': 'filesystem.read'}),
    ('params', {'diagnostic_id': 'runtime.status.v1', 'path': 'SYNTHETIC_SECRET'})])
def test_operation_does_not_expand_r4a(field, value):
    with pytest.raises(ValueError):
        contract.validate_operation({**OPERATION, field: value}, now_ms=NOW)


def test_digest_mismatch_is_not_accepted_as_success():
    with pytest.raises(ValueError, match='^SUPPORT_PAYLOAD_HASH_MISMATCH$'):
        contract.validate_operation({**OPERATION, 'payload_hash': '0' * 64}, now_ms=NOW)
    with pytest.raises(ValueError, match='^SUPPORT_APPROVAL_HASH_MISMATCH$'):
        contract.validate_approval({**APPROVAL, 'approval_hash': '0' * 64}, now_ms=NOW)
    changed = {**OPERATION, 'tenant': 'other'}
    assert contract.hash_payload(changed) != OPERATION['payload_hash']
    with pytest.raises(ValueError):
        contract.validate_operation(changed, now_ms=NOW)


@pytest.mark.parametrize('diagnostic_id', contract.DIAGNOSTIC_IDS)
def test_each_r4a_diagnostic_is_accepted_with_recomputed_payload(diagnostic_id):
    value = {**OPERATION, 'params': {'diagnostic_id': diagnostic_id}}
    value['payload_hash'] = contract.hash_payload(value)
    assert contract.validate_operation(value, now_ms=NOW)['params']['diagnostic_id'] == diagnostic_id


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"b":2,"a":1}', b'{ "a":1}',
    b'{"a":1}\n', b'{"a":-0}', b'{"a":1.0}', b'{"a":1e0}', b'{"a":NaN}',
    b'{"a":Infinity}', b'{"a":"\\u0061"}', b'{"a":"\\/"}', b'"\\n"',
    b'"\\u00e9"', b'"\xff"', b'9007199254740992', b'-9007199254740992', b'\xef\xbb\xbf{}'])
def test_noncanonical_or_unsafe_raw_json_is_rejected(raw):
    with pytest.raises(ValueError, match='^SUPPORT_INVALID_CONTRACT$'):
        contract.parse_canonical(raw)


@pytest.mark.parametrize('value', [1.0, -0.0, float('nan'), float('inf'), 2**53, -(2**53),
    'é', '\n', {'': 1}, {'\n': 1}, {1: 'x'}, tuple(), set(), object(),
    {str(n): n for n in range(65)}, [0] * 1025, 'x' * 16385])
def test_non_cjson_objects_are_rejected(value):
    with pytest.raises(ValueError, match='^SUPPORT_INVALID_CONTRACT$'):
        contract.canonical(value)


def test_cycles_depth_subclasses_and_size_limits():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError):
        contract.canonical(cycle)
    deep = 0
    for _ in range(9):
        deep = [deep]
    with pytest.raises(ValueError):
        contract.canonical(deep)
    class Foreign(dict):
        pass
    with pytest.raises(ValueError):
        contract.canonical(Foreign())
    assert contract.canonical({'a': True, 'b': None}) == '{"a":true,"b":null}'
    assert contract.canonical(9007199254740991) == '9007199254740991'
    assert contract.canonical(-9007199254740991) == '-9007199254740991'
    assert contract.canonical('x' * 8190) == '"' + 'x' * 8190 + '"'
    with pytest.raises(ValueError):
        contract.canonical('x' * 8191)
    with pytest.raises(ValueError):
        contract.parse_canonical(b' ' * 8193)
    for invalid in (True, 0, -1, 16385):
        with pytest.raises(ValueError):
            contract.canonical({}, max_bytes=invalid)


def test_schema_fields_are_the_closed_operation_approval_and_grant_contracts():
    schema = json.loads((ROOT / 'operation.schema.json').read_text())
    for value, definition in ((OPERATION, schema), (APPROVAL, schema['$defs']['approval']),
                              (GRANT, schema['$defs']['grant'])):
        assert definition['additionalProperties'] is False
        assert set(value) == set(definition['required']) == set(definition['properties'])
    assert schema['properties']['params']['properties']['diagnostic_id']['enum'] == list(contract.DIAGNOSTIC_IDS)
    assert schema['properties']['purpose_code']['enum'] == list(contract.PURPOSE_CODES)


def test_all_control_response_signed_payloads_match_the_same_contract():
    vectors = json.loads((ROOT / 'control-vectors.json').read_text())
    import base64
    found = set()
    for vector in vectors['response_vectors']:
        body = vector['body']
        for field, validator in (('operation', contract.validate_operation), ('grant', contract.validate_grant)):
            envelope = body.get(field)
            if envelope:
                if field == 'operation' and 'report_id' in body:
                    # Receipt status returns metadata, whereas poll returns a signed envelope.
                    assert set(body) == {'schema_version', 'operation_id', 'receipt_id', 'report_id',
                                         'report', 'operation', 'request_id'}
                    assert set(envelope) == {'state', 'state_revision'}
                    continue
                encoded = envelope['payload']
                raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
                assert validator(raw, now_ms=vectors['now']) == contract.parse_canonical(raw)
                found.add(field)
    assert found == {'operation', 'grant'}
