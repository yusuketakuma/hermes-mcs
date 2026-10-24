"""Synthetic existing-tick diagnostics: one HTTP, current authority and same-alive sampling."""
import copy
import hashlib
import hmac
import json

import pytest

from adapters.sdc import diagnostic_state as local
from adapters.sdc import diagnostic_sender as sender
from adapters.sdc import provision, support_client, restart_client
from adapters.sdc.diagnostic_sender import DiagnosticSender, DiagnosticSenderError
from adapters.sdc.storage import Store
from adapters.sdc.transport import TransportError
from test_sdc_support_client import managed_baseline

NOW = 1800000000000
DAY = 86400000
UUIDS = [f'10000000-0000-4000-8000-{i:012d}' for i in range(1, 8)]


class World:
    def __init__(self, store):
        self.store, self.at, self.mono = store, NOW, 50.0
        self.calls, self.samples, self.current_calls = [], [], 0
        self.refusal_status = None
        self.target = {'root_fingerprint': store.root_fingerprint(), 'monitor_endpoint_hash': 'a' * 64,
                       'support_endpoint_hash': 'b' * 64, 'root_id': UUIDS[0], 'tenant': 'self',
                       'installation': 'fixture', 'realm_epoch': 'realm-1', 'device_epoch': 1,
                       'device_id': UUIDS[1], 'claim_id': UUIDS[2], 'key_id': UUIDS[3],
                       'environment': 'test', 'audience': 'fixture'}
        self.respond = self.response
        self.supplier = lambda: copy.deepcopy(self.target)
        self.collector = self.sample

    def enable(self):
        return local.enable(self.store, binding=self.target, now_ms=self.at, expires_at=NOW + 30 * DAY,
                            confirm_human=True, reason='fixture-human')

    def current(self):
        self.current_calls += 1
        return self.supplier()

    def __call__(self, path, raw):
        self.refusal_status = None
        assert isinstance(raw, bytes) and len(raw) <= 16384
        # The sender holds no diagnostic lock over network/provider work.
        with Store(self.store.root) as other, other.diagnostic_state_lock():
            state = other.read(local.NAME)
            if path == local.REPORT:
                assert state['pending']['raw'].encode() == raw
                assert state['pending']['attempts'] >= 1 and state['pending']['phase'] == 'unknown'
        self.calls.append((path, raw))
        return self.respond(path, raw)

    def response(self, path, raw):
        body = json.loads(raw)
        if path == local.STATUS:
            return {'schema_version': 'sdc.diagnostics.status.v1', 'request_id': UUIDS[4],
                    'consent': {'state': 'active', 'consent_epoch': 1, 'expires_at': NOW + 20 * DAY},
                    'batches': [{'batch_id': i, 'state': 'unknown', 'content_state': None} for i in body['batch_ids']]}
        return {'schema_version': 'sdc.diagnostics.receipt.v1', 'request_id': UUIDS[4],
                'batch_id': body['batch_id'], 'state': 'accepted', 'content_state': 'stored',
                'consent_epoch': body['consent_epoch'], 'event_count': len(body['events']),
                'dropped_events': body['dropped_events'], 'partial': body['partial'],
                'accepted_at': self.at, 'content_expires_at': self.at + 7 * DAY}

    def sample(self, *, previous_marks, since_ms, now_ms):
        assert since_ms == self.store.read(local.NAME)['receipt']['accepted_at'] and now_ms == self.at
        with Store(self.store.root) as other, other.diagnostic_state_lock():
            assert other.read(local.NAME)['consent_epoch'] == 1
        self.samples.append(copy.deepcopy(previous_marks))
        return {'events': [{'component': 'runtime', 'stage': 'unknown', 'code': 'UNKNOWN',
                            'severity': 'info', 'bucket_start': self.at // 600000 * 600000, 'count': 1}],
                'dropped_events': 0, 'partial': True, 'observed_at': self.at,
                'source_marks': {'runtime': {'observed_at': self.at, 'signature': 'c' * 64}}}

    def sender(self):
        return DiagnosticSender(self.store, current=self.current, send=self, provider=lambda **kw: self.collector(**kw),
                                clock=lambda: self.at / 1000, monotonic=lambda: self.mono)

    def tick(self, *, advance=0):
        self.at += advance
        count = len(self.calls)
        with self.store.lock():
            result = self.sender().tick(deadline=self.mono + 10)
        assert len(self.calls) - count <= 1
        return result


@pytest.fixture
def world(tmp_path):
    with Store(tmp_path / 'private', create_new=True,
               keychain_factory=lambda **_: pytest.fail('native keychain')) as store:
        world = World(store)
        world.enable()
        yield world


def saved(world):
    return world.store.read(local.NAME)


def throws(error):
    def fail(*_args, **_kwargs):
        raise error
    return fail


@pytest.fixture
def managed_world(tmp_path, monkeypatch):
    with Store(tmp_path / 'managed', create_new=True, credential_deadline=60.0,
               keychain_factory=lambda **_: pytest.fail('native keychain')) as store:
        w = World(store)
        w.bundles, install = managed_baseline(store, expected=w.target, now=NOW)
        install('support')
        provision.initialize_profile(store, profile='support', now_ms=NOW, initializer=lambda:
            support_client.initialize_local_state(store, check_pins=lambda *_args, **_kwargs: None, clock=lambda: NOW / 1000))
        install('restart')
        provision.initialize_profile(store, profile='restart', now_ms=NOW, initializer=lambda:
            restart_client.initialize_local_state(store))
        w.target = sender.managed_binding(store, now_ms=NOW)
        w.enable()
        w.targets, w.signed, w.key_reads = [], [], []
        w.deadline, w.before_transport = 60.0, lambda: None
        read = store.read
        def reading(name, default=None):
            if name == 'credential':
                w.key_reads.append(w.mono)
            return read(name, default)
        monkeypatch.setattr(store, 'read', reading)

        class Wire:
            def __init__(self, target):
                w.targets.append(copy.deepcopy(target))
                self.target, self.refusal_status = target, None
                w.before_transport()

            def send(self, path, raw, headers):
                assert path in (local.STATUS, local.REPORT)
                if path == local.REPORT:
                    assert saved(w)['pending']['raw'].encode('ascii') == raw
                assert self.target == {k: w.bundles['support'][k] for k in ('origin', 'audience', 'environment')}
                assert headers['X-SDC-Device'] == w.target['device_id'] and headers['X-SDC-Key-ID'] == w.target['key_id']
                material = '\n'.join(('SDC-HMAC-v1', self.target['audience'], 'POST', path,
                    headers['X-SDC-Device'], headers['X-SDC-Key-ID'], headers['X-SDC-Time'],
                    headers['X-SDC-Nonce'], hashlib.sha256(raw).hexdigest())).encode('ascii')
                assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex('1' * 64), material, hashlib.sha256).hexdigest()
                assert b'support_bundle_hash' not in raw and b'root_fingerprint' not in raw
                w.signed.append((path, raw, copy.deepcopy(headers)))
                try:
                    return w(path, raw)
                finally:
                    self.refusal_status = w.refusal_status

        w.wire = Wire
        def tick(*, advance=0, deadline=None):
            w.at += advance
            calls = len(w.calls)
            with store.lock():
                result = sender.run_managed_once(store, provider=lambda **kw: w.collector(**kw),
                    deadline=w.deadline if deadline is None else deadline, clock=lambda: w.at / 1000,
                    monotonic=lambda: w.mono, transport_factory=w.wire)
            assert len(w.calls) - calls <= 1
            return result
        w.tick = tick
        yield w


def test_managed_real_profiles_actual_signer_status_collect_then_next_tick_report(managed_world, monkeypatch):
    w = managed_world
    assert len({b['origin'] for b in w.bundles.values()}) == len({b['audience'] for b in w.bundles.values()}) == 3
    import sdc_support_signatures
    monkeypatch.setattr(sdc_support_signatures, 'verify_envelope', throws(AssertionError('R4a signature verification')))
    monkeypatch.setattr(sdc_support_signatures, 'verify_restart_envelope', throws(AssertionError('R4b signature verification')))
    monkeypatch.setattr(support_client, '_supplier', throws(AssertionError('R4a authority supplier')))
    assert w.store.read('support-state.json')['enabled'] is False
    assert w.tick()['queued_events'] == 1
    assert w.calls[0] == (local.STATUS, b'{"batch_ids":[],"schema_version":"sdc.diagnostics.status-request.v1"}')
    assert len(w.samples) == 1 and len(w.key_reads) >= 2
    assert w.tick(advance=300000)['result'] == 'accepted'
    assert [path for path, _ in w.calls] == [local.STATUS, local.REPORT]
    assert len(w.samples) == 1 and len(w.targets) == 2 and len(w.signed) == 2
    assert saved(w)['binding']['support_bundle_hash'] == provision.profile_snapshot(w.store, profile='support', now_ms=w.at)['bundle_hash']
    assert 'key' not in sender.managed_binding(w.store, now_ms=w.at)
    assert b'1111111111111111111111111111111111111111111111111111111111111111' not in (w.store.root / local.NAME).read_bytes()


@pytest.mark.parametrize('state', ['disabled', 'expired', 'missing', 'missing-state'])
def test_managed_off_expired_or_missing_has_zero_current_key_transport_provider(managed_world, monkeypatch, state):
    w = managed_world
    if state == 'disabled':
        local.disable(w.store, now_ms=w.at)
    elif state == 'expired':
        w.at = saved(w)['receipt']['expires_at']
    else:
        (w.store.root / local.NAME).unlink()
        if state == 'missing':
            (w.store.root / local.ANCHOR).unlink()
    monkeypatch.setattr(sender, 'managed_binding', throws(AssertionError('inactive current read')))
    if state == 'missing-state':
        with pytest.raises(local.DiagnosticStateError, match='STATE_MISSING'):
            w.tick()
        assert w.store.read(local.NAME) is None and w.store.read(local.ANCHOR) is not None
    else:
        assert w.tick()['enabled'] is False
    assert not w.key_reads and not w.targets and not w.calls and not w.samples


@pytest.mark.parametrize('field,value', [('claim_id', UUIDS[5]), ('device_id', UUIDS[5]), ('key_id', UUIDS[5]),
    ('enabled', False), ('rotation_id', UUIDS[5])])
def test_managed_identity_change_cannot_dispatch(managed_world, field, value):
    w = managed_world
    state = w.store.read('state.json')
    assert state.get(field) != value
    state[field] = value
    w.store.write('state.json', state)
    try:
        w.tick()
    except DiagnosticSenderError:
        pass
    assert not w.calls and not w.samples


@pytest.mark.parametrize('field', ['origin', 'audience', 'expires_at', 'policy_revision'])
def test_managed_changed_bundle_never_uses_old_local_consent(managed_world, monkeypatch, field):
    w = managed_world
    original = provision.profile_snapshot
    def replaced(store, *, profile, now_ms):
        value = original(store, profile=profile, now_ms=now_ms)
        if profile == 'support':
            changes = {'origin': 'https://replacement.invalid', 'audience': 'sdc:replacement',
                       'expires_at': value['bundle']['expires_at'] - 1, 'policy_revision': UUIDS[5]}
            value['bundle'][field] = changes[field]
            value['bundle_hash'] = sender._hash(value['bundle'])
        return value
    monkeypatch.setattr(provision, 'profile_snapshot', replaced)
    before = saved(w)['receipt']
    result = w.tick()
    assert result['enabled'] is False and saved(w)['receipt'] == before
    assert not w.calls and not w.samples and not w.targets


def test_managed_rechecks_binding_after_actual_credential_read(managed_world, monkeypatch):
    w = managed_world
    read = w.store.read
    def changed(name, default=None):
        value = read(name, default)
        if name == 'credential':
            state = read('state.json')
            state['key_id'] = UUIDS[5]
            w.store.write('state.json', state)
        return value
    monkeypatch.setattr(w.store, 'read', changed)
    with pytest.raises(DiagnosticSenderError):
        w.tick()
    assert len(w.key_reads) == 1 and not w.calls and not w.samples and not w.targets


def test_managed_dispatch_reacquires_actual_identity_after_admission_fsync(managed_world, monkeypatch):
    w = managed_world
    write, changed = w.store.write, []
    def writing(name, value):
        write(name, value)
        if name == local.NAME and value['next_due'] and not changed:
            changed.append(True)
            state = w.store.read('state.json')
            state['key_id'] = UUIDS[5]
            write('state.json', state)
            write('credential', {'key_id': UUIDS[5], 'key': '2' * 64})
    monkeypatch.setattr(w.store, 'write', writing)
    w.tick()
    assert changed == [True] and len(w.key_reads) == 2
    assert not w.targets and not w.calls and not w.samples
    assert saved(w)['binding']['key_id'] == w.target['key_id']


def test_managed_credential_timeout_is_original_and_saves_no_admission(managed_world, monkeypatch):
    w, error = managed_world, TimeoutError('synthetic credential deadline')
    original, reached = w.store.read, []
    before = saved(w)
    def reading(name, default=None):
        if name == 'credential':
            reached.append(True)
            raise error
        return original(name, default)
    monkeypatch.setattr(w.store, 'read', reading)
    with pytest.raises(TimeoutError) as caught:
        w.tick()
    assert caught.value is error and reached == [True] and saved(w) == before
    assert not w.calls and not w.samples and not w.targets


def test_managed_metadata_and_local_status_stop_need_no_credential(managed_world, monkeypatch):
    w = managed_world
    read = w.store.read
    def reading(name, default=None):
        if name == 'credential':
            pytest.fail('metadata acquired credentials')
        return read(name, default)
    monkeypatch.setattr(w.store, 'read', reading)
    assert sender.managed_binding(w.store, now_ms=w.at) == w.target
    monkeypatch.setattr(sender, 'managed_binding', throws(AssertionError('local status/stop acquired authority')))
    assert local.status(w.store, now_ms=w.at)['enabled']
    assert not local.disable(w.store, now_ms=w.at)['enabled']
    assert not w.key_reads and not w.calls and not w.samples


def test_managed_sender_never_falls_back_to_legacy_or_monitor(world, monkeypatch):
    read = world.store.read
    def reading(name, default=None):
        if name == 'credential':
            pytest.fail('missing support profile acquired credentials')
        return read(name, default)
    monkeypatch.setattr(world.store, 'read', reading)
    with pytest.raises(DiagnosticSenderError, match='AUTHORITY_UNAVAILABLE'):
        sender.run_managed_once(world.store, provider=throws(AssertionError('provider')),
            deadline=60, clock=lambda: world.at / 1000, monotonic=lambda: world.mono,
            transport_factory=throws(AssertionError('HTTP')))
    assert not world.calls and not world.samples


@pytest.mark.parametrize('stage', ['before-current', 'after-key', 'after-factory'])
def test_managed_original_deadline_is_not_refreshed(managed_world, monkeypatch, stage):
    w = managed_world
    if stage == 'before-current':
        w.mono = w.deadline
    elif stage == 'after-key':
        read = w.store.read
        def exhausted(name, default=None):
            result = read(name, default)
            if name == 'credential':
                w.mono = w.deadline
            return result
        monkeypatch.setattr(w.store, 'read', exhausted)
    else:
        w.before_transport = lambda: setattr(w, 'mono', w.deadline)
    with pytest.raises(TimeoutError, match='SDC_DIAGNOSTIC_DEADLINE'):
        w.tick()
    assert not w.calls and not w.samples
    assert len(w.key_reads) == (0 if stage == 'before-current' else 1 if stage == 'after-key' else 2)


@pytest.mark.parametrize('status', [None, 429, 403])
def test_managed_unknown_and_observed_refusal_use_original_batch_next_tick_status(managed_world, status):
    w = managed_world
    w.tick()
    error = TimeoutError('synthetic HTTP deadline')
    def unknown(_path, _raw):
        w.refusal_status = status
        raise error
    w.respond = unknown
    with pytest.raises(TimeoutError) as caught:
        w.tick(advance=300000)
    assert caught.value is error
    state = saved(w)
    pending = copy.deepcopy(state['pending'])
    if status == 403:
        assert pending is None and not state['enabled'] and state['result'] == 'denied'
        assert len(state['history']) == 1 and state['history'][0]['attempts'] == 1
        assert state['history'][0]['batch_id'] == json.loads(w.calls[-1][1])['batch_id']
    else:
        assert pending['phase'] == 'unknown' and pending['attempts'] == 1
    assert len(w.calls) == 2 and len(w.samples) == 1
    w.respond = w.response
    w.tick(advance=300000)
    if status == 403:
        assert len(w.calls) == 2 and saved(w) == state
    else:
        assert w.calls[-1][0] == local.STATUS
        assert json.loads(w.calls[-1][1])['batch_ids'] == [pending['batch_id']]
        assert saved(w)['pending']['raw'] == pending['raw'] and saved(w)['pending']['attempts'] == 1
    assert len(w.samples) == 1


def test_actual_store_empty_status_collect_next_tick_report_and_receipt(world):
    result = world.tick()
    assert result['queued_events'] == 1 and len(world.samples) == 1
    assert world.calls == [(local.STATUS, b'{"batch_ids":[],"schema_version":"sdc.diagnostics.status-request.v1"}')]
    result = world.tick(advance=300000)
    assert result['result'] == 'accepted' and result['pending_batch_id'] is None
    assert result['last_receipt']['partial'] is True and len(world.calls) == 2
    assert len(world.samples) == 1


def test_disabled_or_never_enabled_has_no_supplier_provider_http(tmp_path):
    with Store(tmp_path / 'private', create_new=True) as store:
        world = World(store)
        assert world.tick()['enabled'] is False
        world.enable()
        local.disable(store, now_ms=NOW)
        assert world.tick()['enabled'] is False
        assert world.current_calls == 0 and world.samples == [] and world.calls == []


def test_persisted_active_observation_does_not_sample_in_new_sender(world):
    world.collector = throws(ValueError('SYNTHETIC_SECRET'))
    with pytest.raises(DiagnosticSenderError, match='^SDC_DIAGNOSTIC_PROVIDER_UNAVAILABLE$'):
        world.tick()
    assert saved(world)['consent_epoch'] == 1 and not saved(world)['queue']
    world.collector = world.sample
    world.tick(advance=1)
    assert world.samples == [] and len(world.calls) == 1
    world.tick(advance=300000)
    assert len(world.calls) == 2 and len(world.samples) == 1


def test_first_inactive_status_waits_for_first_grant_without_collecting(world):
    def inactive(path, raw):
        response = world.response(path, raw)
        response['consent'] = {'state': 'inactive', 'consent_epoch': None, 'expires_at': None}
        return response
    world.respond = inactive
    result = world.tick()
    assert result['enabled'] and result['result'] == 'waiting'
    assert world.samples == [] and saved(world)['consent_epoch'] is None
    world.respond = world.response
    world.tick(advance=300000)
    assert len(world.samples) == 1


def test_cancel_after_report_admission_fsync_starts_no_http_and_keeps_attempt(world, monkeypatch):
    world.tick()
    write = world.store.write
    cancelled = False
    def cancel(name, value):
        nonlocal cancelled
        result = write(name, value)
        if name == local.NAME and value['pending'] is not None and not cancelled:
            cancelled = True
            # File write hook cannot recursively acquire the diagnostic lock. The
            # next injected authority check models cancellation after its release.
            world.supplier = lambda: (local.disable(world.store, now_ms=world.at), world.target)[1]
        return result
    monkeypatch.setattr(world.store, 'write', cancel)
    world.tick(advance=300000)
    assert len(world.calls) == 1 and saved(world)['enabled'] is False
    assert saved(world)['history'][0]['attempts'] == 1


@pytest.mark.parametrize('where', ['before_send', 'during_send', 'before_provider', 'during_provider'])
def test_local_cancel_races_cannot_store_or_resume_events(world, where):
    if where == 'before_send':
        def current():
            local.disable(world.store, now_ms=world.at)
            return world.target
        world.supplier = current
    elif where == 'during_send':
        def reply(path, raw):
            local.disable(world.store, now_ms=world.at)
            return world.response(path, raw)
        world.respond = reply
    elif where == 'before_provider':
        def current():
            if saved(world)['consent_epoch'] == 1:
                local.disable(world.store, now_ms=world.at)
            return world.target
        world.supplier = current
    else:
        def sample(**kw):
            result = world.sample(**kw)
            local.disable(world.store, now_ms=world.at)
            return result
        world.collector = sample
    try:
        world.tick()
    except local.DiagnosticStateError:
        pass
    assert saved(world)['enabled'] is False and saved(world)['queue'] == []
    assert len(world.samples) == (1 if where == 'during_provider' else 0)
    count = len(world.calls)
    world.tick(advance=300000)
    assert len(world.calls) == count


@pytest.mark.parametrize('field,value', [('device_epoch', 2), ('realm_epoch', 'realm-2'),
                                         ('key_id', UUIDS[5]), ('support_endpoint_hash', 'd' * 64),
                                         ('root_id', UUIDS[6]), ('tenant', 'other')])
def test_current_target_change_prevents_any_http(world, field, value):
    world.target[field] = value
    result = world.tick()
    assert result['enabled'] is False and world.calls == [] and world.samples == []


def test_post_response_authority_failure_keeps_receipt_but_never_collects(world):
    world.tick()
    def current():
        if world.calls[-1][0] == local.REPORT:
            raise ValueError('SYNTHETIC_SECRET')
        return world.target
    world.supplier = current
    with pytest.raises(DiagnosticSenderError, match='^SDC_DIAGNOSTIC_AUTHORITY_UNAVAILABLE$'):
        world.tick(advance=300000)
    assert saved(world)['pending'] is None and saved(world)['history'][0]['result'] == 'accepted'
    assert len(world.samples) == 1


@pytest.mark.parametrize('failure', [TransportError(503), OSError('SYNTHETIC_SECRET'), TimeoutError('deadline')])
def test_uncertain_report_requires_status_then_same_raw_replay(world, failure):
    world.tick()
    world.respond = throws(failure)
    try:
        world.tick(advance=300000)
    except (DiagnosticSenderError, TimeoutError):
        pass
    first = world.calls[-1]
    assert saved(world)['pending']['attempts'] == 1
    world.respond = world.response
    world.tick(advance=300000)
    assert world.calls[-1][0] == local.STATUS
    assert json.loads(world.calls[-1][1])['batch_ids'] == [json.loads(first[1])['batch_id']]
    world.tick(advance=300000)
    assert world.calls[-1] == first
    assert len(world.samples) == 1


@pytest.mark.parametrize('http_status', [400, 401, 403, 409, 429])
def test_observed_4xx_survives_parent_timeout_without_false_retry(world, http_status):
    world.tick()
    def fail(_path, _raw):
        world.refusal_status = http_status
        raise TimeoutError('parent-deadline')
    world.respond = fail
    with pytest.raises(TimeoutError):
        world.tick(advance=300000)
    state = saved(world)
    if http_status == 429:
        assert state['enabled'] and state['pending']['phase'] == 'unknown'
    else:
        assert not state['enabled'] and state['result'] == 'denied'
        assert state['pending'] is None and state['history'][0]['attempts'] == 1


@pytest.mark.parametrize('observed,reported,terminal', [
    (429, 403, True), (403, 429, True), (True, 429, False), (429, False, False),
    ('403', 503, False), (429, '403', False), (False, 403, True), (None, 403, True),
])
def test_terminal_refusal_wins_over_429_but_not_bool_or_string(world, observed, reported, terminal):
    world.tick()
    def fail(_path, _raw):
        world.refusal_status = observed
        raise TransportError(reported)
    world.respond = fail
    world.tick(advance=300000)
    state = saved(world)
    assert state['enabled'] is (not terminal)
    assert state['result'] == ('denied' if terminal else 'unknown')
    if terminal:
        assert state['history'][0]['attempts'] == 1
    else:
        assert state['pending']['attempts'] == 1 and state['pending']['phase'] == 'unknown'


@pytest.mark.parametrize('original', [KeyboardInterrupt('original'), TimeoutError('original'), SystemExit(2)])
@pytest.mark.parametrize('where', ['failure_save', 'failure_clock', 'second_cancel'])
def test_original_control_exception_survives_failure_bookkeeping(world, monkeypatch, original, where):
    world.tick()
    world.at += 300000
    sender = world.sender()
    if where == 'failure_save':
        monkeypatch.setattr(local, 'record_failure', throws(OSError('SYNTHETIC_SECRET')))
    elif where == 'second_cancel':
        monkeypatch.setattr(local, 'record_failure', throws(KeyboardInterrupt('second')))
    def fail(_path, _raw):
        if where == 'failure_clock':
            sender.clock = throws(KeyboardInterrupt('clock-second'))
        raise original
    world.respond = fail
    with world.store.lock(), pytest.raises(type(original)) as caught:
        sender.tick(deadline=world.mono + 10)
    assert caught.value is original
    assert world.store._diagnostic_uncertain is True
    assert saved(world)['pending']['attempts'] == 1 and saved(world)['pending']['phase'] == 'unknown'
    with pytest.raises(local.DiagnosticStateError, match='STORAGE_UNCERTAIN'):
        local.status(world.store, now_ms=world.at)


@pytest.mark.parametrize('original', [KeyboardInterrupt('original'), TimeoutError('original')])
@pytest.mark.parametrize('secondary', [OSError('SYNTHETIC_SECRET'), KeyboardInterrupt('second')])
def test_post_response_failure_preserves_original_control_exception(world, monkeypatch, original, secondary):
    world.tick()
    def current():
        if world.calls[-1][0] == local.REPORT:
            raise original
        return world.target
    world.supplier = current
    monkeypatch.setattr(local, 'record_response', throws(secondary))
    with pytest.raises(type(original)) as caught:
        world.tick(advance=300000)
    assert caught.value is original and world.store._diagnostic_uncertain is True
    assert saved(world)['pending']['phase'] == 'unknown' and saved(world)['history'] == []


@pytest.mark.parametrize('moment', ['before', 'supplier', 'status', 'provider', 'report'])
def test_original_monotonic_deadline_never_renews_or_collects_late(world, moment):
    sender = world.sender()
    deadline = world.mono + 1
    if moment == 'report':
        world.tick()
        world.at += 300000
    if moment == 'before':
        deadline = world.mono
    elif moment == 'supplier':
        def current():
            world.mono += 2
            return world.target
        world.supplier = current
    elif moment in ('status', 'report'):
        def reply(path, raw):
            world.mono += 2
            return world.response(path, raw)
        world.respond = reply
    else:
        def sample(**kw):
            result = world.sample(**kw)
            world.mono += 2
            return result
        world.collector = sample
    with world.store.lock(), pytest.raises(TimeoutError, match='SDC_DIAGNOSTIC_DEADLINE'):
        sender.tick(deadline=deadline)
    assert saved(world)['queue'] == []
    if moment in ('before', 'supplier'):
        assert world.calls == [] and world.samples == []
    elif moment == 'status':
        assert world.samples == [] and saved(world)['consent_epoch'] is None
    elif moment == 'report':
        assert saved(world)['history'][0]['result'] == 'accepted'


@pytest.mark.parametrize('kind', ['extra_id', 'missing_id', 'duplicate_id', 'wrong_epoch', 'wrong_count'])
def test_reply_joins_are_independent_of_closed_dto_validation(world, kind):
    world.tick()
    if kind in ('extra_id', 'missing_id', 'duplicate_id'):
        world.respond = throws(TransportError(503))
        world.tick(advance=300000)
    def reply(path, raw):
        result = world.response(path, raw)
        if kind == 'extra_id':
            result['batches'][0]['batch_id'] = UUIDS[6]
        elif kind == 'missing_id':
            result['batches'] = []
        elif kind == 'duplicate_id':
            result['batches'] *= 2
        elif kind == 'wrong_epoch':
            result['consent_epoch'] = 2
        else:
            result['event_count'] += 1
        return result
    world.respond = reply
    with pytest.raises(DiagnosticSenderError, match='RESPONSE_INVALID'):
        world.tick(advance=300000)
    assert saved(world)['pending']['phase'] == 'unknown' and not saved(world)['history']


def test_reservation_write_failure_starts_no_http(world, monkeypatch):
    world.tick()
    original = world.store.write
    def fail(name, value):
        if name == local.NAME and value['pending'] is not None:
            raise OSError('SYNTHETIC_SECRET')
        return original(name, value)
    monkeypatch.setattr(world.store, 'write', fail)
    with pytest.raises(local.DiagnosticStateError, match='STORAGE_UNCERTAIN'):
        world.tick(advance=300000)
    assert len(world.calls) == 1 and len(saved(world)['queue']) == 1


def test_ack_write_failure_reopens_to_reserved_unknown_not_new_id(world, monkeypatch):
    world.tick()
    original = world.store.write
    def fail(name, value):
        if name == local.NAME and value['history']:
            raise OSError('SYNTHETIC_SECRET')
        return original(name, value)
    monkeypatch.setattr(world.store, 'write', fail)
    with pytest.raises(DiagnosticSenderError, match='RESPONSE_INVALID'):
        world.tick(advance=300000)
    pending = saved(world)['pending']
    assert pending['attempts'] == 1 and pending['phase'] == 'unknown'
    assert pending['raw'].encode() == world.calls[-1][1]


def test_first_deadline_during_ack_save_preserves_instance_and_unknown_hold(world, monkeypatch):
    world.tick()
    original_write = world.store.write
    deadline = TimeoutError('synthetic-parent-deadline')
    def fail(name, value):
        if name == local.NAME and value['history']:
            raise deadline
        return original_write(name, value)
    monkeypatch.setattr(world.store, 'write', fail)
    with pytest.raises(TimeoutError) as caught:
        world.tick(advance=300000)
    assert caught.value is deadline and world.store._diagnostic_uncertain is True
    pending = saved(world)['pending']
    assert pending['attempts'] == 1 and pending['phase'] == 'unknown'
    assert pending['raw'].encode() == world.calls[-1][1]
    assert saved(world)['history'] == [] and len(world.calls) == 2
    with pytest.raises(local.DiagnosticStateError, match='STORAGE_UNCERTAIN'):
        world.tick(advance=300000)
    assert len(world.calls) == 2


def test_provider_output_never_escapes_fixed_dto_or_exception(world):
    def contaminated(**kw):
        value = world.sample(**kw)
        value['events'][0]['exception_text'] = 'SYNTHETIC_PRIVATE_TEXT'
        return value
    world.collector = contaminated
    with pytest.raises(ValueError, match='^SDC_DIAGNOSTIC_') as caught:
        world.tick()
    assert 'SYNTHETIC_PRIVATE_TEXT' not in str(caught.value)
    assert b'SYNTHETIC_PRIVATE_TEXT' not in (world.store.root / local.NAME).read_bytes()
    assert saved(world)['queue'] == []


def test_explicit_renew_reconciles_old_metadata_and_samples_only_new_receipt_interval(world):
    world.tick()
    world.respond = throws(TransportError(503))
    world.tick(advance=300000)
    old_raw = world.calls[-1][1]
    old = saved(world)
    old_id = old['pending']['batch_id']
    local.disable(world.store, now_ms=world.at)
    count = len(world.calls)
    world.tick(advance=300000)
    assert len(world.calls) == count
    world.at += 1
    local.renew(world.store, binding=world.target, now_ms=world.at, expires_at=world.at + DAY,
                confirm_human=True, reason='explicit-renew')
    world.respond = world.response
    result = world.tick()
    assert world.calls[-1][0] == local.STATUS
    assert json.loads(world.calls[-1][1])['batch_ids'] == [old_id]
    assert result['consent_epoch'] == 1 and result['local_receipt_id'] != old['receipt']['local_receipt_id']
    assert len(world.samples) == 2 and world.samples[-1] == {}
    world.tick(advance=300000)
    assert world.calls[-1][0] == local.REPORT and world.calls[-1][1] != old_raw
    assert json.loads(world.calls[-1][1])['batch_id'] != old_id
    previous = next(row for row in saved(world)['history'] if row['batch_id'] == old_id)
    assert previous['result'] == 'unknown' and 'raw' not in previous
    assert previous['local_receipt_id'] == old['receipt']['local_receipt_id']


def test_expired_unknown_payload_is_status_only_and_never_rebuilt(world):
    world.tick()
    world.respond = throws(TransportError(503))
    world.tick(advance=300000)
    old_id = json.loads(world.calls[-1][1])['batch_id']
    world.at = NOW + 7 * DAY + 1
    world.respond = world.response
    def no_new_observation(**kw):
        return {'events': [], 'dropped_events': 0, 'partial': True,
                'observed_at': kw['now_ms'], 'source_marks': {}}
    world.collector = no_new_observation
    for advance in (0, 300000):
        world.tick(advance=advance)
        assert world.calls[-1][0] == local.STATUS
        assert json.loads(world.calls[-1][1])['batch_ids'] == [old_id]
        assert saved(world)['pending'] is None and saved(world)['queue'] == []
    assert sum(path == local.REPORT for path, _ in world.calls) == 1


def unfinished_marker(store):
    """A real pending credential intent for this root, appearing after the sender holds its auth."""
    import uuid
    from adapters.sdc import credential_mutation as cm
    ids = dict.fromkeys(('claim_id', 'device_id', 'key_id', 'rotation_id'))
    cm.write_marker(store.fd, {
        'schema_version': cm.SCHEMA, 'mode': 'keychain', 'account': str(uuid.uuid4()),
        'root_fingerprint': store.root_fingerprint(), 'endpoint_hash': store._endpoint_hash(),
        'tick_lock_fingerprint': cm.tick_lock_fingerprint(store._tick_fd),
        'mutation': {'id': str(uuid.uuid4()), 'op': 'write', 'purpose': 'initial-key', 'phase': 'pending',
                     'old_sha256': None, 'new_sha256': '0' * 64, 'started_at': 1,
                     'context': {'before': ids, 'after': {**ids, 'claim_id': str(uuid.uuid4())}, 'sequence': 0,
                                 'sending_sha256': None, 'stop_before_sha256': '0' * 64},
                     'approval': {'receipt_id': str(uuid.uuid4()), 'confirmed_at': 1, 'reason': 'synthetic'},
                     'verified_at': None, 'completed_at': None}}, expected_sha256=None)


def test_managed_report_admitted_before_an_unfinished_intent_is_never_sent(managed_world):
    w = managed_world
    assert w.tick()['queued_events'] == 1
    calls, admitted = len(w.calls), []

    def appear():
        # The report admission is durable and the actual credential/signing material is in RAM.
        admitted.append(copy.deepcopy(saved(w)['pending']))
        unfinished_marker(w.store)
    w.before_transport = appear
    # The sender keeps its fixed public code; the cause is the real Store guard below.
    with pytest.raises(DiagnosticSenderError, match='^SDC_DIAGNOSTIC_UNAVAILABLE$'):
        w.tick(advance=300000)
    assert len(w.targets) == 2  # The real send closure was reached with the actual auth.
    assert len(w.calls) == calls and [path for path, *_ in w.signed] == [local.STATUS]
    with pytest.raises(ValueError, match='^SDC_CREDENTIAL_MUTATION_UNRESOLVED$'):
        w.store.guard_credential()
    reserved, after = admitted[0], saved(w)['pending']
    assert reserved is not None
    # The same admitted raw and its batch stay reserved for a later same-raw status/replay; never cleared or reused.
    assert after is not None and after['raw'] == reserved['raw']
    assert {key: after[key] for key in reserved if key not in ('attempts', 'last_result', 'unknown')} == \
        {key: reserved[key] for key in reserved if key not in ('attempts', 'last_result', 'unknown')}
    assert after['attempts'] == reserved['attempts'] == 1 and after['phase'] == 'unknown'
