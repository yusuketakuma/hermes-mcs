"""Synthetic same-owner execution barriers; no HTTP, OpenSSL, Keychain or service."""
import asyncio
import base64
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
from types import SimpleNamespace

import pytest

from adapters.sdc import restart_control as control
from adapters.sdc import restart_execution as execution
from adapters.sdc import restart_state as consent
from adapters.sdc import provision, restart_planning
from adapters.sdc.storage import Store
from mcs_standalone import restart_owner, runtime

FIXTURES = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/support'
V = json.loads((FIXTURES / 'restart-wire-vectors.json').read_text())
S = json.loads((FIXTURES / 'restart-signature-vectors.json').read_text())
TRACKING_ID = '00000000-0000-4000-8000-000000000099'
CANARY = 'SYNTHETIC_AUTHORITY_ERROR_MUST_NOT_ESCAPE'


def raw(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(raw(value).encode('ascii')).hexdigest()


class Clock:
    wall = V['now']
    elapsed = 0

    def time(self):
        return self.wall / 1000

    def monotonic(self):
        return 1000 + self.elapsed / 1000

    def advance(self, milliseconds):
        self.wall += milliseconds
        self.elapsed += milliseconds


class Carrier:
    def __init__(self, world):
        self.world, self.busy = world, False

    async def send(self, endpoint, path, body, headers, *, deadline):
        assert not self.busy and set(headers) == {'X-SDC-Device', 'X-SDC-Key-ID', 'X-SDC-Time',
                                                'X-SDC-Nonce', 'X-SDC-Signature'}
        self.busy = True
        try:
            request = json.loads(body)
            self.world.sent.append((path, request))
            await asyncio.sleep(0)
            self.world.on_send(path, request)
            if path == control.ADMIT:
                self.world.admission = request
                return {'schema_version': 'sdc.support.restart-response.v1', 'request_id': TRACKING_ID,
                        'operation_id': request['operation_id'], 'receipt_id': request['receipt_id'],
                        'state': 'locally_admitted'}
            assert path == control.GRANT
            grant = copy.deepcopy(V['grant'])
            grant.update(**{k: request[k] for k in ('grant_request_id', 'operation_id', 'payload_hash',
                                                   'receipt_id', 'admission_id')},
                         owner_generation=self.world.admission['owner_generation'])
            self.world.on_grant(grant)
            self.world.grant = grant
            envelope = copy.deepcopy(S['vectors'][1]['envelope'])
            envelope['payload'] = base64.urlsafe_b64encode(raw(grant).encode('ascii')).rstrip(b'=').decode('ascii')
            return {'schema_version': 'sdc.support.restart-response.v1', 'request_id': TRACKING_ID,
                    'grant': envelope}
        finally:
            self.busy = False

    def cancel(self):
        pass

    async def wait_closed(self):
        assert not self.busy


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    root.mkdir()
    (root / 'data').mkdir()
    cfg = {'runtime_mode': 'standalone'}
    (root / 'config.json').write_text(json.dumps(cfg))
    host = runtime.Runtime(root, cfg)
    host.generation = V['expected']['params']['expected_runtime_generation']
    host.started_at = V['preflight']['started_at'] / 1000
    clock = Clock()
    with Store(tmp_path / 'private', create=True) as store:
        endpoint = {'origin': 'https://synthetic.invalid', 'audience': V['expected']['audience'],
                    'environment': V['expected']['environment']}
        identity = {k: V['expected'][k] for k in ('claim_id', 'device_id', 'key_id')}
        key = hashlib.sha256(b'synthetic runtime signing credential').hexdigest()
        store.write('endpoint.json', endpoint)
        store.write('credential', {'key': key, 'key_id': identity['key_id']})
        binding = {**identity, 'root_fingerprint': store.root_fingerprint(), 'monitor_endpoint_hash': '1' * 64,
                   'support_endpoint_hash': digest(endpoint), 'pins_sha256': digest(S['pinned_keys']),
                   'secret_sha256': hashlib.sha256(bytes.fromhex(key)).hexdigest()}
        auth = {'expected': copy.deepcopy(V['expected']), 'registration': copy.deepcopy(V['registration']),
                'endpoint': endpoint, 'pins': copy.deepcopy(S['pinned_keys']), 'identity': identity,
                'key': key, 'binding': binding, 'allowed': True}
        accept = copy.deepcopy(V['controls']['accept'])
        saved = {'schema_version': consent.SCHEMA, 'root_fingerprint': store.root_fingerprint(),
                 'owner_generation': 'b' * 32, 'disabled': False, 'expected': copy.deepcopy(V['expected']),
                 'binding': binding, 'preview': copy.deepcopy(V['preview']), 'receipt': copy.deepcopy(V['localReceipt']),
                 'accept': {'request': {'raw': raw(accept), 'body_hash': digest(accept),
                                        'admitted_at': V['localReceipt']['confirmed_at']},
                            'ack': copy.deepcopy(V['acceptance'])}}
        store.write(consent.NAME, saved)
        result = SimpleNamespace(host=host, store=store, auth=auth, clock=clock, sent=[], verified=[],
                                 on_send=lambda *_: None, on_verify=lambda *_: None,
                                 on_grant=lambda *_: None,
                                 envelope=copy.deepcopy(S['vectors'][0]['envelope']))

        def verify(encoded, *, purpose, pins, expected, now_ms):
            result.verified.append(purpose)
            result.on_verify(purpose)
            assert pins == S['pinned_keys'] and expected == V['expected']
            envelope = json.loads(encoded)
            assert envelope['purpose'] == purpose
            payload = envelope['payload']
            return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))

        result.verify = verify
        monkeypatch.setattr(runtime, 'RestartCarrier', lambda: Carrier(result))
        monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('real child forbidden'))
        yield result
        if host._restart_reservation is not None and host._restart_execution_owner is None:
            host.release_restart(host._restart_reservation['handle'])


async def run(world, **options):
    def authority(*, read_credential):
        read_credential()
        return copy.deepcopy(world.auth)

    args = dict(operation_envelope=world.envelope, approval=copy.deepcopy(V['approval']),
                acceptance=copy.deepcopy(V['acceptance']), authority_supplier=authority,
                deadline=asyncio.get_running_loop().time() + 10, verifier=world.verify,
                clock=world.clock.time, monotonic=world.clock.monotonic)
    args.update(options)
    return await world.host.restart_execute(world.store, **args)


@pytest.fixture
def managed_world(world):
    """Install synthetic issuer profiles around the same independently fixed expected15."""
    vectors = json.loads((FIXTURES.parent / 'provision/provision-vectors.json').read_text())
    assert vectors['now'] == V['now']
    previous = world.store
    saved = copy.deepcopy(previous.read(consent.NAME))
    targets = {'monitor': {'origin': 'https://synthetic-monitor.invalid', 'audience': 'monitor:synthetic',
                           'environment': V['expected']['environment']},
               'support': {'origin': 'https://synthetic-support.invalid', 'audience': 'support:synthetic',
                           'environment': V['expected']['environment']},
               'restart': copy.deepcopy(world.auth['endpoint'])}
    with Store(previous.root.with_name('managed-private'), create_new=True) as store:
        root_id = provision.prepare_root(store, confirm_human=True, reason='synthetic-issuer', now_ms=V['now'])['root_id']
        for profile in ('monitor', 'support', 'restart'):
            bundle = copy.deepcopy(next(row['value'] for row in vectors['positive']
                                        if row['definition'] == 'Bundle' and row['value']['profile_id'] == profile))
            bundle.update(root_id=root_id, **targets[profile],
                          **{k: V['expected'][k] for k in ('tenant', 'installation', 'realm_epoch')})
            if profile != 'monitor':
                bundle.update(**{k: V['expected'][k] for k in ('device_id', 'claim_id', 'device_epoch')})
            if profile == 'restart':
                bundle.update(pins=copy.deepcopy(S['pinned_keys']), expires_at=V['now'] + 1000,
                              import_expires_at=V['now'] + 1000)
            encoded = raw(bundle).encode('ascii')
            expected = {k: bundle[k] for k in provision.EXPECTED_FIELDS if k != 'bundle_hash'}
            expected['bundle_hash'] = hashlib.sha256(encoded).hexdigest()
            plan = provision.prepare_import(store, encoded, expected=expected, now_ms=V['now'])
            provision.commit_import(store, plan, confirm_human=True, reason='synthetic-import', now_ms=V['now'])
            if profile == 'monitor':
                provision.begin_registration(store, now_ms=V['now'])
                store.write('state.json', {'state': 'active', 'enabled': True, **world.auth['identity']})
                store.write('credential', {'key': world.auth['key'], 'key_id': world.auth['identity']['key_id']})
            else:
                def initialize(selected=profile):
                    if selected == 'support':
                        # Parent authorization is supplied by this existing world;
                        # this stub only supplies the profile consumer's root marker.
                        store.write('support-state.json', {'root_fingerprint': store.root_fingerprint()})
                    else:
                        consent.initialize(store)
                        restart_planning.initialize(store)
                provision.initialize_profile(store, profile=profile, now_ms=V['now'], initializer=initialize)
        # This expected binding is built from independent fixture bytes, not the
        # implementation's _profile_binding helper or supplier-supplied metadata.
        binding = {**world.auth['binding'], 'root_fingerprint': store.root_fingerprint(),
                   'monitor_endpoint_hash': digest(targets['monitor']),
                   'profile': {'bundle_hash': digest(bundle), 'expires_at': V['now'] + 1000,
                               'endpoint': targets['restart']}}
        world.auth['binding'] = binding
        saved.update(root_fingerprint=store.root_fingerprint(), binding=copy.deepcopy(binding))
        store.write(consent.NAME, saved)
        world.store, world.managed_bundle = store, copy.deepcopy(bundle)
        try:
            yield world
        finally:
            world.store = previous


def replace_managed_bundle(world):
    """Synthetic durable replacement with internally consistent immutable wire joins."""
    value = world.store.read(provision.NAME)
    slot = value['profiles']['restart']
    bundle = json.loads(slot['raw'])
    bundle['policy_hash'] = 'e' * 64
    slot['raw'], slot['bundle_hash'] = raw(bundle), digest(bundle)
    slot['expected']['bundle_hash'] = slot['bundle_hash']
    for name in ('ack_request', 'status_request'):
        body = json.loads(slot[name]['raw'])
        body['bundle_hash'] = slot['bundle_hash']
        slot[name] = {'raw': raw(body), 'body_hash': digest(body)}
    world.store.write(provision.NAME, value)
    # The test proves a valid new local snapshot, not merely corrupt JSON.
    current = provision.profile_snapshot(world.store, profile='restart', now_ms=world.clock.wall)
    assert current['bundle_hash'] == digest(bundle) != world.auth['binding']['profile']['bundle_hash']
    return current


def customer_reader(world, monkeypatch):
    marker = {'schema_version': 'sdc.credential-store.v2', 'mode': 'keychain',
              'account': '00000000-0000-4000-8000-000000000077',
              'root_fingerprint': world.store.root_fingerprint(),
              'endpoint_hash': world.store._endpoint_hash()}
    world.store._write_file('credential-store.json', marker)

    class Reader:
        def __init__(self, *, check, deadline):
            self.check, self.deadline = check, deadline
            self.raw = raw({'key': world.auth['key'], 'key_id': world.auth['identity']['key_id']}).encode()
            self.reads, self.closed, self.terminal = [], False, False
            self.closing = self.allow_close = None
            world.reader = self

        async def prepare(self, account):
            assert account == marker['account']
            self.check()

        def copy_current(self, account, *, terminal=False):
            assert account == marker['account'] and not self.closed
            self.check()
            self.reads.append((world.store.read(execution.NAME, {}).get('phase'), terminal))
            if terminal:
                self.closed, self.terminal = True, True
            return self.raw

        async def close(self):
            if self.closing is not None:
                self.closing.set()
                await self.allow_close.wait()
            self.closed = True

    monkeypatch.setattr(restart_owner, 'CredentialCarrier', Reader)


@pytest.mark.parametrize('customer', [False, True])
def test_managed_binding_uses_fresh_snapshot_each_current_and_preserves_terminal_latch(managed_world, monkeypatch, customer):
    world = managed_world
    if customer:
        customer_reader(world, monkeypatch)
    snapshots, reads, starts = [], [], []
    read, snapshot = world.store.read_current_credential, provision.profile_snapshot
    start = world.host._restart_support_start
    def current_read(**kwargs):
        reads.append(True)
        return read(**kwargs)
    def current_profile(store, **kwargs):
        assert store is world.store and kwargs == {'profile': 'restart', 'now_ms': world.clock.wall}
        value = snapshot(store, **kwargs)
        snapshots.append(copy.deepcopy(value))
        return value
    def latch(*args, **kwargs):
        assert world.store.read(execution.NAME)['phase'] == 'SPENT'
        if customer:
            assert world.reader.closed and world.reader.terminal
        starts.append(kwargs)
        return start(*args, **kwargs)
    monkeypatch.setattr(world.store, 'read_current_credential', current_read)
    monkeypatch.setattr(provision, 'profile_snapshot', current_profile)
    monkeypatch.setattr(world.host, '_restart_support_start', latch)
    result = asyncio.run(run(world))
    assert result['state'] == 'unknown' and len(starts) == 1
    assert len(snapshots) == len(reads) > 3
    assert all(s['bundle_hash'] == world.auth['binding']['profile']['bundle_hash'] for s in snapshots)
    assert [path for path, _ in world.sent] == [control.ADMIT, control.GRANT]
    assert world.verified == ['restart-operation', 'restart-execution-grant']
    assert all('"profile"' not in raw(body) for _, body in world.sent)
    assert world.store.read(execution.NAME)['binding'] == world.auth['binding']
    assert world.host._restart_execution_owner is None
    if customer:
        assert world.reader.reads[-1] == ('SPENT', True)
        assert sum(terminal for _, terminal in world.reader.reads) == 1


@pytest.mark.parametrize('change', ['profile_hash', 'profile_omitted', 'monitor_hash', 'endpoint', 'pins', 'scope',
                                   'missing', 'corrupt', 'missing_consumer', 'legacy_file', 'expired'])
def test_managed_binding_cannot_adopt_supplier_values_or_fall_back(managed_world, change):
    world = managed_world
    binding = world.auth['binding']
    if change == 'profile_hash':
        binding['profile']['bundle_hash'] = 'd' * 64
    elif change == 'profile_omitted':
        binding.pop('profile')
    elif change == 'monitor_hash':
        binding['monitor_endpoint_hash'] = 'c' * 64
    elif change == 'endpoint':
        world.auth['endpoint']['origin'] = 'https://other.invalid'
        binding['support_endpoint_hash'] = digest(world.auth['endpoint'])
        binding['profile']['endpoint'] = copy.deepcopy(world.auth['endpoint'])
    elif change == 'pins':
        world.auth['pins'][0]['kid'] = 'other-synthetic-key'
        binding['pins_sha256'] = digest(world.auth['pins'])
    elif change == 'scope':
        world.auth['expected']['realm_epoch'] = 'other-synthetic-realm'
    elif change == 'missing':
        (world.store.root / provision.NAME).unlink()
    elif change == 'corrupt':
        world.store.write(provision.NAME, {'synthetic': CANARY})
    elif change == 'missing_consumer':
        (world.store.root / restart_planning.NAME).unlink()
    elif change == 'legacy_file':
        world.store._write_file('endpoint.json', world.auth['endpoint'])
    else:
        world.clock.advance(1000)
    with pytest.raises(restart_owner.RestartOwnerError) as caught:
        asyncio.run(run(world))
    assert CANARY not in str(caught.value)
    assert world.sent == [] and world.verified == []
    assert world.host._support_restart is None and not world.host.restarting
    assert world.host._restart_execution_owner is None and world.host._restart_reservation is None


def test_legacy_authority_cannot_attach_a_profile_without_managed_store(world):
    world.auth['binding']['profile'] = {'bundle_hash': 'c' * 64, 'expires_at': V['now'] + 1000,
                                      'endpoint': copy.deepcopy(world.auth['endpoint'])}
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world))
    assert world.sent == [] and world.host._support_restart is None
    assert world.host._restart_reservation is None


@pytest.mark.parametrize('change', ['expired', 'missing', 'bundle_replaced', 'monitor_disabled', 'missing_consumer'])
def test_post_spent_managed_change_preserves_spent_without_latch(managed_world, monkeypatch, change):
    world = managed_world
    customer_reader(world, monkeypatch)
    write, injected = world.store.write, False
    def changed(name, value):
        nonlocal injected
        write(name, value)
        if name != execution.NAME or value.get('phase') != 'SPENT' or injected:
            return
        injected = True
        if change == 'expired':
            world.clock.advance(1000)  # Grant is still within its independent 29-second window.
        elif change == 'missing':
            (world.store.root / provision.NAME).unlink()
        elif change == 'bundle_replaced':
            fresh = replace_managed_bundle(world)
            # Even a supplier that adopts the new snapshot cannot replace this
            # continuation's already retained, consent-bound profile.
            world.auth['binding']['profile']['bundle_hash'] = fresh['bundle_hash']
        elif change == 'missing_consumer':
            (world.store.root / restart_planning.NAME).unlink()
        else:
            state = world.store.read('state.json')
            state['enabled'] = False
            write('state.json', state)
    monkeypatch.setattr(world.store, 'write', changed)
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world))
    assert injected and world.store.read(execution.NAME)['phase'] == 'SPENT'
    assert world.store.read(execution.NAME)['started_at'] is None
    assert [path for path, _ in world.sent] == [control.ADMIT, control.GRANT]
    assert world.reader.closed and world.host._support_restart is None and not world.host.restarting
    assert world.host._restart_execution_owner is None and world.host._restart_reservation is None


def test_profile_expiry_after_terminal_snapshot_is_checked_before_latch(managed_world, monkeypatch):
    world = managed_world
    customer_reader(world, monkeypatch)
    snapshot = provision.profile_snapshot
    advanced = False
    def expires_after_read(store, **kwargs):
        nonlocal advanced
        result = snapshot(store, **kwargs)
        if world.reader.terminal and not advanced:
            advanced = True
            world.clock.advance(1000)
        return result
    monkeypatch.setattr(provision, 'profile_snapshot', expires_after_read)
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world))
    assert advanced and world.reader.closed and world.reader.terminal
    assert world.reader.reads[-1] == ('SPENT', True)
    assert world.store.read(execution.NAME)['phase'] == 'SPENT'
    assert world.store.read(execution.NAME)['started_at'] is None
    assert world.host._support_restart is None and world.host._restart_reservation is None


@pytest.mark.parametrize('reads', [0, 2])
def test_authority_must_read_current_credential_exactly_once(world, reads):
    def authority(*, read_credential):
        for _ in range(reads):
            read_credential()
        return copy.deepcopy(world.auth)
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world, authority_supplier=authority))
    assert not world.sent and world.host._support_restart is None


def test_each_current_reads_actual_customer_secret_and_terminal_reaps_before_latch(world, monkeypatch):
    customer_reader(world, monkeypatch)
    start = world.host._restart_support_start
    def checked_start(*args, **kwargs):
        assert world.reader.closed and world.reader.terminal
        return start(*args, **kwargs)
    monkeypatch.setattr(world.host, '_restart_support_start', checked_start)
    result = asyncio.run(run(world))
    assert result['phase'] == 'SPENT' and world.host._support_restart is not None
    assert len(world.reader.reads) > 3
    assert [terminal for _, terminal in world.reader.reads].count(True) == 1
    assert world.reader.reads[-1] == ('SPENT', True)
    assert sum(phase == 'SPENT' for phase, _ in world.reader.reads) == 3


def test_post_spent_external_secret_change_without_metadata_change_never_latches(world, monkeypatch):
    customer_reader(world, monkeypatch)
    write = world.store.write
    def changed(name, value):
        write(name, value)
        if name == execution.NAME and value.get('phase') == 'SPENT':
            world.reader.raw = raw({'key': 'b2' * 32, 'key_id': world.auth['identity']['key_id']}).encode()
    monkeypatch.setattr(world.store, 'write', changed)
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world))
    assert world.store.read(execution.NAME)['phase'] == 'SPENT'
    assert world.host._support_restart is None and world.reader.closed
    assert world.reader.reads[-1] == ('SPENT', False)


@pytest.mark.parametrize('key_id', [None, '00000000-0000-4000-8000-000000000078'])
def test_actual_key_id_must_match_the_authority_identity(world, monkeypatch, key_id):
    customer_reader(world, monkeypatch)
    def authority(*, read_credential):
        value = {'key': world.auth['key']}
        if key_id is not None:
            value['key_id'] = key_id
        world.reader.raw = raw(value).encode()
        read_credential()
        return copy.deepcopy(world.auth)
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world, authority_supplier=authority))
    assert world.reader.closed and not world.sent
    assert world.host._support_restart is None


def test_cleanup_error_without_closed_evidence_keeps_owner_until_cleanup_succeeds(world, monkeypatch):
    customer_reader(world, monkeypatch)
    def authority(*, read_credential):
        read_credential()
        reader = world.reader
        close = reader.close
        calls = 0
        async def transient_close():
            nonlocal calls
            calls += 1
            assert world.host._restart_execution_owner is not None
            assert world.host._restart_reservation is not None
            assert_update_locked(world.host)
            if calls == 1:
                assert not reader.closed
                raise OSError(CANARY)
            await close()
        monkeypatch.setattr(reader, 'close', transient_close)
        raise RuntimeError(CANARY)
    with pytest.raises(restart_owner.RestartOwnerError) as error:
        asyncio.run(run(world, authority_supplier=authority))
    assert CANARY not in str(error.value) and world.reader.closed
    assert world.host._restart_execution_owner is None
    assert world.host._restart_reservation is None


def test_customer_cleanup_retains_owner_store_and_update_lock_under_repeated_cancel(world, monkeypatch):
    customer_reader(world, monkeypatch)
    write = world.store.write
    task = None
    def changed(name, value):
        write(name, value)
        if name == execution.NAME and value.get('phase') == 'SPENT':
            world.reader.closing, world.reader.allow_close = asyncio.Event(), asyncio.Event()
            task.cancel()
    monkeypatch.setattr(world.store, 'write', changed)

    async def scenario():
        nonlocal task
        task = asyncio.create_task(run(world))
        while not getattr(world, 'reader', None) or world.reader.closing is None:
            await asyncio.sleep(0)
        await world.reader.closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not world.reader.closed
        assert world.host._restart_execution_owner is not None
        assert world.host._restart_reservation is not None
        assert_update_locked(world.host)
        assert world.store.read(execution.NAME)['phase'] == 'SPENT'
        with world.store.restart_state_lock(), world.store.lock():
            pass
        world.reader.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert world.reader.closed and world.host._restart_execution_owner is None
        assert world.host._restart_reservation is None and world.host._support_restart is None
        assert world.store.read(execution.NAME)['phase'] == 'SPENT'
    asyncio.run(scenario())


def assert_update_locked(host):
    fd = os.open(host.data / 'update.lock', os.O_WRONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_caller_spends_durably_before_one_support_latch_and_never_reports_success(world, monkeypatch):
    observed = []
    start = world.host._restart_support_start

    def latch(*args, **kwargs):
        saved = world.store.read(execution.NAME)
        assert saved['phase'] == 'SPENT'
        assert not world.host.restarting
        assert_update_locked(world.host)
        with Store(world.store.root) as other:
            with pytest.raises(BlockingIOError), other.restart_state_lock():
                pytest.fail('SPENT-to-latch lost disable exclusion')
        observed.append(kwargs)
        return start(*args, **kwargs)

    monkeypatch.setattr(world.host, '_restart_support_start', latch)

    async def scenario():
        result = await run(world)
        assert result['state'] == 'unknown' and len(observed) == 1
        assert world.host.restarting and world.host._support_restart['started_at'] == world.clock.wall
        assert [p for p, _ in world.sent] == [control.ADMIT, control.GRANT]
        assert world.verified == ['restart-operation', 'restart-execution-grant']
        assert not world.host.native_admission.snapshot()['open']
        assert not world.host.delivery_admission.snapshot()['open']
        assert world.host._restart_execution_owner is None
        assert_update_locked(world.host)
        assert world.host.tick(world.clock.time())
        assert not world.host.children
        saved = raw(world.store.read(execution.NAME))
        assert raw(world.grant) not in saved and world.auth['key'] not in saved
        assert world.envelope['signature'] not in saved
        with pytest.raises((ValueError, RuntimeError)):
            await run(world)
        assert len(observed) == 1 and len(world.sent) == 2
        assert world.host.release_restart(world.host._restart_reservation['handle']) is False
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['disabled', 'expected', 'acceptance', 'denied', 'signature'])
def test_untrusted_or_revoked_input_never_sends_or_starts(world, change):
    options = {}
    if change == 'disabled':
        consent.disable(world.store)
    elif change == 'expected':
        world.auth['expected']['service_revision'] += 1
    elif change == 'acceptance':
        options['acceptance'] = {**V['acceptance'], 'accepted_at': V['acceptance']['accepted_at'] + 1}
    elif change == 'denied':
        world.auth['allowed'] = False
    else:
        def invalid(*_a, **_k):
            raise ValueError(CANARY)
        options['verifier'] = invalid
    with pytest.raises(restart_owner.RestartOwnerError) as caught:
        asyncio.run(run(world, **options))
    assert CANARY not in str(caught.value)
    assert not world.sent and not world.host.restarting and world.host._support_restart is None
    assert world.host._restart_reservation is None


@pytest.mark.parametrize('change', ['stop', 'signal', 'config', 'generation', 'identity', 'native', 'delivery',
                                   'update_marker', 'update_journal', 'credential', 'deadline', 'clock_back'])
def test_spent_fsync_then_changed_conditions_never_latch(world, monkeypatch, change):
    write = world.store.write
    injected = False

    def changed(name, value):
        nonlocal injected
        write(name, value)
        if name != execution.NAME or value.get('phase') != 'SPENT' or injected:
            return
        injected = True
        if change == 'stop':
            world.host.restarting = True
        elif change == 'signal':
            world.host._restart_signal(signal.SIGTERM, None)
            world.host._restart_signal(signal.SIGINT, None)
        elif change == 'config':
            (world.host.root / 'config.json').write_text('{"runtime_mode":"standalone","notify":{}}')
        elif change == 'generation':
            world.host.generation = 'f' * 32
        elif change == 'identity':
            world.host._delivery_identity = object()
        elif change == 'native':
            world.host.native_admission.open()
        elif change == 'delivery':
            world.host.delivery_admission.open()
        elif change == 'update_marker':
            (world.host.data / runtime.UPDATE_MARKER_NAME).touch()
        elif change == 'update_journal':
            (world.host.data / 'update_state.json').write_text('{broken')
        elif change == 'credential':
            world.auth['key'] = 'c' * 64
        elif change == 'deadline':
            world.clock.advance(30000)
        else:
            world.clock.wall -= 200

    monkeypatch.setattr(world.store, 'write', changed)
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world))
    assert injected and world.store.read(execution.NAME)['phase'] == 'SPENT'
    assert world.host._support_restart is None
    assert len(world.sent) == 2


@pytest.mark.parametrize('source', ['durable_write', 'authority_supplier'])
def test_pending_task_cancellation_after_spent_never_latches(world, monkeypatch, source):
    write = world.store.write
    injected = False

    def cancel_once():
        nonlocal injected
        if not injected:
            assert world.store.read(execution.NAME)['phase'] == 'SPENT'
            injected = True
            assert asyncio.current_task().cancel()

    def cancel_after_write(name, value):
        write(name, value)
        if name == execution.NAME and value.get('phase') == 'SPENT':
            cancel_once()

    def authority(*, read_credential):
        read_credential()
        saved = world.store.read(execution.NAME)
        if saved is not None and saved['phase'] == 'SPENT':
            cancel_once()
        return copy.deepcopy(world.auth)

    options = {}
    if source == 'durable_write':
        monkeypatch.setattr(world.store, 'write', cancel_after_write)
    else:
        options['authority_supplier'] = authority

    async def scenario():
        task = asyncio.create_task(run(world, **options))
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() and injected
        saved = world.store.read(execution.NAME)
        assert saved['phase'] == 'SPENT' and saved['started_at'] is None
        assert saved['report'] is None
        assert world.host._support_restart is None and not world.host.restarting
        assert world.host._restart_execution_owner is None
        assert world.host._restart_reservation is None
        assert [path for path, _ in world.sent] == [control.ADMIT, control.GRANT]

    asyncio.run(scenario())


def test_first_grant_request_save_consumes_original_clock_anchor(world, monkeypatch):
    begin = execution.begin_grant
    def delayed(*args, **kwargs):
        result = begin(*args, **kwargs)
        world.clock.advance(15000)
        return result
    # A fresh short-lived server grant must not erase time already spent saving its request.
    def fresh(grant):
        grant.update(issued_at=world.clock.wall, not_before=world.clock.wall,
                     expires_at=world.clock.wall + 10000)
    world.on_grant = fresh
    monkeypatch.setattr(execution, 'begin_grant', delayed)
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world))
    assert world.host._support_restart is None
    assert len([p for p, _ in world.sent if p == control.GRANT]) <= 1


def test_failure_saving_started_evidence_does_not_undo_or_repeat_the_latch(world, monkeypatch):
    write = world.store.write
    def fail_started(name, value):
        if name == execution.NAME and value.get('started_at') is not None:
            raise OSError(CANARY)
        write(name, value)
    monkeypatch.setattr(world.store, 'write', fail_started)
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world))
    assert world.host.restarting and world.host._support_restart is not None
    assert world.host._restart_reservation is not None
    saved = world.store.read(execution.NAME)
    assert saved['phase'] == 'SPENT' and saved['started_at'] is None
    assert len(world.sent) == 2


def test_invalid_final_signature_leaves_no_spent_or_start(world):
    def invalid(purpose):
        if purpose == 'restart-execution-grant':
            raise ValueError(CANARY)
    world.on_verify = invalid
    with pytest.raises(restart_owner.RestartOwnerError) as caught:
        asyncio.run(run(world))
    assert CANARY not in str(caught.value)
    assert world.store.read(execution.NAME)['phase'] == 'GRANT_REQUESTED'
    assert world.host._support_restart is None and len(world.sent) == 2


def test_replaced_borrowed_store_descriptor_cannot_continue_after_admit(world):
    original, duplicate = world.store.fd, os.dup(world.store.fd)
    def replace(path, _request):
        if path == control.ADMIT:
            world.store.fd = duplicate
    world.on_send = replace
    try:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(run(world))
        assert world.host._support_restart is None and len(world.sent) == 1
    finally:
        world.store.fd = original
        os.close(duplicate)


def test_signal_between_final_owner_check_and_latch_is_not_support_evidence(world, monkeypatch):
    start = world.host._restart_support_start
    check = world.host._restart_execution_check
    def start_with_race(*args, **kwargs):
        def interrupted(*check_args):
            check(*check_args)
            world.host._restart_signal(signal.SIGTERM, None)
        monkeypatch.setattr(world.host, '_restart_execution_check', interrupted)
        return start(*args, **kwargs)
    monkeypatch.setattr(world.host, '_restart_support_start', start_with_race)
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world))
    assert world.host.restarting and world.host._support_restart is None
    assert world.store.read(execution.NAME)['phase'] == 'SPENT'


@pytest.mark.parametrize('stage', ['ADMITTING', 'SPENT'])
def test_uncertain_write_never_latches_or_retries(world, monkeypatch, stage):
    write = world.store.write
    def uncertain(name, value):
        write(name, value)
        if name == execution.NAME and value.get('phase') == stage:
            raise OSError(CANARY)
    monkeypatch.setattr(world.store, 'write', uncertain)
    with pytest.raises(restart_owner.RestartOwnerError) as caught:
        asyncio.run(run(world))
    assert CANARY not in str(caught.value)
    count = len(world.sent)
    assert world.host._support_restart is None
    with pytest.raises(restart_owner.RestartOwnerError):
        asyncio.run(run(world))
    assert len(world.sent) == count


def test_reopen_retains_metadata_but_cannot_mint_an_execution_owner(world):
    asyncio.run(run(world))
    metadata = execution.status(world.store)
    world.host.release_restart(world.host._restart_reservation['handle'])
    world.host.restarting = False
    world.host._support_restart = None
    world.host.native_admission.open()
    world.host.delivery_admission.open()
    original = world.store
    with Store(original.root) as reopened:
        world.store = reopened
        assert execution.status(reopened) == metadata
        with pytest.raises(restart_owner.RestartOwnerError):
            asyncio.run(run(world))
    world.store = original
    assert len(world.sent) == 2 and world.host._support_restart is None


@pytest.mark.parametrize('drain', [False, True])
def test_cancelled_verifier_retains_reservation_until_actual_worker_completion(world, drain):
    entered, finish = threading.Event(), threading.Event()
    def held(purpose):
        if purpose == 'restart-execution-grant':
            entered.set()
            assert finish.wait(5), 'test failed to release its synthetic verifier'
    world.on_verify = held

    async def scenario():
        task = asyncio.create_task(run(world))
        for _ in range(2000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        cleanup = None
        try:
            if drain:
                world.host.restarting = True
                cleanup = asyncio.create_task(world.host._close_restart_transport())
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            assert not task.done()
            assert world.host._restart_execution_owner is not None
            assert_update_locked(world.host)
            with pytest.raises(RuntimeError, match='execution_busy'):
                world.host.release_restart(world.host._restart_reservation['handle'])
            if cleanup is not None:
                assert not cleanup.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if cleanup is not None:
            await cleanup
        assert world.host._restart_reservation is None
        assert world.host._restart_execution_owner is None and world.host._support_restart is None
    asyncio.run(scenario())


def test_serve_signal_flag_is_synchronous_and_original_handlers_are_restored(world, monkeypatch):
    originals = {sig: object() for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    handlers, touched = dict(originals), []
    def register(sig, handler):
        touched.append(sig)
        previous, handlers[sig] = handlers[sig], handler
        return previous
    monkeypatch.setattr(runtime.signal, 'signal', register)
    monkeypatch.setattr(runtime.signal, 'getsignal', handlers.__getitem__)
    monkeypatch.setattr(runtime, 'Runtime', lambda *_: world.host)
    monkeypatch.setattr(world.host, 'adopt_orphans', lambda: None)
    monkeypatch.setattr(world.host, 'recover_inflight', lambda: None)
    def tick(**_kwargs):
        assert world.host._support_restart is None
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert world.host.restarting
        handlers[signal.SIGINT](signal.SIGINT, None)
        assert world.host._support_restart is None
        return True
    monkeypatch.setattr(world.host, 'tick', tick)
    for _ in range(2):
        assert asyncio.run(runtime.serve(world.host.root, world.host.cfg)) == 0
        assert handlers == originals
    assert signal.SIGHUP not in touched


def test_serve_rejects_non_main_thread_before_owning_resources(world):
    observed = []
    def worker():
        try:
            asyncio.run(runtime.serve(world.host.root, world.host.cfg))
        except RuntimeError as error:
            observed.append(str(error))
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and observed == ['standalone_main_thread_required']
    assert not (world.host.data / 'standalone.lock').exists()
