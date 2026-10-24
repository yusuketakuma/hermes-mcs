"""Exercise customer enrollment with synthetic private files and fake Keychain calls."""
from datetime import datetime, timezone
import hashlib
import json
import os
import time
import uuid

import pytest

from adapters.sdc import credential_mutation as mutations
from adapters.sdc.connector import Connector
from adapters.sdc.keychain import KeychainCredentialStore, KeychainError
from adapters.sdc.protocol import CONSENT, CUSTOMER_CONSENT, sign, strict_json
from adapters.sdc.storage import Store

NOW = 1792663200
KEY = 'a1' * 32
CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
DEVICE = '11111111-1111-4111-8111-111111111111'
KEY_ID = '22222222-2222-4222-8222-222222222222'
NEXT_ID = '33333333-3333-4333-8333-333333333333'
FACTS = {'mode': 'standalone', 'mcs_process': 'running', 'python_version': '3.14.0', 'macos_version': '26.0'}
REASON = 'synthetic customer credential change'


class Native:
    def __init__(self):
        self.items = {}
        self.calls = []
        self.read_status = 0
        self.write_status = 0

    def copy(self, account):
        self.calls.append(('copy', account))
        if self.read_status:
            return self.read_status, b''
        return (0, self.items[account]) if account in self.items else (-25300, b'')

    def add(self, account, raw):
        self.calls.append(('add', account))
        if self.write_status:
            return self.write_status
        if account in self.items:
            return -25299
        self.items[account] = raw
        return 0

    def update(self, account, raw):
        self.calls.append(('update', account))
        if self.write_status:
            return self.write_status
        self.items[account] = raw
        return 0

    def delete(self, account):
        self.calls.append(('delete', account))
        if account not in self.items:
            return -25300
        del self.items[account]
        return 0


class Owner:
    """Fixed owner-proxy shape: real pending lease and verified CAS around the synthetic native item."""

    def __init__(self, account, native):
        self.account, self.item = account, KeychainCredentialStore(account, native=native)

    def read(self, *, deadline=None):
        return self.item.read()

    def write(self, raw, **lease):
        return self._mutate(lambda: self.item.write(raw), **lease)

    def delete(self, **lease):
        return self._mutate(self.item.delete, **lease)

    def _mutate(self, native, *, intent_id, marker_sha256, root_fd, tick_fd, deadline):
        assert type(deadline) is float
        mutations.pending_marker(root_fd, tick_fd, intent_id=intent_id, marker_sha256=marker_sha256, account=self.account)
        try:
            native()
        except KeychainError as error:
            if error.code != 'SDC_KEYCHAIN_NOT_FOUND':
                raise
        verified = mutations.mark_native_verified(root_fd, tick_fd, intent_id=intent_id, marker_sha256=marker_sha256,
                                                  now_ms=int(time.time() * 1000))
        return {'intent_id': intent_id, 'marker_sha256': mutations.marker_sha256(verified)}


def approved(store, operation):
    """Explicit human approval re-bound under the tick lock, as the CLI flags do."""
    store.authorize_credential_mutation(operation=operation, confirm_human=True, reason=REASON, now_ms=NOW * 1000)
    return store.lock()


def initial(claim_id=None):
    return {'purpose': 'initial-key', 'after': {'claim_id': claim_id or str(uuid.uuid4()), 'device_id': None,
                                                'key_id': None, 'rotation_id': None}}


class Remote:
    def __init__(self, store, native, *, customer=True):
        self.store, self.native, self.customer = store, native, customer
        self.calls, self.keys = [], {}
        self.consent = CUSTOMER_CONSENT if customer else CONSENT
        self.scope = 'version-process-status-only'
        self.claim_id, self.pending_key = None, None
        self.state, self.rotation = 'admin_approved', 'missing'
        self.confirm_fields = {}

    def send(self, path, raw, headers=None):
        self.calls.append((path, raw, headers))
        body = strict_json(raw)
        if path.endswith('/claim'):
            assert set(body) == {'claim_id', 'token', 'key', 'connector_version'} | ({'credential_storage'} if self.customer else set())
            if self.customer:
                assert body['credential_storage'] == 'keychain'
                account = self.store.read('credential-store.json')['account']
                assert strict_json(self.native.items[account])['key'] == body['key']
                assert not (self.store.root / 'credential').exists()
            self.claim_id, self.pending_key = body['claim_id'], body['key']
            self.keys[KEY_ID] = body['key']
            return {'claim_id': self.claim_id, 'device_id': DEVICE, 'key_id': KEY_ID, 'audience': CONFIG['audience'], 'state': 'claimed'}
        expected = sign(CONFIG, path, raw, {'device_id': DEVICE, 'key_id': headers['X-SDC-Key-ID']}, self.keys[headers['X-SDC-Key-ID']], now=int(headers['X-SDC-Time']), nonce=headers['X-SDC-Nonce'])
        assert expected['X-SDC-Signature'] == headers['X-SDC-Signature']
        if path.endswith('/status') and path.startswith('/v2/enrollments/'):
            return {'claim_id': self.claim_id, 'device_id': DEVICE, 'key_id': KEY_ID, 'audience': CONFIG['audience'], 'state': self.state, 'tenant_name': 'Synthetic customer', 'environment': CONFIG['environment'], 'alias': 'Synthetic device', 'data_scope': self.scope, 'fingerprint': hashlib.sha256(bytes.fromhex(self.pending_key)).hexdigest()[:16], 'consent_revision': self.consent}
        if path.endswith('/confirm'):
            assert body['consent_revision'] == (CUSTOMER_CONSENT if self.customer else CONSENT)
            self.state = 'active'
            result = {'state': 'active', 'device_id': DEVICE}
            if self.customer:
                result.update(consent_revision=self.consent, data_scope=self.scope)
            return {**result, **self.confirm_fields}
        if path == '/v2/heartbeats':
            return {'code': 'accepted', 'server_time': datetime.fromtimestamp(NOW, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        if path == '/v2/device/suspend':
            return {'code': 'suspended'}
        if path.endswith('/prepare'):
            self.keys[NEXT_ID] = body['key']
            self.rotation = 'prepared'
        if path.endswith('/commit'):
            self.rotation = 'active'
        return {'rotation_id': body['rotation_id'], 'key_id': None if self.rotation == 'missing' else NEXT_ID, 'state': self.rotation}


@pytest.fixture
def world(tmp_path):
    native = Native()
    with Store(tmp_path / 'customer', create=True, keychain_factory=lambda account: Owner(account, native)) as store:
        store.write('endpoint.json', CONFIG)
        with store.lock():
            store.enable_customer()
        remote = Remote(store, native)
        connector = Connector(store, remote, clock=lambda: NOW)
        yield store, native, remote, connector


def activate(world):
    store, native, remote, connector = world
    with approved(store, 'connect'):
        connector.connect('SYNTHETIC_CUSTOMER_TOKEN')
    connector.confirm()
    return store, native, remote, connector


def test_customer_stores_keychain_before_claim_and_binds_customer_consent(world):
    store, native, remote, connector = activate(world)
    marker = store.read('credential-store.json')
    assert set(marker) == {'schema_version', 'mode', 'account', 'root_fingerprint', 'endpoint_hash',
                           'tick_lock_fingerprint', 'mutation'}
    assert marker['schema_version'] == 'sdc.credential-store.v3'
    assert marker['mutation']['purpose'] == 'claim-key-id' and marker['mutation']['phase'] == 'complete'
    assert marker['mutation']['approval']['reason'] == REASON
    assert marker['mode'] == 'keychain'
    st = os.fstat(store.fd)
    root = {'path': str(store.root), 'uid': st.st_uid, 'device': st.st_dev, 'inode': st.st_ino}
    def canonical_hash(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    assert marker['root_fingerprint'] == canonical_hash(root)
    assert marker['endpoint_hash'] == canonical_hash(CONFIG)
    assert uuid.UUID(marker['account']).version == 4
    assert (store.root / 'credential-store.json').stat().st_mode & 0o777 == 0o600
    assert store.read('state.json')['credential_storage'] == 'keychain'
    assert store.read('state.json')['enabled'] is True
    assert not (store.root / 'credential').exists()
    assert connector.tick(FACTS) == 'SDC_ACCEPTED'
    assert [call[0] for call in remote.calls] == ['/v2/enrollments/claim', '/v2/enrollments/status', '/v2/enrollments/confirm', '/v2/heartbeats']
    assert native.items


def test_legacy_self_enrollment_keeps_original_wire_and_file_storage(tmp_path):
    with Store(tmp_path / 'self', create=True, keychain_factory=lambda _: pytest.fail('self must not use Keychain')) as store:
        store.write('endpoint.json', CONFIG)
        remote = Remote(store, None, customer=False)
        connector = Connector(store, remote, clock=lambda: NOW)
        connector.connect('SYNTHETIC_SELF_TOKEN')
        connector.confirm()
        assert connector.consent == CONSENT
        assert (store.root / 'credential').exists()
        assert not (store.root / 'credential-store.json').exists()
        assert 'credential_storage' not in store.read('state.json')
        assert strict_json(remote.calls[2][1])['consent_revision'] == CONSENT


@pytest.mark.parametrize('status', [-25308, -25293, -1])
def test_locked_or_unavailable_customer_tick_never_sends_or_advances_state(world, status):
    store, native, remote, connector = activate(world)
    before = (store.root / 'state.json').read_bytes()
    calls, writes = len(remote.calls), len([x for x in native.calls if x[0] in ('add', 'update')])
    native.read_status = status
    with pytest.raises(KeychainError):
        connector.tick(FACTS)
    assert len(remote.calls) == calls
    assert len([x for x in native.calls if x[0] in ('add', 'update')]) == writes
    assert (store.root / 'state.json').read_bytes() == before
    assert not (store.root / 'credential').exists()


@pytest.mark.parametrize('failure', ['read', 'write'])
def test_first_keychain_failure_prevents_claim_and_plaintext_fallback(world, failure):
    store, native, remote, connector = world
    if failure == 'read':
        native.read_status = -25308
    else:
        native.write_status = -25308
    with pytest.raises(KeychainError), approved(store, 'connect'):
        connector.connect('SYNTHETIC_CUSTOMER_TOKEN')
    assert remote.calls == []
    assert native.items == {}
    assert not (store.root / 'credential').exists()
    assert not (store.root / 'state.json').exists()
    assert store.customer


@pytest.mark.parametrize('raw', [b'null', b'{}', b'[]', b'{"mode":"keychain","mode":"file"}',
    b'{"schema_version":"sdc.credential-store.v1","mode":"file","account":"11111111-1111-4111-8111-111111111111"}',
    b'{"schema_version":"sdc.credential-store.v1","mode":"keychain","account":"11111111-1111-4111-8111-111111111111"}',
    b'{"schema_version":"sdc.credential-store.v1","mode":"keychain","account":"not-a-uuid"}'])
def test_invalid_marker_rejects_every_access_without_reading_legacy_credential(tmp_path, raw):
    with Store(tmp_path / 'invalid', create=True, keychain_factory=lambda _: pytest.fail('invalid marker must not call Keychain')) as store:
        store.write('credential', {'key': KEY})
        credential = (store.root / 'credential').read_bytes()
        marker = store.root / 'credential-store.json'
        marker.write_bytes(raw)
        marker.chmod(0o600)
        for operation in (lambda: store.read('credential', {}), lambda: store.write('credential', {'key': 'b2' * 32}), store.remove_credential, lambda: store.read('state.json', {})):
            with pytest.raises(ValueError):
                operation()
        assert (store.root / 'credential').read_bytes() == credential


@pytest.mark.parametrize('unsafe', ['mode', 'symlink'])
def test_unsafe_marker_never_falls_back_to_plaintext(world, tmp_path, unsafe):
    store, native, *_ = world
    marker = store.root / 'credential-store.json'
    if unsafe == 'mode':
        marker.chmod(0o644)
    else:
        target = tmp_path / 'marker-target'
        target.write_bytes(marker.read_bytes())
        marker.unlink()
        marker.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        store.read('credential', {})
    assert native.calls == []
    assert not (store.root / 'credential').exists()


def test_customer_state_refuses_missing_marker_even_after_store_reopens(world):
    store, native, remote, connector = world
    with approved(store, 'connect'):
        connector.connect('SYNTHETIC_CUSTOMER_TOKEN')
    (store.root / 'credential-store.json').unlink()
    with Store(store.root, keychain_factory=lambda _: pytest.fail('missing marker must not read Keychain')) as reopened:
        with pytest.raises(ValueError, match='SDC_CREDENTIAL_STORE_REQUIRED'):
            reopened.read('credential', {})
    assert len(remote.calls) == 2
    assert native.items
    assert not (store.root / 'credential').exists()


@pytest.mark.parametrize('existing', ['credential', 'active-state'])
def test_customer_mode_refuses_to_migrate_an_existing_self_root(tmp_path, existing):
    with Store(tmp_path / 'self', create=True, keychain_factory=lambda _: pytest.fail('must not call Keychain')) as store:
        name, value = ('credential', {'key': KEY}) if existing == 'credential' else ('state.json', {'state': 'active', 'enabled': True})
        store.write(name, value)
        before = (store.root / name).read_bytes()
        with pytest.raises(ValueError, match='SDC_NEW_CUSTOMER_ROOT_REQUIRED'):
            store.enable_customer()
        assert (store.root / name).read_bytes() == before
        assert not (store.root / 'credential-store.json').exists()


@pytest.mark.parametrize('mismatch', ['consent', 'scope'])
def test_customer_status_rejects_legacy_token_or_expanded_scope(world, mismatch):
    store, _, remote, connector = world
    if mismatch == 'consent':
        remote.consent = CONSENT
    else:
        remote.scope = 'arbitrary-file-transfer'
    with pytest.raises(ValueError, match='SDC_IDENTITY_MISMATCH'), approved(store, 'connect'):
        connector.connect('SYNTHETIC_SELF_TOKEN')
    assert store.read('state.json')['enabled'] is False
    assert all(not call[0].endswith('/confirm') for call in remote.calls)


@pytest.mark.parametrize('fields', [{'consent_revision': CONSENT}, {'data_scope': 'arbitrary-file-transfer'}, {'consent_revision': None, 'data_scope': None}])
def test_customer_confirm_revalidates_consent_and_data_scope(world, fields):
    store, _, remote, connector = world
    with approved(store, 'connect'):
        connector.connect('SYNTHETIC_CUSTOMER_TOKEN')
    remote.confirm_fields = fields
    with pytest.raises(ValueError, match='SDC_CONFIRM_UNCERTAIN'):
        connector.confirm()
    assert store.read('state.json')['enabled'] is False


def test_customer_rotation_and_delete_touch_only_the_selected_keychain_item(world):
    store, native, remote, connector = activate(world)
    other_account = '44444444-4444-4444-8444-444444444444'
    native.items[other_account] = b'{"key":"synthetic-other-application-value"}'
    account = store.read('credential-store.json')['account']
    with approved(store, 'rotate-key'):
        assert connector.rotate()
    assert store.read('credential')['key_id'] == NEXT_ID
    assert store.read('state.json')['key_id'] == NEXT_ID
    assert not (store.root / 'credential').exists()
    state = (store.root / 'state.json').read_bytes()
    with approved(store, 'disconnect'):
        store.remove_credential(mutation={'purpose': 'delete'})
    assert native.items == {other_account: b'{"key":"synthetic-other-application-value"}'}
    marker = store.read('credential-store.json')
    # The account binding is unchanged; the marker keeps only the completed delete receipt.
    assert marker['account'] == account and marker['mutation']['purpose'] == 'delete'
    assert marker['mutation']['phase'] == 'complete' and marker['mutation']['new_sha256'] is None
    assert (store.root / 'state.json').read_bytes() == state
    assert store.read('credential') is None
    with pytest.raises(ValueError, match='SDC_CREDENTIAL'):
        connector.connect()
    assert all(call[1] != other_account for call in native.calls)


def test_valid_customer_marker_ignores_any_legacy_file_and_never_deletes_it(world):
    store, native, _, _ = world
    legacy = store.root / 'credential'
    legacy.write_text(json.dumps({'key': KEY}))
    legacy.chmod(0o600)
    assert store.read('credential') is None
    with approved(store, 'connect'):
        store.write('credential', {'key': 'b2' * 32}, mutation=initial())
    assert store.read('credential')['key'] == 'b2' * 32
    with approved(store, 'disconnect'):
        store.remove_credential(mutation={'purpose': 'delete'})
    assert json.loads(legacy.read_text()) == {'key': KEY}
    assert native.items == {}


@pytest.mark.parametrize('value', [None, [], {}, {'key': 'invalid'}, {'key': KEY, 'key_id': 'invalid'}, {'key': KEY, 'unexpected': KEY}])
def test_customer_credential_schema_rejects_malformed_records_before_send_or_write(world, value):
    store, native, remote, connector = world
    account = store.read('credential-store.json')['account']
    native.items[account] = json.dumps(value).encode()
    with pytest.raises(KeychainError, match='SDC_KEYCHAIN_INVALID_VALUE'):
        connector.connect('SYNTHETIC_CUSTOMER_TOKEN')
    with pytest.raises(KeychainError, match='SDC_KEYCHAIN_INVALID_VALUE'), approved(store, 'connect'):
        store.write('credential', value, mutation=initial())
    assert remote.calls == []
    assert all(call[0] not in ('add', 'update') for call in native.calls)
    assert not (store.root / 'credential').exists()


@pytest.mark.parametrize('raw', [b'[]', b'["SYNTHETIC_PRIVATE"]', b'1', b'true', b'null'])
@pytest.mark.parametrize('name', ['state.json', 'credential', 'endpoint.json', 'inventory-policy.json', 'inventory-spool.json', 'inventory-status.json'])
def test_self_json_files_require_objects_and_preserve_invalid_source_bytes(tmp_path, name, raw):
    with Store(tmp_path / 'self', create=True, keychain_factory=lambda _: pytest.fail('self must not use Keychain')) as store:
        path = store.root / name
        path.write_bytes(raw)
        path.chmod(0o600)
        with pytest.raises(ValueError, match='^SDC_FILE_INVALID$') as caught:
            store.read(name)
        assert path.read_bytes() == raw
        assert 'SYNTHETIC_PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('value', [[], ['SYNTHETIC_PRIVATE'], 1, True, None])
@pytest.mark.parametrize('name', ['state.json', 'credential', 'endpoint.json', 'inventory-policy.json', 'inventory-spool.json', 'inventory-status.json'])
def test_self_json_writes_reject_nonobjects_before_replacing_the_original(tmp_path, name, value):
    with Store(tmp_path / 'self', create=True) as store:
        store.write(name, {'synthetic_existing': True})
        original = (store.root / name).read_bytes()
        with pytest.raises(ValueError, match='^SDC_FILE_INVALID$'):
            store.write(name, value)
        assert (store.root / name).read_bytes() == original


def test_missing_json_file_preserves_the_callers_existing_default(tmp_path):
    with Store(tmp_path / 'self', create=True) as store:
        sentinel = []
        assert store.read('credential') is None
        assert store.read('inventory-policy.json', sentinel) is sentinel


@pytest.mark.parametrize('raw', [b'[]', b'["SYNTHETIC_PRIVATE"]', b'1', b'true', b'null'])
@pytest.mark.parametrize('name', ['state.json', 'credential'])
@pytest.mark.parametrize('next_due', [0, NOW + 300])
def test_self_tick_rejects_nonobject_state_or_credential_before_any_spool_progress(tmp_path, raw, name, next_due):
    with Store(tmp_path / 'self', create=True, keychain_factory=lambda _: pytest.fail('self must not use Keychain')) as store:
        store.write('endpoint.json', CONFIG)
        store.write('credential', {'key': KEY, 'key_id': KEY_ID})
        store.write('state.json', {'state': 'active', 'enabled': True, 'device_id': DEVICE, 'key_id': KEY_ID,
                                   'sequence': 7, 'next_due': next_due, 'sending': None, 'latest': None})
        remote = Remote(store, None, customer=False)
        connector = Connector(store, remote, clock=lambda: NOW)
        path = store.root / name
        path.write_bytes(raw)
        original = {path.name: path.read_bytes() for path in store.root.iterdir()}
        with pytest.raises(ValueError, match='^SDC_FILE_INVALID$'):
            connector.tick(FACTS)
        assert remote.calls == []
        assert {path.name: path.read_bytes() for path in store.root.iterdir()} == original


@pytest.mark.parametrize('raw', [b'[]', b'["SYNTHETIC_PRIVATE"]', b'1', b'true', b'null'])
def test_self_disconnect_rejects_malformed_file_credential_before_disabling_state(tmp_path, raw):
    with Store(tmp_path / 'self', create=True) as store:
        store.write('endpoint.json', CONFIG)
        store.write('credential', {'key': KEY, 'key_id': KEY_ID})
        store.write('state.json', {'state': 'active', 'enabled': True, 'device_id': DEVICE, 'key_id': KEY_ID})
        remote = Remote(store, None, customer=False)
        connector = Connector(store, remote, clock=lambda: NOW)
        (store.root / 'credential').write_bytes(raw)
        before = (store.root / 'state.json').read_bytes()
        with pytest.raises(ValueError, match='^SDC_FILE_INVALID$'):
            connector.disconnect()
        assert (store.root / 'credential').read_bytes() == raw
        assert (store.root / 'state.json').read_bytes() == before
        assert remote.calls == []


@pytest.mark.parametrize('raw', [b'[]', b'["SYNTHETIC_PRIVATE"]', b'1', b'true', b'null'])
@pytest.mark.parametrize('name', ['state.json', 'credential'])
def test_customer_tick_keeps_state_and_keychain_blob_unchanged_for_nonobjects(world, name, raw):
    store, native, remote, connector = activate(world)
    account = store.read('credential-store.json')['account']
    if name == 'state.json':
        (store.root / name).write_bytes(raw)
    else:
        native.items[account] = raw
    state, credential, calls = (store.root / 'state.json').read_bytes(), native.items[account], len(remote.calls)
    with pytest.raises(ValueError) as caught:
        connector.tick(FACTS)
    assert str(caught.value) in ('SDC_FILE_INVALID', 'SDC_KEYCHAIN_INVALID_VALUE')
    assert (store.root / 'state.json').read_bytes() == state
    assert native.items[account] == credential
    assert len(remote.calls) == calls


def customer_access(store, operation):
    if operation == 'read':
        return store.read('credential')
    if operation == 'write':
        return store.write('credential', {'key': 'b2' * 32, 'key_id': NEXT_ID}, mutation=initial())
    if operation == 'remove':
        return store.remove_credential(mutation={'purpose': 'delete'})
    return store.customer


@pytest.mark.parametrize('operation', ['read', 'write', 'remove', 'customer'])
def test_copied_marker_cannot_adopt_another_roots_keychain_account(world, tmp_path, operation):
    source, native, _, _ = activate(world)
    original_marker = (source.root / 'credential-store.json').read_bytes()
    original_items = dict(native.items)
    target = tmp_path / 'copied-marker'
    with Store(target, create=True) as empty:
        empty.write('endpoint.json', CONFIG)
    marker = target / 'credential-store.json'
    marker.write_bytes(original_marker)
    marker.chmod(0o600)
    native.calls.clear()
    with Store(target, keychain_factory=lambda _: pytest.fail('copied marker must not instantiate Keychain')) as reopened:
        with pytest.raises(ValueError, match='^SDC_CREDENTIAL_STORE_INVALID$'):
            customer_access(reopened, operation)
    assert native.calls == []
    assert native.items == original_items
    assert marker.read_bytes() == original_marker
    assert (source.root / 'credential-store.json').read_bytes() == original_marker
    assert not (target / 'credential').exists()


@pytest.mark.parametrize('operation', ['read', 'write', 'remove', 'customer'])
@pytest.mark.parametrize('field,value', [('origin', 'https://changed.invalid'), ('audience', 'sdc:changed'), ('environment', 'changed')])
def test_changed_endpoint_rejects_keychain_access_and_preserves_all_source_bytes(world, operation, field, value):
    store, native, _, _ = activate(world)
    endpoint = store.root / 'endpoint.json'
    changed = json.dumps({**CONFIG, field: value}).encode()
    endpoint.write_bytes(changed)
    marker = (store.root / 'credential-store.json').read_bytes()
    items = dict(native.items)
    native.calls.clear()
    with pytest.raises(ValueError, match='^SDC_CREDENTIAL_STORE_INVALID$'):
        customer_access(store, operation)
    assert endpoint.read_bytes() == changed
    assert (store.root / 'credential-store.json').read_bytes() == marker
    assert native.calls == [] and native.items == items


@pytest.mark.parametrize('field,value', [('origin', 'https://changed.invalid'), ('audience', 'sdc:changed'), ('environment', 'changed')])
def test_customer_endpoint_update_rejects_scope_change_before_replacing_configuration(world, field, value):
    store, native, _, _ = activate(world)
    original = (store.root / 'endpoint.json').read_bytes()
    marker = (store.root / 'credential-store.json').read_bytes()
    items = dict(native.items)
    native.calls.clear()
    with pytest.raises(ValueError, match='^SDC_CREDENTIAL_STORE_INVALID$'):
        store.write('endpoint.json', {**CONFIG, field: value})
    assert (store.root / 'endpoint.json').read_bytes() == original
    assert (store.root / 'credential-store.json').read_bytes() == marker
    assert native.calls == [] and native.items == items


def test_relocated_customer_root_rejects_both_reopen_and_cached_store(world, tmp_path):
    store, native, _, _ = activate(world)
    marker = (store.root / 'credential-store.json').read_bytes()
    items = dict(native.items)
    moved = tmp_path / 'relocated-customer'
    store.root.rename(moved)
    native.calls.clear()
    with Store(moved, keychain_factory=lambda _: pytest.fail('relocated root must not instantiate Keychain')) as reopened:
        with pytest.raises(ValueError, match='^SDC_CREDENTIAL_STORE_INVALID$'):
            reopened.read('credential')
    with pytest.raises(ValueError, match='^SDC_CREDENTIAL_STORE_INVALID$'):
        store.customer
    assert native.calls == [] and native.items == items
    assert (moved / 'credential-store.json').read_bytes() == marker


def test_customer_reopen_keeps_the_same_root_endpoint_and_account(world):
    store, native, _, _ = activate(world)
    marker = (store.root / 'credential-store.json').read_bytes()
    with Store(store.root, keychain_factory=lambda account: KeychainCredentialStore(account, native=native)) as reopened:
        reopened.enable_customer()
        assert reopened.customer
        assert reopened.read('credential-store.json')['account'] == strict_json(marker)['account']
        assert reopened.read('credential')['key_id'] == KEY_ID
    assert (store.root / 'credential-store.json').read_bytes() == marker


def test_customer_cached_keychain_never_accepts_a_replaced_account(world):
    store, native, _, _ = activate(world)
    store.read('credential')
    marker_path = store.root / 'credential-store.json'
    changed = {**strict_json(marker_path.read_bytes()), 'account': str(uuid.uuid4())}
    marker_path.write_text(json.dumps(changed))
    marker = marker_path.read_bytes()
    items = dict(native.items)
    native.calls.clear()
    with pytest.raises(ValueError, match='^SDC_CREDENTIAL_STORE_INVALID$'):
        store.read('credential')
    assert marker_path.read_bytes() == marker
    assert native.calls == [] and native.items == items


@pytest.mark.parametrize('config', [None, {**CONFIG, 'origin': 'http://unapproved.invalid'}, {**CONFIG, 'audience': 'unapproved/audience'}])
def test_customer_marker_requires_an_approved_endpoint_before_creation(tmp_path, config):
    with Store(tmp_path / 'new-customer', create=True, keychain_factory=lambda _: pytest.fail('unapproved endpoint must not instantiate Keychain')) as store:
        if config is not None:
            store.write('endpoint.json', config)
        original = {path.name: path.read_bytes() for path in store.root.iterdir()}
        with pytest.raises(ValueError, match='^SDC_ENDPOINT_NOT_APPROVED$'):
            store.enable_customer()
        assert {path.name: path.read_bytes() for path in store.root.iterdir()} == original
        assert not (store.root / 'credential-store.json').exists()
