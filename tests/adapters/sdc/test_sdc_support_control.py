"""Synthetic wire-shape oracles; context-only negatives do not establish authorization."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import support_control as control

ROOT = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/support'
VECTORS = json.loads((ROOT / 'control-vectors.json').read_text())
KINDS = {'Poll': control.POLL, 'Admit': control.ADMIT, 'Grant': control.GRANT,
         'Report': control.REPORT, 'Status': control.STATUS}
REQUESTS = {path: json.loads(next(v['raw_json'] for v in VECTORS['positive_requests']
            if v['name'] == kind + 'Request-positive')) for kind, path in KINDS.items()}
CANARY = 'SYNTHETIC_SECRET_DO_NOT_COPY'
OTHER_ID = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'


def response_route(vector):
    kind = vector['schema_ref'].rsplit('/', 1)[-1]
    return next((path for name, path in KINDS.items() if kind == name + 'Response'), None)


@pytest.mark.parametrize('vector', VECTORS['positive_requests'], ids=lambda v: v['name'])
def test_all_shared_positive_requests_with_explicit_scope(vector):
    path, raw = vector['path'], vector['raw_json'].encode('ascii')
    assert hashlib.sha256(raw).hexdigest() == vector['raw_sha256']
    if path in REQUESTS:
        assert control.validate_request(path, raw) == json.loads(raw)
    else:
        with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_REQUEST_INVALID$'):
            control.validate_request(path, raw)


@pytest.mark.parametrize('vector', VECTORS['negative_requests'], ids=lambda v: v['name'])
def test_shared_negatives_distinguish_shape_from_authority_and_replay(vector):
    path, raw = vector['path'], vector['raw_json'].encode('ascii')
    if path in REQUESTS and vector['expected_layer'] == 'authorization':
        # No authority/current-case/replay store exists in this pure DTO unit.
        assert control.validate_request(path, raw) == json.loads(raw)
    else:
        with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_REQUEST_INVALID$'):
            control.validate_request(path, raw)
        if path in REQUESTS and vector['expected_layer'] == 'idempotency':
            # Its noncanonical raw bytes fail, but normalized shape alone cannot
            # decide replay identity or authorize regeneration of request bytes.
            assert control.validate_request(path, json.loads(raw)) == json.loads(raw)


@pytest.mark.parametrize('vector', VECTORS['raw_negative_requests'], ids=lambda v: v['name'])
def test_shared_raw_negative_vectors(vector):
    with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_REQUEST_INVALID$'):
        control.validate_request(vector['path'], bytes.fromhex(vector['raw_hex']))


@pytest.mark.parametrize('vector', VECTORS['response_vectors'], ids=lambda v: v['name'])
def test_all_shared_positive_responses_or_explicit_out_of_scope(vector):
    path = response_route(vector)
    if path is None:
        with pytest.raises(ValueError):
            control.validate_response('/unsupported', vector['body'], request={})
    else:
        result = control.validate_response(path, vector['body'], request=REQUESTS[path])
        assert result == vector['body'] and result is not vector['body']
        if path == control.STATUS and vector['body']['report'] is None:
            assert result['report'] is None
            assert result['operation'] == vector['body']['operation']


@pytest.mark.parametrize('vector', VECTORS['negative_response_vectors'], ids=lambda v: v['name'])
def test_all_shared_negative_responses(vector):
    path = response_route(vector)
    with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_RESPONSE_INVALID$'):
        control.validate_response(path or '/unsupported', vector['body'], request=REQUESTS.get(path, {}))


def report_result(diagnostic_id, data):
    return {'schema_version': 'sdc.support.diagnostic-result.v1', 'diagnostic_id': diagnostic_id,
            'generated_at': REQUESTS[control.REPORT]['observed_at'], 'observed_at': None,
            'status': 'unknown', 'duration_ms': None, 'error_code': 'SOURCE_UNKNOWN', 'data': data}


DIAGNOSTICS = [
    report_result('runtime.status.v1', {'mode': 'unknown', 'process': 'unknown',
                  'installed_commit': None, 'loaded_commit': None, 'installed_version': None, 'loaded_version': None}),
    report_result('notification.health.v1', {'service': 'unknown', 'credential_configured': 'unknown',
                  'authentication': 'unknown', 'retry': 'unknown'}),
    report_result('connectivity.status.v1', {'target_id': 'sdc-central', 'connection': 'unknown', 'duration_ms': None}),
]


@pytest.mark.parametrize('result', DIAGNOSTICS, ids=lambda v: v['diagnostic_id'])
def test_all_fixed_diagnostic_fieldsets_and_unknown_evidence_are_supported(result):
    report = {**REQUESTS[control.REPORT], 'state': 'succeeded', 'result': result}
    assert control.validate_request(control.REPORT, report) == report
    result = copy.deepcopy(result)
    result['observed_at'] = result['generated_at'] - 1000
    assert control.validate_request(control.REPORT, {**report, 'result': result})['result']['observed_at'] != report['observed_at']
    result['data']['secret'] = CANARY
    with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_REQUEST_INVALID$'):
        control.validate_request(control.REPORT, {**report, 'result': result})


@pytest.mark.parametrize('field,bad', [('generated_at', True), ('observed_at', True), ('duration_ms', True),
    ('status', CANARY), ('error_code', CANARY), ('diagnostic_id', CANARY), ('generated_at', 2**53),
    ('generated_at', 0), ('observed_at', 2**53 - 1), ('duration_ms', 1800001)])
def test_diagnostic_times_enums_and_report_binding_are_strict(field, bad):
    result = {**DIAGNOSTICS[0], field: bad}
    with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_REQUEST_INVALID$'):
        control.validate_request(control.REPORT, {**REQUESTS[control.REPORT], 'state': 'succeeded', 'result': result})


@pytest.mark.parametrize('status,error', [('observed', 'SOURCE_UNKNOWN'), ('unknown', 'NONE')])
def test_observed_none_contract_is_bidirectional(status, error):
    result = {**DIAGNOSTICS[0], 'status': status, 'error_code': error}
    with pytest.raises(ValueError):
        control.validate_request(control.REPORT, {**REQUESTS[control.REPORT], 'state': 'succeeded', 'result': result})


@pytest.mark.parametrize('state', ['running', 'failed', 'unknown'])
def test_only_succeeded_may_carry_diagnostic_content(state):
    with pytest.raises(ValueError):
        control.validate_request(control.REPORT, {**REQUESTS[control.REPORT], 'state': state, 'result': DIAGNOSTICS[0]})
    assert control.validate_request(control.REPORT, {**REQUESTS[control.REPORT], 'state': state, 'result': None})['result'] is None


def test_succeeded_receipt_only_is_preserved_but_is_not_current_authority_evidence():
    value = {**REQUESTS[control.REPORT], 'state': 'succeeded', 'result': None}
    assert control.validate_request(control.REPORT, value) == value


@pytest.mark.parametrize('path', list(REQUESTS))
def test_request_fieldset_matches_schema_and_rejects_missing_unknown_bool_ids(path):
    schema = json.loads((ROOT / 'control.schema.json').read_text())
    name = next(name for name, route in KINDS.items() if route == path)
    definition = schema['$defs'][name + 'Request']
    value = REQUESTS[path]
    assert set(value) == set(definition['required']) == set(definition['properties'])
    assert definition['additionalProperties'] is False
    for key in value:
        for changed in ({k: v for k, v in value.items() if k != key}, {**value, key: True}):
            with pytest.raises(ValueError):
                control.validate_request(path, changed)
    with pytest.raises(ValueError) as error:
        control.validate_request(path, {**value, CANARY: CANARY})
    assert CANARY not in str(error.value)


@pytest.mark.parametrize('path', list(REQUESTS))
def test_response_echo_ids_are_request_bound_and_server_request_id_is_independent(path):
    if path in (control.ADMIT, control.REPORT):
        body = {'schema_version': 'sdc.support.v1', 'operation_id': REQUESTS[path]['operation_id'],
                'receipt_id': REQUESTS[path]['receipt_id'], 'request_id': OTHER_ID,
                'state': 'locally_admitted' if path == control.ADMIT else REQUESTS[path]['state']}
        if path == control.REPORT:
            body['state_revision'] = REQUESTS[path]['state_revision']
    else:
        body = copy.deepcopy(next(v['body'] for v in VECTORS['response_vectors'] if response_route(v) == path))
    assert control.validate_response(path, body, request=REQUESTS[path]) == body
    for key in ('poll_id', 'operation_id', 'receipt_id', 'report_id'):
        if key in body:
            with pytest.raises(ValueError):
                control.validate_response(path, {**body, key: OTHER_ID}, request=REQUESTS[path])
    for changed in ({**body, CANARY: CANARY}, {k: v for k, v in body.items() if k != 'request_id'}):
        with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_RESPONSE_INVALID$'):
            control.validate_response(path, changed, request=REQUESTS[path])


def test_poll_historical_approval_hash_and_envelope_purpose_are_checked():
    body = copy.deepcopy(next(v['body'] for v in VECTORS['response_vectors'] if v['name'] == 'poll-operation'))
    for value in ('0' * 64, True):
        bad = {**body, 'approval': {**body['approval'], 'approval_hash': value}}
        with pytest.raises(ValueError):
            control.validate_response(control.POLL, bad, request=REQUESTS[control.POLL])
    for changed in ({**body['operation'], 'purpose': 'execution-grant'},
                    {**body['operation'], 'alg': 'HS256'}, {**body['operation'], 'secret': CANARY},
                    {**body['operation'], 'payload': base64.urlsafe_b64encode(b'{"x":1,"x":2}').rstrip(b'=').decode()}):
        with pytest.raises(ValueError):
            control.validate_response(control.POLL, {**body, 'operation': changed}, request=REQUESTS[control.POLL])


@pytest.mark.parametrize('value', [b'\xff', b'{"a":1,"a":2}', b' ' * 16385, {'extra': float('nan')}, [[]] * 1025])
def test_malformed_input_is_fixed_error(value):
    with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_REQUEST_INVALID$'):
        control.validate_request(control.POLL, value)
    with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_RESPONSE_INVALID$'):
        control.validate_response(control.POLL, value, request=REQUESTS[control.POLL])


def test_return_values_are_detached():
    request = {**REQUESTS[control.REPORT], 'state': 'succeeded', 'result': copy.deepcopy(DIAGNOSTICS[0])}
    result = control.validate_request(control.REPORT, request)
    result['result']['data']['mode'] = 'hermes'
    assert request['result']['data']['mode'] == 'unknown'


@pytest.mark.parametrize('result', DIAGNOSTICS, ids=lambda v: v['diagnostic_id'])
def test_each_diagnostic_field_rejects_arbitrary_secret_values(result):
    for key in result['data']:
        changed = {**result, 'data': {**result['data'], key: CANARY}}
        with pytest.raises(ValueError, match='^SDC_SUPPORT_CONTROL_REQUEST_INVALID$') as caught:
            control.validate_request(control.REPORT, {**REQUESTS[control.REPORT], 'state': 'succeeded', 'result': changed})
        assert CANARY not in str(caught.value)


def test_report_ack_state_and_revision_must_echo_that_report():
    request = REQUESTS[control.REPORT]
    response = {key: request[key] for key in ('schema_version', 'operation_id', 'receipt_id', 'state', 'state_revision')}
    response['request_id'] = OTHER_ID
    for change in ({'state_revision': request['state_revision'] + 1}, {'state': 'failed'}):
        with pytest.raises(ValueError):
            control.validate_response(control.REPORT, {**response, **change}, request=request)
