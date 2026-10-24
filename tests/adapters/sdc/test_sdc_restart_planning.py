"""Synthetic parent-bound planning records; no network, Keychain or real Runtime."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.sdc import restart_client as client
from adapters.sdc import restart_contract as contract
from adapters.sdc import restart_planning as planning
from adapters.sdc import restart_state as change
from adapters.sdc import support_client as parent_client
from adapters.sdc import support_state as parent
from adapters.sdc import provision
from adapters.sdc.storage import Store
from test_sdc_support_client import payload as parent_payload, managed_baseline

V = json.loads((Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/support/restart-wire-vectors.json').read_text())
OTHER = '00000000-0000-4000-8000-000000000099'
CANARY = 'SYNTHETIC_PRIVATE_MUST_NOT_ESCAPE'


def raw(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(raw(value).encode('ascii')).hexdigest()


@pytest.fixture
def planning_world(tmp_path, request):
    managed = getattr(request, 'param', False)
    with Store(tmp_path / 'planning-private', create=not managed, create_new=managed) as store:
        expected = copy.deepcopy(V['expected'])
        identity = {k: expected[k] for k in ('claim_id', 'device_id', 'key_id')}
        if managed:
            bundles, install = managed_baseline(store, expected=expected, now=V['now'])
            install('support')
            monitor = {k: bundles['monitor'][k] for k in ('origin', 'audience', 'environment')}
            expected['audience'] = bundles['support']['audience']
        else:
            monitor = {'origin': 'https://monitor.invalid', **{k: expected[k] for k in ('audience', 'environment')}}
            store.write('endpoint.json', monitor)
            store.write('state.json', {**identity, 'state': 'active', 'enabled': True})
            store.write('credential', {'key_id': identity['key_id'], 'key': '1' * 64})
        w = SimpleNamespace(store=store, now=V['now'], expected=expected, monitor=monitor,
            parent_endpoint={**monitor, 'origin': 'https://diagnostic.invalid'},
            restart_endpoint={**monitor, 'origin': 'https://restart.invalid'},
            parent_pins=[{'synthetic_parent': True}], restart_pins=[{'synthetic_restart': True}],
            pin_calls=[], auth={}, registration=copy.deepcopy(V['registration']),
            preflight=copy.deepcopy(V['preflight']), case_id=expected['case_id'], operation_id=V['operation']['operation_id'])
        if managed:
            w.bundles, w.install = bundles, install
            w.parent_endpoint = {k: bundles['support'][k] for k in ('origin', 'audience', 'environment')}
            w.restart_endpoint = {k: bundles['restart'][k] for k in ('origin', 'audience', 'environment')}
            w.parent_pins, w.restart_pins = bundles['support']['pins'], bundles['restart']['pins']
        def check_pins(pins, **kwargs):
            assert pins == w.parent_pins
            w.pin_calls.append(('parent', copy.deepcopy(pins), kwargs))
        def check_restart_pins(pins, **kwargs):
            assert pins == w.restart_pins
            w.pin_calls.append(('restart', copy.deepcopy(pins), kwargs))
        w.check_pins, w.check_restart_pins = check_pins, check_restart_pins
        binding = store.enrollment_binding()
        if managed:
            w.provision = lambda: provision.profile_snapshot(store, profile='restart', now_ms=w.now)
        else:
            store.write('support-bootstrap.json', {'schema_version': parent_client.BOOTSTRAP,
                'root_fingerprint': binding['root_fingerprint'], 'monitor_endpoint_hash': binding['endpoint_hash'],
                'support_endpoint': w.parent_endpoint, 'pins': w.parent_pins})
            w.restart_provision = {'root_fingerprint': binding['root_fingerprint'], 'monitor_endpoint_hash': binding['endpoint_hash'],
                                  'support_endpoint': w.restart_endpoint, 'pins': w.restart_pins}
            w.provision = lambda: copy.deepcopy(w.restart_provision)
        w.parent_supplier = parent_client._supplier(store, check_pins, lambda: w.now / 1000, {})
        # Explicit fresh provision plus a fully synthetic acknowledged R4a case.
        if managed:
            provision.initialize_profile(store, profile='support', now_ms=w.now,
                initializer=lambda: parent_client.initialize_local_state(store, check_pins=check_pins, clock=lambda: w.now / 1000))
            snapshot = parent.prepare(store, binding_supplier=w.parent_supplier, now_ms=w.now)
        else:
            snapshot = parent.prepare(store, binding_supplier=w.parent_supplier, now_ms=w.now, initialize=True)
        request = parent.begin_preview(store, w.case_id, expected_owner_generation=snapshot['owner_generation'],
                                       binding_supplier=w.parent_supplier, now_ms=w.now)
        w.parent_preview = parent_payload(json.loads(request['raw']), now=w.now,
                                          **{k: expected[k] for k in planning.EXPECTED})
        parent.record_preview(store, request, w.parent_preview, now_ms=w.now)
        snapshot = parent.confirm_consent(store, w.parent_preview, expected_owner_generation=snapshot['owner_generation'],
            expected_binding=request['binding'], binding_supplier=w.parent_supplier, now_ms=w.now, confirm_human=True)
        request = parent.begin_accept(store, expected_owner_generation=snapshot['owner_generation'],
                                      binding_supplier=w.parent_supplier, now_ms=w.now)
        parent.record_accept_ack(store, request, {**json.loads(request['raw']), 'case_id': w.case_id,
            'state': 'accepted', 'expires_at': w.parent_preview['consent_expires_at'], 'request_id': OTHER})
        if managed:
            install('restart')
            provision.initialize_profile(store, profile='restart', now_ms=w.now,
                                         initializer=lambda: client.initialize_local_state(store))
        else:
            change.initialize(store)
            planning.initialize(store)
        w.binding_supplier = client._supplier(store, w.provision, check_pins, check_restart_pins, lambda: w.now / 1000, w.auth)
        bindings = w.binding_supplier()
        w.parent_binding, w.restart_binding = bindings['parent_binding'], bindings['binding']
        yield w


def frame(w, store=None):
    return planning.snapshot(store or w.store, binding_supplier=w.binding_supplier, now_ms=w.now)


@pytest.mark.parametrize('planning_world', [True], indirect=True)
def test_managed_wait_reads_profile_metadata_without_credentials_injected_providers_or_verification(planning_world, monkeypatch):
    import sdc_support_signatures
    import subprocess

    w = planning_world
    registered_plan(w)
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
    pins_before = copy.deepcopy(w.pin_calls)
    original_read = w.store.read
    def read(name, default=None):
        if name == 'credential':
            pytest.fail('metadata read must not acquire credentials')
        return original_read(name, default)
    def forbidden(*_args, **_kwargs):
        pytest.fail('metadata read must not invoke a trust/provider/HTTP operation')
    monkeypatch.setattr(w.store, 'read', read)
    monkeypatch.setattr(w.store, '_marker', forbidden)
    monkeypatch.setattr(w.store, '_keychain', forbidden)
    monkeypatch.setattr(sdc_support_signatures, 'verify_envelope', forbidden)
    monkeypatch.setattr(sdc_support_signatures, 'verify_restart_envelope', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    w.binding_supplier = w.provision = forbidden
    observed = planning.local_snapshot(w.store, now_ms=w.now)
    assert observed['parent']['expected']['audience'] == w.bundles['support']['audience']
    samples = []
    def sample(request):
        samples.append(request)
        return {**w.preflight, 'observed_at': w.now}
    assert planning.respond_once(w.store, sample=sample, now=lambda: w.now)
    assert samples[0]['expected']['audience'] == w.bundles['restart']['audience']
    assert w.pin_calls == pins_before


@pytest.mark.parametrize('planning_world', [True], indirect=True)
@pytest.mark.parametrize('profile', ['support', 'restart'])
def test_managed_metadata_rejects_current_profile_change_with_identical_origin_and_pins(planning_world, monkeypatch, profile):
    w = planning_world
    registered_plan(w)
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
    original = provision.profile_snapshot
    def changed(store, *, profile: str, now_ms):
        result = original(store, profile=profile, now_ms=now_ms)
        if profile == changed_profile:
            result['bundle']['expires_at'] += 1
            result['bundle_hash'] = digest(result['bundle'])
        return result
    changed_profile = profile
    monkeypatch.setattr(provision, 'profile_snapshot', changed)
    before = w.store.read(planning.NAME)
    with pytest.raises(ValueError):
        planning.respond_once(w.store, sample=lambda _: pytest.fail('changed profile must not sample'), now=lambda: w.now)
    assert w.store.read(planning.NAME) == before


@pytest.mark.parametrize('planning_world', [True], indirect=True)
@pytest.mark.parametrize('field,value', [('device_id', OTHER), ('claim_id', OTHER), ('key_id', OTHER), ('enabled', False)])
def test_managed_wait_rechecks_active_r1_identity_before_sampling(planning_world, field, value):
    w = planning_world
    registered_plan(w)
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
    state = w.store.read('state.json')
    state[field] = value
    w.store.write('state.json', state)
    with pytest.raises(ValueError):
        planning.respond_once(w.store, sample=lambda _: pytest.fail('changed identity must not sample'), now=lambda: w.now)


def test_managed_restart_partial_initialization_cannot_reopen_or_reapprove_into_a_new_state(tmp_path):
    with Store(tmp_path / 'partial-managed', create_new=True) as store:
        _, install = managed_baseline(store, expected=V['expected'], now=V['now'])
        install('support')
        provision.initialize_profile(store, profile='support', now_ms=V['now'], initializer=lambda:
            parent_client.initialize_local_state(store, check_pins=lambda *_args, **_kwargs: None, clock=lambda: V['now'] / 1000))
        install('restart')
        with pytest.raises(ValueError):
            client.initialize_local_state(store)
        assert store.read(change.NAME) is None and store.read(planning.NAME) is None
        def partial():
            change.initialize(store)
            raise RuntimeError('synthetic-initialization-interrupted')
        with pytest.raises(provision.ProvisionError, match='^SDC_PROVISION_INVALID$'):
            provision.initialize_profile(store, profile='restart', now_ms=V['now'], initializer=partial)
        before = store.read(change.NAME)
        assert before is not None and before['receipt'] is None
        with pytest.raises(ValueError):
            planning.initialize(store)
        with Store(store.root) as reopened:
            with pytest.raises(ValueError):
                client.initialize_local_state(reopened)
            with pytest.raises(ValueError):
                provision.initialize_profile(reopened, profile='restart', now_ms=V['now'],
                                             initializer=lambda: client.initialize_local_state(reopened))
        assert store.read(change.NAME) == before and store.read(planning.NAME) is None


@pytest.mark.parametrize('module', [change, planning], ids=['change', 'planning'])
@pytest.mark.parametrize('stage', ['anchor', 'write-before', 'write-after'])
def test_actual_managed_restart_initializer_preserves_timeout_and_write_hold(tmp_path, monkeypatch, module, stage):
    with Store(tmp_path / 'restart-init-timeout', create_new=True) as store:
        _, install = managed_baseline(store, expected=V['expected'], now=V['now'])
        install('support')
        provision.initialize_profile(store, profile='support', now_ms=V['now'], initializer=lambda:
            parent_client.initialize_local_state(store, check_pins=lambda *_args, **_kwargs: None, clock=lambda: V['now'] / 1000))
        install('restart')
        error = TimeoutError(CANARY)
        write, fingerprint, validate = store.write, store.root_fingerprint, module._validated
        reached, armed = [], []
        def validating(value):
            result = validate(value)
            armed.append(True)
            return result
        def root_fingerprint():
            if stage == 'anchor' and armed:
                reached.append(module.NAME)
                raise error
            return fingerprint()
        def writing(name, value):
            if name == module.NAME:
                reached.append(name)
                if stage == 'write-after':
                    write(name, value)
                raise error
            write(name, value)
        with monkeypatch.context() as patch:
            patch.setattr(store, 'write', writing)
            patch.setattr(store, 'root_fingerprint', root_fingerprint)
            patch.setattr(module, '_validated', validating)
            with pytest.raises(TimeoutError) as caught:
                provision.initialize_profile(store, profile='restart', now_ms=V['now'],
                                             initializer=lambda: client.initialize_local_state(store))
        assert caught.value is error and reached == [module.NAME]
        marker = '_restart_state_uncertain' if module is change else '_restart_planning_uncertain'
        assert getattr(store, marker) and 'restart' in store._provision_profile_failed
        assert not provision.initialization_allowed(store, 'restart')
        assert (store.read(module.NAME) is not None) is (stage == 'write-after')
        with Store(store.root) as reopened:
            with pytest.raises(ValueError):
                provision.initialize_profile(reopened, profile='restart', now_ms=V['now'],
                    initializer=lambda: pytest.fail('consumed initialization cannot run again'))
            state = reopened.read(change.NAME)
            assert state is None or state['receipt'] is None


@pytest.mark.parametrize('name', [parent.NAME, change.NAME, planning.NAME])
@pytest.mark.parametrize('failure', [TimeoutError, OSError, ValueError])
def test_metadata_read_preserves_timeout_but_hides_other_errors(planning_world, monkeypatch, name, failure):
    w = planning_world
    original, reached = w.store.read, []
    error = failure(CANARY)
    def reading(current, default=None):
        if current == name:
            reached.append(current)
            raise error
        return original(current, default)
    monkeypatch.setattr(w.store, 'read', reading)
    with pytest.raises(TimeoutError if failure is TimeoutError else ValueError) as caught:
        planning.local_snapshot(w.store, now_ms=w.now)
    assert reached == [name]
    if failure is TimeoutError:
        assert caught.value is error
    else:
        assert CANARY not in str(caught.value)
    assert not getattr(w.store, '_restart_state_uncertain', False)
    assert not getattr(w.store, '_restart_planning_uncertain', False)


def test_sample_timeout_leaves_request_pending_and_preserves_original(planning_world):
    w = planning_world
    registered_plan(w)
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
    before = w.store.read(planning.NAME)
    error = TimeoutError(CANARY)
    def sample(_request):
        raise error
    with pytest.raises(TimeoutError) as caught:
        planning.respond_once(w.store, sample=sample, now=lambda: w.now)
    assert caught.value is error and w.store.read(planning.NAME) == before


def begin_plan(w):
    planning.begin(w.store, frame(w), binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
    return w.store.read(planning.NAME)['plan']


def registration_response(w, request):
    return {'schema_version': 'sdc.support.restart-registration-response.v1',
            **{k: json.loads(request['raw'])[k] for k in ('case_id', 'local_receipt_id')},
            'registration': copy.deepcopy(w.registration), 'request_id': OTHER}


def registered_plan(w):
    begin_plan(w)
    request = planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    planning.record_registration(w.store, request, registration_response(w, request), binding_supplier=w.binding_supplier, now_ms=w.now)
    return w.store.read(planning.NAME)['plan']


def sample_plan(w, phase='sample'):
    planning.request_sample(w.store, phase, binding_supplier=w.binding_supplier, now_ms=w.now)
    value = {**w.preflight, 'observed_at': w.now}
    assert planning.respond_once(w.store, sample=lambda _: value, now=lambda: w.now)
    return w.store.read(planning.NAME)['plan']


def inspection_response(w, request):
    return {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER,
            'inspection_id': json.loads(request['raw'])['inspection_id'], 'state': 'recorded',
            'expires_at': w.now + 300000, 'registration': copy.deepcopy(w.registration)}


def inspected_plan(w):
    registered_plan(w)
    sample_plan(w)
    planning.confirm_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
    request = planning.begin_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    planning.record_inspection(w.store, request, inspection_response(w, request), binding_supplier=w.binding_supplier, now_ms=w.now)
    return w.store.read(planning.NAME)['plan']


def make_preview(w, request):
    p = w.store.read(planning.NAME)['plan']
    value = copy.deepcopy(V['preview'])
    value['preview_id'] = json.loads(request['raw'])['preview_id']
    value['operation_id'] = value['intent']['operation_id'] = json.loads(request['raw'])['operation_id']
    value['intent']['inspection_id'] = p['inspection_consent']['inspection_id']
    value['intent']['inspection_hash'] = p['inspection']['body_hash']
    return rehash_preview(value)


def rehash_preview(value):
    value['intent_hash'] = value['approval']['intent_hash'] = contract.hash_restart_intent(value['intent'])
    value['approval']['approval_hash'] = contract.hash_restart_approval(value['approval'])
    value['change_binding_hash'] = contract.hash_restart_change_binding(value)
    return value


def prepared_preview(w):
    inspected_plan(w)
    request = planning.begin_preview(w.store, w.operation_id, binding_supplier=w.binding_supplier, now_ms=w.now)
    value = make_preview(w, request)
    planning.record_preview(w.store, request, value, w.registration, binding_supplier=w.binding_supplier, now_ms=w.now)
    return w.store.read(planning.NAME)['plan']


def test_explicit_fresh_initialize_is_required_and_never_replaces_existing_state(tmp_path):
    with Store(tmp_path / 'fresh', create=True) as store:
        with pytest.raises(planning.RestartPlanningError, match='MISSING'):
            planning.status(store)
        assert not (store.root / planning.NAME).exists()
        planning.initialize(store)
        original = store.read(planning.NAME)
        with pytest.raises(ValueError):
            planning.initialize(store)
        assert store.read(planning.NAME) == original and original['plan'] is None


@pytest.mark.parametrize('answer', [False, None, 1, 'yes'])
def test_parent_diagnostic_consent_never_substitutes_for_planning_consent(planning_world, answer):
    w = planning_world
    original = w.store.read(parent.NAME)
    with pytest.raises(planning.RestartPlanningError, match='HUMAN_REQUIRED'):
        planning.begin(w.store, frame(w), binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=answer)
    assert w.store.read(planning.NAME)['plan'] is None and w.store.read(parent.NAME) == original
    assert w.store.read(change.NAME)['receipt'] is None


def test_parent_and_restart_pins_stay_distinct_and_expected_is_built_in_stages(planning_world):
    w = planning_world
    original = w.store.read(parent.NAME)
    p = begin_plan(w)
    assert p['parent']['binding'] == w.parent_binding and p['binding'] == w.restart_binding
    assert w.parent_binding['pins_sha256'] == digest(w.parent_pins) != digest(w.restart_pins)
    assert w.restart_binding['pins_sha256'] == digest(w.restart_pins)
    assert p['parent']['expected'] == {k: w.expected[k] for k in planning.EXPECTED}
    assert 'params' not in p['parent']['expected'] and 'service_revision' not in p['parent']['expected']
    assert p['generation'] is None and p['registration'] is None
    with pytest.raises(ValueError):
        planning._expected(p)
    assert w.store.read(parent.NAME) == original
    assert {kind for kind, _, _ in w.pin_calls} == {'parent', 'restart'}


def test_read_attempts_are_fixed_before_return_and_remain_bounded_after_reopen(planning_world):
    w = planning_world
    begin_plan(w)
    records = [planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)]
    assert planning.status(w.store)['read_attempts'] == 1
    with Store(w.store.root) as reopened:
        for count in range(2, 5):
            records.append(planning.read_attempt(reopened, binding_supplier=w.binding_supplier, now_ms=w.now))
            assert planning.status(reopened)['read_attempts'] == count
        with pytest.raises(ValueError):
            planning.read_attempt(reopened, binding_supplier=w.binding_supplier, now_ms=w.now)
    assert all(value == records[0] for value in records)
    assert records[0]['body_hash'] == digest(json.loads(records[0]['raw']))


def test_terminal_read_keeps_its_request_and_never_adopts_a_late_response(planning_world):
    w = planning_world
    begin_plan(w)
    request = planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    planning.block_read(w.store, request)
    with pytest.raises(ValueError):
        planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    with pytest.raises(ValueError):
        planning.record_registration(w.store, request, registration_response(w, request), binding_supplier=w.binding_supplier, now_ms=w.now)
    p = w.store.read(planning.NAME)['plan']
    assert p['read']['blocked'] and p['read']['request'] == request and p['registration'] is None


@pytest.mark.parametrize('field', ['tenant', 'installation', 'device_id', 'environment', 'realm_epoch', 'policy_epoch', 'device_epoch'])
def test_registration_never_migrates_existing_parent_scope_or_epochs(planning_world, field):
    w = planning_world
    begin_plan(w)
    request = planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    response = registration_response(w, request)
    response['registration'][field] = OTHER if field == 'device_id' else 9 if field.endswith('_epoch') and field != 'realm_epoch' else 'different'
    with pytest.raises(ValueError):
        planning.record_registration(w.store, request, response, binding_supplier=w.binding_supplier, now_ms=w.now)
    assert not planning.status(w.store)['registration_recorded']


def test_registration_adopts_revision_only_once_and_never_extends_parent_expiry(planning_world):
    w = planning_world
    w.registration['service_revision'] = 23
    p = registered_plan(w)
    assert p['registration']['value']['service_revision'] == 23 and p['generation'] is None
    assert p['expires_at'] == p['parent']['expires_at']
    request = p['read']['request']
    with pytest.raises(ValueError):
        planning.record_registration(w.store, request, registration_response(w, request), binding_supplier=w.binding_supplier, now_ms=w.now)
    with pytest.raises(ValueError):
        planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    sample_plan(w)
    actual = planning._expected(w.store.read(planning.NAME)['plan'])
    assert actual == {**w.expected, 'service_revision': 23}


@pytest.mark.parametrize('where', ['parent_disabled', 'parent_owner', 'parent_ack', 'restart_owner', 'planning_owner',
                                   'secret', 'parent_pins', 'restart_pins', 'monitor_disabled'])
def test_parent_and_child_cas_or_current_binding_changes_block_read_adoption(planning_world, where):
    w = planning_world
    snapshot = frame(w)
    begin_plan(w)
    request = planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    if where == 'parent_disabled':
        parent.disable(w.store, now_ms=w.now)
    elif where in ('parent_owner', 'parent_ack'):
        state = w.store.read(parent.NAME)
        if where == 'parent_owner':
            state['owner_generation'] = 'f' * 32
        else:
            state['client']['accept']['ack']['expires_at'] -= 1
        w.store.write(parent.NAME, state)
    elif where in ('restart_owner', 'planning_owner'):
        name = change.NAME if where == 'restart_owner' else planning.NAME
        state = w.store.read(name)
        state['owner_generation'] = 'f' * 32
        w.store.write(name, state)
    elif where == 'secret':
        state = w.store.read('credential')
        state['key'] = '2' * 64
        w.store.write('credential', state)
    elif where == 'parent_pins':
        state = w.store.read('support-bootstrap.json')
        state['pins'] = [{'other': True}]
        w.store.write('support-bootstrap.json', state)
    elif where == 'restart_pins':
        w.restart_provision['pins'] = [{'other': True}]
    else:
        state = w.store.read('state.json')
        state['enabled'] = False
        w.store.write('state.json', state)
    with pytest.raises(ValueError):
        planning.record_registration(w.store, request, registration_response(w, request), binding_supplier=w.binding_supplier, now_ms=w.now)
    with pytest.raises(ValueError):
        planning.begin(w.store, snapshot, binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
    assert w.store.read(planning.NAME)['plan']['registration'] is None


def test_begin_rejects_changed_planning_owner_snapshot(planning_world):
    w = planning_world
    snapshot = frame(w)
    state = w.store.read(planning.NAME)
    state['owner_generation'] = 'f' * 32
    w.store.write(planning.NAME, state)
    with pytest.raises(ValueError):
        planning.begin(w.store, snapshot, binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
    assert w.store.read(planning.NAME)['plan'] is None


def test_parent_http_trace_id_change_does_not_change_business_ack(planning_world):
    w = planning_world
    registered_plan(w)
    state = w.store.read(parent.NAME)
    state['client']['accept']['ack']['request_id'] = V['operation']['operation_id']
    w.store.write(parent.NAME, state)
    assert frame(w)['state']['plan']['registration'] is not None


def test_unacknowledged_parent_never_admits_planning(planning_world):
    w = planning_world
    state = w.store.read(parent.NAME)
    state['client']['accept']['ack'] = None
    w.store.write(parent.NAME, state)
    with pytest.raises(ValueError):
        frame(w)
    assert w.store.read(planning.NAME)['plan'] is None


def test_short_access_lock_order_is_support_then_restart_then_tick(planning_world, monkeypatch):
    w = planning_world
    events = []
    def tracked(name, original):
        @contextmanager
        def acquire():
            with original():
                events.append('enter_' + name)
                try:
                    yield
                finally:
                    events.append('exit_' + name)
        return acquire
    for attribute in ('support_state_lock', 'restart_state_lock', 'lock'):
        monkeypatch.setattr(w.store, attribute, tracked(attribute, getattr(w.store, attribute)))
    frame(w)
    assert events == ['enter_support_state_lock', 'enter_restart_state_lock', 'enter_lock',
                      'exit_lock', 'exit_restart_state_lock', 'exit_support_state_lock']


@pytest.mark.parametrize('stop_or_expire', ['stop', 'parent_expiry', 'registration_expiry'])
def test_late_registration_response_never_adopts_after_stop_or_original_expiry(planning_world, stop_or_expire):
    w = planning_world
    begin_plan(w)
    request = planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    response = registration_response(w, request)
    if stop_or_expire == 'stop':
        planning.disable(w.store)
    elif stop_or_expire == 'parent_expiry':
        w.now = w.parent_preview['consent_expires_at']
    else:
        response['registration']['expires_at'] = w.now
    with pytest.raises(ValueError):
        planning.record_registration(w.store, request, response, binding_supplier=w.binding_supplier, now_ms=w.now)
    assert w.store.read(planning.NAME)['plan']['registration'] is None


def test_sample_is_outside_all_three_locks_and_each_phase_replies_once(planning_world):
    w = planning_world
    registered_plan(w)
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
    seen = []
    def sample(request):
        with Store(w.store.root) as other:
            with other.support_state_lock(), other.restart_state_lock(), other.lock():
                pass
        seen.append(request)
        assert request['expected_generation'] is None and request['phase'] == 'sample'
        assert set(request) == {'request_id', 'planning_owner', 'phase', 'expected_generation', 'expires_at', 'expected'}
        return {**w.preflight, 'observed_at': w.now}
    assert planning.respond_once(w.store, sample=sample, now=lambda: w.now)
    assert not planning.respond_once(w.store, sample=lambda _: pytest.fail('second sample'), now=lambda: w.now)
    assert len(seen) == 1 and planning.status(w.store)['runtime_generation'] == w.preflight['runtime_generation']
    with pytest.raises(ValueError):
        planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)


def test_one_pending_phase_and_separate_inspection_and_change_gates(planning_world):
    w = planning_world
    registered_plan(w)
    for phase in ('refresh', 'confirm'):
        with pytest.raises(ValueError):
            planning.request_sample(w.store, phase, binding_supplier=w.binding_supplier, now_ms=w.now)
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
    with pytest.raises(ValueError):
        planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
    assert w.store.read(change.NAME)['receipt'] is None


def test_sample_request_obeys_the_original_foreground_deadline(planning_world):
    w = planning_world
    registered_plan(w)
    expires = w.now + 3000
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now, expires_at=expires)
    state = w.store.read(planning.NAME)
    assert state['plan']['samples']['sample']['expires_at'] == expires
    w.now = expires
    assert not planning.respond_once(w.store,
        sample=lambda _: pytest.fail('expired slot must not sample'), now=lambda: w.now)
    assert w.store.read(planning.NAME) == state
    with pytest.raises(ValueError):
        planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)


def test_sample_expiry_during_observation_prevents_adoption(planning_world):
    w = planning_world
    registered_plan(w)
    expires = w.now + 3000
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now, expires_at=expires)
    def sample(request):
        assert request['expires_at'] == expires
        w.now = expires
        return {**w.preflight, 'observed_at': w.now}
    with pytest.raises(ValueError):
        planning.respond_once(w.store, sample=sample, now=lambda: w.now)
    assert w.store.read(planning.NAME)['plan']['generation'] is None


@pytest.mark.parametrize('mutation', ['generation', 'owner_alive', 'update', 'intake', 'restarting', 'future', 'old_observation', 'scope'])
def test_invalid_or_new_generation_observation_cannot_replace_original_owner(planning_world, mutation):
    w = planning_world
    registered_plan(w)
    sample_plan(w)
    planning.confirm_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
    w.now += 1
    planning.request_sample(w.store, 'refresh', binding_supplier=w.binding_supplier, now_ms=w.now)
    value = {**w.preflight, 'observed_at': w.now}
    key, replacement = {'generation': ('runtime_generation', 'f' * 32), 'owner_alive': ('owner_alive', False),
        'update': ('update_in_progress', True), 'intake': ('command_intake_in_progress', True), 'restarting': ('restarting', True),
        'future': ('observed_at', w.now + 1), 'old_observation': ('observed_at', w.now - 1), 'scope': ('tenant', 'other')}[mutation]
    value[key] = replacement
    with pytest.raises(ValueError):
        planning.respond_once(w.store, sample=lambda _: value, now=lambda: w.now)
    p = w.store.read(planning.NAME)['plan']
    assert p['generation'] == w.preflight['runtime_generation'] and p['samples']['refresh']['preflight'] is None


@pytest.mark.parametrize('change_during_sample', ['stop', 'parent_stop', 'owner', 'expiry'])
def test_sample_reply_cas_and_deadline_are_rechecked_after_observation(planning_world, change_during_sample):
    w = planning_world
    registered_plan(w)
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
    def sample(_):
        if change_during_sample == 'stop':
            planning.disable(w.store)
        elif change_during_sample == 'parent_stop':
            parent.disable(w.store, now_ms=w.now)
        elif change_during_sample == 'owner':
            state = w.store.read(planning.NAME)
            state['owner_generation'] = 'f' * 32
            w.store.write(planning.NAME, state)
        else:
            w.now = w.store.read(planning.NAME)['plan']['expires_at']
        return {**w.preflight, 'observed_at': w.now}
    with pytest.raises(ValueError):
        planning.respond_once(w.store, sample=sample, now=lambda: w.now)
    assert w.store.read(planning.NAME)['plan']['samples']['sample']['preflight'] is None


def test_inspection_requires_new_human_consent_and_fresh_same_generation_sample(planning_world):
    w = planning_world
    registered_plan(w)
    sample_plan(w)
    with pytest.raises(ValueError):
        planning.begin_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    with pytest.raises(planning.RestartPlanningError, match='HUMAN_REQUIRED'):
        planning.confirm_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    planning.confirm_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
    w.now += 15001
    with pytest.raises(ValueError):
        planning.begin_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    sample_plan(w, 'refresh')
    request = planning.begin_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    assert json.loads(request['raw'])['preflight']['observed_at'] == w.now
    with pytest.raises(ValueError):
        planning.begin_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    with Store(w.store.root) as reopened:
        with pytest.raises(ValueError):
            planning.begin_inspection(reopened, binding_supplier=w.binding_supplier, now_ms=w.now)
    assert w.store.read(planning.NAME)['plan']['inspection'] == request


def test_late_inspection_ack_after_stop_is_evidence_and_never_reenables(planning_world):
    w = planning_world
    registered_plan(w)
    sample_plan(w)
    planning.confirm_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
    request = planning.begin_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
    planning.disable(w.store)
    planning.record_inspection(w.store, request, inspection_response(w, request), binding_supplier=w.binding_supplier, now_ms=w.now)
    assert planning.status(w.store)['disabled'] and planning.status(w.store)['inspection_recorded']
    with pytest.raises(ValueError):
        planning.begin_preview(w.store, w.operation_id, binding_supplier=w.binding_supplier, now_ms=w.now)


def test_unknown_preview_keeps_fixed_id_raw_hash_and_cannot_be_resent(planning_world):
    w = planning_world
    inspected_plan(w)
    request = planning.begin_preview(w.store, w.operation_id, binding_supplier=w.binding_supplier, now_ms=w.now)
    with Store(w.store.root) as reopened:
        with pytest.raises(ValueError):
            planning.begin_preview(reopened, w.operation_id, binding_supplier=w.binding_supplier, now_ms=w.now)
    assert request == w.store.read(planning.NAME)['plan']['preview']
    assert request['body_hash'] == digest(json.loads(request['raw']))


@pytest.mark.parametrize('field', ['inspection_id', 'inspection_hash', 'service_revision', 'generation'])
def test_verified_preview_cannot_supply_expected_or_replace_exact_inspection(planning_world, field):
    w = planning_world
    inspected_plan(w)
    request = planning.begin_preview(w.store, w.operation_id, binding_supplier=w.binding_supplier, now_ms=w.now)
    value = make_preview(w, request)
    if field in ('inspection_id', 'inspection_hash'):
        value['intent'][field] = OTHER if field.endswith('_id') else 'f' * 64
    elif field == 'service_revision':
        for record in (value, value['intent'], value['approval']):
            record[field] += 1
    else:
        for record in (value, value['intent'], value['approval']):
            record['params']['expected_runtime_generation'] = OTHER.replace('-', '')
    rehash_preview(value)
    with pytest.raises(ValueError):
        planning.record_preview(w.store, request, value, w.registration, binding_supplier=w.binding_supplier, now_ms=w.now)
    assert not planning.status(w.store)['preview_recorded']
    assert planning._expected(w.store.read(planning.NAME)['plan']) == w.expected


def test_confirm_phase_keeps_metadata_only_and_never_saves_keys_pins_grant_or_signature(planning_world):
    w = planning_world
    p = prepared_preview(w)
    sample_plan(w, 'confirm')
    assert planning._expected(p) == w.expected and w.store.read(change.NAME)['receipt'] is None
    saved = (w.store.root / planning.NAME).read_text()
    for forbidden in ('"key":', '"pins":', '"signature":', '"grant":', 'synthetic_parent', 'synthetic_restart', '1' * 64):
        assert forbidden not in saved
    assert w.store.read(planning.NAME)['plan']['samples']['confirm']['preflight']['runtime_generation'] == p['generation']


@pytest.mark.parametrize('name', [parent.NAME, change.NAME, planning.NAME])
@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_missing_or_corrupt_state_is_held_without_reinitialization(planning_world, name, damage):
    w = planning_world
    begin_plan(w)
    path = w.store.root / name
    if damage == 'missing':
        path.unlink()
    else:
        path.write_text('{}')
    with pytest.raises(ValueError):
        frame(w)
    assert (not path.exists()) if damage == 'missing' else path.read_text() == '{}'


def test_disable_and_status_require_no_endpoint_credentials_or_tick_lock(planning_world, monkeypatch):
    w = planning_world
    begin_plan(w)
    monkeypatch.setattr(w.store, '_marker', lambda: pytest.fail('credential marker not needed'))
    with Store(w.store.root) as other, other.lock():
        planning.disable(w.store)
        assert planning.status(w.store)['disabled']
    assert w.store.read(planning.NAME)['disabled']


def test_root_replacement_refuses_stale_descriptor_without_writing_new_root(planning_world, tmp_path):
    w = planning_world
    begin_plan(w)
    w.store.root.rename(tmp_path / 'retained')
    w.store.root.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        frame(w)
    with pytest.raises(ValueError):
        planning.status(w.store)
    assert not list(w.store.root.iterdir())


@pytest.mark.parametrize('stage', ['begin', 'read', 'block_read', 'registration', 'sample_request', 'sample_response',
                                   'inspection_consent', 'inspection', 'inspection_ack', 'preview', 'verified_preview', 'disable'])
@pytest.mark.parametrize('failure', ['file', 'directory', 'replace'])
def test_every_persistence_boundary_holds_same_store_on_fsync_or_replace_failure(planning_world, monkeypatch, stage, failure):
    w = planning_world
    if stage == 'begin':
        snapshot = frame(w)
        def action():
            return planning.begin(w.store, snapshot, binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
    elif stage in ('read', 'block_read', 'registration'):
        begin_plan(w)
        if stage == 'read':
            def action():
                return planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
        else:
            request = planning.read_attempt(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
            if stage == 'block_read':
                def action():
                    return planning.block_read(w.store, request)
            else:
                def action():
                    return planning.record_registration(w.store, request, registration_response(w, request), binding_supplier=w.binding_supplier, now_ms=w.now)
    elif stage in ('sample_request', 'sample_response', 'inspection_consent', 'inspection', 'inspection_ack'):
        registered_plan(w)
        if stage == 'sample_request':
            def action():
                return planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
        elif stage == 'sample_response':
            planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
            def action():
                return planning.respond_once(w.store,
                    sample=lambda _: {**w.preflight, 'observed_at': w.now}, now=lambda: w.now)
        else:
            sample_plan(w)
            if stage == 'inspection_consent':
                def action():
                    return planning.confirm_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
            else:
                planning.confirm_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now, confirm_human=True)
                if stage == 'inspection':
                    def action():
                        return planning.begin_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
                else:
                    request = planning.begin_inspection(w.store, binding_supplier=w.binding_supplier, now_ms=w.now)
                    def action():
                        return planning.record_inspection(w.store, request, inspection_response(w, request), binding_supplier=w.binding_supplier, now_ms=w.now)
    elif stage in ('preview', 'verified_preview'):
        inspected_plan(w)
        if stage == 'preview':
            def action():
                return planning.begin_preview(w.store, w.operation_id, binding_supplier=w.binding_supplier, now_ms=w.now)
        else:
            request = planning.begin_preview(w.store, w.operation_id, binding_supplier=w.binding_supplier, now_ms=w.now)
            value = make_preview(w, request)
            def action():
                return planning.record_preview(w.store, request, value, w.registration, binding_supplier=w.binding_supplier, now_ms=w.now)
    else:
        begin_plan(w)
        def action():
            return planning.disable(w.store)
    original_sync, original_replace = os.fsync, os.replace
    def fsync(fd):
        if ('directory' if fd == w.store.fd else 'file') == failure:
            raise OSError(CANARY)
        return original_sync(fd)
    def replace(src, dst, **kwargs):
        if dst == planning.NAME and failure == 'replace':
            raise OSError(CANARY)
        return original_replace(src, dst, **kwargs)
    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)
    with pytest.raises(planning.RestartPlanningError, match='UNCERTAIN') as caught:
        action()
    assert CANARY not in str(caught.value) and planning.status(w.store)['uncertain']
    with pytest.raises(ValueError):
        frame(w)
    assert not list(w.store.root.glob('.sdc-*'))


@pytest.mark.parametrize('layout', ['legacy', 'managed', 'conflict', 'changed'])
def test_planning_uses_current_monitor_projection_without_credential_or_provider_access(planning_world, monkeypatch, layout):
    from adapters.sdc import provision

    w = planning_world
    original = begin_plan(w)
    expected = planning.local_snapshot(w.store, now_ms=w.now)
    if layout != 'legacy':
        projected = dict(w.monitor)
        if layout == 'changed':
            projected['origin'] = 'https://changed.invalid'
        monkeypatch.setattr(provision, 'monitor_endpoint', lambda store: projected)
        if layout != 'conflict':
            (w.store.root / 'endpoint.json').unlink()

    monkeypatch.setattr(w.store, '_marker', lambda: pytest.fail('metadata must not inspect credentials'))
    monkeypatch.setattr(w.store, '_keychain', lambda: pytest.fail('metadata must not access Keychain'))
    if layout in ('conflict', 'changed'):
        with pytest.raises(ValueError, match='CONFLICT'):
            planning.local_snapshot(w.store, now_ms=w.now)
    else:
        assert planning.local_snapshot(w.store, now_ms=w.now) == expected
    assert w.store.read(planning.NAME)['plan'] == original
