"""Use only synthetic private files and fake transport; never a live endpoint."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from adapters.sdc.connector import Connector
from adapters.sdc.protocol import CONSENT, observation, sign, strict_json
from adapters.sdc.storage import Store
from adapters.sdc.transport import TransportError

NOW = 1792663200
KEY = 'a1' * 32
CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
FACTS = {'mode': 'standalone', 'mcs_process': 'running', 'python_version': '3.14.0', 'macos_version': '26.0', 'mcs_commit': 'a' * 40, 'loaded_commit': None}


class Fake:
    def __init__(self):
        self.calls = []
        self.error = None
        self.device_id, self.key_id, self.next_id = [str(uuid.uuid4()) for _ in range(3)]
        self.claim_id = None
        self.pending_key = None
        self.state = 'claimed'
        self.rotation = 'missing'
        self.lose = None
        self.keys = {}

    def send(self, path, raw, headers=None):
        self.calls.append((path, raw, headers))
        if self.error:
            raise self.error
        body = strict_json(raw)
        if not path.endswith('/claim'):
            key = self.keys[headers['X-SDC-Key-ID']]
            expected = sign(CONFIG, path, raw, {'device_id': headers['X-SDC-Device'], 'key_id': headers['X-SDC-Key-ID']}, key, now=int(headers['X-SDC-Time']), nonce=headers['X-SDC-Nonce'])
            assert expected['X-SDC-Signature'] == headers['X-SDC-Signature']
        if path.endswith('/claim'):
            self.claim_id = body['claim_id']
            self.pending_key = body['key']
            self.keys[self.key_id] = body['key']
            result = {'claim_id': self.claim_id, 'device_id': self.device_id, 'key_id': self.key_id, 'audience': CONFIG['audience'], 'state': 'claimed'}
        elif path.startswith('/v2/enrollments/'):
            if path.endswith('/confirm'):
                assert self.state in ('admin_approved', 'active')
                self.state = 'active'
            result = {'claim_id': self.claim_id, 'device_id': self.device_id, 'key_id': self.key_id, 'audience': CONFIG['audience'], 'state': self.state, 'tenant_name': 'Synthetic self', 'environment': 'synthetic', 'alias': 'Synthetic Mac', 'data_scope': 'version-process-status-only', 'fingerprint': hashlib.sha256(bytes.fromhex(self.pending_key)).hexdigest()[:16], 'consent_revision': CONSENT}
        elif path == '/v2/heartbeats':
            result = {'code': 'accepted', 'server_time': datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace('+00:00', 'Z')}
        elif path == '/v2/device/suspend':
            result = {'code': 'suspended'}
        else:
            if path.endswith('/prepare'):
                self.rotation = 'prepared'
                self.keys[self.next_id] = body['key']
            if path.endswith('/commit'):
                self.rotation = 'active'
            result = {'rotation_id': body['rotation_id'], 'key_id': None if self.rotation == 'missing' else self.next_id, 'state': self.rotation}
        if self.lose == path:
            self.lose = None
            raise TransportError()
        return result


@pytest.fixture
def world(tmp_path):
    root = (tmp_path / 'private').resolve()
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
        fake = Fake()
        clock = [NOW]
        connector = Connector(store, fake, clock=lambda: clock[0])
        yield store, fake, clock, connector


def activate(world):
    store, fake, clock, connector = world
    connector.connect('synthetic-token')
    fake.state = 'admin_approved'
    assert connector.connect()['state'] == 'admin_approved'
    connector.confirm()
    return store, fake, clock, connector


def test_private_storage_permissions_symlink_and_atomic_replace(world, tmp_path):
    store, *_ = world
    store.write('credential', {'key': KEY})
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert (store.root / 'credential').stat().st_mode & 0o777 == 0o600
    (store.root / 'credential').chmod(0o644)
    with pytest.raises(ValueError):
        store.read('credential')
    (store.root / 'credential').chmod(0o600)
    (store.root / 'credential').unlink()
    (store.root / 'credential').symlink_to(tmp_path / 'synthetic-target')
    with pytest.raises(OSError):
        store.write('credential', {'key': KEY})
    assert not (tmp_path / 'synthetic-target').exists()


def test_private_root_symlink_and_repository_are_refused(tmp_path):
    target = tmp_path / 'target'
    target.mkdir(mode=0o700)
    link = tmp_path / 'link'
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        Store(link)
    with pytest.raises(ValueError):
        Store(Path(__file__).resolve().parents[3] / 'synthetic-private')


def test_nonblocking_tick_lock_is_exclusive(world):
    store, *_ = world
    with store.lock(), pytest.raises(BlockingIOError):
        with Store(store.root) as other, other.lock():
            pass


def test_claim_key_and_id_survive_response_loss_without_token_persistence(world):
    store, fake, _, connector = world
    fake.lose = '/v2/enrollments/claim'
    with pytest.raises(TransportError):
        connector.connect('synthetic-token-secret')
    before = store.read('credential'), store.read('state.json')['claim_id']
    assert 'synthetic-token-secret' not in ''.join(p.read_text() for p in store.root.iterdir())
    connector.connect('synthetic-token-secret')
    assert store.read('credential')['key'] == before[0]['key']
    assert store.read('state.json')['claim_id'] == before[1]
    assert store.read('state.json')['enabled'] is False


def test_tick_is_disabled_by_default_and_never_collects_business_fields(world):
    store, fake, _, connector = world
    assert connector.tick(FACTS) == 'SDC_DISABLED'
    assert fake.calls == []
    activate(world)
    assert connector.tick({**FACTS, 'hostname': 'SYNTHETIC_PRIVATE', 'patient_body': 'SYNTHETIC_PRIVATE', 'python_version': 'SYNTHETIC_PRIVATE'}) == 'SDC_ACCEPTED'
    body = strict_json(fake.calls[-1][1])
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(body)
    assert body['installed']['python_version'] is None
    assert body['runtime']['loaded_commit'] is None
    assert body['clock_status'] == 'unknown'
    assert store.read('state.json')['clock_status'] == 'ok'


def test_retry_preserves_event_bytes_refreshes_nonce_and_sends_at_most_once(world):
    store, fake, clock, connector = activate(world)
    fake.error = TransportError(503)
    assert connector.tick(FACTS) == 'SDC_RETRY_LATER'
    raw, headers = fake.calls[-1][1:]
    count = len(fake.calls)
    assert connector.tick(FACTS) == 'SDC_BACKOFF'
    assert len(fake.calls) == count
    clock[0] = int(store.read('state.json')['next_due']) + 1
    fake.error = None
    assert connector.tick({**FACTS, 'mcs_process': 'stopped'}) == 'SDC_ACCEPTED'
    assert fake.calls[-1][1] == raw
    assert fake.calls[-1][2]['X-SDC-Nonce'] != headers['X-SDC-Nonce']
    assert store.read('state.json')['sequence'] == 1
    assert store.read('state.json')['latest']['runtime']['mcs_process'] == 'stopped'


def test_clock_diagnosis_is_used_only_on_a_later_new_event(world):
    store, fake, clock, connector = activate(world)
    connector.tick(FACTS)
    clock[0] += 300
    connector.tick(FACTS)
    assert strict_json(fake.calls[-1][1])['clock_status'] == 'ok'
    assert store.read('state.json')['sequence'] == 2


def test_permanent_4xx_stops_without_auto_registration_or_sequence_follow(world):
    store, fake, clock, connector = activate(world)
    fake.error = TransportError(409, 'SEQUENCE_CONFLICT')
    assert connector.tick(FACTS) == 'SDC_REQUEST_DENIED'
    assert store.read('state.json')['enabled'] is False
    count = len(fake.calls)
    clock[0] += 100000
    assert connector.tick(FACTS) == 'SDC_DISABLED'
    assert len(fake.calls) == count
    assert store.read('state.json')['sending'] is not None


def test_offline_disconnect_stays_locally_disabled_and_central_unconfirmed(world):
    store, fake, _, connector = activate(world)
    fake.error = TransportError()
    assert connector.disconnect() is False
    assert store.read('state.json')['enabled'] is False
    assert store.read('state.json')['central_suspended'] is False


@pytest.mark.parametrize('lost', ['/v2/keys/prepare', '/v2/keys/commit'])
def test_rotation_resumes_same_saved_key_and_rotation_after_response_loss(world, lost):
    store, fake, _, connector = activate(world)
    fake.lose = lost
    with pytest.raises(TransportError):
        connector.rotate()
    before = store.read('credential')['next_key'], store.read('state.json')['rotation_id']
    connector.rotate()
    assert store.read('credential')['key'] == before[0]
    assert store.read('credential')['key_id'] == fake.next_id
    assert 'rotation_id' not in store.read('state.json')
    assert {strict_json(raw)['rotation_id'] for path, raw, _ in fake.calls if path.startswith('/v2/keys/')} == {before[1]}


def test_rotation_recovers_local_publish_crash(world, monkeypatch):
    store, fake, _, connector = activate(world)
    original = store.write
    def crash(name, value):
        if name == 'credential' and value.get('completed_rotation_id'):
            raise OSError('SYNTHETIC_CRASH')
        original(name, value)
    monkeypatch.setattr(store, 'write', crash)
    with pytest.raises(OSError):
        connector.rotate()
    assert store.read('state.json')['key_id'] == fake.next_id
    assert connector.secret(store.read('state.json'), store.read('credential')) == store.read('credential')['next_key']
    monkeypatch.setattr(store, 'write', original)
    connector.rotate()
    assert store.read('credential')['key_id'] == fake.next_id


def test_transition_log_is_bounded_and_counts_discarded_entries(world):
    store, _, clock, connector = activate(world)
    state = store.read('state.json')
    state['transitions'] = [{'at': NOW - 8 * 86400, 'code': 'SDC_OLD'}] + [{'at': NOW, 'code': 'SDC_SYNTHETIC'} for _ in range(1200)]
    connector._transition(state, 'SDC_ACCEPTED')
    assert len(state['transitions']) == 1000
    assert state['dropped_events'] == 202
    assert len(json.dumps(state).encode()) < 8 * 1024 * 1024


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e1000}', b'\xff'])
def test_strict_json_rejects_shared_unsafe_shapes(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


def test_hmac_canonical_matches_shared_python_vector():
    vector = json.loads((Path(__file__).parent / 'hmac-vectors.json').read_text())['vectors'][0]
    state = {'device_id': vector['device_id'], 'key_id': vector['key_id']}
    assert sign(CONFIG, vector['path'], vector['raw_body'].encode(), state, vector['key'], now=int(vector['time']), nonce=vector['nonce'])['X-SDC-Signature'] == vector['signature']


def test_fixed_dto_allowlist_normalizes_invalid_values_to_unknown():
    result = observation({'mode': ['bad'], 'mcs_commit': 'secret', 'python_version': 'secret'}, 1, str(uuid.uuid4()), '2026-10-22T10:00:00Z', 0)
    assert result['runtime']['mode'] == 'unknown'
    assert result['installed']['mcs_commit'] is None


def test_malformed_ack_preserves_sending_body_and_backs_off(world, monkeypatch):
    store, fake, _, connector = activate(world)
    monkeypatch.setattr(fake, 'send', lambda *args: {'code': 'accepted', 'server_time': 'not-a-time'})
    assert connector.tick(FACTS) == 'SDC_RETRY_LATER'
    assert store.read('state.json')['sending'] is not None


def test_oversized_and_hardlinked_credential_files_are_refused(world, tmp_path):
    store, *_ = world
    (store.root / 'credential').write_bytes(b'x' * 4097)
    (store.root / 'credential').chmod(0o600)
    with pytest.raises(ValueError):
        store.read('credential')
    (store.root / 'credential').unlink()
    target = tmp_path / 'synthetic-credential'
    target.write_text('{}')
    target.chmod(0o600)
    import os
    os.link(target, store.root / 'credential')
    with pytest.raises(ValueError):
        store.read('credential')


def test_transient_401_can_resume_same_device_without_reenrollment(world):
    store, fake, _, connector = activate(world)
    identity = store.read('state.json')['device_id']
    fake.error = TransportError(401, 'UNAUTHENTICATED')
    connector.tick(FACTS)
    before = store.read('state.json')
    assert before['state'] == 'active'
    assert before['enabled'] is False
    fake.error = None
    connector.resume()
    after = store.read('state.json')
    assert after['device_id'] == identity
    assert after['sequence'] == before['sequence']
    assert after['sending'] == before['sending']
    assert after['enabled'] is True
    assert fake.calls[-1][0] == '/v2/keys/status'


def test_expired_prepared_rotation_replaces_only_unused_candidate(world):
    store, fake, _, connector = activate(world)
    fake.lose = '/v2/keys/prepare'
    with pytest.raises(TransportError):
        connector.rotate()
    prior_credential, prior_state = store.read('credential'), store.read('state.json')
    fake.rotation = 'revoked'
    assert connector.rotate() is False
    current_credential, current_state = store.read('credential'), store.read('state.json')
    assert current_credential['key'] == prior_credential['key']
    assert current_credential['key_id'] == prior_credential['key_id']
    assert current_credential['next_key'] != prior_credential['next_key']
    assert current_state['rotation_id'] != prior_state['rotation_id']
    fake.rotation, fake.next_id = 'missing', str(uuid.uuid4())
    assert connector.rotate() is True
    assert store.read('credential')['key_id'] == fake.next_id


def test_interrupted_rotation_remains_recoverable_while_tick_is_disabled(world):
    store, fake, _, connector = activate(world)
    fake.lose = '/v2/keys/commit'
    with pytest.raises(TransportError):
        connector.rotate()
    state = store.read('state.json')
    state['enabled'] = False
    store.write('state.json', state)
    assert connector.rotate() is True
    assert store.read('state.json')['enabled'] is False
    assert 'rotation_id' not in store.read('state.json')
    connector.resume()
    assert store.read('state.json')['enabled'] is True


def test_lost_commit_recovers_with_new_key_after_old_key_grace_expiry(world, monkeypatch):
    store, fake, _, connector = activate(world)
    original_id = store.read('state.json')['device_id']
    old_id = store.read('credential')['key_id']
    fake.lose = '/v2/keys/commit'
    with pytest.raises(TransportError):
        connector.rotate()
    original_send = fake.send
    def expired_old_key(path, raw, headers=None):
        if headers and headers['X-SDC-Key-ID'] == old_id:
            raise TransportError(403, 'DEVICE_DENIED')
        return original_send(path, raw, headers)
    monkeypatch.setattr(fake, 'send', expired_old_key)
    state = store.read('state.json')
    state.update(state='suspended', enabled=False)
    store.write('state.json', state)
    assert connector.rotate() is True
    assert store.read('state.json')['device_id'] == original_id
    assert store.read('state.json')['state'] == 'active'
    assert store.read('state.json')['enabled'] is False
    connector.resume()
    assert store.read('state.json')['enabled'] is True


def test_explicit_offline_disconnect_cannot_resume_through_pending_rotation(world):
    store, fake, _, connector = activate(world)
    fake.lose = '/v2/keys/commit'
    with pytest.raises(TransportError):
        connector.rotate()
    fake.error = TransportError()
    assert connector.disconnect() is False
    assert store.read('state.json')['disconnected'] is True
    fake.error = None
    before = len(fake.calls)
    with pytest.raises(ValueError):
        connector.rotate()
    with pytest.raises(ValueError):
        connector.resume()
    assert len(fake.calls) == before
    assert store.read('state.json')['enabled'] is False
