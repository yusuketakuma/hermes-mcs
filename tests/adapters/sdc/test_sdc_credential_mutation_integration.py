"""Exercise real mutation callers with private synthetic files and an owned-native fake."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import sdc_cli
from adapters.sdc import credential_mutation as mutations
from adapters.sdc import storage
from adapters.sdc.connector import Connector
from adapters.sdc.keychain import KeychainError
from adapters.sdc.protocol import CUSTOMER_CONSENT
from adapters.sdc.storage import Store
from adapters.sdc.transport import TransportError
from test_sdc_connector import CONFIG, FACTS, NOW, Fake as MonitorPeer

REASON = 'synthetic credential mutation integration'
FLAGS = ['--confirm-human', '--reason', REASON]
IDS = ('claim_id', 'device_id', 'key_id', 'rotation_id')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(',', ':'), allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identities(state):
    return {name: state.get(name) for name in IDS}


def forbidden(*_args, **_kwargs):
    pytest.fail('real credential, process or network boundary was reached')


class Peer(MonitorPeer):
    """Reuse the independent synthetic HMAC peer, with customer consent only."""

    def send(self, path, raw, headers=None):
        result = super().send(path, raw, headers)
        if path.startswith('/v2/enrollments/') and not path.endswith('/claim'):
            result = {**result, 'consent_revision': CUSTOMER_CONSENT}
        return result


class NativeOwner:
    """Only native calls are fake; the pending lease and verified CAS are real."""

    def __init__(self, world, account):
        self.world, self.account = world, account
        self.closed = False

    def read(self, *, deadline=None):
        self.world.reads.append((self.account, deadline))
        self.world.events.append(('read', deadline))
        if self.account not in self.world.items:
            raise KeychainError('SDC_KEYCHAIN_NOT_FOUND')
        return self.world.items[self.account]

    def write(self, raw, **kwargs):
        return self._mutate(raw, **kwargs)

    def delete(self, **kwargs):
        return self._mutate(None, **kwargs)

    def _mutate(self, raw, *, intent_id, marker_sha256, root_fd, tick_fd, deadline):
        world = self.world
        assert type(deadline) in (int, float)
        marker = mutations.pending_marker(root_fd, tick_fd, intent_id=intent_id,
                                          marker_sha256=marker_sha256, account=self.account)
        operation = marker['mutation']
        assert operation['phase'] == 'pending'
        assert operation['old_sha256'] == (digest(json.loads(world.items[self.account]))
                                          if self.account in world.items else None)
        assert operation['new_sha256'] == (digest(json.loads(raw)) if raw is not None else None)
        assert operation['context']['before'] == identities(world.state())
        pending = [event for event in world.events if event[0] == 'marker'
                   and event[1]['mutation']['id'] == intent_id]
        assert pending and pending[-1][1] == marker
        assert ('fsync', 'marker:pending', 'directory') in world.events
        world.events.append(('native', deepcopy(marker)))
        world.native.append(deepcopy(marker))
        if world.fail == (operation['purpose'], 'before-native'):
            raise world.failure
        if raw is None:
            world.items.pop(self.account, None)
        else:
            world.items[self.account] = raw
        # A changed value alone is not proof that cleanup/native use has ended.
        if world.fail == (operation['purpose'], 'after-native'):
            raise world.failure
        result = mutations.mark_native_verified(root_fd, tick_fd, intent_id=intent_id,
                                               marker_sha256=marker_sha256, now_ms=world.now_ms)
        world.events.append(('native-settled', deepcopy(result)))
        if world.fail == (operation['purpose'], 'after-verified'):
            raise world.failure
        return {'intent_id': intent_id, 'marker_sha256': mutations.marker_sha256(result)}

    def close(self):
        self.closed = True


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    result = SimpleNamespace(root=root, items={}, reads=[], native=[], events=[], jobs=[],
                             now_ms=NOW * 1000, fail=None,
                             failure=TimeoutError('synthetic interrupted mutation'),
                             fsync_fail=None, scope=None, peer=Peer(), owners=[])

    def state():
        path = root / 'state.json'
        return json.loads(path.read_bytes()) if path.exists() else {}

    def marker():
        return json.loads((root / mutations.NAME).read_bytes())

    def owner(account):
        selected = NativeOwner(result, account)
        result.owners.append(selected)
        return selected

    class SyntheticStore(Store):
        def __init__(self, path, **kwargs):
            super().__init__(path, keychain_factory=owner, **kwargs)

    result.state, result.marker, result.open = state, marker, lambda: SyntheticStore(root)
    result.connector = lambda store: Connector(store, result.peer, clock=lambda: result.now_ms / 1000)

    original_marker = mutations.write_marker
    original_write = Store._write_file
    original_fsync = os.fsync

    def write_marker(root_fd, value, *, expected_sha256):
        phase = value['mutation']['phase'] if value.get('mutation') else 'empty'
        previous, result.scope = result.scope, 'marker:' + phase
        try:
            saved = original_marker(root_fd, value, expected_sha256=expected_sha256)
            result.events.append(('marker', deepcopy(saved)))
            return saved
        finally:
            result.scope = previous

    def write_file(store, name, value):
        previous, result.scope = result.scope, name
        try:
            original_write(store, name, value)
            if name == 'state.json':
                result.events.append(('state', deepcopy(value)))
        finally:
            result.scope = previous

    def fsync(fd):
        kind = 'directory' if os.path.samestat(os.fstat(fd), root.stat()) else 'file'
        if result.fsync_fail == (result.scope, kind):
            raise result.failure
        original_fsync(fd)
        result.events.append(('fsync', result.scope, kind))

    # Install tracing only after private root creation; no live files are opened.
    with SyntheticStore(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
        store.enable_customer()
    monkeypatch.setattr(mutations, 'write_marker', write_marker)
    monkeypatch.setattr(Store, '_write_file', write_file)
    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(sdc_cli, 'Store', SyntheticStore)
    monkeypatch.setattr(sdc_cli, 'Transport', lambda _config: result.peer)
    monkeypatch.setattr(sdc_cli.time, 'time', lambda: result.now_ms / 1000)
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config',
                        lambda: {'sdc': {'state_root': str(root)}, 'runtime_mode': 'standalone'})
    monkeypatch.setattr(sdc_cli.getpass, 'getpass', lambda _: 'SYNTHETIC_ONE_TIME_TOKEN')
    monkeypatch.setattr('builtins.input', lambda _: '1')
    monkeypatch.setattr(sdc_cli, 'service', lambda enabled, path, **kw: result.jobs.append((enabled, Path(path))))
    monkeypatch.setattr(storage, 'KeychainCredentialStore', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    result.peer.state = 'admin_approved'
    return result


def authorize(store, world, operation):
    store.authorize_credential_mutation(operation=operation, confirm_human=True,
                                        reason=REASON, now_ms=world.now_ms)


def activate(world):
    with world.open() as store, store.lock():
        authorize(store, world, 'connect')
        connector = world.connector(store)
        connector.connect('SYNTHETIC_ONE_TIME_TOKEN')
        connector.confirm()
    assert world.state()['state'] == 'active'
    world.events.clear()
    world.native.clear()
    world.peer.calls.clear()


def rotate(world):
    with world.open() as store, store.lock():
        authorize(store, world, 'rotate-key')
        return world.connector(store).rotate()


def completed(world, purpose):
    values = [event[1] for event in world.events if event[0] == 'marker'
              and event[1]['mutation']['purpose'] == purpose]
    assert [value['mutation']['phase'] for value in values] == ['pending', 'verified', 'complete']
    pending, verified, complete = values
    intent = pending['mutation']['id']
    assert {value['mutation']['id'] for value in values} == {intent}
    assert all(value['mutation']['context'] == pending['mutation']['context'] for value in values)
    assert pending['mutation']['approval']['reason'] == REASON
    assert complete['mutation']['completed_at'] >= verified['mutation']['verified_at']
    indices = {phase: next(index for index, event in enumerate(world.events)
                          if event[0] == 'marker' and event[1]['mutation']['id'] == intent
                          and event[1]['mutation']['phase'] == phase)
               for phase in ('pending', 'verified', 'complete')}
    native_index = next(index for index, event in enumerate(world.events)
                        if event[0] == 'native' and event[1]['mutation']['id'] == intent)
    assert indices['pending'] < native_index < indices['verified'] < indices['complete']
    between = world.events[indices['verified'] + 1:indices['complete']]
    assert any(event[0] == 'state' and identities(event[1]) == pending['mutation']['context']['after']
               for event in between)
    assert ('fsync', 'state.json', 'directory') in between
    for phase in ('pending', 'verified', 'complete'):
        assert ('fsync', 'marker:' + phase, 'file') in world.events
        assert ('fsync', 'marker:' + phase, 'directory') in world.events
    return complete['mutation']


def test_connect_owns_initial_and_claim_id_mutations_before_network_identity_publication(world):
    with world.open() as store, store.lock():
        authorize(store, world, 'connect')
        result = world.connector(store).connect('SYNTHETIC_ONE_TIME_TOKEN')
    first = completed(world, 'initial-key')
    claim = completed(world, 'claim-key-id')
    assert first['context']['before'] == dict.fromkeys(IDS)
    assert first['context']['after'] == {**dict.fromkeys(IDS), 'claim_id': result['claim_id']}
    assert claim['context']['before'] == first['context']['after']
    assert claim['context']['after'] == {'claim_id': result['claim_id'], 'device_id': world.peer.device_id,
                                         'key_id': world.peer.key_id, 'rotation_id': None}
    assert world.state()['enabled'] is False
    assert not (world.root / 'credential').exists()
    marker_raw = (world.root / mutations.NAME).read_text()
    assert 'SYNTHETIC_ONE_TIME_TOKEN' not in marker_raw
    assert world.peer.pending_key not in marker_raw


def test_rotation_preserves_immutable_send_and_publishes_new_key_before_finalizing(world):
    activate(world)
    with world.open() as store:
        state = store.read('state.json')
        state.update(sequence=7, sending='{"synthetic":"immutable pending heartbeat"}', enabled=False)
        store.write('state.json', state)
    before = world.state()
    assert rotate(world) is True
    candidate = completed(world, 'rotation-candidate')
    key_id = completed(world, 'rotation-key-id')
    final = completed(world, 'rotation-finalize')
    assert candidate['context']['before'] == identities(before)
    rotation_id = candidate['context']['after']['rotation_id']
    assert rotation_id and candidate['context']['after'] == {**identities(before), 'rotation_id': rotation_id}
    assert key_id['context']['before'] == key_id['context']['after'] == candidate['context']['after']
    assert final['context']['before'] == {**candidate['context']['after'], 'key_id': world.peer.next_id}
    assert final['context']['after'] == {**final['context']['before'], 'rotation_id': None}
    for intent in (candidate, key_id, final):
        assert intent['context']['sequence'] == 7
        assert intent['context']['sending_sha256'] == hashlib.sha256(before['sending'].encode()).hexdigest()
    assert world.state()['sequence'] == 7 and world.state()['sending'] == before['sending']
    assert world.state()['enabled'] is False
    assert world.state()['key_id'] == world.peer.next_id
    assert 'rotation_id' not in world.state()
    native = json.loads(next(iter(world.items.values())))
    assert native['key_id'] == world.peer.next_id and native['completed_rotation_id'] == rotation_id
    assert 'next_key' not in native and 'next_key_id' not in native


def test_revoked_rotation_replaces_candidate_once_without_changing_active_identity(world):
    activate(world)
    world.peer.lose = '/v2/keys/prepare'
    with pytest.raises(TransportError):
        rotate(world)
    previous_state = world.state()
    previous_value = json.loads(next(iter(world.items.values())))
    world.events.clear()
    world.peer.rotation = 'revoked'
    assert rotate(world) is False
    replacement = completed(world, 'rotation-replacement')
    assert replacement['context']['before'] == identities(previous_state)
    after = replacement['context']['after']
    assert after['rotation_id'] != previous_state['rotation_id']
    assert {name: after[name] for name in IDS[:3]} == {name: previous_state[name] for name in IDS[:3]}
    current = json.loads(next(iter(world.items.values())))
    assert current['key'] == previous_value['key'] and current['key_id'] == previous_value['key_id']
    assert current['next_key'] != previous_value['next_key']
    assert 'next_key_id' not in current


def test_actual_cli_reenrollment_has_new_claim_but_retains_prior_identity_in_receipt(world):
    activate(world)
    with world.open() as store:
        state = store.read('state.json')
        state.update(state='suspended', enabled=False, disconnected=True, sequence=9)
        store.write('state.json', state)
    before = world.state()
    world.peer.state = 'admin_approved'
    assert sdc_cli.main(['connect', '--state-root', str(world.root), *FLAGS]) == 0
    intent = completed(world, 'reenroll-key')
    assert intent['context']['before'] == identities(before)
    assert intent['context']['after'] == {**dict.fromkeys(IDS), 'claim_id': world.state()['claim_id']}
    assert intent['context']['after']['claim_id'] != before['claim_id']
    assert intent['context']['sequence'] == 9
    assert world.state()['sequence'] == 0
    assert world.state()['enabled'] is True
    assert [event['mutation']['purpose'] for event in world.native] == ['reenroll-key', 'claim-key-id']


def test_actual_cli_delete_saves_stop_before_native_and_keeps_receipt_and_identity(world):
    activate(world)
    before = world.state()
    assert sdc_cli.main(['disconnect', '--state-root', str(world.root), *FLAGS]) == 0
    intent = completed(world, 'delete')
    assert intent['context']['before'] == intent['context']['after'] == identities(before)
    assert intent['new_sha256'] is None
    assert world.items == {}
    assert world.state()['enabled'] is False and world.state()['disconnected'] is True
    assert identities(world.state()) == identities(before)
    assert world.marker()['mutation']['phase'] == 'complete'
    assert world.jobs == [(False, world.root)]
    native_index = next(i for i, row in enumerate(world.events) if row[0] == 'native')
    assert any(row[0] == 'state' and row[1].get('disconnected') is True
               for row in world.events[:native_index])


@pytest.mark.parametrize('failure_at', ['before-native', 'after-native'])
def test_pending_reopen_does_not_turn_equal_native_value_into_completion_or_new_attempt(world, monkeypatch, failure_at):
    world.fail = ('initial-key', failure_at)
    with world.open() as store, store.lock():
        authorize(store, world, 'connect')
        with pytest.raises(TimeoutError) as caught:
            world.connector(store).connect('SYNTHETIC_ONE_TIME_TOKEN')
        assert caught.value is world.failure
    pending = world.marker()
    assert pending['mutation']['phase'] == 'pending'
    assert ('state.json' in {path.name for path in world.root.iterdir()}) is False
    reads, native, network = len(world.reads), len(world.native), len(world.peer.calls)
    world.fail = None
    with world.open() as store, store.lock():
        monkeypatch.setattr('adapters.sdc.connector.secrets.token_hex', forbidden)
        monkeypatch.setattr('adapters.sdc.connector.uuid.uuid4', forbidden)
        with pytest.raises(ValueError):
            world.connector(store).connect('SYNTHETIC_ONE_TIME_TOKEN')
        with pytest.raises(ValueError):
            store.reconcile_credential(confirm_human=True, reason=REASON, now_ms=world.now_ms)
    assert world.marker() == pending
    assert (len(world.reads), len(world.native), len(world.peer.calls)) == (reads, native, network)


def test_verified_reopen_needs_explicit_current_read_and_publishes_only_original_claim(world):
    world.fail = ('initial-key', 'after-verified')
    with world.open() as store, store.lock():
        authorize(store, world, 'connect')
        with pytest.raises(TimeoutError) as caught:
            world.connector(store).connect('SYNTHETIC_ONE_TIME_TOKEN')
        assert caught.value is world.failure
    verified = world.marker()
    assert verified['mutation']['phase'] == 'verified'
    reads, native, calls = len(world.reads), len(world.native), len(world.peer.calls)
    world.fail = None
    with world.open() as store, store.lock():
        with pytest.raises(ValueError):
            store.read('credential')
        assert len(world.reads) == reads
        store.reconcile_credential(confirm_human=True, reason=REASON, now_ms=world.now_ms)
    assert len(world.reads) == reads + 1
    assert len(world.native) == native and len(world.peer.calls) == calls
    assert world.marker()['mutation']['id'] == verified['mutation']['id']
    assert world.marker()['mutation']['phase'] == 'complete'
    assert identities(world.state()) == verified['mutation']['context']['after']
    assert world.state()['enabled'] is False


@pytest.mark.parametrize('boundary', ['marker:pending', 'marker:verified', 'state.json', 'marker:complete'])
def test_fsync_failure_preserves_original_exception_and_never_repeats_native_on_same_store(world, boundary):
    world.fsync_fail = (boundary, 'directory')
    with world.open() as store, store.lock():
        authorize(store, world, 'connect')
        with pytest.raises(TimeoutError) as caught:
            world.connector(store).connect('SYNTHETIC_ONE_TIME_TOKEN')
        assert caught.value is world.failure
        native, calls = len(world.native), len(world.peer.calls)
        assert native == (0 if boundary == 'marker:pending' else 1)
        assert calls == 0
        world.fsync_fail = None
        with pytest.raises(ValueError):
            world.connector(store).connect('SYNTHETIC_ONE_TIME_TOKEN')
        assert len(world.native) == native and len(world.peer.calls) == calls


def test_v2_read_compatibility_upgrades_only_at_an_explicit_mutation(world):
    activate(world)
    current = world.marker()
    v2 = {key: current[key] for key in ('mode', 'account', 'root_fingerprint', 'endpoint_hash')}
    v2['schema_version'] = 'sdc.credential-store.v2'
    # Historical disk bytes are a fixture, never a product downgrade operation.
    path = world.root / mutations.NAME
    path.write_bytes(canonical(v2))
    with world.open() as store:
        assert store.read('credential')['key_id'] == world.peer.key_id
        assert world.marker() == v2
        with store.lock():
            with pytest.raises(ValueError):
                world.connector(store).rotate()
    assert world.marker() == v2
    assert rotate(world) is True
    assert world.marker()['schema_version'] == 'sdc.credential-store.v3'
    completed(world, 'rotation-candidate')


def test_pending_active_identity_holds_tick_but_metadata_status_and_stop_need_no_key(world, monkeypatch):
    activate(world)
    world.fail = ('rotation-candidate', 'after-native')
    with pytest.raises(TimeoutError):
        rotate(world)
    before = world.marker()
    world.fail = None
    reads, native, calls = len(world.reads), len(world.native), len(world.peer.calls)
    with world.open() as store, monkeypatch.context() as blocked:
        blocked.setattr('adapters.sdc.connector.uuid.uuid4', forbidden)
        blocked.setattr('adapters.sdc.connector.secrets.token_hex', forbidden)
        with pytest.raises(ValueError):
            world.connector(store).tick(FACTS)
    assert sdc_cli.main(['connection', '--state-root', str(world.root)]) == 0
    monkeypatch.setattr('builtins.input', lambda _: '2')
    assert sdc_cli.main(['disconnect', '--state-root', str(world.root)]) == 0
    assert world.state()['enabled'] is False and world.state()['disconnected'] is True
    assert world.marker() == before
    assert (len(world.reads), len(world.native), len(world.peer.calls)) == (reads, native, calls)
    assert world.jobs == [(False, world.root)]


def test_verified_reconcile_never_overwrites_a_later_local_stop(world):
    activate(world)
    world.fail = ('rotation-candidate', 'after-verified')
    with pytest.raises(TimeoutError):
        rotate(world)
    verified = world.marker()
    world.fail = None
    with world.open() as store:
        state = store.read('state.json')
        state.update(enabled=False, disconnected=True)
        store.write('state.json', state)
    stopped = world.state()
    native, network = len(world.native), len(world.peer.calls)
    with world.open() as store, store.lock():
        with pytest.raises(ValueError):
            store.reconcile_credential(confirm_human=True, reason=REASON, now_ms=world.now_ms)
    assert world.state() == stopped
    # Only the explicit reconcile approval is added; the original intent and approval are unchanged.
    assert {key: item for key, item in world.marker().items() if key != 'reconcile'} == verified
    assert world.marker()['reconcile']['reason'] == REASON
    assert len(world.native) == native and len(world.peer.calls) == network


@pytest.mark.parametrize('drift', ['sequence', 'sending'])
def test_verified_reconcile_holds_when_the_send_progress_moved_on(world, drift):
    activate(world)
    world.fail = ('rotation-candidate', 'after-verified')
    with pytest.raises(TimeoutError):
        rotate(world)
    verified, world.fail = world.marker(), None
    with world.open() as store:
        state = store.read('state.json')
        if drift == 'sequence':
            state['sequence'] = state.get('sequence', 0) + 1
        else:
            state['sending'] = '{"synthetic":"a later immutable heartbeat"}'
        store.write('state.json', state)
    moved = world.state()
    with world.open() as store, store.lock():
        with pytest.raises(ValueError, match='SDC_CREDENTIAL_MUTATION_HELD'):
            store.reconcile_credential(confirm_human=True, reason=REASON, now_ms=world.now_ms)
    assert world.state() == moved
    assert world.marker()['mutation'] == verified['mutation']


def test_reconcile_approval_is_durable_before_the_copy_and_a_failed_save_poisons(world):
    world.fail = ('initial-key', 'after-verified')
    with world.open() as store, store.lock():
        authorize(store, world, 'connect')
        with pytest.raises(TimeoutError):
            world.connector(store).connect('SYNTHETIC_ONE_TIME_TOKEN')
    verified, world.fail = world.marker(), None
    world.events.clear()
    with world.open() as store, store.lock():
        store.reconcile_credential(confirm_human=True, reason=REASON, now_ms=world.now_ms)
    receipt = next(i for i, row in enumerate(world.events) if row[0] == 'marker' and 'reconcile' in row[1])
    copy = next(i for i, row in enumerate(world.events) if row[0] == 'read')
    assert receipt < copy and ('fsync', 'marker:verified', 'directory') in world.events[:copy]
    assert world.marker()['reconcile']['intent_id'] == verified['mutation']['id']
    assert world.marker()['mutation']['approval'] == verified['mutation']['approval']
    # A second context whose receipt save fails is poisoned and never copies.
    world.fail = ('rotation-candidate', 'after-verified')
    with world.open() as store, store.lock():
        authorize(store, world, 'connect')
        connector = world.connector(store)
        connector.connect('SYNTHETIC_ONE_TIME_TOKEN')
        connector.confirm()
    with pytest.raises(TimeoutError):
        rotate(world)
    world.fail, world.fsync_fail = None, ('marker:verified', 'directory')
    reads = len(world.reads)
    with world.open() as store, store.lock():
        with pytest.raises(TimeoutError):
            store.reconcile_credential(confirm_human=True, reason=REASON, now_ms=world.now_ms)
        world.fsync_fail = None
        with pytest.raises(ValueError, match='SDC_CREDENTIAL_UNCERTAIN'):
            store.reconcile_credential(confirm_human=True, reason=REASON, now_ms=world.now_ms)
    assert len(world.reads) == reads


def test_identity_changed_after_confirmation_is_held_before_any_copy_or_native(world):
    activate(world)
    with world.open() as store:
        authorize(store, world, 'rotate-key')
        changed = {**store.read('state.json'), 'key_id': '44444444-4444-4444-8444-444444444444'}
        # Another writer changed the identity between the human confirmation and the lock.
        store.write('state.json', changed)
        reads, native = len(world.reads), len(world.native)
        with store.lock(), pytest.raises(ValueError, match='SDC_CREDENTIAL_APPROVAL_STALE'):
            store.write('credential', {'key': '03' * 32}, mutation={'purpose': 'rotation-candidate', 'after': {
                **identities(changed), 'rotation_id': '55555555-5555-4555-8555-555555555555'}})
    assert (len(world.reads), len(world.native)) == (reads, native)
    assert world.state() == changed


@pytest.mark.parametrize('late', ['first-copy', 'after-native', 'second-writer'])
def test_one_scope_deadline_is_never_extended_and_a_late_result_is_not_adopted(world, monkeypatch, late):
    clock = [1000.0]
    monkeypatch.setattr(storage, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=storage.time.time))
    original, read = NativeOwner._mutate, NativeOwner.read

    def slow_read(owner, **kwargs):
        if late == 'first-copy' and not owner.world.reads:
            clock[0] += 10  # The connector's first ordinary copy already spends the whole budget.
        return read(owner, **kwargs)
    monkeypatch.setattr(NativeOwner, 'read', slow_read)

    def slow(owner, raw, **lease):
        result = original(owner, raw, **lease)
        if late == 'after-native':
            clock[0] += 10
        return result
    monkeypatch.setattr(NativeOwner, '_mutate', slow)
    send = world.peer.send

    def slow_claim(path, raw, headers=None):
        if late == 'second-writer' and path.endswith('/claim'):
            clock[0] += 10  # The claim HTTP is inside the same scope; the second writer cannot restart it.
        return send(path, raw, headers)
    monkeypatch.setattr(world.peer, 'send', slow_claim)
    with world.open() as store, store.lock():
        authorize(store, world, 'connect')
        with pytest.raises(ValueError, match='SDC_CREDENTIAL_DEADLINE'):
            world.connector(store).connect('SYNTHETIC_ONE_TIME_TOKEN')
    # Inside the approved scope even the first ordinary copy carries the one 5 s bound.
    assert {deadline for _account, deadline in world.reads} == {1005.0}
    if late == 'first-copy':
        assert world.native == [] and world.marker()['mutation'] is None
    elif late == 'after-native':
        assert world.marker()['mutation']['phase'] == 'verified' and not (world.root / 'state.json').exists()
    else:
        assert world.marker()['mutation']['purpose'] == 'initial-key'
        assert world.marker()['mutation']['phase'] == 'complete'
        assert [row['mutation']['purpose'] for row in world.native] == ['initial-key']


def test_cli_reconcile_entry_adopts_only_a_verified_intent_and_then_stops(world, capsys):
    activate(world)
    world.fail = ('rotation-candidate', 'after-verified')
    with pytest.raises(TimeoutError):
        rotate(world)
    world.fail = None
    native, calls, reads = len(world.native), len(world.peer.calls), len(world.reads)
    assert sdc_cli.main(['rotate-key', '--state-root', str(world.root)]) == 1
    # The ordinary change flags alone never complete a recorded intent.
    assert sdc_cli.main(['rotate-key', '--state-root', str(world.root), *FLAGS]) == 1
    assert len(world.reads) == reads and world.marker()['mutation']['phase'] == 'verified'
    assert sdc_cli.main(['rotate-key', '--state-root', str(world.root), '--reconcile-credential', *FLAGS]) == 0
    assert world.marker()['mutation']['phase'] == 'complete'
    assert world.marker()['reconcile']['reason'] == REASON
    assert len(world.reads) == reads + 1
    assert (len(world.native), len(world.peer.calls)) == (native, calls)
    assert world.state()['enabled'] is True and world.jobs == []
    assert '照合し' in capsys.readouterr().out


def test_cli_pending_intent_is_held_without_any_copy(world, capsys):
    activate(world)
    world.fail = ('rotation-candidate', 'after-native')
    with pytest.raises(TimeoutError):
        rotate(world)
    world.fail = None
    pending, reads, calls = world.marker(), len(world.reads), len(world.peer.calls)
    for command in ('rotate-key', 'connect', 'disconnect'):
        assert sdc_cli.main([command, '--state-root', str(world.root), '--reconcile-credential', *FLAGS]) == 1
    for command in ('rotate-key', 'connect'):
        assert sdc_cli.main([command, '--state-root', str(world.root), *FLAGS]) == 1
    assert world.marker() == pending
    assert (len(world.reads), len(world.peer.calls)) == (reads, calls)
    assert '自動で再試行' in capsys.readouterr().err


def pending_rotation(world):
    activate(world)
    world.fail = ('rotation-candidate', 'after-native')
    with pytest.raises(TimeoutError):
        rotate(world)
    world.fail = None
    assert world.marker()['mutation']['phase'] == 'pending'


def test_cached_key_r1_request_is_refused_once_an_intent_is_unfinished(world):
    activate(world)
    with world.open() as store:
        connector = world.connector(store)
        state = store.read('state.json')
        key = connector.secret(state, store.read('credential'))
    # The intent appears after the key was already in RAM.
    world.fail = ('rotation-candidate', 'after-native')
    with pytest.raises(TimeoutError):
        rotate(world)
    calls = len(world.peer.calls)
    with world.open() as store:
        with pytest.raises(ValueError, match='SDC_CREDENTIAL_MUTATION_UNRESOLVED'):
            world.connector(store).request('/v2/heartbeats', {'synthetic': True}, state, key)
    assert len(world.peer.calls) == calls


def test_tick_entry_holds_every_sender_without_copy_or_network(world, capsys):
    pending_rotation(world)
    reads, calls, before = len(world.reads), len(world.peer.calls), world.state()
    assert sdc_cli.tick(world.root) == 0
    assert (len(world.reads), len(world.peer.calls)) == (reads, calls)
    assert world.state() == before
    assert 'SDC_CREDENTIAL_MUTATION_UNRESOLVED' in capsys.readouterr().err


@pytest.mark.parametrize('client', ['support', 'restart'])
def test_support_and_restart_cached_auth_send_is_guarded_after_signing(world, monkeypatch, client):
    from adapters.sdc import restart_client, support_client
    pending_rotation(world)
    module = support_client if client == 'support' else restart_client
    monkeypatch.setattr(module, 'sign_support' if client == 'support' else 'sign_restart', lambda *_a, **_k: {})
    if client == 'restart':
        monkeypatch.setattr(restart_client.control, 'validate_request', lambda *_a, **_k: None)
    raw = '{"synthetic":true}'
    record = {'raw': raw, 'body_hash': hashlib.sha256(raw.encode()).hexdigest()}
    with world.open() as store:
        auth = {'endpoint': {}, 'identity': {}, 'key': '0' * 64, 'guard': store.guard_credential}
        with pytest.raises(ValueError, match='SDC_CREDENTIAL_MUTATION_UNRESOLVED'):
            module._send('/synthetic', record, auth, forbidden, lambda: NOW, lambda: None)


@pytest.mark.parametrize('module,dispatch', [('diagnostic_sender', 'transport.send(path, raw, headers)'),
                                             ('provision', 'response = client.send(path, raw, headers)')])
def test_remaining_dispatch_sites_check_the_marker_immediately_before_sending(module, dispatch):
    # Static order check only; these closures are exercised by their own synthetic suites.
    lines = (Path(storage.__file__).parent / (module + '.py')).read_text().splitlines()
    at = [index for index, line in enumerate(lines) if dispatch in line]
    assert len(at) == 1
    assert [line.strip().split('  #')[0] for line in lines[at[0] - 2:at[0]]][0] == 'store.guard_credential()'


def test_cli_explicit_reconcile_recovers_a_visible_complete_after_its_final_fsync_failed(world, capsys):
    activate(world)
    world.fsync_fail = ('marker:complete', 'directory')
    with pytest.raises(TimeoutError):
        rotate(world)
    world.fsync_fail = None
    visible = world.marker()
    assert visible['mutation']['phase'] == 'complete'
    native, calls, reads, jobs = len(world.native), len(world.peer.calls), len(world.reads), list(world.jobs)
    assert sdc_cli.main(['disconnect', '--state-root', str(world.root), '--reconcile-credential', *FLAGS]) == 0
    assert world.marker()['mutation'] == visible['mutation']
    assert world.marker()['reconcile']['intent_id'] == visible['mutation']['id']
    assert len(world.reads) == reads + 1
    # Recovery only: no new change, HTTP, service change or re-enable.
    assert (len(world.native), len(world.peer.calls), world.jobs) == (native, calls, jobs)
    assert world.state()['enabled'] is True and not world.state().get('disconnected')
    assert '照合し' in capsys.readouterr().out


def test_later_writer_in_the_same_scope_holds_an_external_claim_or_device_change(world, monkeypatch):
    send = world.peer.send

    def tamper(path, raw, headers=None):
        result = send(path, raw, headers)
        if path.endswith('/claim'):
            # Another writer replaced the claim on disk between this scope's own two writers.
            state = world.state()
            (world.root / 'state.json').write_text(json.dumps({**state, 'claim_id': '66666666-6666-4666-8666-666666666666'}))
        return result
    monkeypatch.setattr(world.peer, 'send', tamper)
    with world.open() as store, store.lock():
        authorize(store, world, 'connect')
        with pytest.raises(ValueError, match='SDC_CREDENTIAL_APPROVAL_STALE'):
            world.connector(store).connect('SYNTHETIC_ONE_TIME_TOKEN')
    assert [row['mutation']['purpose'] for row in world.native] == ['initial-key']
    assert world.marker()['mutation']['purpose'] == 'initial-key'


def test_customer_delete_without_flags_reports_the_needed_confirmation_after_the_stop(world, capsys):
    activate(world)
    native, before = len(world.native), dict(world.items)
    assert sdc_cli.main(['disconnect', '--state-root', str(world.root)]) == 1
    assert world.state()['disconnected'] is True and world.state()['enabled'] is False
    assert (len(world.native), world.items) == (native, before)
    err = capsys.readouterr().err
    assert '--confirm-human' in err and 'ローカル送信の停止は完了' in err
