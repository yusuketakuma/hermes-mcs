"""Synthetic fixed central wire oracles; no signature, owner or service is exercised."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import restart_contract as rc
from adapters.sdc import support_contract as old

ROOT = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/support'
V = json.loads((ROOT / 'restart-wire-vectors.json').read_text())
SCHEMA = json.loads((ROOT / 'restart-wire.schema.json').read_text())
NOW = 1792663200000
OTHER_ID = '00000000-0000-4000-8000-000000000063'
OTHER_GEN = OTHER_ID.replace('-', '')
KINDS = ('intent', 'approval', 'preview', 'operation', 'grant', 'registration', 'acceptance', 'local_receipt', 'result')
SOURCE = {kind: V['localReceipt' if kind == 'local_receipt' else kind] for kind in KINDS}
CANARY = 'SYNTHETIC_SECRET_NOT_FOR_OUTPUT'


def digest(value):
    # Independent stdlib CJSON oracle, never candidate output as expected.
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()


def chain(**changes):
    values = {key: copy.deepcopy(V[key]) for key in ('operation', 'approval', 'preview', 'acceptance', 'registration', 'preflight', 'expected', 'grant', 'admission')}
    values.update(local_receipt=copy.deepcopy(V['localReceipt']), now_ms=NOW)
    values.update(changes)
    return rc.validate_restart_authorization(**values)


def outcome(value=None, **changes):
    options = {key: copy.deepcopy(V[key]) for key in ('operation', 'grant', 'registration', 'admission')}
    options['now_ms'] = NOW
    options.update(changes)
    return rc.validate_restart_outcome(V['result'] if value is None else value, **options)


def invalid(call):
    with pytest.raises(ValueError, match=r'^SUPPORT_(INVALID_RESTART_CONTRACT|RESTART_BINDING_CONFLICT|RESTART_EXPIRED|SIGNATURE_PURPOSE_DENIED|RESTART_TARGET_CONFLICT|RESTART_STALE|RESTART_NOT_READY)$') as caught:
        call()
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize('kind', KINDS)
def test_all_fixed_records_and_closed_schema(kind):
    value = SOURCE[kind]
    actual = rc.validate_restart_record(kind, value, now_ms=NOW)
    assert actual == value and actual is not value
    definition = SCHEMA['$defs'][kind]
    assert definition['additionalProperties'] is False
    assert set(definition['required']) == set(definition['properties']) == set(value)
    for key in value:
        changed = copy.deepcopy(value)
        del changed[key]
        invalid(lambda: rc.validate_restart_record(kind, changed, now_ms=NOW))
    for key in ('patient_id', 'secret', 'raw_log', 'path', 'url', 'argv', 'shell'):
        invalid(lambda: rc.validate_restart_record(kind, {**value, key: CANARY}, now_ms=NOW))


@pytest.mark.parametrize('kind', tuple(V['controls']))
def test_eight_fixed_control_requests_and_exact_schema(kind):
    value = V['controls'][kind]
    assert rc.validate_restart_control(kind, value, now_ms=NOW) == value
    definition = SCHEMA['$defs']['control_' + kind]
    assert definition['additionalProperties'] is False
    assert set(definition['required']) == set(definition['properties']) == set(value)
    for key in value:
        changed = copy.deepcopy(value)
        del changed[key]
        invalid(lambda: rc.validate_restart_control(kind, changed, now_ms=NOW))
    invalid(lambda: rc.validate_restart_control(kind, {**value, 'extra': CANARY}, now_ms=NOW))


def test_hash_dag_matches_independent_projection_and_fixed_literals():
    assert V['now'] == NOW and len(rc.EXPECTED_FIELDS) == 15 and set(rc.EXPECTED_FIELDS) == set(V['expected'])
    assert rc.hash_restart_intent(V['intent']) == digest(V['intent']) == 'cd7d5abfb3cfb182d173f124dbce8a7b236ae44057cf091d3371d90f31f44ec7'
    for field, record, function, expected in (
        ('approval_hash', 'approval', rc.hash_restart_approval, '7fd4112806226a12d2dda7d227cf42d40803c3fd07da5c16d1377231b4adca45'),
        ('payload_hash', 'operation', rc.hash_restart_payload, 'b17461d76ad45ece3a3f7ceee3771cab56b6136a775e4753afacf7066423fab4'),
        ('receipt_hash', 'localReceipt', rc.hash_restart_local_receipt, 'd8c5fec530723a846f0fc5f98877ca5ac59c238fdf0eaf4369bdaf9ca99686f8'),
    ):
        value = copy.deepcopy(V[record])
        del value[field]
        assert digest(value) == function(V[record]) == expected
        assert function({**V[record], field: 'f' * 64}) == expected
    binding = {'schema_version': 'sdc.support.restart-change-binding.v1', 'intent_hash': V['operation']['intent_hash'],
               'approval_hash': V['approval']['approval_hash'], 'change_consent_id': V['preview']['change_consent_id'],
               'claim_id': V['preview']['claim_id'], 'key_id': V['preview']['key_id']}
    assert rc.hash_restart_change_binding(V['preview']) == rc.hash_restart_change_binding(V['operation']) == digest(binding) == 'fedbfed9e65507bf793dae8d2a84ac03bdd3b650a32e2d0c5ef681a5b752a622'
    assert rc.hash_restart_preview(V['preview']) == digest(V['preview']) == 'e282270b3242b78c81e938836f813da6c624e35856a7c7b4c7677b87916847d8'


@pytest.mark.parametrize('purpose,key', [('restart-operation', 'operation'), ('restart-execution-grant', 'grant'), ('restart-consent-preview', 'preview')])
def test_typed_purpose_and_detached_canonical_input(purpose, key):
    value = copy.deepcopy(V[key])
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii')
    assert rc.validate_typed_payload(purpose, raw, now_ms=NOW) == value
    result = rc.validate_typed_payload(purpose, value, now_ms=NOW)
    result['params']['effects_profile'] = 'mutated'
    assert value == V[key]
    for bad in (None, True, -1, 2**53, 1.5):
        invalid(lambda: rc.validate_typed_payload(purpose, value, now_ms=bad))
    invalid(lambda: rc.validate_typed_payload(purpose, value, now_ms=NOW, allow_expired=1))
    for bad in (raw.replace(b'{', b'{"product":"hermes-mcs",', 1), b'\xef\xbb\xbf' + raw, raw + b' trailing', b'[' * 9 + b'0' + b']' * 9,
                b'{"a":-0}', b'{"a":NaN}', b'{"a":1e0}', b'{"a":"\\u000a"}', b'{"a":"\\u3042"}', b' ' * 8193):
        invalid(lambda: rc.validate_typed_payload(purpose, bad, now_ms=NOW))


@pytest.mark.parametrize('field', ('intent_hash', 'change_binding_hash', 'payload_hash'))
def test_typed_hash_tampering_rejected(field):
    invalid(lambda: rc.validate_typed_payload('restart-operation', {**V['operation'], field: 'f' * 64}, now_ms=NOW))


def test_nested_approval_and_old_purposes_rejected_without_r4a_changes():
    p = copy.deepcopy(V['preview'])
    p['approval']['approved_by'] = 'c' * 64
    invalid(lambda: rc.validate_typed_payload('restart-consent-preview', p, now_ms=NOW))
    for purpose in ('operation', 'execution-grant', 'consent-preview', 'unknown'):
        invalid(lambda: rc.validate_typed_payload(purpose, V['operation'], now_ms=NOW))
    invalid(lambda: rc.validate_typed_payload('restart-operation', V['preview'], now_ms=NOW))
    for name, validator, value in (('operation', old.validate_operation, V['operation']), ('grant', old.validate_grant, V['grant']), ('preview', old.validate_preview, V['preview'])):
        with pytest.raises(ValueError):
            validator(value, now_ms=NOW)
    for filename in ('signature-vectors.json', 'consent-preview-vectors.json'):
        fixture = json.loads((ROOT / filename).read_text())
        for vector in fixture['vectors']:
            for kind in KINDS:
                invalid(lambda: rc.validate_restart_record(kind, vector['payload'], now_ms=fixture['now']))


@pytest.mark.parametrize('kind,limit', [('intent', 1800000), ('approval', 1800000), ('preview', 300000), ('operation', 1800000), ('grant', 30000)])
def test_lifetime_halfopen_bounds_no_r1_clock_grace(kind, limit):
    value = SOURCE[kind]
    for change in ({'expires_at': value['issued_at'] + limit + 1}, {'not_before': value['expires_at']}, {'expires_at': NOW}, {'issued_at': True}, {'issued_at': -1}, {'issued_at': 2**53}):
        invalid(lambda: rc.validate_restart_record(kind, {**value, **change}, now_ms=NOW))
    assert rc.validate_restart_record(kind, value, now_ms=value['expires_at'], allow_expired=True) == value
    invalid(lambda: rc.validate_restart_record(kind, value, now_ms=value['not_before'] - 1, allow_expired=True))


@pytest.mark.parametrize('key', rc.EXPECTED_FIELDS)
def test_all_independent_expected_fields_required_and_mismatch_denied(key):
    expected = copy.deepcopy(V['expected'])
    del expected[key]
    invalid(lambda: chain(expected=expected))
    expected = copy.deepcopy(V['expected'])
    expected[key] = 'different'
    invalid(lambda: chain(expected=expected))


def test_authorization_positive_and_independent_authority_mandatory():
    assert chain()['grant'] == V['grant']
    assert chain(grant=None, admission=None)['grant'] is None
    for value in (None, {}, [], {'secret': CANARY}):
        invalid(lambda: chain(expected=value))
    invalid(lambda: chain(expected={**V['expected'], 'unknown': CANARY}))
    invalid(lambda: chain(approval={**V['approval'], 'approval_kind': 'preauthorized'}))
    invalid(lambda: chain(approval={**V['approval'], 'expires_at': V['intent']['expires_at'] + 1}))
    actual = chain()
    actual['preview']['params']['effects_profile'] = 'changed'
    assert V['preview']['params']['effects_profile'] == 'mcs.standalone.restart.v1'


@pytest.mark.parametrize('change', [{'state': 'revoked'}, {'tenant': 'other'}, {'installation': 'other'}, {'device_id': OTHER_ID}, {'environment': 'other'},
                                  {'realm_epoch': 'other'}, {'policy_epoch': 2}, {'device_epoch': 3}, {'service_revision': 2},
                                  {'service_id': 'other'}, {'effects_profile': 'other'}, {'not_before': NOW + 1}, {'expires_at': NOW}])
def test_registry_revocation_scope_epochs_and_window(change):
    invalid(lambda: chain(registration={**V['registration'], **change}))


@pytest.mark.parametrize('change', [{'observed_at': NOW - 15001}, {'observed_at': NOW + 1}, {'runtime_generation': OTHER_GEN}, {'owner_alive': False},
                                  {'update_in_progress': True}, {'command_intake_in_progress': True}, {'restarting': True}, {'environment': 'other'}])
def test_preflight_fresh_owner_eligibility_is_distinct_from_request_shape(change):
    invalid(lambda: chain(preflight={**V['preflight'], **change}))
    if 'update_in_progress' in change:
        request = {**V['controls']['grant'], 'preflight': {**V['preflight'], **change}}
        assert rc.validate_restart_control('grant', request, now_ms=NOW) == request


@pytest.mark.parametrize('key', ('accept_id', 'preview_id', 'operation_id', 'change_consent_id', 'local_change_receipt_id', 'change_binding_hash', 'preview_payload_hash', 'local_change_receipt_hash'))
def test_ack_receipt_every_binding(key):
    invalid(lambda: chain(acceptance={**V['acceptance'], key: OTHER_ID if key.endswith('_id') else 'f' * 64}))


@pytest.mark.parametrize('change', [{'confirmed_at': V['preview']['not_before'] - 1}, {'confirmed_at': V['preview']['expires_at']},
                                  {'confirmed_at': V['acceptance']['accepted_at'] + 1}, {'preview_id': OTHER_ID},
                                  {'local_change_receipt_id': OTHER_ID}, {'change_accept_id': OTHER_ID}, {'receipt_hash': 'f' * 64}])
def test_persisted_local_change_receipt_cannot_be_reassigned(change):
    invalid(lambda: chain(local_receipt={**V['localReceipt'], **change}))


def test_expired_preview_historical_acceptance_does_not_extend_other_authority():
    now = V['preview']['expires_at'] + 1
    invalid(lambda: rc.validate_typed_payload('restart-consent-preview', V['preview'], now_ms=now))
    preflight = {**V['preflight'], 'observed_at': now}
    assert chain(now_ms=now, preflight=preflight, grant=None, admission=None)['acceptance'] == V['acceptance']
    grant = {**V['grant'], 'issued_at': now, 'not_before': now, 'expires_at': now + 30000}
    assert chain(now_ms=now, preflight=preflight, grant=grant)['grant'] == grant
    invalid(lambda: chain(now_ms=V['acceptance']['expires_at'], preflight={**preflight, 'observed_at': V['acceptance']['expires_at']}, grant=None, admission=None))
    for accepted in (V['preview']['not_before'] - 1, V['preview']['expires_at'], NOW + 1):
        invalid(lambda: chain(acceptance={**V['acceptance'], 'accepted_at': accepted}))
    invalid(lambda: chain(acceptance={**V['acceptance'], 'expires_at': V['localReceipt']['expires_at'] + 1}))
    renewed = {**V['preview'], 'preview_id': OTHER_ID, 'issued_at': NOW, 'not_before': NOW, 'expires_at': NOW + 60000}
    assert rc.hash_restart_change_binding(renewed) == V['preview']['change_binding_hash']
    assert rc.hash_restart_preview(renewed) != V['operation']['preview_payload_hash']
    invalid(lambda: chain(preview=renewed))


@pytest.mark.parametrize('change', [{'receipt_id': OTHER_ID}, {'admission_id': OTHER_ID}, {'owner_generation': OTHER_GEN}, {'extra': True}])
def test_grant_joins_only_independent_admission(change):
    invalid(lambda: chain(admission={**V['admission'], **change}))


def test_grant_never_predates_acceptance_or_outlives_approval():
    invalid(lambda: chain(grant={**V['grant'], 'issued_at': V['acceptance']['accepted_at'] - 1, 'not_before': V['acceptance']['accepted_at'] - 1, 'expires_at': NOW + 1}))
    now = V['approval']['expires_at'] - 1000
    preflight = {**V['preflight'], 'observed_at': now}
    valid = {**V['grant'], 'issued_at': now, 'not_before': now, 'expires_at': V['approval']['expires_at']}
    assert chain(now_ms=now, preflight=preflight, grant=valid)['grant'] == valid
    invalid(lambda: chain(now_ms=now, preflight=preflight, grant={**valid, 'expires_at': valid['expires_at'] + 1}))


@pytest.mark.parametrize('key', ('start_receipt_id', 'started_at', 'drain_completed_at', 'old_owner_exit_at', 'handoff_receipt_id',
                               'handoff_parent_receipt_id', 'successor_runtime_generation', 'supervisor_launch_id', 'successor_launch_id', 'ready_observed_at'))
def test_success_missing_any_native_causal_evidence_rejected(key):
    invalid(lambda: rc.validate_restart_result({**V['result'], key: None}, now_ms=NOW))


def unknown_result():
    return {**V['result'], **{key: None for key in ('start_receipt_id', 'started_at', 'drain_completed_at', 'old_owner_exit_at', 'handoff_receipt_id',
            'handoff_parent_receipt_id', 'successor_runtime_generation', 'supervisor_launch_id', 'successor_launch_id', 'ready_observed_at')},
            'readiness': 'unknown', 'error_code': 'RESULT_UNKNOWN'}


def test_outcome_scope_causal_capability_window_and_unknown_preserved():
    assert outcome() == V['result']
    for change in ({'grant_id': OTHER_ID}, {'operation_id': OTHER_ID}, {'receipt_id': OTHER_ID}, {'payload_hash': 'f' * 64}, {'service_revision': 2},
                   {'params': {**V['result']['params'], 'expected_runtime_generation': OTHER_GEN}}, {'started_at': V['grant']['not_before'] - 1},
                   {'successor_runtime_generation': V['operation']['params']['expected_runtime_generation']}, {'supervisor_launch_id': OTHER_ID}, {'generated_at': NOW + 1}):
        invalid(lambda: outcome({**V['result'], **change}))
    registration = {**V['registration'], 'causal_evidence_supported': False}
    assert chain(registration=registration)['registration'] == registration
    invalid(lambda: outcome(registration=registration))
    assert outcome(unknown_result(), registration=registration) == unknown_result()
    later = V['registration']['expires_at']
    assert outcome(unknown_result(), now_ms=later) == unknown_result()
    invalid(lambda: outcome(now_ms=later))


def test_report_unknown_stays_unknown_and_outer_ids_and_time_bound():
    report = V['controls']['report']
    for change in ({'operation_id': OTHER_ID}, {'payload_hash': 'f' * 64}, {'receipt_id': OTHER_ID}, {'observed_at': NOW - 1}, {'state': 'running'}, {'result': None}):
        invalid(lambda: rc.validate_restart_control('report', {**report, **change}, now_ms=NOW))
    unknown = {**report, 'state': 'unknown', 'result': None}
    assert rc.validate_restart_control('report', unknown, now_ms=NOW) == unknown
    invalid(lambda: rc.validate_restart_control('inspection', {**V['controls']['inspection'], 'case_id': OTHER_ID}, now_ms=NOW))
    invalid(lambda: rc.validate_restart_control('unknown', report, now_ms=NOW))
    invalid(lambda: rc.validate_restart_control('report', {**report, 'state_revision': True}, now_ms=NOW))
    invalid(lambda: rc.validate_restart_control('grant', ' ' * 16385, now_ms=NOW))


def test_first_accepted_tracking_deadline_is_fixed_independent_of_replay():
    v = copy.deepcopy(V)
    # Recompute with independent stdlib hashes so the candidate cannot manufacture its own oracle.
    v['intent']['expires_at'] = NOW + 1600000
    intent_hash = digest(v['intent'])
    v['approval'].update(intent_hash=intent_hash, expires_at=NOW + 1500000)
    v['approval']['approval_hash'] = digest({k: val for k, val in v['approval'].items() if k != 'approval_hash'})
    v['preview'].update(intent=v['intent'], approval=v['approval'], intent_hash=intent_hash)
    binding = {'schema_version': 'sdc.support.restart-change-binding.v1', 'intent_hash': intent_hash,
               'approval_hash': v['approval']['approval_hash'], 'change_consent_id': v['preview']['change_consent_id'],
               'claim_id': v['preview']['claim_id'], 'key_id': v['preview']['key_id']}
    binding_hash = digest(binding)
    v['preview']['change_binding_hash'] = binding_hash
    preview_hash = digest(v['preview'])
    v['localReceipt'].update(intent_hash=intent_hash, approval_hash=v['approval']['approval_hash'], change_binding_hash=binding_hash,
                             preview_payload_hash=preview_hash, expires_at=v['approval']['expires_at'])
    v['localReceipt']['receipt_hash'] = digest({k: val for k, val in v['localReceipt'].items() if k != 'receipt_hash'})
    v['operation'].update(v['intent'])
    v['operation'].update(schema_version='sdc.support.restart-operation.v1', intent_hash=intent_hash, approval_hash=v['approval']['approval_hash'],
                           change_binding_hash=binding_hash, preview_payload_hash=preview_hash, local_change_receipt_hash=v['localReceipt']['receipt_hash'])
    v['operation']['payload_hash'] = digest({k: val for k, val in v['operation'].items() if k != 'payload_hash'})
    v['acceptance'].update(change_binding_hash=binding_hash, preview_payload_hash=preview_hash,
                           local_change_receipt_hash=v['localReceipt']['receipt_hash'], expires_at=v['localReceipt']['expires_at'])
    deadline = v['acceptance']['accepted_at'] + 900000
    now = deadline - 1000
    v['grant'].update(intent_hash=intent_hash, payload_hash=v['operation']['payload_hash'], approval_hash=v['approval']['approval_hash'],
                      change_binding_hash=binding_hash, preview_payload_hash=preview_hash, local_change_receipt_hash=v['localReceipt']['receipt_hash'],
                      issued_at=now, not_before=now, expires_at=deadline)
    v['preflight']['observed_at'] = now
    opts = {key: v[key] for key in ('operation', 'approval', 'preview', 'acceptance', 'registration', 'preflight', 'expected', 'grant', 'admission')}
    opts.update(local_receipt=v['localReceipt'], now_ms=now)
    assert rc.validate_restart_authorization(**opts)['grant'] == v['grant']
    opts['grant'] = {**v['grant'], 'expires_at': deadline + 1}
    invalid(lambda: rc.validate_restart_authorization(**opts))


@pytest.mark.parametrize('key', rc.EXPECTED_FIELDS)
def test_grant_scope_epochs_and_exact_params_cannot_substitute_operation(key):
    changed = copy.deepcopy(V['grant'])
    value = changed[key]
    if key == 'params':
        changed[key]['expected_runtime_generation'] = OTHER_GEN
    elif type(value) is int:
        changed[key] += 1
    elif key.endswith('_id'):
        changed[key] = OTHER_ID
    else:
        changed[key] = 'other'
    invalid(lambda: chain(grant=changed))


@pytest.mark.parametrize('key', ('approval_id', 'approved_by', 'intent_hash', 'approval_hash'))
def test_approval_identity_actor_intent_selfhash_cannot_substitute_preview(key):
    invalid(lambda: chain(approval={**V['approval'], key: OTHER_ID if key.endswith('_id') else 'c' * 64}))


@pytest.mark.parametrize('now', (None, True, False, -1, 2**53, 0.5))
def test_every_public_current_time_boundary_rejects_non_safe_integer(now):
    invalid(lambda: rc.validate_restart_result(V['result'], now_ms=now))
    invalid(lambda: rc.validate_restart_control('preview', V['controls']['preview'], now_ms=now))
    invalid(lambda: rc.validate_restart_record('intent', V['intent'], now_ms=now))
    invalid(lambda: chain(now_ms=now))
    invalid(lambda: outcome(now_ms=now))


@pytest.mark.parametrize('key', ('generated_at', 'started_at', 'drain_completed_at', 'old_owner_exit_at', 'ready_observed_at', 'service_revision'))
def test_result_boolean_never_counts_as_time_or_revision(key):
    invalid(lambda: rc.validate_restart_result({**V['result'], key: True}, now_ms=NOW))


def test_canonical_malformed_and_oversize_boundaries_do_not_echo_private_values():
    for software in (CANARY, float('nan'), {'unknown': CANARY}, [CANARY]):
        invalid(lambda: rc.validate_typed_payload('restart-operation', {**V['operation'], 'params': software}, now_ms=NOW))
    cyclic = copy.deepcopy(V['operation'])
    cyclic['params'] = cyclic
    invalid(lambda: rc.validate_typed_payload('restart-operation', cyclic, now_ms=NOW))
    invalid(lambda: rc.validate_typed_payload('restart-operation', {**V['operation'], 'x': CANARY * 1000}, now_ms=NOW))
