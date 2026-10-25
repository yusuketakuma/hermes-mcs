"""Synthetic initial registration read contract; no origin provisioning, consent, persistence or HTTP."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import restart_control as control

ROOT = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts'
V = json.loads((ROOT / 'support/restart-registration-vectors.json').read_text())
SCHEMA = json.loads((ROOT / 'support/restart-registration.schema.json').read_text())
REQUEST = next(v['value'] for v in V['positive'] if v['definition'] == 'ReadRequest')
RESPONSE = next(v['value'] for v in V['positive'] if v['definition'] == 'ReadResponse')
NOW, PATH = V['now'], '/v2/support/restarts/services/read'
OTHER = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
CANARY = 'SYNTHETIC_PRIVATE_NEVER_IN_ERRORS'


def raw(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')


def response(value=RESPONSE, *, request=REQUEST, now_ms=NOW):
    return control.validate_response(PATH, value, request=request, now_ms=now_ms)


def rejected(value, *, request=False, now_ms=NOW):
    kind = 'REQUEST' if request else 'RESPONSE'
    with pytest.raises(ValueError, match='^SDC_RESTART_CONTROL_' + kind + '_INVALID$') as caught:
        if request:
            control.validate_request(PATH, value, now_ms=now_ms)
        else:
            response(value, now_ms=now_ms)
    assert CANARY not in str(caught.value)


def test_central_copies_and_existing_shared_contracts_keep_their_frozen_bytes():
    sources = json.loads((ROOT / 'sources.json').read_text())
    frozen = {'support/restart-registration.schema.json': '01a05565e821ab0b134e69903513b97c3608779da1818de217dad6fd87d774d8',
              'support/restart-registration-vectors.json': 'eb6ce02d696631810d3ab58ce98085407d8303da7adb1389eb98439e1338387c',
              'provision/provision.schema.json': 'b2bea5be19d01a3fb3021408145e39aa9f8a4cd2d76fd7acc0d54c404f2b3649',
              'provision/provision-vectors.json': 'd6c49ea2dc62d5780f87b8f26f3cb9106f7b36ca36fa66f88b84bcd396fd81aa',
              'diagnostics/diagnostics.schema.json': 'ada29dd8777db53672e75a14a16fc1ed175135a34c4fdb21e56e29908b3ac427',
              'diagnostics/diagnostics-vectors.json': '97ee03a1766fc538ac5c505a508b5e16f765b64c698c76b8bf04218a1b561fae'}
    release_files = {
        'releases/apply-api.schema.json': 'a7f68f5cdc8c69b32b3803c03556106e67b464337b5def9089e1d8532d80b082',
        'releases/apply-vectors.json': '0ed3564a62d7e24f18170ad25e882ca1f63feac0d1164bab73fc26fd168329a3',
        'releases/apply.schema.json': '1f10723acabb3defa2639e81aaf1cd8cb4c4f4a48e7959cc7c9ba806215416a0',
        'releases/release-vectors.json': '8d6fd9ddfb445b3a47b96b4359bd88d3d277a6884b2ee2ab9a2a7544ea888381',
        'releases/release.schema.json': 'ba6161cb73cc2e77409412e74b4c09ca91bef90504d5b5eca9cf9315a37d763f',
        'releases/signature-vectors.json': '6c33fb6823f2c784f126b6b6f3be6ab3dd3e9c17882fba100ba22a08f1b31379',
    }
    assert len(sources['files']) == 26
    assert {path: sources['files'][path] for path in release_files} == release_files
    assert set(sources['release_apply_component']['files']) == release_files.keys()
    original = {path: digest for path, digest in sources['files'].items() if path not in release_files}
    # Pin the original 20 entries as well as the six byte-identical central release contracts.
    assert len(original) == 20
    assert hashlib.sha256(raw(original)).hexdigest() == '63dcc9080cc19d99e57faed6d159944fa5c88d8d43bb1198a63a75adf0f46c98'
    assert frozen.keys() <= sources['files'].keys()
    for path, expected in sources['files'].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected
        if path in frozen:
            assert expected == frozen[path]
        elif path.startswith('support/'):
            assert expected == V['source_sha256'][path.removeprefix('support/')]
    component = sources['restart_registration_component']
    assert component['packet_sha256'] == '3f38a7fb0e5f1f6f972b4196795da596c7b011d3a837f7098b04350777ec9de9'
    assert component['openapi_reference']['source_path'] == 'docs/api/restart-registration-openapi.yaml'
    assert component['openapi_reference']['sha256'] == 'c8ac8df14d1b19b397a836c736910e5f57515dc11e5b116985aa7cabb2d67eb9'
    assert component['openapi_reference']['vendored'] is False
    assert component['product_runtime_connected'] is False and component['real_environment_accepted'] is False


@pytest.mark.parametrize('definition,value', [('ReadRequest', REQUEST), ('ReadResponse', RESPONSE)])
def test_fixed_positive_request_and_http_response_match_closed_schema(definition, value):
    actual = control.validate_request(PATH, value, now_ms=NOW) if definition == 'ReadRequest' else response(value)
    assert actual == value and actual is not value
    schema = SCHEMA['$defs'][definition]
    assert schema['additionalProperties'] is False
    assert set(schema['required']) == set(schema['properties']) == set(actual)


@pytest.mark.parametrize('vector', [v for v in V['negative'] if v['definition'] in ('ReadRequest', 'ReadResponse')], ids=lambda v: v['name'])
def test_fixed_http_negative_vectors(vector):
    rejected(vector['value'], request=vector['definition'] == 'ReadRequest')


@pytest.mark.parametrize('vector', [v for v in V['negative'] if v['definition'] == 'ReadResult' and v['name'] != 'Handler result with request ID'], ids=lambda v: v['name'])
def test_registration_negatives_remain_invalid_when_wrapped_as_http(vector):
    rejected({**vector['value'], 'request_id': RESPONSE['request_id']})


def test_handler_and_error_vectors_are_not_misread_as_success_responses():
    for vector in V['positive']:
        if vector['definition'] in ('ReadResult', 'ErrorResponse'):
            rejected(vector['value'])
    # This is negative only for the central handler, whose result excludes request_id.
    wrapped = next(v['value'] for v in V['negative'] if v['name'] == 'Handler result with request ID')
    assert response(wrapped) == wrapped
    for vector in V['negative']:
        if vector['definition'] == 'ErrorResponse':
            rejected(vector['value'])
    assert {v['definition'] for group in ('positive', 'negative') for v in V[group]} == {
        'ReadRequest', 'ReadResponse', 'ReadResult', 'ErrorResponse'}


@pytest.mark.parametrize('is_request', [True, False])
def test_missing_extra_duplicate_and_noncanonical_inputs_never_escape_raw_values(is_request):
    original = REQUEST if is_request else RESPONSE
    for field in original:
        value = copy.deepcopy(original)
        del value[field]
        rejected(value, request=is_request)
    rejected({**original, 'private': CANARY}, request=is_request)
    encoded = raw(original)
    for invalid in (encoded.replace(b'{', b'{"case_id":"' + OTHER.encode() + b'",', 1),
                    b'\xef\xbb\xbf' + encoded, encoded + b' trailing', b'{"x":-0}', b'{"x":1e0}',
                    b'{"x":NaN}', b'{"x":"\\u00e9"}', b' ' + encoded, b' ' * 16385):
        rejected(invalid, request=is_request)
    cycle = copy.deepcopy(original)
    cycle['private'] = cycle
    rejected(cycle, request=is_request)


def test_exact_canonical_inputs_return_detached_records():
    assert control.validate_request(PATH, raw(REQUEST), now_ms=NOW) == REQUEST
    actual = response(raw(RESPONSE), request=raw(REQUEST))
    actual['registration']['service_revision'] = 7
    assert RESPONSE['registration']['service_revision'] == 1


@pytest.mark.parametrize('field', ['case_id', 'local_receipt_id'])
def test_response_echoes_both_original_ids(field):
    rejected({**RESPONSE, field: OTHER})
    for invalid in (None, 1, True, OTHER.upper(), '00000000-0000-1000-8000-000000000002'):
        rejected({**REQUEST, field: invalid}, request=True)
        rejected({**RESPONSE, field: invalid})


@pytest.mark.parametrize('changes', [{'state': 'revoked'}, {'not_before': NOW + 1}, {'expires_at': NOW},
                                    {'expires_at': NOW - 1}, {'service_revision': 0}, {'service_revision': True},
                                    {'service_revision': 2**53}, {'causal_evidence_supported': None},
                                    {'service_id': 'other'}, {'effects_profile': 'other'}, {'origin': CANARY}])
def test_registration_requires_current_active_known_service_shape(changes):
    value = copy.deepcopy(RESPONSE)
    value['registration'].update(changes)
    rejected(value)


def test_first_revision_is_returned_without_fixed_one_or_causal_success_assumption():
    value = copy.deepcopy(RESPONSE)
    value['registration'].update(service_revision=7, causal_evidence_supported=False, not_before=NOW)
    assert response(value) == value
    # Pure shape cannot establish which known case baseline this tenant belongs to.
    value['registration']['tenant'] = 'other-synthetic-tenant'
    assert response(value)['registration']['tenant'] == 'other-synthetic-tenant'


@pytest.mark.parametrize('bad', [None, True, False, -1, 2**53, 0.1])
def test_time_is_required_safe_integer_for_read_request_and_response(bad):
    rejected(REQUEST, request=True, now_ms=bad)
    rejected(RESPONSE, now_ms=bad)


@pytest.mark.parametrize('path', [PATH + '?x=1', PATH + '#x', PATH + '/', PATH.upper(), 'https://example.invalid' + PATH])
def test_registration_read_path_has_no_query_or_dynamic_target(path):
    with pytest.raises(ValueError, match='^SDC_RESTART_CONTROL_REQUEST_INVALID$'):
        control.validate_request(path, REQUEST, now_ms=NOW)


def test_read_never_returns_operation_grant_pin_or_business_receipt():
    for field in ('operation', 'grant', 'inspection', 'pin', 'receipt_id'):
        rejected({**RESPONSE, field: CANARY})
    changed = {**RESPONSE, 'request_id': OTHER}
    assert response(changed) == changed
    rejected({**RESPONSE, 'schema_version': 'sdc.support.restart-response.v1'})
