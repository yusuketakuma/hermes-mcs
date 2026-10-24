"""Synthetic initial profile imports retain independent scope, permits and monitor ACK history."""
import copy
import hashlib
import hmac
import json
import os
import pickle

import pytest

from adapters.sdc import provision as local
from adapters.sdc import restart_planning, restart_state, support_state
from adapters.sdc.storage import Store, LIMITS
from adapters.sdc.transport import TransportError
from test_sdc_provision import (V, NOW, DEVICE, KEY, OTHER, REASON, CANARY,
                               active, encoded, record, failure, Wire, exchange, ack_response)
from test_sdc_provision import world  # noqa: F401


@pytest.fixture
def registered(world):  # noqa: F811 - reuse the imported pytest fixture
    active(world)
    return world


def material(store, profile, **changes):
    bundle = copy.deepcopy(next(v['value'] for v in V['positive']
                                if v['definition'] == 'Bundle' and v['value']['profile_id'] == profile))
    bundle.update(root_id=record(store)['root_id'], origin=f'https://{profile}.invalid',
                  audience=f'sdc:{profile}')
    bundle.update(changes)
    raw = encoded(bundle)
    expected = {k: bundle[k] for k in ('console_origin', 'tenant', 'installation', 'environment',
                                      'enrollment_id', 'profile_id', 'origin', 'audience')}
    expected['bundle_hash'] = hashlib.sha256(raw).hexdigest()
    return raw, expected


def initialize_records(store, profile):
    snapshot = local.profile_snapshot(store, profile=profile, now_ms=NOW)
    assert local.initialization_allowed(store, profile)
    if profile == 'support':
        bundle = snapshot['bundle']
        binding = {'root_fingerprint': snapshot['root_fingerprint'],
                   'monitor_endpoint_hash': snapshot['monitor_endpoint_hash'],
                   'support_endpoint_hash': store._fingerprint({k: bundle[k] for k in ('origin', 'audience', 'environment')}),
                   'pins_sha256': hashlib.sha256(encoded(bundle['pins'])).hexdigest(),
                   'secret_sha256': hashlib.sha256(bytes.fromhex('a' * 64)).hexdigest(), 'key_id': KEY}
        binding['profile'] = {'bundle_hash': snapshot['bundle_hash'], 'expires_at': bundle['expires_at'],
                              'endpoint': {k: bundle[k] for k in ('origin', 'audience', 'environment')}}
        support_state.prepare(store, binding_supplier=lambda: binding, now_ms=NOW, initialize=True)
    else:
        restart_state.initialize(store)
        restart_planning.initialize(store)


def initialize(store, profile):
    return local.initialize_profile(store, profile=profile, now_ms=NOW,
                                    initializer=lambda: initialize_records(store, profile))


def install(store, profile, *, ready=True, **changes):
    raw, expected = material(store, profile, **changes)
    plan = local.prepare_import(store, raw, expected=expected, now_ms=NOW)
    receipt = local.commit_import(store, plan, confirm_human=True, reason=REASON, now_ms=NOW)
    if ready:
        assert initialize(store, profile) == receipt
    return raw, expected


class ProfileWire(Wire):
    def __init__(self, store, profile, respond=None):
        super().__init__(store, respond)
        self.profile = profile

    def send(self, path, raw, headers):
        with Store(self.store.root) as other, other.lock():
            slot = record(other)['profiles'][self.profile]
            expected = slot['ack_request' if path == local.ACK else 'status_request']
            assert expected['raw'].encode('ascii') == raw
            assert expected['body_hash'] == hashlib.sha256(raw).hexdigest()
            assert slot['last_result'] == 'unknown'
            if path == local.ACK:
                assert len(slot['ack_attempts']) >= 1
        assert headers['X-SDC-Device'] == DEVICE and headers['X-SDC-Key-ID'] == KEY
        message = '\n'.join(('SDC-HMAC-v1', 'sdc:synthetic', 'POST', path, DEVICE, KEY,
                             headers['X-SDC-Time'], headers['X-SDC-Nonce'], hashlib.sha256(raw).hexdigest()))
        assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex('a' * 64), message.encode(), hashlib.sha256).hexdigest()
        self.sent.append((path, raw))
        return self.respond(path, raw) if self.respond else ack_response(self.store, raw)


def send(store, profile, wire, *, action='ack', now=NOW):
    return local.ack_once(store, profile=profile, transport_factory=wire, clock=lambda: now / 1000,
                          action=action, confirm_human=True, reason=REASON)


def status_response(store, profile, receipt=None, state='current'):
    slot = record(store)['profiles'][profile]
    bundle = json.loads(slot['raw'])
    value = {k: bundle[k] for k in ('provision_id', 'root_id', 'generation', 'realm_epoch', 'policy_revision', 'expires_at')}
    value.update(bundle_hash=slot['bundle_hash'], current_generation=bundle['generation'],
                 current_provision_id=bundle['provision_id'], state=state, receipt=receipt)
    return {'schema_version': 'sdc.provision.status-response.v1', 'status': value, 'request_id': OTHER}


def test_three_profiles_keep_distinct_targets_and_one_monitor_registration(registered):
    monitor = copy.deepcopy(record(registered))
    for profile in ('support', 'restart'):
        raw, _ = install(registered, profile)
        snapshot = local.profile_snapshot(registered, profile=profile, now_ms=NOW)
        assert set(snapshot) == {'bundle', 'root_fingerprint', 'monitor_endpoint_hash', 'bundle_hash', 'local_receipt_id'}
        assert snapshot['bundle'] == json.loads(raw)
        assert snapshot['bundle_hash'] == hashlib.sha256(raw).hexdigest()
        assert snapshot['monitor_endpoint_hash'] == registered._fingerprint(local.monitor_endpoint(registered))
        snapshot['bundle']['pins'].clear()
        assert local.profile_snapshot(registered, profile=profile, now_ms=NOW)['bundle']['pins']
        assert not local.initialization_allowed(registered, profile)
    saved = record(registered)
    assert saved['profiles']['monitor'] == monitor['profiles']['monitor']
    assert saved['registration'] == monitor['registration']
    bootstrap = local.support_bootstrap(registered)
    assert set(bootstrap) == {'schema_version', 'root_fingerprint', 'monitor_endpoint_hash', 'support_endpoint', 'pins', 'authority'}
    assert bootstrap['support_endpoint'] == {'origin': 'https://support.invalid', 'audience': 'sdc:support', 'environment': 'synthetic'}
    assert bootstrap['authority'] == local.profile_snapshot(registered, profile='support', now_ms=NOW)
    assert not (registered.root / 'support-bootstrap.json').exists()
    assert registered.read('support-state.json')['enabled'] is False
    assert registered.read('restart-state.json')['receipt'] is None
    assert registered.read('restart-planning.json')['plan'] is None
    assert registered.read('restart-execution.json') is None
    assert 3 * (3 * 2 * 16384 + 2 * 16384 + 8192) + 8192 == local.STATE_MAX_BYTES < LIMITS[local.NAME]


def test_current_profile_metadata_never_reads_secret_or_authenticates_a_central_head(registered, monkeypatch):
    install(registered, 'support')
    install(registered, 'restart')
    read = registered.read

    def metadata(name, default=None):
        assert name != 'credential'
        return read(name, default)

    monkeypatch.setattr(registered, 'read', metadata)
    monkeypatch.setattr(local, '_current_identity', lambda *_: pytest.fail('metadata must not obtain a signing key'))
    for profile in ('support', 'restart'):
        snapshot = local.profile_snapshot(registered, profile=profile, now_ms=NOW)
        assert 'current_head' not in snapshot and 'key' not in snapshot
        assert snapshot['bundle']['profile_id'] == profile
    assert local.support_bootstrap(registered)['authority']['bundle']['profile_id'] == 'support'


@pytest.mark.parametrize(('field', 'value'), [('tenant', 'other'), ('installation', 'other'),
    ('environment', 'other'), ('enrollment_id', OTHER), ('realm_epoch', 'other'),
    ('device_id', OTHER), ('claim_id', OTHER), ('root_id', OTHER),
    ('generation', 2), ('previous_provision_id', OTHER)])
def test_independently_confirmed_but_wrong_cohort_or_generation_cannot_replace_registration(registered, field, value):
    before = record(registered)
    raw, expected = material(registered, 'support', **{field: value})
    failure(lambda: local.prepare_import(registered, raw, expected=expected, now_ms=NOW))
    assert record(registered) == before


def test_restart_requires_support_cohort_and_same_device_epoch(registered):
    raw, expected = material(registered, 'restart')
    failure(lambda: local.prepare_import(registered, raw, expected=expected, now_ms=NOW), 'MISSING')
    install(registered, 'support')
    raw, expected = material(registered, 'restart', device_epoch=2)
    failure(lambda: local.prepare_import(registered, raw, expected=expected, now_ms=NOW), 'CONFLICT')


@pytest.mark.parametrize('change', ['purpose', 'spki', 'pin-expiry', 'raw', 'expected-hash'])
def test_invalid_support_distribution_never_creates_a_slot_or_permission(registered, change):
    raw, expected = material(registered, 'support')
    if change in ('purpose', 'spki', 'pin-expiry'):
        bundle = json.loads(raw)
        if change == 'purpose':
            bundle['pins'][0]['purposes'] = ['restart-consent-preview']
        elif change == 'spki':
            bundle['pins'][0]['spki'] = 'invalid'
        else:
            bundle['pins'][0]['expires_at'] = NOW
        raw = encoded(bundle)
        expected['bundle_hash'] = hashlib.sha256(raw).hexdigest()
    elif change == 'raw':
        raw += b' '
        expected['bundle_hash'] = hashlib.sha256(raw).hexdigest()
    else:
        expected['bundle_hash'] = '0' * 64
    before = record(registered)
    failure(lambda: local.prepare_import(registered, raw, expected=expected, now_ms=NOW))
    assert record(registered) == before and not getattr(registered, '_provision_profile_permits', {})


def test_commit_rechecks_current_identity_and_never_uses_prompt_time_as_permission(registered):
    raw, expected = material(registered, 'support')
    plan = local.prepare_import(registered, raw, expected=expected, now_ms=NOW)
    failure(lambda: local.commit_import(registered, plan, confirm_human=False, reason=REASON, now_ms=NOW), 'CONSENT_REQUIRED')
    current = registered.read('state.json')
    current['device_id'] = OTHER
    registered.write('state.json', current)
    failure(lambda: local.commit_import(registered, plan, confirm_human=True, reason=REASON, now_ms=NOW), 'CONFLICT')
    assert record(registered)['profiles']['support'] is None


@pytest.mark.parametrize(('field', 'value'), [('enabled', False), ('claim_id', OTHER), ('device_id', OTHER),
    ('key_id', 'invalid'), ('disconnected', True), ('central_suspended', True), ('enrollment_hold', True), ('rotation_id', OTHER)])
def test_import_and_current_snapshot_recheck_local_identity_without_a_key_read(registered, monkeypatch, field, value):
    install(registered, 'support')
    monitor = registered.read('state.json')
    monitor[field] = value
    registered.write('state.json', monitor)
    real_read = registered.read

    def metadata_only(name, default=None):
        assert name != 'credential'
        return real_read(name, default)

    monkeypatch.setattr(registered, 'read', metadata_only)
    failure(lambda: local.profile_snapshot(registered, profile='support', now_ms=NOW))
    raw, expected = material(registered, 'restart')
    failure(lambda: local.prepare_import(registered, raw, expected=expected, now_ms=NOW))
    assert local.monitor_endpoint(registered)['origin'] == 'https://ingest.invalid'


@pytest.mark.parametrize('profile', ['support', 'restart'])
def test_profile_init_permission_is_one_store_one_callback_and_no_lock_is_held(registered, profile):
    if profile == 'restart':
        install(registered, 'support')
    install(registered, profile, ready=False)
    permission = registered._provision_profile_permits[profile]
    with pytest.raises(TypeError):
        pickle.dumps(permission)
    assert not local.initialization_allowed(registered, profile)
    with Store(registered.root) as other:
        other._provision_profile_permits = {profile: permission}
        failure(lambda: local.initialize_profile(other, profile=profile, now_ms=NOW,
                                                 initializer=lambda: pytest.fail('foreign Store cannot initialize')), 'CONFLICT')

    def callback():
        with Store(registered.root) as other, other.support_state_lock(), other.restart_state_lock(), other.lock():
            assert local.initialization_allowed(registered, profile)
            assert not local.initialization_allowed(other, profile)
        initialize_records(registered, profile)

    local.initialize_profile(registered, profile=profile, now_ms=NOW, initializer=callback)
    assert not local.initialization_allowed(registered, profile)
    failure(lambda: local.initialize_profile(registered, profile=profile, now_ms=NOW,
                                             initializer=lambda: pytest.fail('consumed permit cannot initialize')), 'CONFLICT')


@pytest.mark.parametrize('profile', ['support', 'restart'])
@pytest.mark.parametrize('mode', ['reopen', 'reapproval', 'no-record', 'partial-cancel'])
def test_missing_or_partial_initialization_never_regenerates_permission(registered, profile, mode):
    if profile == 'restart':
        install(registered, 'support')
    raw, expected = install(registered, profile, ready=False)
    receipt = copy.deepcopy(record(registered)['profiles'][profile]['receipt'])
    if mode == 'reapproval':
        plan = local.prepare_import(registered, raw, expected=expected, now_ms=NOW + 1)
        assert local.commit_import(registered, plan, confirm_human=True, reason='again', now_ms=NOW + 1) == receipt
        failure(lambda: initialize(registered, profile), 'CONFLICT')
    elif mode in ('no-record', 'partial-cancel'):
        error = KeyboardInterrupt(CANARY)

        def callback():
            if mode == 'partial-cancel':
                registered._write_file(local._CONSUMERS[profile][0], {'root_fingerprint': registered.root_fingerprint()})
                raise error

        if mode == 'partial-cancel':
            with pytest.raises(KeyboardInterrupt) as caught:
                local.initialize_profile(registered, profile=profile, now_ms=NOW, initializer=callback)
            assert caught.value is error
        else:
            failure(lambda: local.initialize_profile(registered, profile=profile, now_ms=NOW, initializer=callback), 'CONFLICT')
        failure(lambda: local.profile_snapshot(registered, profile=profile, now_ms=NOW), 'UNCERTAIN')
    with Store(registered.root) as other:
        assert not local.initialization_allowed(other, profile)
        failure(lambda: initialize(other, profile), 'CONFLICT')
        if mode != 'partial-cancel' or profile == 'restart':
            failure(lambda: local.profile_snapshot(other, profile=profile, now_ms=NOW), 'CONFLICT')
        else:
            with pytest.raises(support_state.SupportStateError):
                support_state.client_status(other, now_ms=NOW)
        assert record(other)['profiles'][profile]['receipt'] == receipt
        assert local.monitor_endpoint(other)['origin'] == 'https://ingest.invalid'
        assert local.begin_registration(other, now_ms=NOW)['registration_started']


def test_existing_consumer_history_and_physical_bootstrap_are_not_adopted(registered):
    raw, expected = material(registered, 'support')
    registered._write_file('support-state.json', {'root_fingerprint': registered.root_fingerprint()})
    failure(lambda: local.prepare_import(registered, raw, expected=expected, now_ms=NOW), 'CONFLICT')
    os.unlink('support-state.json', dir_fd=registered.fd)  # Remove only this test's deliberately foreign record.
    registered._write_file('support-bootstrap.json', {'synthetic': True})
    failure(lambda: local.prepare_import(registered, raw, expected=expected, now_ms=NOW), 'CONFLICT')


@pytest.mark.parametrize('profile', ['monitor', 'support', 'restart'])
def test_provision_ack_for_every_profile_uses_monitor_target_and_own_immutable_slot(registered, profile):
    install(registered, 'support')
    install(registered, 'restart')
    before = record(registered)
    wire = ProfileWire(registered, profile)
    result = send(registered, profile, wire)
    assert result['profile'] == profile and result['acknowledged']
    assert len(wire.sent) == 1
    after = record(registered)
    for name in ('monitor', 'support', 'restart'):
        if name != profile:
            assert after['profiles'][name] == before['profiles'][name]
    slot = after['profiles'][profile]
    assert slot['ack_request'] == before['profiles'][profile]['ack_request']
    assert slot['receipt'] == before['profiles'][profile]['receipt'] and len(slot['ack_attempts']) == 1
    assert send(registered, profile, wire)['acknowledged'] and len(wire.sent) == 1


@pytest.mark.parametrize('profile', ['support', 'restart'])
def test_profile_unknown_reopens_with_same_four_attempts_then_status_after_import_expiry(registered, profile):
    install(registered, 'support')
    if profile == 'restart':
        install(registered, 'restart')
    slot = record(registered)['profiles'][profile]
    request, receipt = copy.deepcopy(slot['ack_request']), copy.deepcopy(slot['receipt'])

    def refused(_path, _raw):
        raise TransportError(429, CANARY)

    for _ in range(4):
        with Store(registered.root) as other:
            wire = ProfileWire(other, profile, refused)
            assert send(other, profile, wire)['state'] == 'unknown'
            assert wire.sent == [(local.ACK, request['raw'].encode('ascii'))]
    failure(lambda: send(registered, profile, ProfileWire(registered, profile)), 'ATTEMPTS_EXHAUSTED')
    saved = record(registered)['profiles'][profile]
    assert saved['ack_request'] == request and saved['receipt'] == receipt
    full = ack_response(registered, request['raw'].encode())['receipt']
    later = json.loads(saved['raw'])['import_expires_at'] + 1
    wire = ProfileWire(registered, profile, lambda *_: status_response(registered, profile, full))
    assert send(registered, profile, wire, action='status', now=later)['acknowledged']
    assert wire.sent == [(local.STATUS, saved['status_request']['raw'].encode('ascii'))]
    assert record(registered)['profiles'][profile]['ack'] == full


def test_foreign_profile_ack_and_changed_primary_receipt_are_not_adopted(registered):
    install(registered, 'support')
    monitor = record(registered)['profiles']['monitor']
    wrong = ack_response(registered, monitor['ack_request']['raw'].encode('ascii'))
    wire = ProfileWire(registered, 'support', lambda *_: wrong)
    failure(lambda: send(registered, 'support', wire), 'CONFLICT')
    slot = record(registered)['profiles']['support']
    assert slot['ack'] is None and len(slot['ack_attempts']) == 1 and slot['last_result'] == 'unknown'
    assert record(registered)['profiles']['monitor'] == monitor
    assert send(registered, 'support', ProfileWire(registered, 'support'))['acknowledged']
    original = copy.deepcopy(record(registered)['profiles']['support']['ack'])
    changed = {**original, 'accepted_at': original['accepted_at'] + 1}
    wire = ProfileWire(registered, 'support', lambda *_: status_response(registered, 'support', changed))
    failure(lambda: send(registered, 'support', wire, action='status'), 'CONFLICT')
    assert record(registered)['profiles']['support']['ack'] == original


def test_profile_ack_rechecks_identity_after_native_read_admission(registered, monkeypatch):
    install(registered, 'support')
    before = record(registered)
    original = local._current_identity

    def changed(store):
        state = store.read('state.json')
        state['device_id'] = OTHER
        store.write('state.json', state)
        return original(store)

    monkeypatch.setattr(local, '_current_identity', changed)
    wire = ProfileWire(registered, 'support')
    failure(lambda: send(registered, 'support', wire), 'CONFLICT')
    assert wire.sent == [] and record(registered) == before


def test_late_profile_ack_preserves_evidence_without_reenabling_support(registered):
    install(registered, 'support')

    def stopped(_path, raw):
        support_state.disable(registered, now_ms=NOW)
        return ack_response(registered, raw)

    wire = ProfileWire(registered, 'support', stopped)
    assert send(registered, 'support', wire)['acknowledged']
    assert registered.read('support-state.json')['enabled'] is False
    assert record(registered)['profiles']['support']['ack'] is not None
    assert registered.read('state.json')['enabled'] is True


@pytest.mark.parametrize('profile', ['support', 'restart'])
def test_expiry_or_revocation_of_one_profile_does_not_stop_monitor_or_restore_other_permissions(registered, profile):
    install(registered, 'support')
    install(registered, 'restart')
    expires = json.loads(record(registered)['profiles'][profile]['raw'])['expires_at']
    failure(lambda: local.profile_snapshot(registered, profile=profile, now_ms=expires), 'EXPIRED')
    wire = ProfileWire(registered, profile, lambda *_: status_response(registered, profile, state='revoked'))
    send(registered, profile, wire, action='status')
    failure(lambda: local.profile_snapshot(registered, profile=profile, now_ms=NOW), 'CONFLICT')
    assert local.monitor_endpoint(registered)['origin'] == 'https://ingest.invalid'
    assert registered.read('state.json')['enabled'] is True
    assert not local.initialization_allowed(registered, profile)
    assert exchange(registered, Wire(registered))['acknowledged']


def test_profile_save_failure_consumes_no_new_permission_and_keeps_original_monitor(registered, monkeypatch):
    before = copy.deepcopy(record(registered)['profiles']['monitor'])
    raw, expected = material(registered, 'support')
    plan = local.prepare_import(registered, raw, expected=expected, now_ms=NOW)
    original = registered._write_file

    def uncertain(name, value):
        original(name, value)
        if name == local.NAME:
            raise OSError('synthetic fsync failure after replacement')

    monkeypatch.setattr(registered, '_write_file', uncertain)
    failure(lambda: local.commit_import(registered, plan, confirm_human=True, reason=REASON, now_ms=NOW), 'UNCERTAIN')
    assert not getattr(registered, '_provision_profile_permits', {})
    with Store(registered.root) as other:
        assert record(other)['profiles']['monitor'] == before
        failure(lambda: initialize(other, 'support'), 'CONFLICT')
        failure(lambda: local.profile_snapshot(other, profile='support', now_ms=NOW), 'CONFLICT')


def test_legacy_initialization_contract_is_unchanged_and_managed_missing_does_not_fallback(tmp_path, registered):
    with Store(tmp_path / 'legacy', create=True) as legacy:
        assert local.initialization_allowed(legacy, 'support') is True
        assert local.initialization_allowed(legacy, 'restart') is True
        assert local.support_bootstrap(legacy) is None
    failure(lambda: local.support_bootstrap(registered), 'MISSING')
    assert local.initialization_allowed(registered, 'support') is False


@pytest.mark.parametrize('entry', ['status', 'snapshot', 'bootstrap', 'init-permission'])
def test_metadata_read_timeout_is_the_original_instance_without_write_poison(registered, monkeypatch, entry):
    install(registered, 'support')
    error = TimeoutError('synthetic read deadline')
    original = registered._read_file

    def timed_out(name, default=None):
        if name == local.NAME:
            raise error
        return original(name, default)

    actions = {'status': lambda: local.status(registered, now_ms=NOW),
               'snapshot': lambda: local.profile_snapshot(registered, profile='support', now_ms=NOW),
               'bootstrap': lambda: local.support_bootstrap(registered),
               'init-permission': lambda: local.initialization_allowed(registered, 'support')}
    monkeypatch.setattr(registered, '_read_file', timed_out)
    with pytest.raises(TimeoutError) as caught:
        actions[entry]()
    assert caught.value is error and not getattr(registered, '_provision_uncertain', False)


@pytest.mark.parametrize('stage', ['anchor-open', 'anchor-fsync', 'write', 'readback'])
def test_root_anchor_timeout_keeps_original_instance_and_uncertain_guard(tmp_path, monkeypatch, stage):
    error = TimeoutError('synthetic anchor deadline')
    with Store(tmp_path / 'new-root', create_new=True) as store:
        original_open, original_read = store._open, store._read_file
        original_sync = os.fsync

        def opening(name, flags=os.O_RDONLY):
            if stage == 'anchor-open' and name == local.NAME:
                raise error
            return original_open(name, flags)

        def syncing(fd):
            if stage == 'anchor-fsync' and fd == store.fd:
                raise error
            return original_sync(fd)

        def writing(_name, _value):
            raise error

        def reading(name, default=None):
            if stage == 'readback' and name == local.NAME:
                raise error
            return original_read(name, default)

        monkeypatch.setattr(store, '_open', opening)
        monkeypatch.setattr(local.os, 'fsync', syncing)
        monkeypatch.setattr(store, '_read_file', reading)
        if stage == 'write':
            monkeypatch.setattr(store, '_write_file', writing)
        with pytest.raises(TimeoutError) as caught:
            local.prepare_root(store, confirm_human=True, reason=REASON, now_ms=NOW)
        assert caught.value is error and store._provision_uncertain
        failure(lambda: local.status(store, now_ms=NOW), 'UNCERTAIN')
        assert (store.root / local.NAME).exists() is (stage != 'anchor-open')


@pytest.mark.parametrize('stage', ['write', 'readback'])
def test_profile_import_write_timeout_is_original_and_never_issues_initialization_permission(registered, monkeypatch, stage):
    raw, expected = material(registered, 'support')
    plan = local.prepare_import(registered, raw, expected=expected, now_ms=NOW)
    error = TimeoutError('synthetic profile write deadline')
    original_write, original_read = registered._write_file, registered._read_file
    written = []

    def writing(name, value):
        if name == local.NAME and stage == 'write':
            raise error
        original_write(name, value)
        if name == local.NAME:
            written.append(True)

    def reading(name, default=None):
        if name == local.NAME and written and stage == 'readback':
            raise error
        return original_read(name, default)

    monkeypatch.setattr(registered, '_write_file', writing)
    monkeypatch.setattr(registered, '_read_file', reading)
    with pytest.raises(TimeoutError) as caught:
        local.commit_import(registered, plan, confirm_human=True, reason=REASON, now_ms=NOW)
    assert caught.value is error and registered._provision_uncertain
    assert not getattr(registered, '_provision_profile_permits', {})
    failure(lambda: initialize(registered, 'support'), 'UNCERTAIN')


def test_initializer_timeout_preserves_original_and_only_holds_its_profile(registered):
    install(registered, 'support', ready=False)
    error = TimeoutError('synthetic initializer deadline')

    def timed_out():
        initialize_records(registered, 'support')
        raise error

    with pytest.raises(TimeoutError) as caught:
        local.initialize_profile(registered, profile='support', now_ms=NOW, initializer=timed_out)
    assert caught.value is error
    assert not local.initialization_allowed(registered, 'support')
    failure(lambda: local.profile_snapshot(registered, profile='support', now_ms=NOW), 'UNCERTAIN')
    assert local.monitor_endpoint(registered)['origin'] == 'https://ingest.invalid'
    assert local.begin_registration(registered, now_ms=NOW)['registration_started']
    assert registered.read('support-state.json')['enabled'] is False


def test_ack_key_read_timeout_is_original_without_reservation_or_dispatch(registered, monkeypatch):
    install(registered, 'support')
    before = record(registered)
    error = TimeoutError('synthetic current credential deadline')

    def timed_out(_store):
        raise error

    monkeypatch.setattr(local, '_current_identity', timed_out)
    wire = ProfileWire(registered, 'support')
    with pytest.raises(TimeoutError) as caught:
        send(registered, 'support', wire)
    assert caught.value is error and wire.sent == [] and record(registered) == before


@pytest.mark.parametrize('observed_status', [None, 401, 429, 403])
def test_ack_dispatch_timeout_keeps_first_instance_and_observed_denial_priority(registered, observed_status):
    install(registered, 'support')
    original = copy.deepcopy(record(registered)['profiles']['support']['ack_request'])
    error = TimeoutError('synthetic request deadline')

    def timed_out(_path, _raw):
        wire.refusal_status = observed_status
        raise error

    wire = ProfileWire(registered, 'support', timed_out)
    with pytest.raises(TimeoutError) as caught:
        send(registered, 'support', wire)
    assert caught.value is error and len(wire.sent) == 1
    slot = record(registered)['profiles']['support']
    assert slot['ack_request'] == original and len(slot['ack_attempts']) == 1
    assert slot['ack_denied'] is (observed_status == 403)
    assert slot['last_result'] == ('denied' if observed_status == 403 else 'unknown')
    if observed_status == 403:
        failure(lambda: send(registered, 'support', wire), 'CONFLICT')
        assert len(wire.sent) == 1


@pytest.mark.parametrize('after_write', [False, True])
def test_second_timeout_while_saving_refusal_cannot_replace_first_timeout(registered, monkeypatch, after_write):
    install(registered, 'support')
    first, second = TimeoutError('synthetic first deadline'), TimeoutError('synthetic second deadline')
    original = registered._write_file

    def writing(name, value):
        denied = name == local.NAME and value['profiles']['support']['ack_denied']
        if denied and not after_write:
            raise second
        original(name, value)
        if denied:
            raise second

    def timed_out(_path, _raw):
        wire.refusal_status = 403
        raise first

    monkeypatch.setattr(registered, '_write_file', writing)
    wire = ProfileWire(registered, 'support', timed_out)
    with pytest.raises(TimeoutError) as caught:
        send(registered, 'support', wire)
    assert caught.value is first and registered._provision_uncertain and len(wire.sent) == 1
    failure(lambda: send(registered, 'support', wire), 'UNCERTAIN')
