"""Exact local synthetic diagnostics vectors and independent closed-DTO oracles."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import diagnostic_contract as dc

ASSETS = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/diagnostics'
V = json.loads((ASSETS / 'diagnostics-vectors.json').read_text())
SCHEMA = json.loads((ASSETS / 'diagnostics.schema.json').read_text())
NOW = V['now']
CANARY = 'SYNTHETIC_SECRET_NOT_FOR_OUTPUT'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(',', ':')).encode('ascii')).hexdigest()


def records():
    out = {}
    for vector in V['positive']:
        out.setdefault(vector['definition'], copy.deepcopy(vector['value']))
    submit, grant = out['ArtifactSubmit'], out['ConsentGrantRequest']
    out['Artifact'] = copy.deepcopy(submit['artifact'])
    out['Event'] = copy.deepcopy(out['Batch']['events'][0])
    out['UUID'], out['SHA256'] = grant['device_id'], submit['artifact_sha256']
    out['Consent'] = {
        'consent_id': grant['consent_id'], 'device_id': grant['device_id'], 'consent_epoch': 1,
        'consent_revision': 'sdc-diagnostic-events-consent-v1', 'granted_by': out['SHA256'],
        'not_before': NOW, 'expires_at': grant['expires_at'], 'revoked_at': None, 'active': True,
    }
    out['DeviceReadRequest'] = {'device_id': grant['device_id']}
    out['EventDeleteRequest'] = {'deletion_id': out['ConsentRevokeRequest']['revocation_id'],
                                 'device_id': grant['device_id']}
    out['ArtifactReadRequest'] = {'case_id': submit['case_id'], 'device_id': grant['device_id']}
    out['ConsentGrantResponse'] = {'code': 'diagnostic_consent_granted', 'scope': out['ConsentScope'],
                                    'consent': out['Consent']}
    revoked = {**out['Consent'], 'revoked_at': NOW + 1, 'active': False}
    out['ConsentRevokeResponse'] = {'code': 'diagnostic_consent_revoked', 'consent': revoked}
    out['ConsentList'] = {'code': 'diagnostic_consents', 'device_id': grant['device_id'],
                           'scope': out['ConsentScope'], 'consents': [out['Consent'], revoked]}
    summary = {k: v for k, v in out['Receipt'].items() if k not in ('schema_version', 'state')}
    out['EventList'] = {'code': 'diagnostic_events', 'device_id': grant['device_id'],
                         'events': [{**out['Event'], 'batch_id': out['Batch']['batch_id']}],
                         'batches': [summary]}
    out['Deletion'] = {**out['EventDeleteRequest'], 'deleted_by': out['SHA256'],
                       'deleted_at': NOW, 'expires_at': NOW + 15552000000}
    out['EventDeleteResponse'] = {'code': 'diagnostic_events_deleted', 'deletion': out['Deletion']}
    out['DeletionReadRequest'] = {'deletion_id': out['Deletion']['deletion_id']}
    out['DeletionReadResponse'] = {'code': 'diagnostic_deletion', 'deletion': out['Deletion']}
    approval = {**out['ArtifactApprovalRequest'], 'artifact_kind': 'sdc.diagnostics.case-artifact.v1',
                'approved_by': out['SHA256'], 'created_at': NOW, 'expires_at': NOW + 1800000,
                'consumed_at': None, 'revoked_at': None}
    out['ArtifactApprovalResponse'] = {'code': 'diagnostic_artifact_approved', 'approval': approval}
    out['ArtifactList'] = {'code': 'diagnostic_case_artifacts', 'case_id': submit['case_id'],
                            'device_id': grant['device_id'],
                            'artifacts': [{**out['ArtifactReceipt'], 'artifact': out['Artifact']}]}
    for name in ('Receipt', 'Status', 'ArtifactReceipt'):
        out[name + 'Response'] = {**out[name], 'request_id': out['ErrorResponse']['request_id']}
    return copy.deepcopy(out)


R = records()


def invalid(call):
    with pytest.raises(dc.DiagnosticContractError, match='^SDC_DIAGNOSTIC_[A-Z_]+$') as caught:
        call()
    assert CANARY not in str(caught.value)
    assert caught.value.args == (caught.value.code,)


@pytest.mark.parametrize('vector', V['positive'], ids=lambda v: v['name'])
def test_all_exact_positive_vectors(vector):
    value = copy.deepcopy(vector['value'])
    result = dc.validate_diagnostic(vector['definition'], value, now_ms=NOW)
    assert result == value == vector['value'] and result is not value
    raw = json.dumps(value, indent=1).encode()
    assert dc.parse_diagnostic(vector['definition'], raw, now_ms=NOW) == value
    assert dc.validate_diagnostic_shape(vector['definition'], value) == value
    assert dc.parse_diagnostic_shape(vector['definition'], raw) == value


@pytest.mark.parametrize('vector', V['negative'], ids=lambda v: v['name'])
def test_all_exact_negative_vectors(vector):
    invalid(lambda: dc.validate_diagnostic(vector['definition'], vector['value'], now_ms=NOW))
    invalid(lambda: dc.parse_diagnostic(vector['definition'], json.dumps(vector['value']), now_ms=NOW))
    invalid(lambda: dc.validate_diagnostic_shape(vector['definition'], vector['value']))


@pytest.mark.parametrize('vector', V['raw_negative'], ids=lambda v: v['name'])
def test_all_exact_raw_negative_vectors(vector):
    for raw in (vector['text'], vector['text'].encode('utf-8')):
        invalid(lambda: dc.parse_diagnostic(vector['definition'], raw, now_ms=NOW))
        invalid(lambda: dc.parse_diagnostic_shape(vector['definition'], raw))


def test_fixed_definitions_enums_limits_and_vectors_match_local_exact_schema():
    assert (len(V['positive']), len(V['negative']), len(V['raw_negative'])) == (14, 25, 2)
    assert set(R) == set(dc.DEFINITIONS) == set(SCHEMA['$defs'])
    assert len(R) == 33
    assert dict(dc.LIMITS) == SCHEMA['x-limits']
    for key, values in (('component', dc.COMPONENTS), ('stage', dc.STAGES),
                        ('code', dc.CODES), ('severity', dc.SEVERITIES)):
        assert list(values) == SCHEMA['$defs']['Event']['properties'][key]['enum']
    assert tuple(map(len, (dc.COMPONENTS, dc.STAGES, dc.CODES, dc.SEVERITIES))) == (8, 8, 10, 4)
    assert list(dc.PLATFORM_COMPONENTS) == SCHEMA['$defs']['PlatformFault']['properties']['component']['enum']
    assert list(dc.PLATFORM_CODES) == SCHEMA['$defs']['PlatformFault']['properties']['code']['enum']


def objects(value, path=()):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from objects(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from objects(child, path + (index,))


def at(value, path):
    for key in path:
        value = value[key]
    return value


@pytest.mark.parametrize('name', tuple(R))
def test_every_definition_and_nested_object_is_closed_and_detached(name):
    source = copy.deepcopy(R[name])
    actual = dc.validate_diagnostic(name, source, now_ms=NOW)
    assert actual == source == R[name]
    for path, obj in objects(source):
        for field in obj:
            changed = copy.deepcopy(source)
            del at(changed, path)[field]
            invalid(lambda: dc.validate_diagnostic(name, changed, now_ms=NOW))
        changed = copy.deepcopy(source)
        at(changed, path)['unknown'] = CANARY
        invalid(lambda: dc.validate_diagnostic(name, changed, now_ms=NOW))
        assert at(actual, path) is not obj
    assert source == R[name]


@pytest.mark.parametrize('name', ('Event', 'Batch', 'Artifact', 'ArtifactSubmit'))
def test_new_event_age_boundaries_and_stored_id_shape_do_not_refresh_bytes(name):
    value = copy.deepcopy(R[name])
    event = value if name == 'Event' else value['artifact']['events'][0] if name == 'ArtifactSubmit' else value['events'][0]
    event['bucket_start'] = NOW
    if name == 'ArtifactSubmit':
        value['artifact_sha256'] = digest(value['artifact'])
    # For multi-event Batch, retain the same boundary for its second, distinct tuple.
    if name == 'Batch':
        for item in value['events']:
            item['bucket_start'] = NOW
    assert dc.validate_diagnostic(name, value, now_ms=NOW + 604800000) == value
    assert dc.validate_diagnostic(name, value, now_ms=NOW - 300000) == value
    invalid(lambda: dc.validate_diagnostic(name, value, now_ms=NOW + 604800001))
    invalid(lambda: dc.validate_diagnostic(name, value, now_ms=NOW - 300001))
    raw = json.dumps(value, separators=(',', ':')).encode()
    original = bytes(raw)
    assert dc.parse_diagnostic_shape(name, raw) == value
    invalid(lambda: dc.parse_diagnostic(name, raw, now_ms=NOW + 8 * 86400000))
    assert raw == original and value == json.loads(raw)


@pytest.mark.parametrize('name', ('Receipt', 'Status', 'Consent', 'ConsentGrantResponse',
                                  'ConsentRevokeResponse', 'ConsentList', 'EventList',
                                  'ArtifactApprovalResponse', 'ArtifactList', 'Deletion'))
def test_old_observations_remain_metadata_without_becoming_current_authority(name):
    source = copy.deepcopy(R[name])
    assert dc.validate_diagnostic(name, source, now_ms=NOW + 200 * 86400000) == source
    assert source == R[name]


@pytest.mark.parametrize('delta,valid', [(59999, False), (60000, True), (7776000000, True), (7776000001, False)])
def test_new_consent_lifetime_and_old_id_shape_are_separate(delta, valid):
    value = {**R['ConsentGrantRequest'], 'expires_at': NOW + delta}
    if valid:
        assert dc.validate_diagnostic('ConsentGrantRequest', value, now_ms=NOW) == value
    else:
        invalid(lambda: dc.validate_diagnostic('ConsentGrantRequest', value, now_ms=NOW))
    assert dc.validate_diagnostic_shape('ConsentGrantRequest', value) == value


@pytest.mark.parametrize('field,values', [
    ('count', [0, 100001, -1, True, None, 1.5]),
    ('bucket_start', [-1, NOW + 1, True, 2**53]),
    ('component', [CANARY, None, 'ingest']),
    ('stage', [CANARY, None]),
    ('code', [CANARY, 'D1_UNAVAILABLE', None]),
    ('severity', [CANARY, None]),
])
def test_event_field_boundaries(field, values):
    for bad in values:
        invalid(lambda: dc.validate_diagnostic('Event', {**R['Event'], field: bad}, now_ms=NOW))


def test_event_count_drop_partial_epoch_and_unique_tuple_boundaries():
    batch = copy.deepcopy(R['Batch'])
    batch['events'] = [{**R['Event'], 'bucket_start': NOW - n * 600000, 'count': 100000} for n in range(64)]
    batch.update(consent_epoch=2**53 - 1, dropped_events=1000000, partial=True)
    assert dc.validate_diagnostic('Batch', batch, now_ms=NOW) == batch
    for key, value in (('events', []), ('events', batch['events'] + [R['Event']]),
                       ('dropped_events', 1000001), ('dropped_events', True),
                       ('partial', 0), ('consent_epoch', 0), ('consent_epoch', 2**53)):
        invalid(lambda: dc.validate_diagnostic('Batch', {**batch, key: value}, now_ms=NOW))
    duplicate = [{**R['Event'], 'count': 1}, {**R['Event'], 'count': 2}]
    invalid(lambda: dc.validate_diagnostic('Batch', {**batch, 'events': duplicate}, now_ms=NOW))


@pytest.mark.parametrize('changed', ('hash', 'case', 'nested-case'))
def test_artifact_digest_and_case_join_are_independently_bound(changed):
    value = copy.deepcopy(R['ArtifactSubmit'])
    assert digest(value['artifact']) == value['artifact_sha256']
    if changed == 'hash':
        value['artifact_sha256'] = 'f' * 64
    elif changed == 'case':
        value['case_id'] = R['UUID']
    else:
        value['artifact']['case_id'] = R['UUID']
        value['artifact_sha256'] = digest(value['artifact'])
    invalid(lambda: dc.validate_diagnostic('ArtifactSubmit', value, now_ms=NOW))
    invalid(lambda: dc.validate_diagnostic_shape('ArtifactSubmit', value))


@pytest.mark.parametrize('content_state', ('expired', 'deleted'))
def test_artifact_list_content_projection_and_parent_case_join(content_state):
    value = copy.deepcopy(R['ArtifactList'])
    value['artifacts'][0].update(content_state=content_state, artifact=None)
    assert dc.validate_diagnostic('ArtifactList', value, now_ms=NOW) == value
    value['artifacts'][0]['artifact'] = R['Artifact']
    invalid(lambda: dc.validate_diagnostic('ArtifactList', value, now_ms=NOW))
    value = copy.deepcopy(R['ArtifactList'])
    value['case_id'] = R['UUID']
    invalid(lambda: dc.validate_diagnostic('ArtifactList', value, now_ms=NOW))
    value = copy.deepcopy(R['ArtifactList'])
    value['artifacts'][0]['artifact_sha256'] = 'f' * 64
    invalid(lambda: dc.validate_diagnostic('ArtifactList', value, now_ms=NOW))


def test_consent_device_join_and_revoked_metadata_are_not_grants():
    value = copy.deepcopy(R['ConsentList'])
    value['device_id'] = R['Batch']['batch_id']
    invalid(lambda: dc.validate_diagnostic('ConsentList', value, now_ms=NOW))
    value = copy.deepcopy(R['Consent'])
    value.update(active=False, revoked_at=NOW)
    assert dc.validate_diagnostic('Consent', value, now_ms=NOW) == value
    value['active'] = True
    invalid(lambda: dc.validate_diagnostic('Consent', value, now_ms=NOW))


def test_independently_capped_event_and_batch_lists_do_not_require_full_join():
    value = copy.deepcopy(R['EventList'])
    value['batches'] = []
    assert dc.validate_diagnostic('EventList', value, now_ms=NOW) == value


def test_status_unknown_is_exact_metadata_and_never_creates_consent():
    value = copy.deepcopy(R['Status'])
    value['consent'] = {'state': 'inactive', 'consent_epoch': None, 'expires_at': None}
    value['batches'] = [{'batch_id': R['Batch']['batch_id'], 'state': 'unknown', 'content_state': None}]
    assert dc.validate_diagnostic('Status', value, now_ms=NOW) == value
    for change in ({'state': 'not_sent'}, {'content_state': 'deleted'}, {'consent_epoch': 1}):
        changed = copy.deepcopy(value)
        changed['batches'][0].update(change)
        invalid(lambda: dc.validate_diagnostic('Status', changed, now_ms=NOW))


def test_status_ids_unique_but_empty_request_and_inactive_status_are_valid():
    source = R['StatusRequest']
    assert dc.validate_diagnostic('StatusRequest', {**source, 'batch_ids': []}, now_ms=NOW)['batch_ids'] == []
    duplicate = [R['Batch']['batch_id']] * 2
    invalid(lambda: dc.validate_diagnostic('StatusRequest', {**source, 'batch_ids': duplicate}, now_ms=NOW))


@pytest.mark.parametrize('name,field,limit', [('ConsentList', 'consents', 20),
                                            ('EventList', 'events', 500), ('EventList', 'batches', 100),
                                            ('ArtifactList', 'artifacts', 8), ('Status', 'batches', 16)])
def test_response_array_limits_are_independent_of_body_byte_bound(name, field, limit):
    source = copy.deepcopy(R[name])
    source[field] = source[field][:1] * limit
    assert dc.validate_diagnostic(name, source, now_ms=NOW) == source
    source[field] = source[field][:1] * (limit + 1)
    invalid(lambda: dc.validate_diagnostic(name, source, now_ms=NOW))


@pytest.mark.parametrize('name,container,time_field,delta', [
    ('Receipt', (), 'content_expires_at', 1),
    ('Deletion', (), 'expires_at', -1),
    ('ArtifactApprovalResponse', ('approval',), 'expires_at', 1),
    ('Consent', (), 'expires_at', 7776000000),
])
def test_fixed_retention_and_lifetime_metadata_are_internally_consistent(name, container, time_field, delta):
    value = copy.deepcopy(R[name])
    at(value, container)[time_field] += delta
    invalid(lambda: dc.validate_diagnostic_shape(name, value))


@pytest.mark.parametrize('value', (None, True, -1, 2**53, float('nan'), 1.0))
def test_now_requires_explicit_safe_integer_and_shape_mode_is_separate(value):
    invalid(lambda: dc.validate_diagnostic('Event', R['Event'], now_ms=value))
    invalid(lambda: dc.parse_diagnostic('Event', json.dumps(R['Event']), now_ms=value))


@pytest.mark.parametrize('token', ('-0', '-0.0', '-0e0', '-1e-9999', 'NaN', 'Infinity',
                                  '-Infinity', '1e9999', '9007199254740992', '0.5'))
def test_raw_number_semantics_reject_negative_zero_noninteger_and_unsafe(token):
    raw = json.dumps(R['Batch'], separators=(',', ':')).replace('"dropped_events":7', '"dropped_events":' + token)
    assert token in raw
    invalid(lambda: dc.parse_diagnostic('Batch', raw, now_ms=NOW))
    invalid(lambda: dc.parse_diagnostic_shape('Batch', raw))


@pytest.mark.parametrize('token', ('7.0', '7e0', '70e-1'))
def test_central_integer_valued_decimal_and_exponent_are_normalized_only_in_returned_copy(token):
    raw = json.dumps(R['Batch'], separators=(',', ':')).replace('"dropped_events":7', '"dropped_events":' + token)
    original = raw
    value = dc.parse_diagnostic('Batch', raw, now_ms=NOW)
    assert value == R['Batch'] and type(value['dropped_events']) is int and raw == original
    source = {**R['Batch'], 'dropped_events': 7.0}
    invalid(lambda: dc.validate_diagnostic('Batch', source, now_ms=NOW))
    assert type(source['dropped_events']) is float


@pytest.mark.parametrize('value', (-0.0, float('inf'), 0.5, 7.0, 2**53))
def test_direct_number_invalidity_is_not_lost_by_snapshot(value):
    invalid(lambda: dc.validate_diagnostic('Batch', {**R['Batch'], 'dropped_events': value}, now_ms=NOW))


def test_raw_size_bom_duplicate_utf8_depth_and_noncanonical_whitespace():
    raw = json.dumps(R['Batch'], separators=(',', ':')).encode()
    padded = raw + b' ' * (16384 - len(raw))
    assert dc.parse_diagnostic('Batch', padded, now_ms=NOW) == R['Batch']
    for bad in (padded + b' ', b'\xef\xbb\xbf' + raw, b'\xff' + raw, raw + b'{}',
                b'[' * 9 + b'0' + b']' * 9, raw.replace(b'{', b'{"partial":true,', 1),
                b' ' * 16385, b'', b'1' * 16384):
        invalid(lambda: dc.parse_diagnostic('Batch', bad, now_ms=NOW))
    # Escaped ASCII is legal JSON and remains the same closed fixed enum.
    escaped = raw.replace(b'notification', b'notificatio\\u006e')
    assert dc.parse_diagnostic('Batch', escaped, now_ms=NOW) == R['Batch']


def test_direct_depth_cycles_foreign_types_and_unknown_definition_are_fixed_failures():
    nested = 0
    for _ in range(9):
        nested = [nested]
    cycle = []
    cycle.append(cycle)
    for value in (nested, cycle, object(), {CANARY: object()}, b'{}'):
        invalid(lambda: dc.validate_diagnostic('Batch', value, now_ms=NOW))
    for name in (None, [], CANARY, 'batch'):
        invalid(lambda: dc.validate_diagnostic(name, R['Batch'], now_ms=NOW))


def test_platform_fault_uses_its_separate_closed_observer_enums():
    value = R['PlatformFault']
    assert dc.validate_diagnostic('PlatformFault', value, now_ms=NOW + 200 * 86400000) == value
    for change in ({'component': 'runtime'}, {'code': 'TIMEOUT'}, {'bucket_start': NOW + 1},
                   {'severity': 'error'}, {'count': 1}):
        invalid(lambda: dc.validate_diagnostic('PlatformFault', {**value, **change}, now_ms=NOW))


def test_maximum_admin_value_shape_is_distinct_from_supported_raw_body_size():
    value = copy.deepcopy(R['EventList'])
    value['events'] *= 500
    value['batches'] *= 100
    assert dc.validate_diagnostic_shape('EventList', value) == value
    raw = json.dumps(value, separators=(',', ':')).encode()
    assert len(raw) > 16384
    invalid(lambda: dc.parse_diagnostic_shape('EventList', raw))


@pytest.mark.parametrize('name,values', [
    ('UUID', [None, True, '', 'a' * 36, 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA',
              '00000000-0000-1000-8000-000000000001']),
    ('SHA256', [None, True, '', 'f' * 63, 'f' * 65, 'F' * 64, 'g' * 64]),
])
def test_scalar_definitions_require_exact_lowercase_closed_forms(name, values):
    for value in values:
        invalid(lambda: dc.validate_diagnostic(name, value, now_ms=NOW))
    invalid(lambda: dc.validate_diagnostic(name, R[name] + '\n', now_ms=NOW))


@pytest.mark.parametrize('field', ('component', 'stage', 'code', 'severity'))
def test_every_fixed_event_enum_member(field):
    for member in SCHEMA['$defs']['Event']['properties'][field]['enum']:
        value = {**R['Event'], field: member}
        assert dc.validate_diagnostic('Event', value, now_ms=NOW) == value


def test_every_fixed_error_code_and_scope_value():
    for code in SCHEMA['$defs']['ErrorResponse']['properties']['code']['enum']:
        value = {**R['ErrorResponse'], 'code': code}
        assert dc.validate_diagnostic('ErrorResponse', value, now_ms=NOW) == value
    for field, value in R['ConsentScope'].items():
        replacement = list(reversed(value)) if isinstance(value, list) else 1 if type(value) is int else CANARY
        invalid(lambda: dc.validate_diagnostic('ConsentScope', {**R['ConsentScope'], field: replacement}, now_ms=NOW))


def test_artifact_approval_numeric_bounds_and_history_do_not_imply_unused_authority():
    value = copy.deepcopy(R['ArtifactApprovalRequest'])
    for field in ('case_epoch', 'local_consent_epoch'):
        assert dc.validate_diagnostic('ArtifactApprovalRequest', {**value, field: 2**53 - 1}, now_ms=NOW)
        for bad in (0, True, 2**53):
            invalid(lambda: dc.validate_diagnostic('ArtifactApprovalRequest', {**value, field: bad}, now_ms=NOW))
    for size in (2, 16384):
        assert dc.validate_diagnostic('ArtifactApprovalRequest', {**value, 'artifact_size': size}, now_ms=NOW)
    for bad in (1, 16385, False):
        invalid(lambda: dc.validate_diagnostic('ArtifactApprovalRequest', {**value, 'artifact_size': bad}, now_ms=NOW))
    response = copy.deepcopy(R['ArtifactApprovalResponse'])
    response['approval'].update(consumed_at=NOW + 1, revoked_at=NOW + 2)
    assert dc.validate_diagnostic('ArtifactApprovalResponse', response, now_ms=NOW + 1800001) == response
