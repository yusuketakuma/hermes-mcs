"""Synthetic private Store -> original R1 -> immutable provision ACK/status callers."""
import copy
import hashlib
import json
import os
from pathlib import Path
import pickle

import pytest

from adapters.sdc import provision as local
from adapters.sdc.connector import Connector
from adapters.sdc.storage import Store
from adapters.sdc.support_contract import canonical
from adapters.sdc.transport import TransportError

V = json.loads((Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/provision/provision-vectors.json').read_text())
NOW = V['now']
DEVICE = '00000000-0000-4000-8000-000000000005'
CLAIM = '00000000-0000-4000-8000-000000000006'
KEY = '00000000-0000-4000-8000-000000000007'
OTHER = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
CANARY = 'SYNTHETIC_PRIVATE_NEVER_IN_OUTPUT'
REASON = 'initial-provision'


def encoded(value):
    return canonical(value, max_bytes=16384).encode('ascii')


def record(store):
    return store._read_file(local.NAME)


def setup_root(store):
    return local.prepare_root(store, confirm_human=True, reason=REASON, now_ms=NOW)


def material(store):
    value = copy.deepcopy(next(v['value'] for v in V['positive']
                               if v['definition'] == 'Bundle' and v['value']['profile_id'] == 'monitor'))
    value['root_id'] = record(store)['root_id']
    raw = encoded(value)
    expected = {key: value[key] for key in ('console_origin', 'tenant', 'installation', 'environment',
                                           'enrollment_id', 'profile_id', 'origin', 'audience')}
    expected['bundle_hash'] = hashlib.sha256(raw).hexdigest()
    return raw, expected


def import_bundle(store, *, now=NOW):
    raw, expected = material(store)
    plan = local.prepare_import(store, raw, expected=expected, now_ms=now)
    return local.commit_import(store, plan, confirm_human=True, reason=REASON, now_ms=now)


def active(store):
    local.begin_registration(store, now_ms=NOW)
    with store.lock():
        local.guard_registration(store, {}, {})
        store.write('credential', {'key': 'a' * 64, 'key_id': KEY})
        store.write('state.json', {'state': 'active', 'enabled': True, 'claim_id': CLAIM,
                                  'device_id': DEVICE, 'key_id': KEY, 'sequence': 0})


@pytest.fixture
def world(tmp_path):
    with Store(tmp_path / 'private', create_new=True) as store:
        setup_root(store)
        import_bundle(store)
        yield store


def failure(call, code=None):
    pattern = '^SDC_PROVISION_' + (code or '[A-Z_]+') + '$'
    with pytest.raises(local.ProvisionError, match=pattern) as caught:
        call()
    assert CANARY not in str(caught.value)


def ack_response(store, raw, *, accepted_at=NOW + 10):
    body = json.loads(raw)
    state = store._read_file('state.json')
    receipt = {k: v for k, v in body.items() if k != 'schema_version'}
    receipt.update({k: state[k] for k in ('device_id', 'claim_id', 'key_id')})
    receipt.update(generation=1, accepted_at=accepted_at)
    return {'schema_version': 'sdc.provision.ack-response.v1', 'receipt': receipt, 'request_id': OTHER}


def status_response(store, *, receipt=None, state='current'):
    slot = record(store)['profiles']['monitor']
    bundle = json.loads(slot['raw'])
    value = {k: bundle[k] for k in ('provision_id', 'root_id', 'generation', 'realm_epoch', 'policy_revision', 'expires_at')}
    value.update(bundle_hash=slot['bundle_hash'], current_generation=bundle['generation'],
                 current_provision_id=bundle['provision_id'], state=state, receipt=receipt)
    return {'schema_version': 'sdc.provision.status-response.v1', 'status': value, 'request_id': OTHER}


class Wire:
    def __init__(self, store, respond=None):
        self.store, self.respond, self.sent = store, respond, []

    def __call__(self, config):
        assert config == local.monitor_endpoint(self.store)
        return self

    def send(self, path, raw, headers):
        # A real separate Store must acquire the lock while HTTP is in progress.
        with Store(self.store.root) as other, other.lock():
            saved = record(other)
            slot = saved['profiles']['monitor']
            request = slot['ack_request' if path == local.ACK else 'status_request']
            assert raw.decode() == request['raw']
            assert hashlib.sha256(raw).hexdigest() == request['body_hash']
            if path == local.ACK:
                assert len(slot['ack_attempts']) >= 1
            assert slot['last_result'] == 'unknown'
        assert headers['X-SDC-Device'] == DEVICE
        assert headers['X-SDC-Key-ID'] == KEY
        self.sent.append((path, raw))
        if self.respond:
            return self.respond(path, raw)
        return ack_response(self.store, raw) if path == local.ACK else status_response(self.store)


def exchange(store, wire, *, action='ack', now=NOW, **options):
    return local.ack_once(store, profile='monitor', transport_factory=wire, clock=lambda: now / 1000,
                          action=action, confirm_human=True, reason=REASON, **options)


def test_root_identity_is_local_durable_and_not_inferred_from_empty_or_existing_roots(tmp_path):
    with Store(tmp_path / 'fresh', create_new=True) as store:
        result = setup_root(store)
        assert result['state'] == 'prepared' and result['root_id'] == record(store)['root_id']
        assert record(store)['root_fingerprint'] == store.root_fingerprint()
        assert record(store)['profiles'] == {'monitor': None, 'support': None, 'restart': None}
        failure(lambda: setup_root(store), 'CONFLICT')
    with Store(tmp_path / 'fresh') as reopened:
        assert local.status(reopened, now_ms=NOW)['root_id'] == result['root_id']
        failure(lambda: setup_root(reopened), 'CONFLICT')
    with Store(tmp_path / 'empty', create=True) as empty:
        failure(lambda: setup_root(empty), 'CONFLICT')


@pytest.mark.parametrize('human,reason', [(False, REASON), (1, REASON), (True, ''),
                                       (True, ' '), (True, '\n'), (True, 'x' * 129)])
def test_root_requires_explicit_bounded_human_reason(tmp_path, human, reason):
    with Store(tmp_path / 'fresh', create_new=True) as store:
        failure(lambda: local.prepare_root(store, confirm_human=human, reason=reason, now_ms=NOW), 'CONSENT_REQUIRED')
        assert record(store) is None


def test_legacy_projection_and_guard_do_not_add_a_provision_requirement(tmp_path):
    with Store(tmp_path / 'legacy', create=True) as store:
        config = {'origin': 'https://legacy.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
        store.write('endpoint.json', config)
        assert local.monitor_endpoint(store) is None
        local.guard_registration(store, {}, {})
        assert local.begin_registration(store, now_ms=NOW)['state'] == 'legacy'
        assert store.read('endpoint.json') == config


def test_import_exact_hash_expected_and_detached_plan_preserve_original_receipt(world):
    slot = record(world)['profiles']['monitor']
    raw, expected = material(world)
    assert slot['expected'] == expected
    assert slot['raw'].encode() == raw
    assert not (world.root / 'endpoint.json').exists()
    assert world.read('endpoint.json') == local.monitor_endpoint(world)
    plan = local.prepare_import(world, raw, expected=expected, now_ms=NOW + 1)
    original = copy.deepcopy(slot['receipt'])
    assert local.commit_import(world, plan, confirm_human=True, reason='reconfirm', now_ms=NOW + 1) == original
    assert record(world)['profiles']['monitor']['receipt'] == original
    expected['origin'] = 'https://different.invalid'
    assert record(world)['profiles']['monitor']['expected'] != expected


@pytest.mark.parametrize('field', sorted(local.EXPECTED_FIELDS))
def test_import_never_builds_expected_from_the_bundle(world, field):
    raw, expected = material(world)
    wrong = {'bundle_hash': 'b' * 64, 'enrollment_id': OTHER, 'profile_id': 'support',
             'console_origin': 'https://other.invalid', 'origin': 'https://other.invalid',
             'tenant': 'different', 'installation': 'different', 'environment': 'different', 'audience': 'different'}
    expected[field] = wrong[field]
    before = record(world)
    failure(lambda: local.prepare_import(world, raw, expected=expected, now_ms=NOW))
    assert record(world) == before


def test_import_rejects_reformatting_unknown_expected_profile_and_root_generation(world):
    raw, expected = material(world)
    for bad in ({**expected, 'private': CANARY}, {k: v for k, v in expected.items() if k != 'origin'}):
        failure(lambda: local.prepare_import(world, raw, expected=bad, now_ms=NOW))
    changed = b' ' + raw
    failure(lambda: local.prepare_import(world, changed, expected=expected, now_ms=NOW), 'CONFLICT')
    failure(lambda: local.prepare_import(world, changed, expected={**expected, 'bundle_hash': hashlib.sha256(changed).hexdigest()}, now_ms=NOW), 'CONFLICT')
    for values in ({'root_id': OTHER}, {'generation': 2, 'previous_provision_id': OTHER}):
        changed = encoded({**json.loads(raw), **values})
        failure(lambda: local.prepare_import(world, changed, expected={**expected, 'bundle_hash': hashlib.sha256(changed).hexdigest()}, now_ms=NOW), 'CONFLICT')
    changed = json.loads(raw)
    changed['origin'] += ':443'
    changed_raw = encoded(changed)
    failure(lambda: local.prepare_import(world, changed_raw, expected={**expected, 'origin': changed['origin'], 'bundle_hash': hashlib.sha256(changed_raw).hexdigest()}, now_ms=NOW))


def test_import_cancel_expiry_plan_mutation_and_cas_never_create_registration(world):
    raw, expected = material(world)
    plan = local.prepare_import(world, raw, expected=expected, now_ms=NOW)
    failure(lambda: local.commit_import(world, plan, confirm_human=False, reason=REASON, now_ms=NOW), 'CONSENT_REQUIRED')
    changed = copy.deepcopy(plan)
    changed['bundle']['tenant'] = 'different'
    failure(lambda: local.commit_import(world, changed, confirm_human=True, reason=REASON, now_ms=NOW), 'CONFLICT')
    failure(lambda: local.commit_import(world, plan, confirm_human=True, reason=REASON, now_ms=plan['bundle']['import_expires_at']))
    saved = record(world)
    saved['reason'] = 'different-confirmation'
    world._write_file(local.NAME, saved)
    failure(lambda: local.commit_import(world, plan, confirm_human=True, reason=REASON, now_ms=NOW), 'CONFLICT')
    assert record(world)['registration'] is None


def test_reopen_requires_same_bundle_reapproval_before_first_io(world):
    raw, expected = material(world)
    with Store(world.root) as reopened:
        failure(lambda: local.begin_registration(reopened, now_ms=NOW), 'REGISTRATION_HELD')
        plan = local.prepare_import(reopened, raw, expected=expected, now_ms=NOW + 1)
        local.commit_import(reopened, plan, confirm_human=True, reason=REASON, now_ms=NOW + 1)
        result = local.begin_registration(reopened, now_ms=NOW + 1)
        assert result['registration_started']
        with reopened.lock():
            local.guard_registration(reopened, {}, {})
            failure(lambda: local.guard_registration(reopened, {}, {}), 'REGISTRATION_HELD')
    failure(lambda: local.begin_registration(world, now_ms=NOW + 2), 'REGISTRATION_HELD')


def test_one_store_permit_is_not_serializable_or_borrowable(world):
    local.begin_registration(world, now_ms=NOW)
    permit = world._provision_registration_permit
    with pytest.raises(TypeError, match='SDC_PROVISION_CONFLICT'):
        pickle.dumps(permit)
    with Store(world.root) as reopened:
        reopened._provision_registration_permit = permit
        failure(lambda: local.guard_registration(reopened, {}, {}), 'REGISTRATION_HELD')


@pytest.mark.parametrize('missing', ['state', 'credential', 'both'])
def test_started_registration_partial_or_missing_files_never_make_a_new_claim(world, missing):
    active(world)
    if missing in ('state', 'both'):
        os.unlink('state.json', dir_fd=world.fd)
    if missing in ('credential', 'both'):
        os.unlink('credential', dir_fd=world.fd)
    with Store(world.root) as reopened:
        failure(lambda: local.begin_registration(reopened, now_ms=NOW + 1), 'REGISTRATION_HELD')
        failure(lambda: Connector(reopened, None).connect('synthetic-unusable-token'), 'REGISTRATION_HELD')


@pytest.mark.parametrize('assigned_key', [False, True])
def test_pending_claim_without_device_resumes_same_identity_even_after_partial_key_id_save(world, assigned_key):
    local.begin_registration(world, now_ms=NOW)
    local.guard_registration(world, {}, {})
    world.write('state.json', {'state': 'pending', 'enabled': False, 'claim_id': CLAIM, 'sequence': 0})
    credential = {'key': 'a' * 64, **({'key_id': KEY} if assigned_key else {})}
    world.write('credential', credential)
    with Store(world.root) as reopened:
        assert local.begin_registration(reopened, now_ms=NOW + 1)['registration_started']
        local.guard_registration(reopened, reopened.read('state.json'), reopened.read('credential'))
        assert reopened.read('state.json')['claim_id'] == CLAIM
        assert reopened.read('credential') == credential


@pytest.mark.parametrize('response_key', [KEY, OTHER])
def test_actual_claim_replay_preserves_previously_saved_assigned_key_id(world, response_key):
    local.begin_registration(world, now_ms=NOW)
    local.guard_registration(world, {}, {})
    world.write('state.json', {'state': 'pending', 'enabled': False, 'claim_id': CLAIM, 'sequence': 0})
    world.write('credential', {'key': 'a' * 64, 'key_id': KEY})
    sent = []

    class Replay:
        def send(self, path, raw, headers=None):
            sent.append((path, json.loads(raw)))
            if path.endswith('/claim'):
                return {'claim_id': CLAIM, 'device_id': DEVICE, 'key_id': response_key,
                        'audience': local.monitor_endpoint(world)['audience']}
            config = local.monitor_endpoint(world)
            return {'claim_id': CLAIM, 'device_id': DEVICE, 'key_id': KEY,
                    'audience': config['audience'], 'environment': config['environment'],
                    'fingerprint': hashlib.sha256(bytes.fromhex('a' * 64)).hexdigest()[:16],
                    'consent_revision': 'sdc-self-monitoring-v1', 'data_scope': 'version-process-status-only',
                    'tenant_name': 'Synthetic tenant', 'alias': 'Synthetic device',
                    'state': 'admin_approved', 'pending_expires_at': NOW + 300000}

    with Store(world.root) as reopened:
        local.begin_registration(reopened, now_ms=NOW + 1)
        with reopened.lock():
            connector = Connector(reopened, Replay(), clock=lambda: NOW / 1000)
            if response_key == KEY:
                assert connector.connect('synthetic-unusable-token')['state'] == 'admin_approved'
                assert reopened.read('state.json')['device_id'] == DEVICE
            else:
                with pytest.raises(ValueError, match='^SDC_IDENTITY_MISMATCH$'):
                    connector.connect('synthetic-unusable-token')
                assert 'device_id' not in reopened.read('state.json')
            assert reopened.read('credential') == {'key': 'a' * 64, 'key_id': KEY}
    assert sent[0][1]['claim_id'] == CLAIM and sent[0][1]['key'] == 'a' * 64


@pytest.mark.parametrize('field,value', [('state', 'suspended'), ('state', 'revoked'), ('central_suspended', True),
                                       ('disconnected', True), ('enrollment_hold', 'held'), ('rotation_id', OTHER)])
def test_existing_stop_and_rotation_cannot_reach_legacy_reset(world, field, value):
    active(world)
    state = world.read('state.json')
    state[field] = value
    world.write('state.json', state)
    failure(lambda: local.begin_registration(world, now_ms=NOW + 1), 'REGISTRATION_HELD')


@pytest.mark.parametrize('action', ['ack', 'status', 'begin'])
@pytest.mark.parametrize('case', ['disabled', 'malformed', 'rotation'])
def test_managed_invalid_state_is_rejected_before_credential_read_or_http(world, monkeypatch, action, case):
    active(world)
    state = world._read_file('state.json')
    if case == 'disabled':
        state.update(enabled=False, disconnected=True)
    else:
        field, value = {'malformed': ('claim_id', 'not-a-uuid'), 'rotation': ('rotation_id', OTHER)}[case]
        state[field] = value
    world._write_file('state.json', state)
    before = copy.deepcopy(record(world))
    reads = []
    with Store(world.root) as reopened:
        original_read = reopened.read

        def forbid_credential(name, default=None):
            if name == 'credential':
                reads.append(name)
                pytest.fail('Known invalid state must not reach the credential reader')
            return original_read(name, default)

        def no_transport(config):
            pytest.fail('Known invalid state must not construct a transport or send HTTP')

        monkeypatch.setattr(reopened, 'read', forbid_credential)
        if action == 'begin':
            failure(lambda: local.begin_registration(reopened, now_ms=NOW), 'REGISTRATION_HELD')
        else:
            failure(lambda: exchange(reopened, no_transport, action=action), 'REGISTRATION_HELD')
        assert not reads and record(reopened) == before


@pytest.mark.parametrize('action', ['ack', 'status'])
def test_pending_registration_cannot_read_ack_credentials(world, monkeypatch, action):
    active(world)
    world._write_file('state.json', {'state': 'pending', 'enabled': False, 'claim_id': CLAIM, 'sequence': 0})
    before = copy.deepcopy(record(world))

    def forbidden(*args, **kwargs):
        pytest.fail('A pending registration cannot read ACK credentials or send HTTP')

    monkeypatch.setattr(world, 'read', forbidden)
    failure(lambda: exchange(world, forbidden, action=action), 'REGISTRATION_HELD')
    assert record(world) == before


def test_paused_active_can_prepare_manual_resume_with_same_identity(world):
    active(world)
    state = world._read_file('state.json')
    state.update(enabled=False, sequence=17)
    world._write_file('state.json', state)
    credential = world._read_file('credential')
    before = copy.deepcopy(record(world))
    with Store(world.root) as reopened:
        assert local.begin_registration(reopened, now_ms=NOW)['registration_started'] is True
        with reopened.lock():
            local.guard_registration(reopened, reopened.read('state.json'), reopened.read('credential'))
        assert reopened._read_file('state.json') == state
        assert reopened._read_file('credential') == credential
        assert record(reopened) == before
        assert getattr(reopened, '_provision_registration_permit', None) is None


@pytest.mark.parametrize('action', ['ack', 'status'])
def test_paused_active_cannot_read_ack_credentials(world, monkeypatch, action):
    active(world)
    state = world._read_file('state.json')
    state['enabled'] = False
    world._write_file('state.json', state)
    before = copy.deepcopy(record(world))

    def forbidden(*args, **kwargs):
        pytest.fail('A paused active registration cannot read ACK credentials or send HTTP')

    monkeypatch.setattr(world, 'read', forbidden)
    failure(lambda: exchange(world, forbidden, action=action), 'REGISTRATION_HELD')
    assert record(world) == before


@pytest.mark.parametrize('enabled', [0, 1, None, 'false'])
def test_manual_resume_requires_a_strict_boolean_enabled_value(world, monkeypatch, enabled):
    active(world)
    state = world._read_file('state.json')
    state['enabled'] = enabled
    world._write_file('state.json', state)

    def forbidden(*args, **kwargs):
        pytest.fail('Malformed enabled state must not reach the credential reader')

    monkeypatch.setattr(world, 'read', forbidden)
    failure(lambda: local.begin_registration(world, now_ms=NOW), 'REGISTRATION_HELD')


def test_actual_connector_claim_consent_confirm_then_provision_ack(world):
    local.begin_registration(world, now_ms=NOW)
    seen = []

    class EnrollmentWire:
        def send(self, path, raw, headers=None):
            seen.append(path)
            body = json.loads(raw)
            state = world.read('state.json')
            if path.endswith('/claim'):
                assert body['claim_id'] == state['claim_id']
                return {'claim_id': state['claim_id'], 'device_id': DEVICE, 'key_id': KEY,
                        'audience': local.monitor_endpoint(world)['audience']}
            config = local.monitor_endpoint(world)
            result = {k: state[k] for k in ('claim_id', 'device_id', 'key_id')}
            result.update(audience=config['audience'], environment=config['environment'],
                          fingerprint=hashlib.sha256(bytes.fromhex(world.read('credential')['key'])).hexdigest()[:16],
                          consent_revision='sdc-self-monitoring-v1', data_scope='version-process-status-only',
                          tenant_name='Synthetic tenant', alias='Synthetic device',
                          pending_expires_at=NOW + 300000,
                          state='active' if path.endswith('/confirm') else 'admin_approved')
            return result

    connector = Connector(world, EnrollmentWire(), clock=lambda: NOW / 1000)
    with world.lock():
        result = connector.connect('synthetic-unusable-token')
        connector.save_enrollment_consent(result)
        connector.confirm()
    assert world.read('state.json')['state'] == 'active'
    assert record(world)['profiles']['monitor']['ack'] is None
    wire = Wire(world)
    assert exchange(world, wire)['acknowledged'] is True
    assert seen == ['/v2/enrollments/claim', '/v2/enrollments/status', '/v2/enrollments/confirm']
    assert len(wire.sent) == 1


def test_ack_one_dispatch_is_durable_and_repeated_ack_never_sends_again(world):
    active(world)
    wire = Wire(world)
    assert exchange(world, wire)['acknowledged']
    first = record(world)['profiles']['monitor']['ack']
    assert exchange(world, wire)['acknowledged']
    assert len(wire.sent) == 1
    assert record(world)['profiles']['monitor']['ack'] == first
    assert 'key' not in record(world)['profiles']['monitor']['ack_attempts'][0]


def test_ack_unknown_preserves_exact_raw_across_reopen_and_caps_attempts_at_four(world):
    active(world)
    sent = []

    def timeout(path, raw):
        sent.append(raw)
        raise TransportError(503, CANARY)

    for _ in range(4):
        with Store(world.root) as reopened:
            assert exchange(reopened, Wire(reopened, timeout))['state'] == 'unknown'
    with Store(world.root) as reopened:
        failure(lambda: exchange(reopened, Wire(reopened, timeout)), 'ATTEMPTS_EXHAUSTED')
    assert len(sent) == 4 and len(set(sent)) == 1
    assert len(record(world)['profiles']['monitor']['ack_attempts']) == 4


def test_past_import_unknown_ack_requires_status_to_recover_original_receipt(world):
    active(world)
    saved_receipt = None

    def lost(path, raw):
        nonlocal saved_receipt
        saved_receipt = ack_response(world, raw)['receipt']
        raise TransportError()

    first = Wire(world, lost)
    exchange(world, first)
    slot = record(world)['profiles']['monitor']
    original = copy.deepcopy(slot['ack_request'])
    expiry = json.loads(slot['raw'])['import_expires_at']
    endpoint = local.monitor_endpoint(world)
    with Store(world.root) as reopened:
        wire = Wire(reopened)
        failure(lambda: exchange(reopened, wire, now=expiry), 'EXPIRED')
        assert not wire.sent and len(record(reopened)['profiles']['monitor']['ack_attempts']) == 1
        response = status_response(reopened, receipt=saved_receipt)
        status_wire = Wire(reopened, lambda path, raw: response)
        assert exchange(reopened, status_wire, action='status', now=expiry)['acknowledged']
        current = record(reopened)['profiles']['monitor']
        assert status_wire.sent == [(local.STATUS, current['status_request']['raw'].encode('ascii'))]
        assert current['ack'] == saved_receipt and current['ack_request'] == original
        assert len(current['ack_attempts']) == 1
        assert local.monitor_endpoint(reopened) == endpoint and reopened.read('state.json')['enabled'] is True
        assert exchange(reopened, wire, now=expiry)['acknowledged']  # Existing receipt is metadata only.
        assert not wire.sent


@pytest.mark.parametrize('stage', ['sign', 'factory'])
@pytest.mark.parametrize('cancel', [False, True])
def test_ack_preparation_failure_does_not_reserve_or_change_state(world, monkeypatch, stage, cancel):
    from adapters.sdc import protocol

    active(world)
    before = copy.deepcopy(record(world))
    original_error = KeyboardInterrupt(CANARY) if cancel else ValueError(CANARY)
    wire = Wire(world)
    calls = []

    def failed(*args, **kwargs):
        calls.append(stage)
        raise original_error

    if stage == 'sign':
        monkeypatch.setattr(protocol, 'sign_provision', failed)
        factory = wire
    else:
        factory = failed
    if cancel:
        with pytest.raises(KeyboardInterrupt) as caught:
            exchange(world, factory)
        assert caught.value is original_error
    else:
        failure(lambda: exchange(world, factory), 'INVALID')
    assert calls == [stage] and not wire.sent
    assert record(world) == before
    assert not record(world)['profiles']['monitor']['ack_attempts']


def test_ack_expiry_after_reservation_raises_without_dispatch_or_rollback(world, monkeypatch):
    active(world)
    now = [NOW]
    slot = record(world)['profiles']['monitor']
    original_request = copy.deepcopy(slot['ack_request'])
    expiry = json.loads(slot['raw'])['import_expires_at']
    original_write = world._write_file
    writes = []

    def advance_after_reservation(name, value):
        original_write(name, value)
        if name == local.NAME:
            writes.append(name)
            now[0] = expiry

    monkeypatch.setattr(world, '_write_file', advance_after_reservation)
    wire = Wire(world)
    failure(lambda: local.ack_once(world, profile='monitor', transport_factory=wire,
                                  clock=lambda: now[0] / 1000, confirm_human=True, reason=REASON), 'EXPIRED')
    assert not wire.sent and writes == [local.NAME]
    slot = record(world)['profiles']['monitor']
    assert slot['last_result'] == 'unknown' and slot['ack_denied'] is False
    assert slot['ack_request'] == original_request and len(slot['ack_attempts']) == 1
    with Store(world.root) as reopened:
        failure(lambda: exchange(reopened, Wire(reopened), now=expiry), 'EXPIRED')
        assert record(reopened)['profiles']['monitor'] == slot


@pytest.mark.parametrize('change', ['binding', 'stop', 'expiry'])
def test_ack_admission_rechecks_changes_during_local_preparation(world, change):
    active(world)
    now = [NOW]
    wire = Wire(world)
    expiry = json.loads(record(world)['profiles']['monitor']['raw'])['import_expires_at']

    def prepare(config):
        if change == 'binding':
            changed = record(world)
            changed['registration']['registration_id'] = OTHER
            world._write_file(local.NAME, changed)
        elif change == 'stop':
            changed = world._read_file('state.json')
            changed['enabled'] = False
            world._write_file('state.json', changed)
        else:
            now[0] = expiry
        return wire(config)

    failure(lambda: local.ack_once(world, profile='monitor', transport_factory=prepare,
                                  clock=lambda: now[0] / 1000, confirm_human=True, reason=REASON),
            'EXPIRED' if change == 'expiry' else 'CONFLICT')
    slot = record(world)['profiles']['monitor']
    assert not wire.sent and not slot['ack_attempts'] and slot['last_result'] == 'not-sent'


def test_status_is_one_explicit_send_without_new_quota_and_null_remains_unknown(world):
    active(world)
    wire = Wire(world)
    for _ in range(5):
        assert exchange(world, wire, action='status')['state'] == 'unknown'
    assert len(wire.sent) == 5 and len(set(raw for _, raw in wire.sent)) == 1
    slot = record(world)['profiles']['monitor']
    assert not slot['ack_attempts'] and slot['ack'] is None
    assert slot['last_status']['status']['receipt'] is None


def test_matching_status_recovers_original_ack_and_wrapper_id_can_change(world):
    active(world)
    receipt = None

    def lost(path, raw):
        nonlocal receipt
        receipt = ack_response(world, raw)['receipt']
        raise TransportError()

    exchange(world, Wire(world, lost))
    wire = Wire(world, lambda path, raw: status_response(world, receipt=receipt))
    assert exchange(world, wire, action='status')['acknowledged']
    changed = status_response(world, receipt=receipt)
    changed['request_id'] = CLAIM
    assert exchange(world, Wire(world, lambda path, raw: changed), action='status')['acknowledged']
    assert record(world)['profiles']['monitor']['ack'] == receipt


@pytest.mark.parametrize('field,value', [('root_id', OTHER), ('ack_id', OTHER), ('local_receipt_id', OTHER),
                                       ('bundle_hash', 'b' * 64), ('device_id', OTHER), ('claim_id', OTHER),
                                       ('key_id', OTHER), ('generation', 2), ('applied_at', NOW + 1)])
def test_mismatched_ack_never_becomes_primary_receipt(world, field, value):
    active(world)

    def changed(path, raw):
        response = ack_response(world, raw)
        response['receipt'][field] = value
        return response

    wire = Wire(world, changed)
    failure(lambda: exchange(world, wire), 'CONFLICT')
    assert len(wire.sent) == 1
    assert record(world)['profiles']['monitor']['ack'] is None


def test_existing_primary_receipt_is_fully_immutable(world):
    active(world)
    exchange(world, Wire(world))
    primary = record(world)['profiles']['monitor']['ack']
    changed = {**primary, 'accepted_at': primary['accepted_at'] + 1}
    failure(lambda: exchange(world, Wire(world, lambda path, raw: status_response(world, receipt=changed)), action='status'), 'CONFLICT')
    assert record(world)['profiles']['monitor']['ack'] == primary


def test_late_matching_ack_is_evidence_only_and_does_not_reenable_r1(world):
    active(world)

    def stopped(path, raw):
        response = ack_response(world, raw)
        state = world.read('state.json')
        state['enabled'] = False
        world.write('state.json', state)
        return response

    assert exchange(world, Wire(world, stopped))['acknowledged']
    assert world.read('state.json')['enabled'] is False


def test_expired_initial_ack_does_not_disable_monitor_projection_or_r1(world):
    active(world)
    slot = record(world)['profiles']['monitor']
    bundle = json.loads(slot['raw'])
    wire = Wire(world)
    failure(lambda: exchange(world, wire, now=bundle['import_expires_at']), 'EXPIRED')
    assert not wire.sent and not record(world)['profiles']['monitor']['ack_attempts']
    assert local.monitor_endpoint(world) == {k: bundle[k] for k in ('origin', 'audience', 'environment')}
    assert world.read('state.json')['enabled'] is True


def test_current_clock_after_credential_read_still_blocks_expired_first_ack(world, monkeypatch):
    active(world)
    now = [NOW]
    expiry = json.loads(record(world)['profiles']['monitor']['raw'])['import_expires_at']
    original = world.read

    def delayed(name, default=None):
        result = original(name, default)
        if name == 'credential':
            now[0] = expiry
        return result

    monkeypatch.setattr(world, 'read', delayed)
    wire = Wire(world)
    failure(lambda: local.ack_once(world, profile='monitor', transport_factory=wire, clock=lambda: now[0] / 1000,
                                  confirm_human=True, reason=REASON), 'EXPIRED')
    assert not wire.sent


def test_known_denial_is_terminal_but_metadata_status_remains_available(world):
    active(world)

    def denied(path, raw):
        raise TransportError(403, CANARY)

    assert exchange(world, Wire(world, denied))['state'] == 'denied'
    failure(lambda: exchange(world, Wire(world)), 'CONFLICT')
    assert exchange(world, Wire(world), action='status')['state'] == 'denied'
    assert record(world)['profiles']['monitor']['ack_denied'] is True
    with Store(world.root) as reopened:
        failure(lambda: exchange(reopened, Wire(reopened)), 'CONFLICT')
        assert len(record(reopened)['profiles']['monitor']['ack_attempts']) == 1


@pytest.mark.parametrize('http_status', [401, 429])
@pytest.mark.parametrize('cancel', [False, True])
def test_temporary_ack_refusal_is_unknown_and_human_retries_use_original_request(world, http_status, cancel):
    active(world)
    slot = record(world)['profiles']['monitor']
    original_request = copy.deepcopy(slot['ack_request'])
    original_receipt = copy.deepcopy(slot['receipt'])
    sent = []
    for attempt in range(4):
        with Store(world.root) as reopened:
            original_error = KeyboardInterrupt(CANARY) if cancel else TransportError(http_status, CANARY)

            def temporary(path, raw):
                if cancel:
                    wire.refusal_status = http_status
                raise original_error

            wire = Wire(reopened, temporary)
            if cancel:
                with pytest.raises(KeyboardInterrupt) as caught:
                    exchange(reopened, wire)
                assert caught.value is original_error
            else:
                assert exchange(reopened, wire)['state'] == 'unknown'
            slot = record(reopened)['profiles']['monitor']
            assert slot['ack_denied'] is False and slot['last_result'] == 'unknown' and slot['ack'] is None
            assert slot['ack_request'] == original_request and slot['receipt'] == original_receipt
            assert len(slot['ack_attempts']) == attempt + 1
            if attempt == 0:
                before = copy.deepcopy(record(reopened))
                failure(lambda: local.ack_once(reopened, profile='monitor', transport_factory=wire,
                                              clock=lambda: NOW / 1000, confirm_human=False, reason=REASON),
                        'CONSENT_REQUIRED')
                assert record(reopened) == before
            assert wire.sent == [(local.ACK, original_request['raw'].encode('ascii'))]
            sent.extend(wire.sent)
    with Store(world.root) as reopened:
        wire = Wire(reopened)
        failure(lambda: exchange(reopened, wire), 'ATTEMPTS_EXHAUSTED')
        assert not wire.sent and len(record(reopened)['profiles']['monitor']['ack_attempts']) == 4
    assert len(sent) == 4 and len(set(sent)) == 1


@pytest.mark.parametrize('http_status', [401, 429])
def test_temporary_ack_refusal_never_extends_original_import_deadline(world, http_status):
    active(world)

    def temporary(path, raw):
        raise TransportError(http_status, CANARY)

    assert exchange(world, Wire(world, temporary))['state'] == 'unknown'
    before = copy.deepcopy(record(world))
    expiry = json.loads(before['profiles']['monitor']['raw'])['import_expires_at']
    with Store(world.root) as reopened:
        wire = Wire(reopened)
        failure(lambda: exchange(reopened, wire, now=expiry), 'EXPIRED')
        assert not wire.sent and record(reopened) == before
        assert reopened._read_file('state.json')['enabled'] is True


@pytest.mark.parametrize('error_status,observed_status', [(401, 403), (403, 429)])
@pytest.mark.parametrize('cancel', [False, True])
def test_terminal_ack_refusal_takes_priority_over_temporary_evidence(world, error_status, observed_status, cancel):
    active(world)
    original_error = KeyboardInterrupt(CANARY) if cancel else TransportError(error_status, CANARY)
    if cancel:
        original_error.status = error_status

    def refused(path, raw):
        wire.refusal_status = observed_status
        raise original_error

    wire = Wire(world, refused)
    if cancel:
        with pytest.raises(KeyboardInterrupt) as caught:
            exchange(world, wire)
        assert caught.value is original_error
    else:
        assert exchange(world, wire)['state'] == 'denied'
    slot = record(world)['profiles']['monitor']
    assert slot['ack_denied'] is True and slot['last_result'] == 'denied'
    with Store(world.root) as reopened:
        retry = Wire(reopened)
        failure(lambda: exchange(reopened, retry), 'CONFLICT')
        assert not retry.sent and len(record(reopened)['profiles']['monitor']['ack_attempts']) == 1
    assert len(wire.sent) == 1


def test_cancel_after_observed_refusal_preserves_original_exception_and_durable_denial(world):
    active(world)
    cancelled = KeyboardInterrupt(CANARY)
    clients = []

    def refused(path, raw):
        wire.refusal_status = 403
        raise cancelled

    wire = Wire(world, refused)

    def factory(config):
        clients.append(config)
        return wire(config)

    before = copy.deepcopy(record(world)['profiles']['monitor']['ack_request'])
    with pytest.raises(KeyboardInterrupt) as caught:
        exchange(world, factory)
    assert caught.value is cancelled
    slot = record(world)['profiles']['monitor']
    assert slot['ack_denied'] is True and slot['last_result'] == 'denied'
    assert slot['ack_request'] == before and len(slot['ack_attempts']) == 1
    failure(lambda: exchange(world, factory), 'CONFLICT')
    with Store(world.root) as reopened:
        failure(lambda: exchange(reopened, factory), 'CONFLICT')
    assert len(clients) == len(wire.sent) == 1


@pytest.mark.parametrize('evidence', [None, True, 503, '401'])
def test_cancel_without_int_4xx_evidence_keeps_unknown_and_original_request(world, evidence):
    active(world)
    cancelled = KeyboardInterrupt(CANARY)

    def interrupted(path, raw):
        wire.refusal_status = evidence
        raise cancelled

    wire = Wire(world, interrupted)
    before = copy.deepcopy(record(world)['profiles']['monitor']['ack_request'])
    with pytest.raises(KeyboardInterrupt) as caught:
        exchange(world, wire)
    assert caught.value is cancelled
    slot = record(world)['profiles']['monitor']
    assert slot['ack_denied'] is False and slot['last_result'] == 'unknown'
    assert slot['ack_request'] == before and len(slot['ack_attempts']) == 1
    assert len(wire.sent) == 1
    with Store(world.root) as reopened:
        retry = Wire(reopened)
        assert exchange(reopened, retry)['acknowledged']
        assert retry.sent == wire.sent
        assert len(record(reopened)['profiles']['monitor']['ack_attempts']) == 2


@pytest.mark.parametrize('second_cancel', [False, True])
def test_cancel_refusal_save_failure_holds_store_and_preserves_first_exception(world, monkeypatch, second_cancel):
    active(world)
    cancelled = KeyboardInterrupt(CANARY)
    second = KeyboardInterrupt('second cancellation') if second_cancel else OSError(CANARY)
    original = world._write_file
    writes = []

    def uncertain(name, value):
        if name == local.NAME:
            writes.append(name)
            if len(writes) == 2:
                raise second
        return original(name, value)

    def refused(path, raw):
        wire.refusal_status = 403
        raise cancelled

    monkeypatch.setattr(world, '_write_file', uncertain)
    wire = Wire(world, refused)
    with pytest.raises(KeyboardInterrupt) as caught:
        exchange(world, wire)
    assert caught.value is cancelled
    assert world._provision_uncertain is True
    assert record(world)['profiles']['monitor']['last_result'] == 'unknown'
    failure(lambda: exchange(world, Wire(world)), 'UNCERTAIN')
    assert len(wire.sent) == 1 and len(writes) == 2


@pytest.mark.parametrize('state', ['revoked', 'superseded', 'expired'])
def test_noncurrent_status_prevents_ack_and_later_metadata_does_not_clear_it(world, state):
    active(world)
    wire = Wire(world, lambda path, raw: status_response(world, state=state))
    assert exchange(world, wire, action='status')['state'] == 'denied'
    failure(lambda: exchange(world, Wire(world)), 'CONFLICT')
    assert exchange(world, Wire(world), action='status')['state'] == 'denied'
    with Store(world.root) as reopened:
        failure(lambda: exchange(reopened, Wire(reopened)), 'CONFLICT')
        assert not record(reopened)['profiles']['monitor']['ack_attempts']


def test_malformed_reply_leaves_one_durable_unknown_without_retry(world):
    active(world)
    wire = Wire(world, lambda path, raw: {'private': CANARY})
    failure(lambda: exchange(world, wire))
    assert len(wire.sent) == 1
    assert local.status(world, now_ms=NOW)['state'] == 'unknown'
    assert record(world)['profiles']['monitor']['ack'] is None


def test_real_lock_contention_never_dispatches_or_consumes_ack_attempt(world):
    active(world)
    wire = Wire(world)
    with Store(world.root) as other, other.lock():
        with pytest.raises(BlockingIOError):
            exchange(world, wire)
    assert not wire.sent and not record(world)['profiles']['monitor']['ack_attempts']


def test_record_cas_change_during_http_does_not_adopt_response(world):
    active(world)

    def changed(path, raw):
        response = ack_response(world, raw)
        state = record(world)
        state['registration']['registration_id'] = OTHER
        world._write_file(local.NAME, state)
        return response

    wire = Wire(world, changed)
    failure(lambda: exchange(world, wire), 'CONFLICT')
    assert len(wire.sent) == 1 and record(world)['profiles']['monitor']['ack'] is None


def test_unknown_or_uninstalled_profile_never_gets_monitor_authority(world):
    for profile in ('unknown', None):
        failure(lambda: local.ack_once(world, profile=profile, transport_factory=lambda _: None,
                                      clock=lambda: NOW / 1000, confirm_human=True, reason=REASON), 'UNSUPPORTED')
    for profile in ('support', 'restart'):
        failure(lambda: local.ack_once(world, profile=profile, transport_factory=lambda _: None,
                                      clock=lambda: NOW / 1000, confirm_human=True, reason=REASON), 'REGISTRATION_HELD')


@pytest.mark.parametrize('stage', [1, 2, 3])
def test_root_guard_and_each_fsync_failure_leave_hold_not_fresh(tmp_path, monkeypatch, stage):
    with Store(tmp_path / 'fresh', create_new=True) as store:
        real, calls = os.fsync, []

        def failed(fd):
            calls.append(fd)
            if len(calls) == stage:
                raise OSError(CANARY)
            return real(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, 'fsync', failed)
            failure(lambda: setup_root(store), 'UNCERTAIN')
            failure(lambda: local.monitor_endpoint(store), 'UNCERTAIN')
        assert (store.root / local.NAME).exists()
        with Store(store.root) as reopened:
            failure(lambda: setup_root(reopened), 'CONFLICT')
            failure(lambda: local.begin_registration(reopened, now_ms=NOW))


@pytest.mark.parametrize('stage', [1, 2])
def test_import_fsync_failure_cannot_start_r1_even_when_complete_record_is_visible(tmp_path, monkeypatch, stage):
    with Store(tmp_path / 'fresh', create_new=True) as store:
        setup_root(store)
        real, calls = os.fsync, []

        def failed(fd):
            calls.append(fd)
            if len(calls) == stage:
                raise OSError(CANARY)
            return real(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, 'fsync', failed)
            failure(lambda: import_bundle(store), 'UNCERTAIN')
            failure(lambda: local.begin_registration(store, now_ms=NOW), 'UNCERTAIN')
        with Store(store.root) as reopened:
            failure(lambda: local.begin_registration(reopened, now_ms=NOW))
            assert reopened._read_file('state.json') is None and reopened._read_file('credential') is None


@pytest.mark.parametrize('operation', ['replace', 'readback'])
def test_import_replace_or_readback_failure_keeps_uncertain_and_no_registration(world, monkeypatch, operation):
    raw, expected = material(world)
    plan = local.prepare_import(world, raw, expected=expected, now_ms=NOW)
    if operation == 'replace':
        def failed(*args, **kwargs):
            raise OSError(CANARY)
        monkeypatch.setattr(os, 'replace', failed)
    else:
        real, writes = world._read_file, []
        original_write = world._write_file

        def wrote(name, value):
            original_write(name, value)
            writes.append(name)

        def readback(name, default=None):
            if name == local.NAME and writes:
                raise OSError(CANARY)
            return real(name, default)

        monkeypatch.setattr(world, '_write_file', wrote)
        monkeypatch.setattr(world, '_read_file', readback)
    failure(lambda: local.commit_import(world, plan, confirm_human=True, reason=REASON, now_ms=NOW), 'UNCERTAIN')
    assert getattr(world, '_provision_uncertain') is True
    failure(lambda: local.begin_registration(world, now_ms=NOW), 'UNCERTAIN')


def test_registration_marker_save_failure_never_issues_a_ram_permit(world, monkeypatch):
    original = world._write_file

    def uncertain(name, value):
        original(name, value)
        if name == local.NAME:
            raise OSError(CANARY)

    monkeypatch.setattr(world, '_write_file', uncertain)
    failure(lambda: local.begin_registration(world, now_ms=NOW), 'UNCERTAIN')
    assert getattr(world, '_provision_registration_permit', None) is None
    with Store(world.root) as reopened:
        failure(lambda: local.begin_registration(reopened, now_ms=NOW), 'REGISTRATION_HELD')


def test_ack_admission_save_failure_sends_nothing_and_result_save_failure_retains_original_ids(world, monkeypatch):
    active(world)
    original = world._write_file
    wire = Wire(world)

    def failed(name, value):
        if name == local.NAME:
            raise OSError(CANARY)
        return original(name, value)

    with monkeypatch.context() as patch:
        patch.setattr(world, '_write_file', failed)
        failure(lambda: exchange(world, wire), 'UNCERTAIN')
    assert not wire.sent
    with Store(world.root) as reopened:
        before = record(reopened)['profiles']['monitor']['ack_request']
        writes = []
        real = reopened._write_file

        def result_failed(name, value):
            if name == local.NAME:
                writes.append(value)
                if len(writes) == 2:
                    raise OSError(CANARY)
            return real(name, value)

        transport = Wire(reopened)
        monkeypatch.setattr(reopened, '_write_file', result_failed)
        failure(lambda: exchange(reopened, transport), 'UNCERTAIN')
        assert len(transport.sent) == 1
    with Store(world.root) as reopened:
        assert record(reopened)['profiles']['monitor']['ack_request'] == before
        assert record(reopened)['profiles']['monitor']['ack'] is None
        assert exchange(reopened, Wire(reopened))['acknowledged']


def test_root_swap_corruption_and_projection_conflict_do_not_fallback(world):
    saved = record(world)
    world._write_file('endpoint.json', {'origin': 'https://other.invalid', 'audience': 'other', 'environment': 'other'})
    failure(lambda: local.monitor_endpoint(world), 'CONFLICT')
    os.unlink('endpoint.json', dir_fd=world.fd)
    world._write_file(local.NAME, {**saved, 'private_key': CANARY})
    failure(lambda: local.monitor_endpoint(world))
    world._write_file(local.NAME, saved)
    original = world.root
    moved = original.with_name('moved')
    original.rename(moved)
    original.mkdir(mode=0o700)
    failure(lambda: local.monitor_endpoint(world))


def test_record_disappearance_on_known_store_never_downgrades_to_legacy(world):
    active(world)
    assert local.status(world, now_ms=NOW)['registration_started']
    state, credential = world.read('state.json'), world.read('credential')
    os.unlink(local.NAME, dir_fd=world.fd)
    for action in (lambda: local.monitor_endpoint(world), lambda: local.status(world, now_ms=NOW),
                   lambda: local.begin_registration(world, now_ms=NOW),
                   lambda: local.guard_registration(world, state, credential)):
        failure(action, 'CONFLICT')


def test_local_status_projection_never_reads_credentials_or_calls_http(world, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('metadata must not read credentials')

    monkeypatch.setattr(world, 'read', forbidden)
    monkeypatch.setattr(world, '_keychain', forbidden)
    assert local.status(world, now_ms=NOW)['state'] == 'imported'
    assert local.monitor_endpoint(world)['origin'] == 'https://ingest.invalid'
    assert len(json.dumps(record(world), ensure_ascii=True, separators=(',', ':')).encode()) < local.STATE_MAX_BYTES


def test_ack_reserved_before_an_unfinished_intent_is_never_dispatched_or_refunded(world, monkeypatch):
    from test_sdc_diagnostic_sender import unfinished_marker
    active(world)
    original_write = world._write_file
    appeared = []

    def intent_after_reservation(name, value):
        original_write(name, value)
        if name == local.NAME and not appeared:
            # Inside ack_once's own lock, after the durable reservation and the actual credential read.
            appeared.append(copy.deepcopy(record(world)['profiles']['monitor']))
            unfinished_marker(world)

    monkeypatch.setattr(world, '_write_file', intent_after_reservation)
    wire = Wire(world)
    with pytest.raises(local.ProvisionError, match='^SDC_PROVISION_INVALID$') as caught:
        exchange(world, wire)
    # The fixed public code hides the cause; the chained cause is the real Store guard.
    cause = caught.value.__cause__ or caught.value.__context__
    assert str(cause) == 'SDC_CREDENTIAL_MUTATION_UNRESOLVED'
    assert not wire.sent
    reserved, slot = appeared[0], record(world)['profiles']['monitor']
    # The reservation, its exact request/ID and the consumed attempt stay; nothing is rolled back or refunded.
    assert slot == reserved and len(slot['ack_attempts']) == 1 and slot['last_result'] == 'unknown'
    assert slot['ack'] is None and slot['ack_denied'] is False
