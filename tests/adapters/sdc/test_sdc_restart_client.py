"""Synthetic foreground planning/consent through real records; all HTTP and crypto are stubs."""
import base64
import copy
import fcntl
import hashlib
import hmac
import json
import os
from types import SimpleNamespace

import pytest

from adapters.sdc import restart_client as client
from adapters.sdc import restart_control as control
from adapters.sdc import restart_planning as planning
from adapters.sdc import restart_state as change
from adapters.sdc import support_client as parent_client
from adapters.sdc import support_state as parent
from adapters.sdc.storage import Store
from adapters.sdc.transport import TransportError
from test_sdc_restart_planning import (OTHER, CANARY, make_preview, rehash_preview,
                                        registration_response, inspection_response, raw, begin_plan, registered_plan)
from test_sdc_restart_planning import planning_world  # noqa: F401


@pytest.fixture
def world(planning_world):  # noqa: F811 - imported pytest fixture
    w = planning_world
    w.sent, w.prompts, w.verifications, w.actions, w.samples = [], [], [], {}, []
    w.elapsed = 0
    w.sample = lambda _: {**w.preflight, 'observed_at': w.now}

    def no_state_locks():
        with Store(w.store.root) as other:
            with other.support_state_lock(), other.restart_state_lock(), other.lock():
                pass
            with pytest.raises(BlockingIOError), other.support_session_lock():
                pytest.fail('foreground session lost')

    def pause(seconds):
        w.now += int(seconds * 1000)
        w.elapsed += seconds
        def sample(req):
            no_state_locks()
            w.samples.append(copy.deepcopy(req))
            return w.sample(req)
        planning.respond_once(w.store, sample=sample, now=lambda: w.now)

    def send(path, payload, headers):
        no_state_locks()
        body = json.loads(payload)
        saved = w.store.read(planning.NAME)['plan']
        field = {control.REGISTRATION_READ: 'read', control.INSPECTION: 'inspection', control.PREVIEW: 'preview'}.get(path)
        if field:
            record = saved[field]['request'] if field == 'read' else saved[field]
        else:
            record = w.store.read(change.NAME)['accept']['request']
        assert record['raw'].encode('ascii') == payload
        material = '\n'.join(('SDC-HMAC-v1', w.restart_endpoint['audience'], 'POST', path,
            headers['X-SDC-Device'], headers['X-SDC-Key-ID'], headers['X-SDC-Time'], headers['X-SDC-Nonce'],
            hashlib.sha256(payload).hexdigest())).encode('ascii')
        assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex('1' * 64), material, hashlib.sha256).hexdigest()
        w.sent.append((path, payload, dict(headers)))
        if path in w.actions:
            return w.actions[path](body)
        if path == control.REGISTRATION_READ:
            return registration_response(w, record)
        if path == control.INSPECTION:
            return inspection_response(w, record)
        if path == control.PREVIEW:
            preview = make_preview(w, record)
            independent = planning._expected(saved)
            for item in (preview, preview['intent'], preview['approval']):
                item.update(copy.deepcopy(independent))
            preview = rehash_preview(preview)
            envelope = {'alg': 'PS256', 'kid': 'synthetic-restart', 'purpose': 'restart-consent-preview',
                        'payload': base64.urlsafe_b64encode(raw(preview).encode('ascii')).rstrip(b'=').decode('ascii'),
                        'signature': 'A' * 512}
            return {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER,
                    'preview_id': body['preview_id'], 'preview': envelope, 'registration': copy.deepcopy(w.registration)}
        assert path == control.ACCEPT
        receipt = w.store.read(change.NAME)['receipt']
        return {**body, 'schema_version': 'sdc.support.restart-acceptance.v1', 'operation_id': receipt['operation_id'],
                'state': 'accepted', 'expires_at': receipt['expires_at'], 'accepted_at': w.now, 'request_id': OTHER}

    def verifier(payload, **kwargs):
        no_state_locks()
        w.verifications.append(copy.deepcopy(kwargs))
        assert kwargs['purpose'] == 'restart-consent-preview' and kwargs['pins'] == w.restart_pins
        assert kwargs['expected'] == planning._expected(w.store.read(planning.NAME)['plan'])
        value = json.loads(payload)
        return json.loads(base64.urlsafe_b64decode(value['payload'] + '=' * (-len(value['payload']) % 4)))

    def confirm(stage, value, target):
        no_state_locks()
        w.prompts.append((stage, copy.deepcopy(value), copy.deepcopy(target)))
        return True

    w.pause, w.verifier, w.confirm, w.transport = pause, verifier, confirm, lambda _: SimpleNamespace(send=send)
    return w


def options(w, **changes):
    return {'restart_provision': w.provision, 'check_pins': w.check_pins, 'check_restart_pins': w.check_restart_pins,
            'transport_factory': w.transport, 'confirm': w.confirm, 'clock': lambda: w.now / 1000,
            'monotonic': lambda: w.elapsed, 'pause': w.pause, **changes}


def plan(w, **changes):
    return client.plan(w.store, w.case_id, **options(w, **changes))


def consent(w, **changes):
    return client.consent(w.store, w.operation_id, verifier=w.verifier, **options(w, **changes))


@pytest.mark.parametrize('planning_world', [True], indirect=True)
def test_managed_three_profile_targets_keep_parent_scope_and_restart_expected_separate(world):
    w = world
    original = copy.deepcopy(w.store.read(parent.NAME)['receipt'])
    assert len({b['origin'] for b in w.bundles.values()}) == 3
    assert len({b['audience'] for b in w.bundles.values()}) == 3
    assert plan(w)['state'] == 'planned'
    assert consent(w)['state'] == 'accepted'
    p = w.store.read(planning.NAME)['plan']
    value = w.store.read(change.NAME)
    assert w.store.read(parent.NAME)['receipt'] == original
    assert p['parent']['expected']['audience'] == w.bundles['support']['audience']
    assert value['expected']['audience'] == w.bundles['restart']['audience']
    assert value['binding']['profile']['endpoint'] == w.restart_endpoint
    assert p['parent']['binding']['profile']['endpoint'] == w.parent_endpoint
    assert value['binding']['profile']['bundle_hash'] != p['parent']['binding']['profile']['bundle_hash']
    assert w.verifications[0]['expected']['audience'] == w.bundles['restart']['audience']
    assert all(target == w.restart_endpoint for _, _, target in w.prompts)
    assert w.pin_calls and {which for which, _, _ in w.pin_calls} == {'parent', 'restart'}
    assert all(call['purpose'] == ('consent-preview' if which == 'parent' else 'restart-consent-preview')
               for which, _, call in w.pin_calls)


@pytest.mark.parametrize('planning_world', [True], indirect=True)
@pytest.mark.parametrize('field,value', [('tenant', 'other'), ('realm_epoch', 'other'),
    ('device_epoch', 3), ('claim_id', OTHER), ('device_id', OTHER), ('audience', 'sdc:other'),
    ('pins', [])])
def test_managed_restart_snapshot_must_match_installed_genuine_profile_before_io(world, field, value):
    snapshot = world.provision()
    assert snapshot['bundle'][field] != value
    snapshot['bundle'][field] = value
    snapshot['bundle_hash'] = parent_client._hash(snapshot['bundle'])
    with pytest.raises(ValueError):
        plan(world, restart_provision=lambda: copy.deepcopy(snapshot))
    assert not world.sent and not world.prompts and not world.samples


@pytest.mark.parametrize('planning_world', [True], indirect=True)
def test_managed_restart_never_extends_parent_profiles_registration_or_foreground_deadline(world):
    original = world.now
    world.registration['expires_at'] = original + 120000
    deadlines = []
    assert plan(world, limit_deadline=deadlines.append)['state'] == 'planned'
    p = world.store.read(planning.NAME)['plan']
    assert p['expires_at'] == original + 120000
    assert p['expires_at'] <= min(p['parent']['expires_at'], p['binding']['profile']['expires_at'])
    assert deadlines == sorted(deadlines, reverse=True)
    assert deadlines[-1] <= original + 120000


@pytest.mark.parametrize('stage', ['provider', 'parent-pins', 'restart-pins', 'credential'])
@pytest.mark.parametrize('failure', [TimeoutError, OSError, ValueError])
def test_actual_restart_supplier_and_state_boundary_preserve_only_timeout(world, monkeypatch, stage, failure):
    w, error, reached = world, failure(CANARY), []
    def unavailable(*_args, **_kwargs):
        reached.append(stage)
        raise error
    provider, parent_pins, restart_pins = w.provision, w.check_pins, w.check_restart_pins
    if stage == 'provider':
        provider = unavailable
    elif stage == 'parent-pins':
        parent_pins = unavailable
    elif stage == 'restart-pins':
        restart_pins = unavailable
    else:
        read = w.store.read
        def reading(name, default=None):
            if name == 'credential':
                unavailable()
            return read(name, default)
        monkeypatch.setattr(w.store, 'read', reading)
    supplier = client._supplier(w.store, provider, parent_pins, restart_pins, lambda: w.now / 1000, {})
    with pytest.raises(TimeoutError if failure is TimeoutError else change.RestartStateError) as caught:
        change.prepare(w.store, binding_supplier=lambda: supplier()['binding'], now_ms=w.now)
    assert reached == [stage] and not w.sent
    if failure is TimeoutError:
        assert caught.value is error
    else:
        assert CANARY not in str(caught.value)


@pytest.mark.parametrize('second_error', [None, OSError, TimeoutError, KeyboardInterrupt])
def test_stop_children_attempts_both_cleanups_and_preserves_first_timeout(world, monkeypatch, second_error):
    first, calls = TimeoutError(CANARY), []
    def planning_stop(_store):
        calls.append('planning')
        raise first
    disable = change.disable
    def change_stop(store):
        calls.append('change')
        if second_error is not None:
            raise second_error('synthetic second cleanup failure')
        return disable(store)
    monkeypatch.setattr(planning, 'disable', planning_stop)
    monkeypatch.setattr(change, 'disable', change_stop)
    with pytest.raises(TimeoutError) as caught:
        client.stop_children(world.store)
    assert caught.value is first and calls == ['planning', 'change']
    assert world.store.read(change.NAME)['disabled'] is (second_error is None)
    assert not world.sent


@pytest.fixture
def contender(world):
    """Another real Store owns the contended lock; callers must release partial locks."""
    with Store(world.store.root) as other:
        held = None
        def hold():
            nonlocal held
            assert held is None
            held = other.restart_state_lock()
            held.__enter__()
        def release():
            nonlocal held
            if held is not None:
                held.__exit__(None, None, None)
                held = None
        def partial_locks_released():
            with other.support_state_lock(), other.lock():
                pass
        try:
            yield SimpleNamespace(hold=hold, release=release, partial_locks_released=partial_locks_released)
        finally:
            release()


@pytest.mark.parametrize('stage,path', [
    ('response', control.REGISTRATION_READ), ('response', control.INSPECTION),
    ('response', control.PREVIEW), ('verified', control.PREVIEW), ('saved', control.PREVIEW),
    ('response', control.ACCEPT), ('saved', control.ACCEPT), ('saved', control.INSPECTION),
])
def test_received_response_survives_real_lock_contention_once(world, contender, monkeypatch, stage, path):
    w = world
    if path in (control.PREVIEW, control.ACCEPT):
        assert plan(w)['state'] == 'planned'
    transport, verifier = w.transport, w.verifier
    waits, locked_at = [], []
    def hold():
        locked_at.append(w.now)
        contender.hold()
    def send(route, *args):
        value = transport(w.restart_endpoint).send(route, *args)
        if route == path and stage == 'response':
            hold()
        return value
    def verify(*args, **kwargs):
        value = verifier(*args, **kwargs)
        if stage == 'verified':
            hold()
        return value
    if stage == 'saved':
        module, name = (change, 'record_accept_ack') if path == control.ACCEPT else (planning,
            'record_preview' if path == control.PREVIEW else 'record_inspection')
        record = getattr(module, name)
        def save(*args, **kwargs):
            value = record(*args, **kwargs)
            hold()
            return value
        monkeypatch.setattr(module, name, save)
    def pause(seconds):
        if seconds == 0.05:
            waits.append(seconds)
            contender.partial_locks_released()
            contender.release()
        w.pause(seconds)
    w.verifier = verify
    action = consent if path in (control.PREVIEW, control.ACCEPT) else plan
    assert action(w, transport_factory=lambda _: SimpleNamespace(send=send), pause=pause)['state'] == (
        'accepted' if action is consent else 'planned')
    assert waits == [0.05] and len(locked_at) == 1
    assert sum(route == path for route, _, _ in w.sent) == 1
    assert len(w.verifications) == (1 if action is consent else 0)
    assert [stage for stage, _, _ in w.prompts] == (['read', 'inspection', 'change'] if action is consent else ['read', 'inspection'])
    with Store(w.store.root) as reopened:
        saved = reopened.read(planning.NAME)['plan']
        assert saved['registration'] is not None and saved['inspection_ack'] is not None
        if action is consent:
            assert saved['verified_preview'] is not None and reopened.read(change.NAME)['accept']['ack'] is not None
        if path == control.REGISTRATION_READ:
            assert saved['registration']['fetched_at'] == locked_at[0] + 50
        if stage == 'verified':
            assert saved['verified_at'] == locked_at[0] + 50


@pytest.mark.parametrize('answered', [False, True])
def test_runtime_wait_snapshot_survives_real_lock_contention(world, contender, monkeypatch, answered):
    w = world
    request_sample, local_snapshot = planning.request_sample, planning.local_snapshot
    waits, held = [], False
    def request(*args, **kwargs):
        value = request_sample(*args, **kwargs)
        if not answered:
            contender.hold()
        return value
    def observe(*args, **kwargs):
        nonlocal held
        value = local_snapshot(*args, **kwargs)
        if answered and not held and value['state']['plan']['samples']['sample']['preflight'] is not None:
            contender.hold()
            held = True
        return value
    def pause(seconds):
        if seconds == 0.05:
            waits.append(seconds)
            contender.partial_locks_released()
            contender.release()
        w.pause(seconds)
    monkeypatch.setattr(planning, 'request_sample', request)
    monkeypatch.setattr(planning, 'local_snapshot', observe)
    assert plan(w, pause=pause)['state'] == 'planned'
    assert waits == [0.05] and len(w.samples) == 1
    assert [route for route, _, _ in w.sent] == [control.REGISTRATION_READ, control.INSPECTION]


@pytest.mark.parametrize('path', [control.REGISTRATION_READ, control.INSPECTION, control.PREVIEW, control.ACCEPT])
def test_response_lock_contention_expires_without_another_dispatch(world, contender, path):
    w = world
    if path in (control.PREVIEW, control.ACCEPT):
        assert plan(w)['state'] == 'planned'
    transport, started = w.transport, w.now
    waits = []
    def send(route, *args):
        value = transport(w.restart_endpoint).send(route, *args)
        if route == path:
            contender.hold()
        return value
    def pause(seconds):
        if seconds == 0.05:
            contender.partial_locks_released()
            waits.append(seconds)
            w.now += 50
            w.elapsed += seconds
        else:
            w.pause(seconds)
    def budget():
        pass
    budget.expires_at = lambda: started + 1000
    with pytest.raises(client.RestartClientError, match='EXPIRED'):
        (consent if path in (control.PREVIEW, control.ACCEPT) else plan)(w,
            transport_factory=lambda _: SimpleNamespace(send=send), pause=pause, budget=budget)
    assert 0 < len(waits) < 20 and w.now < started + 1000
    assert sum(route == path for route, _, _ in w.sent) == 1 and w.sent[-1][0] == path
    contender.release()
    with Store(w.store.root) as reopened:
        p = reopened.read(planning.NAME)['plan']
        if path == control.REGISTRATION_READ:
            assert p['read']['attempts'] == 1 and p['registration'] is None
        elif path == control.INSPECTION:
            assert p['inspection'] is not None and p['inspection_ack'] is None
        elif path == control.PREVIEW:
            assert p['preview'] is not None and p['verified_preview'] is None and not w.verifications
        else:
            assert reopened.read(change.NAME)['accept']['ack'] is None


@pytest.mark.parametrize('path', [control.INSPECTION, control.PREVIEW, control.ACCEPT])
def test_stop_during_response_lock_retry_preserves_only_late_ack(world, contender, path):
    w = world
    if path != control.INSPECTION:
        assert plan(w)['state'] == 'planned'
    transport = w.transport
    waits = []
    def send(route, *args):
        value = transport(w.restart_endpoint).send(route, *args)
        if route == path:
            contender.hold()
        return value
    def pause(seconds):
        if seconds != 0.05:
            return w.pause(seconds)
        waits.append(seconds)
        contender.partial_locks_released()
        contender.release()
        parent_client.stop(w.store, now_ms=w.now)
        client.stop_children(w.store)
    action = consent if path != control.INSPECTION else plan
    kwargs = {'transport_factory': lambda _: SimpleNamespace(send=send), 'pause': pause}
    if path == control.ACCEPT:
        assert action(w, **kwargs) == {'state': 'ack_recorded_inactive'}
        assert w.store.read(change.NAME)['accept']['ack'] is not None
    else:
        with pytest.raises(ValueError):
            action(w, **kwargs)
        p = w.store.read(planning.NAME)['plan']
        assert (p['inspection_ack'] is not None) if path == control.INSPECTION else p['verified_preview'] is None
    assert waits == [0.05] and w.sent[-1][0] == path
    assert sum(route == path for route, _, _ in w.sent) == 1
    assert not w.store.read(parent.NAME)['enabled'] and w.store.read(planning.NAME)['disabled']
    assert w.store.read(change.NAME)['disabled']


def test_verified_response_lock_retry_rechecks_current_binding(world, contender):
    w = world
    assert plan(w)['state'] == 'planned'
    verifier, waits = w.verifier, []
    def verify(*args, **kwargs):
        value = verifier(*args, **kwargs)
        contender.hold()
        return value
    def pause(seconds):
        assert seconds == 0.05
        waits.append(seconds)
        contender.release()
        w.restart_provision['root_fingerprint'] = '0' * 64
    w.verifier = verify
    with pytest.raises(ValueError):
        consent(w, pause=pause)
    assert waits == [0.05] and len(w.verifications) == 1
    assert w.store.read(planning.NAME)['plan']['verified_preview'] is None
    assert w.store.read(change.NAME)['receipt'] is None and w.sent[-1][0] == control.PREVIEW


@pytest.mark.parametrize('interrupt', [False, True])
def test_verified_response_lock_retry_expiry_or_cancellation_cannot_confirm(world, contender, interrupt):
    w = world
    assert plan(w)['state'] == 'planned'
    verifier, expires = w.verifier, []
    def verify(*args, **kwargs):
        value = verifier(*args, **kwargs)
        expires.append(value['expires_at'])
        contender.hold()
        return value
    def pause(seconds):
        assert seconds == 0.05
        contender.partial_locks_released()
        contender.release()
        if interrupt:
            raise KeyboardInterrupt
        w.elapsed += (expires[0] - w.now) / 1000
        w.now = expires[0]
    w.verifier = verify
    with pytest.raises(KeyboardInterrupt if interrupt else ValueError):
        consent(w, pause=pause)
    assert len(w.verifications) == 1 and w.sent[-1][0] == control.PREVIEW
    assert [stage for stage, _, _ in w.prompts] == ['read', 'inspection']
    assert w.store.read(planning.NAME)['plan']['verified_preview'] is None and w.store.read(change.NAME)['receipt'] is None
    if interrupt:
        assert w.store.read(planning.NAME)['disabled']


@pytest.mark.parametrize('path', [control.INSPECTION, control.PREVIEW, control.ACCEPT])
def test_malformed_response_is_not_retried_as_local_contention(world, path):
    w = world
    if path != control.INSPECTION:
        assert plan(w)['state'] == 'planned'
    w.actions[path] = lambda _: {'private': CANARY}
    def pause(seconds):
        assert seconds != 0.05, 'malformed response retried locally'
        w.pause(seconds)
    with pytest.raises(ValueError) as caught:
        (consent if path != control.INSPECTION else plan)(w, pause=pause)
    assert CANARY not in str(caught.value) and sum(route == path for route, _, _ in w.sent) == 1
    assert w.sent[-1][0] == path


@pytest.mark.parametrize('path', [control.INSPECTION, control.ACCEPT])
def test_uncertain_response_write_is_not_retried_as_lock_contention(world, monkeypatch, path):
    w = world
    if path == control.ACCEPT:
        assert plan(w)['state'] == 'planned'
    write, writes = w.store.write, []
    def uncertain(name, value):
        write(name, value)
        if ((path == control.INSPECTION and name == planning.NAME and value['plan']['inspection_ack'] is not None)
                or (path == control.ACCEPT and name == change.NAME and value['accept'] and value['accept']['ack'] is not None)):
            writes.append(name)
            raise BlockingIOError('synthetic uncertain directory fsync')
    def pause(seconds):
        assert seconds != 0.05, 'uncertain write retried'
        w.pause(seconds)
    monkeypatch.setattr(w.store, 'write', uncertain)
    with pytest.raises(ValueError, match='UNCERTAIN'):
        (consent if path == control.ACCEPT else plan)(w, pause=pause)
    assert len(writes) == 1 and sum(route == path for route, _, _ in w.sent) == 1


def test_whole_client_sequence_keeps_independent_expected_and_four_distinct_permissions(world):
    w = world
    parent_before = w.store.read(parent.NAME)
    assert plan(w) == {'state': 'planned'}
    assert w.store.read(change.NAME)['receipt'] is None
    assert consent(w) == {'state': 'accepted'}
    assert [path for path, _, _ in w.sent] == [control.REGISTRATION_READ, control.INSPECTION, control.PREVIEW, control.ACCEPT]
    assert [stage for stage, _, _ in w.prompts] == ['read', 'inspection', 'change']
    assert [sample['phase'] for sample in w.samples] == ['sample', 'confirm']
    assert w.verifications[0]['expected'] == w.expected and w.store.read(parent.NAME) == parent_before
    state = w.store.read(change.NAME)
    assert state['accept']['ack'] is not None and state['receipt']['change_accept_id'] == state['accept']['ack']['accept_id']
    assert plan(w)['state'] == 'planned' and consent(w)['state'] == 'accepted' and len(w.sent) == 4
    saved = (w.store.root / planning.NAME).read_text()
    assert '"key":' not in saved and '"grant":' not in saved and '"signature":' not in saved and '"pins":' not in saved


def test_cli_to_actual_runtime_metadata_to_inspection_and_consent(world, tmp_path, monkeypatch, capsys):
    import sdc_support_cli as cli
    from mcs_standalone import runtime
    w = world
    root = tmp_path / 'runtime'
    root.mkdir(mode=0o700)
    (root / 'data').mkdir(mode=0o700)
    cfg = {'runtime_mode': 'standalone'}
    (root / 'config.json').write_text(json.dumps(cfg))
    (root / 'config.json').chmod(0o600)
    (root / 'data/update.lock').touch(mode=0o600)
    monkeypatch.setattr(runtime.time, 'time', lambda: w.now / 1000)
    host = runtime.Runtime(root, cfg)
    fd = os.open(root / 'data/standalone.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    host._planning_host_fd = fd
    w.sample = host._restart_planning_preflight
    monkeypatch.setattr(cli, 'RestartTransport', w.transport)
    monkeypatch.setattr(cli.sdc_support_signatures, 'validate_pins', w.check_pins)
    monkeypatch.setattr(cli.sdc_support_signatures, 'validate_restart_pins', w.check_restart_pins)
    monkeypatch.setattr(cli.sdc_support_signatures, 'verify_restart_envelope', w.verifier)
    monkeypatch.setattr(cli, '_confirm_restart', w.confirm)
    original_plan, original_consent = client.plan, client.consent
    monkeypatch.setattr(client, 'plan', lambda store, identifier, **kw: original_plan(store, identifier,
        **{**kw, 'clock': lambda: w.now / 1000, 'monotonic': lambda: w.elapsed, 'pause': w.pause}))
    monkeypatch.setattr(client, 'consent', lambda store, identifier, **kw: original_consent(store, identifier,
        **{**kw, 'clock': lambda: w.now / 1000, 'monotonic': lambda: w.elapsed, 'pause': w.pause}))
    try:
        assert cli.main(['plan-restart', w.case_id, '--state-root', str(w.store.root)], restart_provision=w.provision) == 0
        assert cli.main(['consent-restart', w.operation_id, '--state-root', str(w.store.root)], restart_provision=w.provision) == 0
        state = w.store.read(change.NAME)
        assert state['expected']['params']['expected_runtime_generation'] == host.generation
        assert state['expected']['params']['expected_runtime_generation'] != w.expected['params']['expected_runtime_generation']
        assert state['accept']['ack'] is not None and not host.restarting and host._restart_execution_owner is None
        assert '再起動は実行していません' in capsys.readouterr().out
    finally:
        host._planning_host_fd = None
        os.close(fd)


@pytest.mark.parametrize('answer', [False, None, 1, 'yes'])
def test_no_read_confirmation_means_no_read_or_runtime_request(world, answer):
    assert plan(world, confirm=lambda *_: answer) == {'state': 'cancelled'}
    assert not world.sent and not world.samples and world.store.read(planning.NAME)['plan'] is None


def test_no_inspection_confirmation_cancels_planning_without_change_permission(world):
    assert plan(world, confirm=lambda stage, *_: stage == 'read') == {'state': 'cancelled'}
    assert [path for path, _, _ in world.sent] == [control.REGISTRATION_READ]
    assert planning.status(world.store)['disabled'] and world.store.read(change.NAME)['receipt'] is None


def test_slow_human_inspection_prompt_gets_only_one_same_generation_refresh(world):
    original = world.confirm
    def confirm(stage, *args):
        if stage == 'inspection':
            world.now += 15001
            world.elapsed += 15.001
        return original(stage, *args)
    assert plan(world, confirm=confirm)['state'] == 'planned'
    assert [sample['phase'] for sample in world.samples] == ['sample', 'refresh']
    body = json.loads(world.sent[-1][1])
    assert body['preflight']['observed_at'] == world.now


@pytest.mark.parametrize('status', [None, 500, 502, 503, 504, 599])
def test_read_unknown_retries_exact_body_with_new_headers_and_never_more_than_four(world, status):
    error = TransportError() if status is None else TransportError(status, 'SDC_NETWORK_UNAVAILABLE')
    world.actions[control.REGISTRATION_READ] = lambda _: (_ for _ in ()).throw(error)
    with pytest.raises(client.RestartClientError, match='READ_HELD'):
        plan(world)
    assert len(world.sent) == 4 and len({body for _, body, _ in world.sent}) == 1
    assert len({headers['X-SDC-Nonce'] for _, _, headers in world.sent}) == 4
    with Store(world.store.root) as reopened, pytest.raises(ValueError):
        client.plan(reopened, world.case_id, **options(world))
    assert len(world.sent) == 4 and planning.status(world.store)['read_attempts'] == 4


@pytest.mark.parametrize('code', [200, 302, 400, 401, 403, 404, 409, 413, 415, 429, 499, 600, 503.0])
def test_observed_terminal_read_status_stops_after_one_dispatch(world, code):
    world.actions[control.REGISTRATION_READ] = lambda _: (_ for _ in ()).throw(TransportError(code, 'STATE_CONFLICT'))
    with pytest.raises(TransportError):
        plan(world)
    assert len(world.sent) == 1 and world.store.read(planning.NAME)['plan']['read']['blocked']
    with pytest.raises(ValueError):
        plan(world)
    assert len(world.sent) == 1


def test_redirect_denied_read_remains_terminal_even_with_default_503(world):
    world.actions[control.REGISTRATION_READ] = lambda _: (_ for _ in ()).throw(TransportError(code='SDC_REDIRECT_DENIED'))
    with pytest.raises(TransportError):
        plan(world)
    assert len(world.sent) == 1 and world.store.read(planning.NAME)['plan']['read']['blocked']


def test_read_retry_after_beyond_original_budget_is_not_waited(world):
    world.actions[control.REGISTRATION_READ] = lambda _: (_ for _ in ()).throw(TransportError(503, retry_after=301))
    with pytest.raises(client.RestartClientError, match='EXPIRED'):
        plan(world, pause=lambda _: pytest.fail('wait exceeds budget'))
    assert len(world.sent) == 1


@pytest.mark.parametrize('field,value', [('case_id', OTHER), ('local_receipt_id', OTHER), ('tenant', 'other'),
                                        ('policy_epoch', 77), ('state', 'revoked'), ('expires_at', 1)])
def test_well_identified_read_mismatch_or_revocation_is_terminal(world, field, value):
    def wrong(_):
        request = world.store.read(planning.NAME)['plan']['read']['request']
        response = registration_response(world, request)
        (response if field in ('case_id', 'local_receipt_id') else response['registration'])[field] = value
        return response
    world.actions[control.REGISTRATION_READ] = wrong
    with pytest.raises(ValueError):
        plan(world)
    assert len(world.sent) == 1 and not planning.status(world.store)['registration_recorded']


def test_malformed_success_read_has_only_original_four_attempts(world):
    world.actions[control.REGISTRATION_READ] = lambda _: {'private': CANARY}
    with pytest.raises(ValueError) as caught:
        plan(world)
    assert CANARY not in str(caught.value) and len(world.sent) == 4


@pytest.mark.parametrize('where', ['read_prompt', 'inspection_prompt', 'change_prompt'])
def test_parent_stop_during_any_prompt_blocks_next_admission(world, where):
    if where == 'change_prompt':
        assert plan(world)['state'] == 'planned'
    original = world.confirm
    def confirm(stage, *args):
        if stage + '_prompt' == where:
            assert parent_client.stop(world.store, now_ms=world.now)['foreground_ended'] is False
        return original(stage, *args)
    with pytest.raises(ValueError):
        (consent if where == 'change_prompt' else plan)(world, confirm=confirm)
    forbidden = {'read_prompt': control.REGISTRATION_READ, 'inspection_prompt': control.INSPECTION, 'change_prompt': control.ACCEPT}[where]
    assert forbidden not in [path for path, _, _ in world.sent]
    assert world.store.read(change.NAME)['receipt'] is None


@pytest.mark.parametrize('path,state', [(control.INSPECTION, 'inspection_unknown'), (control.PREVIEW, 'preview_unknown')])
def test_unknown_non_read_request_is_never_reissued(world, path, state):
    if path == control.PREVIEW:
        assert plan(world)['state'] == 'planned'
    world.actions[path] = lambda _: (_ for _ in ()).throw(TransportError())
    action = consent if path == control.PREVIEW else plan
    with pytest.raises(TransportError):
        action(world)
    count = len(world.sent)
    assert action(world) == {'state': state} and len(world.sent) == count


def test_changed_operation_expected_is_rejected_before_human_change_consent(world):
    assert plan(world)['state'] == 'planned'
    verifier = world.verifier
    def malicious(payload, **kwargs):
        value = verifier(payload, **kwargs)
        for item in (value, value['intent'], value['approval']):
            item['service_revision'] += 1
        return rehash_preview(value)
    world.verifier = malicious
    with pytest.raises(ValueError):
        consent(world)
    assert [stage for stage, _, _ in world.prompts] == ['read', 'inspection']
    assert world.store.read(change.NAME)['receipt'] is None


def test_same_accept_raw_is_recoverable_but_late_ack_after_stop_never_enables(world):
    assert plan(world)['state'] == 'planned'
    world.actions[control.ACCEPT] = lambda _: (_ for _ in ()).throw(TransportError())
    with pytest.raises(TransportError):
        consent(world)
    original = world.sent[-1][1]
    del world.actions[control.ACCEPT]
    assert consent(world)['state'] == 'accepted'
    assert world.sent[-1][1] == original and len([p for p, _, _ in world.prompts if p == 'change']) == 1
    state = world.store.read(change.NAME)
    changed = {**state['accept']['ack'], 'accepted_at': state['accept']['ack']['accepted_at'] + 1, 'request_id': OTHER}
    with pytest.raises(ValueError):
        change.record_accept_ack(world.store, state['accept']['request'], changed, now_ms=world.now + 1)


def test_inflight_accept_ack_after_parent_stop_is_retained_without_reenable(world):
    assert plan(world)['state'] == 'planned'
    def late(body):
        parent_client.stop(world.store, now_ms=world.now)
        client.stop_children(world.store)
        receipt = world.store.read(change.NAME)['receipt']
        return {**body, 'schema_version': 'sdc.support.restart-acceptance.v1', 'request_id': OTHER,
                'operation_id': receipt['operation_id'], 'state': 'accepted', 'accepted_at': world.now,
                'expires_at': receipt['expires_at']}
    world.actions[control.ACCEPT] = late
    assert consent(world) == {'state': 'ack_recorded_inactive'}
    assert world.store.read(change.NAME)['accept']['ack'] is not None and world.store.read(change.NAME)['disabled']
    assert not world.store.read(parent.NAME)['enabled']


def customer_marker(w):
    w.store._write_file('credential-store.json', {'schema_version': 'sdc.credential-store.v2', 'mode': 'keychain',
        'account': OTHER, 'root_fingerprint': w.store.root_fingerprint(), 'endpoint_hash': w.store._endpoint_hash()})
    state = w.store._read_file('state.json')
    state['credential_storage'] = 'keychain'
    w.store._write_file('state.json', state)


def test_customer_runtime_metadata_response_never_reads_marker_keychain_provider_or_pins(world, monkeypatch):
    w = world
    registered_plan(w)
    planning.request_sample(w.store, 'sample', binding_supplier=w.binding_supplier, now_ms=w.now)
    customer_marker(w)
    monkeypatch.setattr(w.store, '_marker', lambda: pytest.fail('metadata marker access'))
    monkeypatch.setattr(w.store, '_keychain', lambda: pytest.fail('metadata Keychain access'))
    before = len(w.pin_calls)
    assert planning.respond_once(w.store, sample=lambda _: {**w.preflight, 'observed_at': w.now}, now=lambda: w.now)
    assert len(w.pin_calls) == before and planning.local_snapshot(w.store, now_ms=w.now)['state']['plan']['generation']


def test_unanswered_wait_is_local_but_current_key_is_rechecked_after_response(world, monkeypatch):
    w = world
    customer_marker(w)
    reads, waits, pins = [], [], []
    waiting = False
    credential = {'key_id': w.expected['key_id'], 'key': '1' * 64}
    def keychain_read():
        assert not waiting, 'unanswered metadata wait reached Keychain'
        reads.append(True)
        return json.dumps(credential).encode('ascii')
    reader = SimpleNamespace(for_account=lambda account: SimpleNamespace(read=keychain_read),
                             close=lambda: None, closed=True)
    monkeypatch.setattr(w.store, '_credential_reader', reader)
    def pause(seconds):
        nonlocal waiting
        w.now += int(seconds * 1000)
        w.elapsed += seconds
        if not waits:
            waiting = True
            pins.append(len(w.pin_calls))
            waits.append(len(reads))
        else:
            assert len(reads) == waits[0] and len(w.pin_calls) == pins[0]
            waits.append(len(reads))
        if len(waits) == 4:
            assert planning.respond_once(w.store, sample=lambda _: {**w.preflight, 'observed_at': w.now}, now=lambda: w.now)
            assert len(reads) == waits[0]
            waiting = False
    assert plan(w, pause=pause)['state'] == 'planned'
    assert len(waits) == 4 and len(reads) > waits[0]


def test_unprovisioned_client_and_runtime_hook_do_no_private_or_owner_access():
    store = SimpleNamespace()
    with pytest.raises(client.RestartClientError, match='NOT_PROVISIONED'):
        client.plan(store, OTHER, restart_provision=None, check_pins=None, check_restart_pins=None, transport_factory=None, confirm=None)


def test_status_and_parent_first_stop_never_read_keychain_or_send(world, monkeypatch):
    begin_plan(world)
    monkeypatch.setattr(world.store, '_marker', lambda: pytest.fail('status/stop credential access'))
    assert client.status(world.store, now_ms=world.now)['planning']['read_attempts'] == 0
    parent_client.stop(world.store, now_ms=world.now)
    client.stop_children(world.store)
    assert not world.store.read(parent.NAME)['enabled']
    assert client.status(world.store, now_ms=world.now)['planning']['disabled']
    assert world.store.read(change.NAME)['disabled'] and not world.sent
