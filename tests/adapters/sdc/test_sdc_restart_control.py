"""Synthetic restart API shape/binding oracles; no signature or current authority is exercised."""
import base64
import copy
import json
from pathlib import Path

import pytest

from adapters.sdc import restart_contract as restart
from adapters.sdc import restart_control as control

ROOT = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/support'
V = json.loads((ROOT / 'restart-api-vectors.json').read_text())
SCHEMA = json.loads((ROOT / 'restart-api.schema.json').read_text())
NOW = V['now']
KINDS = {'Inspection': control.INSPECTION, 'Preview': control.PREVIEW, 'Accept': control.ACCEPT,
         'Poll': control.POLL, 'Admit': control.ADMIT, 'Grant': control.GRANT,
         'Report': control.REPORT, 'Status': control.STATUS}
REQUESTS = {name: next(x['value'] for x in V['positive'] if x['definition'] == name + 'Request') for name in KINDS}
RESPONSES = {name: next(x['value'] for x in V['positive'] if x['definition'] == name + 'Response') for name in KINDS}
OTHER = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
CANARY = 'SYNTHETIC_SECRET_NOT_FOR_OUTPUT'


def route(vector):
    return next((name for name in KINDS if vector['definition'] in (name + 'Request', name + 'Response')), None)


def original_request(name, response):
    request = copy.deepcopy(REQUESTS[name])
    if name == 'Report':
        request.update(state=response['state'], state_revision=response['state_revision'])
        if response['state'] != 'succeeded':
            request['result'] = None
    return request


def invalid_response(name, response, request=None, now_ms=NOW):
    with pytest.raises(ValueError, match='^SDC_RESTART_CONTROL_RESPONSE_INVALID$') as caught:
        control.validate_response(KINDS[name], response, request=REQUESTS[name] if request is None else request, now_ms=now_ms)
    assert CANARY not in str(caught.value)


def invalid_request(path, value, **options):
    with pytest.raises(ValueError, match='^SDC_RESTART_CONTROL_REQUEST_INVALID$') as caught:
        control.validate_request(path, value, now_ms=options.pop('now_ms', NOW), **options)
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize('vector', [v for v in V['positive'] if route(v)], ids=lambda v: v['name'])
def test_all_applicable_fixed_positive_vectors(vector):
    name = route(vector)
    value = vector['value']
    if vector['definition'].endswith('Request'):
        actual = control.validate_request(KINDS[name], value, now_ms=NOW)
    else:
        actual = control.validate_response(KINDS[name], value, request=original_request(name, value), now_ms=NOW)
    assert actual == value and actual is not value
    if vector['definition'].endswith('Response'):
        definition = SCHEMA['$defs'][vector['definition']]
        assert definition['additionalProperties'] is False
        assert set(definition['required']) == set(definition['properties']) == set(actual)


@pytest.mark.parametrize('vector', [v for v in V['negative'] if route(v)], ids=lambda v: v['name'])
def test_all_applicable_fixed_negative_vectors(vector):
    name = route(vector)
    if vector['definition'].endswith('Request'):
        invalid_request(KINDS[name], vector['value'])
    else:
        invalid_response(name, vector['value'])


def test_vectors_excluded_only_for_explicit_admin_error_or_handler_contracts():
    for group in ('positive', 'negative'):
        for vector in V[group]:
            if not route(vector):
                definition = vector['definition']
                assert definition.startswith('Admin') or definition.endswith('Result') or definition == 'ErrorResponse'
    # Handler outputs omit request_id and cannot enter the HTTP response API.
    for name in KINDS:
        value = next(x['value'] for x in V['positive'] if x['definition'] == name + 'Result')
        invalid_response(name, value)
    for vector in V['positive']:
        if vector['definition'].startswith('Admin'):
            invalid_request('/v2/support/restarts/admin/register', vector['value'])


@pytest.mark.parametrize('name', tuple(KINDS))
def test_request_and_response_missing_unknown_fields_rejected(name):
    for key in REQUESTS[name]:
        value = copy.deepcopy(REQUESTS[name])
        del value[key]
        invalid_request(KINDS[name], value)
    invalid_request(KINDS[name], {**REQUESTS[name], 'secret': CANARY})
    for key in RESPONSES[name]:
        value = copy.deepcopy(RESPONSES[name])
        del value[key]
        invalid_response(name, value)
    invalid_response(name, {**RESPONSES[name], 'secret': CANARY})


@pytest.mark.parametrize('name', tuple(KINDS))
def test_canonical_raw_and_detached_output(name):
    value = RESPONSES[name]
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii')
    actual = control.validate_response(KINDS[name], raw, request=REQUESTS[name], now_ms=NOW)
    assert actual == value
    actual['request_id'] = OTHER
    assert RESPONSES[name]['request_id'] != OTHER
    for broken in (raw.replace(b'{', b'{"request_id":"' + OTHER.encode() + b'",', 1), b'\xef\xbb\xbf' + raw,
                   raw + b' trailing', b'{"a":-0}', b'{"a":1e0}', b'{"a":NaN}', b' ' * 16385):
        invalid_response(name, broken)


@pytest.mark.parametrize('bad', (None, True, False, -1, 2**53, 0.1))
def test_explicit_time_is_safe_integer(bad):
    invalid_request(control.PREVIEW, REQUESTS['Preview'], now_ms=bad)
    invalid_response('Preview', RESPONSES['Preview'], now_ms=bad)


@pytest.mark.parametrize('name,key', [('Inspection', 'inspection_id'), ('Preview', 'preview_id'), ('Accept', 'accept_id'),
                                    ('Accept', 'preview_id'), ('Accept', 'change_consent_id'), ('Accept', 'local_change_receipt_id'),
                                    ('Admit', 'operation_id'), ('Admit', 'receipt_id'), ('Report', 'operation_id'),
                                    ('Report', 'receipt_id'), ('Status', 'operation_id'), ('Status', 'receipt_id'), ('Status', 'report_id')])
def test_outer_response_echoes_original_request(name, key):
    invalid_response(name, {**RESPONSES[name], key: OTHER})


def replace_payload(envelope, **changes):
    result = copy.deepcopy(envelope)
    payload = json.loads(base64.urlsafe_b64decode(result['payload'] + '=' * (-len(result['payload']) % 4)))
    payload.update(changes)
    result['payload'] = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).rstrip(b'=').decode()
    return result


@pytest.mark.parametrize('key,value', [('grant_request_id', OTHER), ('operation_id', OTHER), ('receipt_id', OTHER), ('admission_id', OTHER),
                                    ('payload_hash', 'f' * 64), ('case_id', OTHER), ('tenant', 'other'), ('device_id', OTHER)])
def test_grant_payload_echo_ids_hash_and_preflight_target(key, value):
    response = copy.deepcopy(RESPONSES['Grant'])
    response['grant'] = replace_payload(response['grant'], **{key: value})
    invalid_response('Grant', response)


@pytest.mark.parametrize('name,field,purpose', [('Preview', 'preview', 'restart-consent-preview'), ('Poll', 'operation', 'restart-operation'), ('Grant', 'grant', 'restart-execution-grant')])
def test_envelope_purpose_encoding_closed_shape_and_typed_payload(name, field, purpose):
    for change in ({'purpose': 'operation'}, {'purpose': 'restart-other'}, {'kid': ''}, {'kid': CANARY + '\n'}, {'alg': 'none'},
                   {'signature': 'A' * 511}, {'payload': 'A'}, {'payload': 'e30='}, {'secret': CANARY}):
        response = copy.deepcopy(RESPONSES[name])
        response[field].update(change)
        invalid_response(name, response)
    response = copy.deepcopy(RESPONSES[name])
    response[field] = replace_payload(response[field], secret=CANARY)
    invalid_response(name, response)
    response[field] = replace_payload(RESPONSES[name][field], payload_hash='f' * 64) if name == 'Poll' else replace_payload(RESPONSES[name][field], schema_version='sdc.support.v1')
    invalid_response(name, response)
    assert RESPONSES[name][field]['purpose'] == purpose


@pytest.mark.parametrize('field', ('operation', 'approval', 'acceptance', 'registration'))
def test_poll_four_records_all_null_or_all_present(field):
    value = copy.deepcopy(RESPONSES['Poll'])
    value[field] = None
    invalid_response('Poll', value)
    empty = {**RESPONSES['Poll'], 'operation': None, 'approval': None, 'acceptance': None, 'registration': None}
    assert control.validate_response(control.POLL, empty, request=REQUESTS['Poll'], now_ms=NOW) == empty
    empty[field] = RESPONSES['Poll'][field]
    invalid_response('Poll', empty)


def test_poll_individual_approval_hash_case_and_acceptance_bound_without_claiming_authority():
    for field, key, value in (('approval', 'approved_by', 'c' * 64), ('approval', 'approval_id', OTHER),
                              ('approval', 'approval_kind', 'preauthorized'), ('acceptance', 'accept_id', OTHER),
                              ('acceptance', 'local_change_receipt_hash', 'f' * 64), ('registration', 'service_revision', 2)):
        response = copy.deepcopy(RESPONSES['Poll'])
        response[field][key] = value
        invalid_response('Poll', response)
    request = {**REQUESTS['Poll'], 'case_id': OTHER}
    invalid_response('Poll', RESPONSES['Poll'], request=request)


def test_request_historical_replay_is_explicit_future_never_permitted():
    request = REQUESTS['Grant']
    later = NOW + 16000
    invalid_request(control.GRANT, request, now_ms=later)
    assert control.validate_request(control.GRANT, request, now_ms=later, allow_historical=True) == request
    # Current grant still alive: delayed response uses the original request's shape.
    assert control.validate_response(control.GRANT, RESPONSES['Grant'], request=request, now_ms=later) == RESPONSES['Grant']
    future = copy.deepcopy(request)
    future['preflight']['observed_at'] = later + 1
    invalid_request(control.GRANT, future, now_ms=later, allow_historical=True)
    invalid_request(control.GRANT, request, allow_historical=1)
    invalid_response('Grant', RESPONSES['Grant'], now_ms=NOW + 30000)


def test_accept_stored_ack_ignores_http_id_and_retains_historical_expiry_only():
    value = RESPONSES['Accept']
    later = value['expires_at'] + 1
    assert control.validate_response(control.ACCEPT, value, request=REQUESTS['Accept'], now_ms=later) == value
    ack = {key: val for key, val in value.items() if key != 'request_id'}
    assert restart.validate_restart_acceptance(ack, now_ms=later, allow_expired=True) == ack
    with pytest.raises(ValueError):
        restart.validate_restart_acceptance(value, now_ms=later, allow_expired=True)
    invalid_response('Accept', {**value, 'accepted_at': later + 1}, now_ms=later)
    invalid_response('Accept', {**value, 'preview_payload_hash': 'f' * 64})


def test_status_null_and_body_hash_are_uninterpreted_metadata_not_retry_permission():
    response = {**RESPONSES['Status'], 'report': None, 'operation': {'state': 'unknown', 'state_revision': 0}}
    assert control.validate_response(control.STATUS, response, request=REQUESTS['Status'], now_ms=NOW) == response
    for change in ({'state_revision': True}, {'state_revision': -1}, {'state': 'completed'}, {'secret': CANARY}):
        invalid_response('Status', {**response, 'operation': {**response['operation'], **change}})
    response = copy.deepcopy(RESPONSES['Status'])
    response['report']['body_hash'] = 'f' * 64
    assert control.validate_response(control.STATUS, response, request=REQUESTS['Status'], now_ms=NOW)['report']['body_hash'] == 'f' * 64
    for change in ({'state_revision': False}, {'state_revision': 0}, {'body_hash': CANARY}, {'result': None}):
        invalid_response('Status', {**response, 'report': {**response['report'], **change}})


def test_report_state_revision_exact_echo_and_no_private_errors():
    for change in ({'state': 'unknown'}, {'state_revision': 4}, {'state_revision': True}):
        invalid_response('Report', {**RESPONSES['Report'], **change})
    cyclic = copy.deepcopy(RESPONSES['Poll'])
    cyclic['operation'] = cyclic
    invalid_response('Poll', cyclic)
    for path in ('/v2/support/requests/poll', control.POLL + '?x=1', control.POLL + '#x', control.POLL + '/', 'https://example.invalid' + control.POLL, None):
        invalid_request(path, REQUESTS['Poll'])


def test_original_eight_routes_and_registration_read_are_fixed_restart_posts():
    assert control.PATHS == (
        '/v2/support/restarts/inspections/report', '/v2/support/restarts/cases/preview',
        '/v2/support/restarts/cases/accept', '/v2/support/restarts/requests/poll',
        '/v2/support/restarts/operations/admit', '/v2/support/restarts/operations/grant',
        '/v2/support/restarts/operations/report', '/v2/support/restarts/operations/status',
        '/v2/support/restarts/services/read')
    assert tuple(KINDS.values()) == control.PATHS[:-1]
    assert control.REGISTRATION_READ == control.PATHS[-1]


def test_http_trace_id_can_change_and_status_is_not_current_report_consistency():
    response = {**RESPONSES['Status'], 'request_id': OTHER, 'operation': {'state': 'expired', 'state_revision': 9}}
    assert control.validate_response(control.STATUS, response, request=REQUESTS['Status'], now_ms=NOW) == response


def test_registration_history_is_not_current_permission_and_future_is_refused():
    response = copy.deepcopy(RESPONSES['Inspection'])
    response['registration']['expires_at'] = NOW - 1
    assert control.validate_response(control.INSPECTION, response, request=REQUESTS['Inspection'], now_ms=NOW) == response
    response['registration']['not_before'] = NOW + 1
    response['registration']['expires_at'] = NOW + 1000
    invalid_response('Inspection', response)


def test_failed_null_is_only_wire_shape_not_central_business_acceptance():
    request = {**REQUESTS['Report'], 'state': 'failed', 'result': None}
    assert control.validate_request(control.REPORT, request, now_ms=NOW) == request
    # The central handler's failed/null denial is outside this syntax-only unit.
