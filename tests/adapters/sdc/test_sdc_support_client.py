"""Fully synthetic foreground consent, durable request and stop-race oracles."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.sdc import support_client as client
from adapters.sdc import support_state as local
from adapters.sdc import provision
from adapters.sdc.storage import Store
from adapters.sdc.support_contract import hash_consent_binding
from adapters.sdc.transport import TransportError
from test_sdc_support_runner import world as runner_world  # noqa: F401
from test_sdc_support_runner import run as run_diagnostic, POLL, ADMIT, GRANT, REPORT, STATUS

NOW = 1792663200000
CASE = '33333333-3333-4333-8333-333333333333'
IDS = {'claim_id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'device_id': '22222222-2222-4222-8222-222222222222',
       'key_id': 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'}
MONITOR = {'origin': 'https://monitor.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
SUPPORT = {**MONITOR, 'origin': 'https://support.invalid'}
CANARY = 'SYNTHETIC_PRIVATE_DO_NOT_ECHO'


def payload(request, now=NOW, **changes):
    value = {'schema_version': 'sdc.support.consent-preview.v1', 'preview_id': request['preview_id'],
             'product': 'hermes-mcs', 'audience': MONITOR['audience'], 'tenant': 'tenant-a', 'installation': 'installation-a',
             **IDS, 'environment': MONITOR['environment'], 'case_id': request['case_id'], 'purpose_code': 'HEALTH_CHECK',
             'capability_revision': 'sdc-support-capabilities-v1', 'diagnostic_ids': ['runtime.status.v1'],
             'operator_refs': ['a' * 64], 'approved_by': 'b' * 64, 'consent_revision': 'sdc-support-consent-v1',
             'realm_epoch': 'opaque-1', 'policy_epoch': 1, 'local_consent_epoch': 1, 'case_epoch': 1, 'device_epoch': 1,
             'issued_at': now, 'not_before': now, 'expires_at': now + 300000, 'consent_expires_at': now + 1800000, **changes}
    value['consent_binding_hash'] = hash_consent_binding(value)
    return value


def managed_baseline(store, *, expected, now):
    """Install synthetic independently confirmed Bundles, without an ACK or real credentials."""
    vectors = json.loads((Path(__file__).resolve().parents[3] /
        'adapters/sdc/contracts/provision/provision-vectors.json').read_text())
    provision.prepare_root(store, confirm_human=True, reason='synthetic-profiles', now_ms=now)
    root_id = store.read(provision.NAME)['root_id']
    bundles = {}
    for profile in ('monitor', 'support', 'restart'):
        bundle = copy.deepcopy(next(row['value'] for row in vectors['positive']
            if row['definition'] == 'Bundle' and row['value']['profile_id'] == profile))
        bundle.update(root_id=root_id, issued_at=now, import_expires_at=now + 900000,
                      expires_at=now + 1800000, origin=f'https://{profile}.invalid', audience=f'sdc:{profile}-fixture')
        bundle.update({k: expected[k] for k in ('tenant', 'installation', 'realm_epoch', 'environment')})
        if profile != 'monitor':
            bundle.update({k: expected[k] for k in ('device_id', 'claim_id', 'device_epoch')})
        for pin in bundle['pins']:
            pin.update(not_before=now - 60000, expires_at=now + 3600000)
        bundles[profile] = bundle

    def install(profile):
        bundle = bundles[profile]
        raw = client.canonical(bundle, max_bytes=16384).encode('ascii')
        independent = {k: bundle[k] for k in provision.EXPECTED_FIELDS if k != 'bundle_hash'}
        independent['bundle_hash'] = client._hash(bundle)
        plan = provision.prepare_import(store, raw, expected=independent, now_ms=now)
        provision.commit_import(store, plan, confirm_human=True, reason='synthetic-profiles', now_ms=now)

    install('monitor')
    provision.begin_registration(store, now_ms=now)
    with store.lock():
        provision.guard_registration(store, {}, {})
        store.write('credential', {'key_id': expected['key_id'], 'key': '1' * 64})
        store.write('state.json', {**{k: expected[k] for k in ('claim_id', 'device_id', 'key_id')},
                                  'state': 'active', 'enabled': True, 'sequence': 0})
    return bundles, install


@pytest.fixture
def world(tmp_path, request):
    managed = getattr(request, 'param', False)
    with Store(tmp_path / 'support-private', create=not managed, create_new=managed) as store:
        if managed:
            scope = {**IDS, 'tenant': 'tenant-a', 'installation': 'installation-a', 'realm_epoch': 'opaque-1',
                     'device_epoch': 1, 'environment': MONITOR['environment']}
            bundles, install = managed_baseline(store, expected=scope, now=NOW)
            install('support')
            scope['audience'] = bundles['support']['audience']
        else:
            store.write('endpoint.json', MONITOR)
            store.write('state.json', {**IDS, 'state': 'active', 'enabled': True})
            store.write('credential', {'key_id': IDS['key_id'], 'key': '1' * 64})
            binding = store.enrollment_binding()
            store.write('support-bootstrap.json', {'schema_version': client.BOOTSTRAP, 'root_fingerprint': binding['root_fingerprint'],
                        'monitor_endpoint_hash': binding['endpoint_hash'], 'support_endpoint': SUPPORT, 'pins': [{'synthetic': True}]})
            scope = {}
        world = SimpleNamespace(store=store, now=NOW, calls=[], actions={}, prompts=[], verifier_calls=[], check_calls=[])
        def check(pins, **kwargs):
            world.check_calls.append((copy.deepcopy(pins), kwargs))
        def verifier(raw, **kwargs):
            world.verifier_calls.append(kwargs)
            return json.loads(raw)['payload']
        def send(path, raw, headers):
            # No monitoring/state lock crosses HTTP; full foreground lock does.
            with Store(store.root) as other:
                with other.support_state_lock(), other.lock():
                    pass
                with pytest.raises(BlockingIOError):
                    with other.support_session_lock():
                        pass
            world.calls.append((path, raw, headers))
            request = json.loads(raw)
            action = world.actions.get(path)
            if action:
                return action(request)
            if path == client.PREVIEW:
                return {'schema_version': 'sdc.support.v1', 'preview_id': request['preview_id'],
                        'preview': {'payload': payload(request, **scope)}, 'request_id': CASE}
            value = store.read(local.NAME)['receipt']['preview']
            return {**request, 'case_id': value['case_id'], 'state': 'accepted', 'expires_at': value['consent_expires_at'], 'request_id': CASE}
        world.check = check
        world.verifier = verifier
        world.transport = lambda _: SimpleNamespace(send=send)
        def confirm(value, origin):
            world.prompts.append((value, origin))
            return True
        world.confirm = confirm
        world.supplier = client._supplier(store, check, lambda: world.now / 1000, {})
        # Explicit synthetic offline provisioning, never performed by the client/CLI.
        def initialize():
            return client.initialize_local_state(store, check_pins=check, clock=lambda: world.now / 1000)
        if managed:
            provision.initialize_profile(store, profile='support', now_ms=NOW, initializer=initialize)
            world.bundles, world.install, world.scope = bundles, install, scope
        else:
            initialize()
        yield world


def connect(w, **kwargs):
    return client.connect(w.store, CASE, verifier=w.verifier, check_pins=w.check, transport_factory=w.transport,
                          confirm=kwargs.pop('confirm', w.confirm), clock=lambda: w.now / 1000, **kwargs)


@pytest.mark.parametrize('world', [True], indirect=True)
def test_managed_support_uses_own_audience_and_bundle_scope_without_monitor_ack(world):
    assert connect(world)['state'] == 'accepted'
    receipt = world.store.read(local.NAME)['receipt']
    snapshot = provision.profile_snapshot(world.store, profile='support', now_ms=world.now)
    assert receipt['binding']['profile']['bundle_hash'] == snapshot['bundle_hash']
    assert receipt['binding']['profile']['endpoint']['audience'] == world.bundles['support']['audience']
    assert receipt['preview']['audience'] != world.bundles['monitor']['audience']
    assert all(receipt['preview'][k] == world.bundles['support'][k] for k in client._PROFILE_SCOPE)
    assert world.prompts[0][1]['origin'] == 'https://support.invalid'
    assert world.store.read(provision.NAME)['profiles']['monitor']['ack'] is None
    assert not (world.store.root / 'support-bootstrap.json').exists()


@pytest.mark.parametrize('world', [True], indirect=True)
@pytest.mark.parametrize('field,value', [('tenant', 'other'), ('installation', 'other'),
    ('realm_epoch', 'other'), ('device_epoch', 2)])
def test_managed_preview_cannot_supply_its_own_expected_scope(world, field, value):
    def wrong(raw, **_):
        result = json.loads(raw)['payload']
        result[field] = value
        result['consent_binding_hash'] = hash_consent_binding(result)
        return result
    world.verifier = wrong
    with pytest.raises(ValueError):
        connect(world)
    assert len(world.calls) == 1 and not world.prompts
    assert world.store.read(local.NAME)['receipt'] is None


@pytest.mark.parametrize('world', [True], indirect=True)
@pytest.mark.parametrize('field', ['expires_at', 'issued_at', 'policy_revision'])
def test_profile_change_with_same_origin_and_pins_cannot_reuse_old_case(world, monkeypatch, field):
    assert connect(world)['state'] == 'accepted'
    before = world.store.read(local.NAME)
    current = provision.profile_snapshot(world.store, profile='support', now_ms=world.now)
    bootstrap = provision.support_bootstrap(world.store)
    changed = copy.deepcopy(current)
    changed['bundle'][field] = (CASE if field == 'policy_revision' else
                                changed['bundle'][field] + (1000 if field == 'expires_at' else -1))
    changed['bundle_hash'] = client._hash(changed['bundle'])
    monkeypatch.setattr(provision, 'profile_snapshot', lambda *_args, **_kwargs: copy.deepcopy(changed))
    monkeypatch.setattr(provision, 'support_bootstrap', lambda _store: {**bootstrap, 'authority': copy.deepcopy(changed)})
    with pytest.raises(ValueError):
        connect(world)
    assert len(world.calls) == 2 and world.store.read(local.NAME) == before


@pytest.mark.parametrize('world', [True], indirect=True)
def test_managed_bundle_expiry_blocks_send_and_local_status_needs_no_credentials(world, monkeypatch):
    assert connect(world)['state'] == 'accepted'
    world.now = world.bundles['support']['expires_at']
    original = world.store.read
    def read(name, default=None):
        if name == 'credential':
            pytest.fail('expired profile must stop before credentials')
        return original(name, default)
    monkeypatch.setattr(world.store, 'read', read)
    with pytest.raises(ValueError):
        connect(world)
    assert not client.status(world.store, now_ms=world.now)['enabled'] and len(world.calls) == 2


@pytest.mark.parametrize('world', [True], indirect=True)
def test_managed_profile_expiry_during_accept_keeps_ack_inactive(world):
    world.now += 1000
    world.actions[client.PREVIEW] = lambda request: {
        'schema_version': 'sdc.support.v1', 'preview_id': request['preview_id'],
        'preview': {'payload': payload(request, now=world.now, **world.scope)}, 'request_id': CASE}
    def delayed_ack(request):
        preview = world.store.read(local.NAME)['receipt']['preview']
        world.now = world.bundles['support']['expires_at']
        assert world.now < preview['consent_expires_at']
        return {**request, 'case_id': preview['case_id'], 'state': 'accepted',
                'expires_at': preview['consent_expires_at'], 'request_id': CASE}
    world.actions[client.ACCEPT] = delayed_ack
    assert connect(world)['state'] == 'ack_recorded_inactive'
    state = world.store.read(local.NAME)
    assert state['client']['accept']['ack'] is not None
    assert not client.status(world.store, now_ms=world.now)['enabled']


def test_managed_initialization_requires_same_store_one_use_window_and_missing_state_holds(tmp_path):
    with Store(tmp_path / 'managed-init', create_new=True) as store:
        expected = {**IDS, 'tenant': 'tenant-a', 'installation': 'installation-a', 'realm_epoch': 'opaque-1',
                    'device_epoch': 1, 'environment': 'synthetic'}
        _, install = managed_baseline(store, expected=expected, now=NOW)
        install('support')
        def initialize(target=store):
            return client.initialize_local_state(target, check_pins=lambda *_args, **_kwargs: None,
                                                 clock=lambda: NOW / 1000)
        with pytest.raises(ValueError):
            initialize()
        assert store.read(local.NAME) is None
        provision.initialize_profile(store, profile='support', now_ms=NOW, initializer=initialize)
        before = store.read(local.NAME)
        with pytest.raises(ValueError):
            initialize()
        assert store.read(local.NAME) == before
        (store.root / local.NAME).unlink()
        with pytest.raises(ValueError):
            initialize()
        with Store(store.root) as reopened:
            with pytest.raises(ValueError):
                initialize(reopened)
            with pytest.raises(ValueError):
                provision.initialize_profile(reopened, profile='support', now_ms=NOW, initializer=lambda: initialize(reopened))
        assert store.read(local.NAME) is None


@pytest.mark.parametrize('stage', ['pins', 'credential', 'state-read', 'write-before', 'write-after'])
def test_actual_managed_support_initializer_preserves_timeout_and_consumes_permission(tmp_path, monkeypatch, stage):
    with Store(tmp_path / 'managed-timeout', create_new=True) as store:
        expected = {**IDS, 'tenant': 'tenant-a', 'installation': 'installation-a', 'realm_epoch': 'opaque-1',
                    'device_epoch': 1, 'environment': 'synthetic'}
        _, install = managed_baseline(store, expected=expected, now=NOW)
        install('support')
        error = TimeoutError(CANARY)
        reached = []
        read, write = store.read, store.write
        def timeout():
            reached.append(stage)
            raise error
        def check(*_args, **_kwargs):
            if stage == 'pins':
                timeout()
        def reading(name, default=None):
            if name == ('credential' if stage == 'credential' else local.NAME) and stage in ('credential', 'state-read'):
                timeout()
            return read(name, default)
        def writing(name, value):
            if name == local.NAME and stage == 'write-before':
                timeout()
            write(name, value)
            if name == local.NAME and stage == 'write-after':
                timeout()
        with monkeypatch.context() as patch:
            patch.setattr(store, 'read', reading)
            patch.setattr(store, 'write', writing)
            with pytest.raises(TimeoutError) as caught:
                provision.initialize_profile(store, profile='support', now_ms=NOW, initializer=lambda:
                    client.initialize_local_state(store, check_pins=check, clock=lambda: NOW / 1000))
        assert caught.value is error and reached == [stage]
        assert not provision.initialization_allowed(store, 'support')
        assert 'support' in store._provision_profile_failed
        assert (read(local.NAME) is not None) is (stage == 'write-after')
        if stage == 'write-after':
            assert read(local.NAME)['enabled'] is False
        with pytest.raises(ValueError):
            provision.profile_snapshot(store, profile='support', now_ms=NOW)
        with Store(store.root) as reopened, pytest.raises(ValueError):
            provision.initialize_profile(reopened, profile='support', now_ms=NOW,
                initializer=lambda: pytest.fail('consumed initialization cannot run again'))


@pytest.mark.parametrize('failure', [OSError, ValueError])
def test_support_supplier_and_state_read_keep_non_timeout_error_details_private(world, monkeypatch, failure):
    def unavailable(*_args, **_kwargs):
        raise failure(CANARY)
    with pytest.raises(local.SupportStateError) as caught:
        local.prepare(world.store, binding_supplier=unavailable, now_ms=NOW)
    assert CANARY not in str(caught.value)
    monkeypatch.setattr(world.store, 'read', unavailable)
    with pytest.raises(local.SupportStateError) as caught:
        client.status(world.store, now_ms=NOW)
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize('stage', ['provider', 'guard'])
def test_diagnostic_timeout_saves_unknown_without_more_network_and_preserves_instance(runner_world, stage):  # noqa: F811
    w, error = runner_world, TimeoutError(CANARY)
    original, completed = w.provider, []
    def provider(*args, **kwargs):
        value = original(*args, **kwargs)
        completed.append(True)
        if stage == 'provider':
            raise error
        return value
    def budget():
        if completed:
            raise error
    with pytest.raises(TimeoutError) as caught:
        run_diagnostic(w, provider=provider, budget=budget)
    assert caught.value is error and completed == [True]
    assert [path for path, _, _ in w.calls] == [POLL, ADMIT, GRANT]
    active = w.store.read(local.NAME)['runner']['active']
    assert active['phase'] == 'RESULT'
    body = json.loads(active['report']['raw'])
    assert body['state'] == 'unknown' and body['result'] is None and CANARY not in active['report']['raw']
    w.status_report = 'null'
    assert run_diagnostic(w)['state'] == 'result_unknown'
    assert w.calls[-1][0] == STATUS and len(w.providers) == 1


@pytest.mark.parametrize('after_write', [False, True])
@pytest.mark.parametrize('secondary', [OSError, TimeoutError, KeyboardInterrupt])
def test_diagnostic_unknown_save_failure_preserves_first_timeout(runner_world, monkeypatch, after_write, secondary):  # noqa: F811
    w, first = runner_world, TimeoutError(CANARY)
    original_provider, write = w.provider, w.store.write
    reached = []
    def provider(*args, **kwargs):
        original_provider(*args, **kwargs)
        raise first
    def writing(name, value):
        active = value.get('runner', {}).get('active') if name == local.NAME else None
        if active is not None and active['phase'] == 'RESULT':
            reached.append(True)
            if after_write:
                write(name, value)
            raise secondary('synthetic secondary save failure')
        write(name, value)
    monkeypatch.setattr(w.store, 'write', writing)
    with pytest.raises(TimeoutError) as caught:
        run_diagnostic(w, provider=provider)
    assert caught.value is first and reached == [True]
    assert [path for path, _, _ in w.calls] == [POLL, ADMIT, GRANT] and len(w.providers) == 1
    active = w.store.read(local.NAME)['runner']['active']
    assert active['phase'] == ('RESULT' if after_write else 'STARTED')
    if after_write:
        assert json.loads(active['report']['raw'])['state'] == 'unknown'


def test_sent_diagnostic_report_timeout_preserves_inflight_bytes_without_same_call_status(runner_world):  # noqa: F811
    w, error = runner_world, TimeoutError(CANARY)
    def timed_out(_request):
        raise error
    w.before[REPORT] = timed_out
    with pytest.raises(TimeoutError) as caught:
        run_diagnostic(w)
    assert caught.value is error
    assert [path for path, _, _ in w.calls] == [POLL, ADMIT, GRANT, REPORT]
    active = w.store.read(local.NAME)['runner']['active']
    assert active['phase'] == 'REPORTING' and active['report']['raw'].encode('ascii') == w.calls[-1][1]
    assert run_diagnostic(w)['state'] == 'report_reconciled'
    assert w.calls[-1][0] == STATUS and len(w.providers) == 1


def test_complete_consent_persists_before_each_send_and_reuses_ack(world):
    w = world
    assert connect(w)['state'] == 'accepted'
    state = w.store.read(local.NAME)
    assert state['receipt']['accepted'] is True
    accept = state['client']['accept']
    assert json.loads(accept['raw'])['local_receipt_id'] == state['receipt']['local_receipt_id']
    assert accept['ack'] is not None and accept['attempts'] == 1
    assert [p for p, _, _ in w.calls] == [client.PREVIEW, client.ACCEPT]
    assert w.verifier_calls[0]['purpose'] == 'consent-preview'
    assert w.prompts[0][0]['tenant'] == 'tenant-a'
    original = copy.deepcopy(state)
    assert connect(w)['state'] == 'accepted'
    assert len(w.calls) == 2 and w.store.read(local.NAME) == original


@pytest.mark.parametrize('answer', [False, None, 1, '1'])
def test_no_explicit_bool_consent_means_no_accept(world, answer):
    assert connect(world, confirm=lambda *_: answer)['state'] == 'cancelled'
    assert [p for p, _, _ in world.calls] == [client.PREVIEW]
    assert world.store.read(local.NAME)['receipt'] is None


def test_missing_state_or_provision_never_repairs_or_sends(world):
    w = world
    (w.store.root / local.NAME).unlink()
    with pytest.raises(local.SupportStateError, match='MISSING'):
        connect(w)
    assert not w.calls and not (w.store.root / local.NAME).exists()
    local.prepare(w.store, binding_supplier=w.supplier, now_ms=NOW, initialize=True)
    (w.store.root / 'support-bootstrap.json').unlink()
    with pytest.raises(ValueError):
        connect(w)
    assert not w.calls


@pytest.mark.parametrize('change', ['claim_id', 'device_id', 'key_id', 'environment', 'audience', 'case_id', 'preview_id'])
def test_signed_but_wrong_known_identity_never_displays_or_accepts(world, change):
    def wrong(raw, **_):
        value = json.loads(raw)['payload']
        value[change] = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd' if change.endswith('_id') else 'different'
        value['consent_binding_hash'] = hash_consent_binding(value)
        return value
    world.verifier = wrong
    with pytest.raises(ValueError):
        connect(world)
    assert not world.prompts and len(world.calls) == 1


@pytest.mark.parametrize('where', ['credential_next_key', 'credential_next_key_id', 'state_rotation_id', 'state_disconnected', 'state_disabled'])
def test_rotation_or_inactive_never_sends(world, where):
    w = world
    name = 'credential' if where.startswith('credential') else 'state.json'
    value = w.store.read(name)
    if where == 'state_disabled':
        value['enabled'] = False
    else:
        key = where.split('_', 1)[1]
        value[key] = None if key != 'disconnected' else True
    w.store.write(name, value)
    with pytest.raises(ValueError):
        connect(w)
    assert not w.calls


def test_ack_lost_reuses_id_bytes_receipt_and_original_expiry_after_preview_expiry(world):
    w = world
    def lost(_):
        raise TransportError()
    w.actions[client.ACCEPT] = lost
    with pytest.raises(TransportError):
        connect(w)
    original = w.store.read(local.NAME)
    w.now += 400000
    del w.actions[client.ACCEPT]
    assert connect(w)['state'] == 'accepted'
    a = w.store.read(local.NAME)
    assert a['receipt'] == original['receipt']
    assert a['client']['accept']['request_id'] == original['client']['accept']['request_id']
    assert a['client']['accept']['raw'] == original['client']['accept']['raw']
    assert a['client']['accept']['deadline'] == original['client']['accept']['deadline']
    assert w.calls[1][1] == w.calls[2][1] and len(w.prompts) == 1


def test_preview_ack_lost_reuses_same_request_without_invented_refresh(world):
    w = world
    w.actions[client.PREVIEW] = lambda _: (_ for _ in ()).throw(TransportError())
    with pytest.raises(TransportError):
        connect(w)
    first = w.calls[0][1]
    del w.actions[client.PREVIEW]
    assert connect(w)['state'] == 'accepted'
    assert first == w.calls[1][1]


def test_stop_during_human_prompt_prevents_receipt_and_accept(world):
    def stop(*_):
        result = client.stop(world.store, now_ms=world.now)
        assert result == {'state': 'stopped', 'foreground_ended': False}
        return True
    with pytest.raises(local.SupportStateError, match='CONFLICT'):
        connect(world, confirm=stop)
    assert world.store.read(local.NAME)['receipt'] is None and len(world.calls) == 1


def test_stop_after_accept_admission_keeps_ack_without_reenable(world):
    def stop_then_ack(request):
        assert client.stop(world.store, now_ms=world.now)['foreground_ended'] is False
        return {**request, 'case_id': CASE, 'state': 'accepted', 'expires_at': NOW + 1800000, 'request_id': CASE}
    world.actions[client.ACCEPT] = stop_then_ack
    assert connect(world)['state'] == 'ack_recorded_inactive'
    state = world.store.read(local.NAME)
    assert not state['enabled'] and state['tombstones'] and state['client']['accept']['ack']
    before = len(world.calls)
    with pytest.raises(ValueError):
        connect(world)
    assert len(world.calls) == before
    assert client.stop(world.store, now_ms=world.now)['foreground_ended'] is True


@pytest.mark.parametrize('field,value', [('local_consent_epoch', True), ('expires_at', NOW + 1800001), ('case_id', IDS['claim_id']), ('extra', CANARY)])
def test_wrong_ack_stays_unknown(world, field, value):
    def ack(request):
        response = {**request, 'case_id': CASE, 'state': 'accepted', 'expires_at': NOW + 1800000, 'request_id': CASE}
        response[field] = value
        return response
    world.actions[client.ACCEPT] = ack
    with pytest.raises(ValueError):
        connect(world)
    assert client.status(world.store, now_ms=NOW)['accept'] == 'unknown'


@pytest.mark.parametrize('stage', ['preview', 'receipt', 'accept'])
def test_write_failure_never_sends_unpersisted_request(world, monkeypatch, stage):
    original = world.store.write
    def fail(name, value):
        c = value.get('client', {})
        if name == local.NAME and ((stage == 'preview' and c.get('preview'))
                or (stage == 'receipt' and value.get('receipt')) or (stage == 'accept' and c.get('accept'))):
            raise OSError(CANARY)
        return original(name, value)
    monkeypatch.setattr(world.store, 'write', fail)
    with pytest.raises(OSError):
        connect(world)
    assert len(world.calls) == (0 if stage == 'preview' else 1)


def test_ack_save_failure_retries_same_body(world, monkeypatch):
    original = world.store.write
    def fail(name, value):
        if name == local.NAME and value.get('client', {}).get('accept', {}) and value['client']['accept']['ack']:
            raise OSError(CANARY)
        return original(name, value)
    monkeypatch.setattr(world.store, 'write', fail)
    with pytest.raises(OSError):
        connect(world)
    monkeypatch.setattr(world.store, 'write', original)
    assert connect(world)['state'] == 'accepted'
    assert world.calls[1][1] == world.calls[2][1]


def test_prompt_expiry_does_not_save_consent_or_accept(world):
    def elapsed(*_):
        world.now += 300000
        return True
    with pytest.raises(ValueError):
        connect(world, confirm=elapsed)
    assert len(world.calls) == 1 and world.store.read(local.NAME)['receipt'] is None


def test_started_accept_does_not_outlive_original_case_deadline(world):
    world.actions[client.ACCEPT] = lambda _: (_ for _ in ()).throw(TransportError())
    with pytest.raises(TransportError):
        connect(world)
    world.now += 1800000
    with pytest.raises(ValueError):
        connect(world)
    assert len(world.calls) == 2


def test_status_stop_do_not_read_bootstrap_credentials_or_network(world, monkeypatch):
    original = world.store.read
    def only_local(name, *args):
        assert name == local.NAME
        return original(name, *args)
    monkeypatch.setattr(world.store, 'read', only_local)
    assert client.status(world.store, now_ms=NOW)['accept'] == 'not_sent'
    assert client.stop(world.store, now_ms=NOW)['foreground_ended']


def test_busy_session_refuses_second_foreground_without_send(world):
    with Store(world.store.root) as other, other.support_session_lock():
        with pytest.raises(BlockingIOError):
            connect(world)
    assert not world.calls


def test_budget_expiry_after_preview_does_not_accept(world):
    def budget():
        if world.calls:
            raise TimeoutError(CANARY)
    with pytest.raises(TimeoutError):
        connect(world, budget=budget)
    assert len(world.calls) == 1 and world.store.read(local.NAME)['receipt'] is None


def test_bootstrap_copy_or_change_before_human_cas_rejects(world):
    def changed(*_):
        value = world.store.read('support-bootstrap.json')
        value['support_endpoint']['origin'] = 'https://changed.invalid'
        world.store.write('support-bootstrap.json', value)
        return True
    with pytest.raises(ValueError):
        connect(world, confirm=changed)
    assert len(world.calls) == 1


def test_untrusted_pin_check_failure_is_before_http(world):
    world.check = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError(CANARY))
    with pytest.raises(ValueError):
        connect(world)
    assert not world.calls and not world.prompts


@pytest.mark.parametrize('change', ['credential', 'rotation', 'endpoint', 'pins'])
def test_fresh_binding_after_prompt_rejects_changed_owner_identity(world, change):
    def changed(*_):
        if change == 'credential':
            world.store.write('credential', {'key_id': IDS['key_id'], 'key': '2' * 64})
        elif change == 'rotation':
            world.store.write('state.json', {**world.store.read('state.json'), 'rotation_id': CASE})
        elif change == 'endpoint':
            world.store.write('endpoint.json', {**MONITOR, 'origin': 'https://changed.invalid'})
        else:
            value = world.store.read('support-bootstrap.json')
            value['pins'].append({'synthetic_other': True})
            world.store.write('support-bootstrap.json', value)
        return True
    with pytest.raises(ValueError):
        connect(world, confirm=changed)
    assert world.store.read(local.NAME)['receipt'] is None
    assert [p for p, _, _ in world.calls] == [client.PREVIEW]


@pytest.mark.parametrize('lock_name', ['support_state_lock', 'lock'])
def test_busy_short_lock_has_no_network_or_repair(world, lock_name):
    before = world.store.read(local.NAME)
    with Store(world.store.root) as other, getattr(other, lock_name)():
        with pytest.raises(BlockingIOError):
            connect(world)
    assert world.store.read(local.NAME) == before and not world.calls


def test_stop_busy_state_is_failure_and_does_not_claim_success(world):
    before = world.store.read(local.NAME)
    with Store(world.store.root) as other, other.support_state_lock():
        with pytest.raises(BlockingIOError):
            client.stop(world.store, now_ms=NOW)
    assert world.store.read(local.NAME) == before


def test_stop_is_independent_of_monitor_lock(world):
    before = world.store.read('state.json'), world.store.read('credential')
    with Store(world.store.root) as other, other.lock():
        assert client.stop(world.store, now_ms=NOW)['foreground_ended'] is True
    assert (world.store.read('state.json'), world.store.read('credential')) == before


def test_copied_root_rejects_bootstrap_and_state_without_network(world, tmp_path):
    with Store(tmp_path / 'other-private', create=True) as other:
        for name in ('endpoint.json', 'state.json', 'credential', 'support-bootstrap.json', local.NAME):
            other.write(name, world.store.read(name))
        with pytest.raises(ValueError):
            client.connect(other, CASE, verifier=world.verifier, check_pins=world.check,
                           transport_factory=world.transport, confirm=world.confirm, clock=lambda: NOW / 1000)
        with pytest.raises(ValueError):
            client.stop(other, now_ms=NOW)
    assert not world.calls


def test_bad_closed_preview_wrapper_never_displays_raw_fields(world):
    world.actions[client.PREVIEW] = lambda request: {'schema_version': 'sdc.support.v1', 'preview_id': request['preview_id'],
                                                    'preview': {'payload': payload(request)}, 'request_id': CASE, 'extra': CANARY}
    with pytest.raises(client.SupportClientError, match='^SDC_SUPPORT_RESPONSE_INVALID$'):
        connect(world)
    assert not world.prompts


def test_bad_signature_never_displays_or_registers(world):
    world.verifier = lambda *_a, **_kw: (_ for _ in ()).throw(ValueError(CANARY))
    with pytest.raises(ValueError):
        connect(world)
    assert not world.prompts and world.store.read(local.NAME)['receipt'] is None


@pytest.mark.parametrize('field', ['claim_id', 'device_id'])
def test_prompt_monitor_identity_change_with_same_key_is_rejected(world, field):
    def changed(*_):
        value = world.store.read('state.json')
        value[field] = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
        world.store.write('state.json', value)
        return True
    with pytest.raises(ValueError):
        connect(world, confirm=changed)
    assert world.store.read(local.NAME)['receipt'] is None
    assert [path for path, _, _ in world.calls] == [client.PREVIEW]


@pytest.mark.parametrize('field', ['claim_id', 'device_id'])
@pytest.mark.parametrize('accepted', [False, True])
def test_new_session_rechecks_receipt_monitor_identity_before_cached_or_unknown_accept(world, field, accepted):
    if not accepted:
        world.actions[client.ACCEPT] = lambda _: (_ for _ in ()).throw(TransportError())
        with pytest.raises(TransportError):
            connect(world)
    else:
        assert connect(world)['state'] == 'accepted'
    before = world.store.read(local.NAME)
    value = world.store.read('state.json')
    value[field] = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
    world.store.write('state.json', value)
    calls = len(world.calls)
    with pytest.raises(client.SupportClientError, match='IDENTITY_CHANGED'):
        connect(world)
    assert len(world.calls) == calls and world.store.read(local.NAME) == before
