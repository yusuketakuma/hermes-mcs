"""Keep consent-bound enrollment separate from the observation senders."""
import hashlib
import uuid

import pytest

from adapters.sdc.connector import Connector
from adapters.sdc.protocol import CONSENT, strict_json
from adapters.sdc.storage import Store
from adapters.sdc.transport import TransportError

NOW = 1_792_663_200
CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}


class Peer:
    def __init__(self):
        self.claim = None
        self.device, self.key_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.key, self.state = None, 'claimed'
        self.calls, self.patch, self.lose, self.error = [], {}, None, None

    def send(self, path, raw, headers=None):
        body = strict_json(raw)
        self.calls.append((path, body, headers))
        if self.error:
            raise self.error
        if path.endswith('/claim'):
            if self.claim is not None:
                assert (body['claim_id'], body['key']) == (self.claim, self.key)
            self.claim, self.key = body['claim_id'], body['key']
        elif path.endswith('/confirm'):
            assert self.state in ('admin_approved', 'active')
            self.state = 'active'
        result = {'claim_id': self.claim, 'device_id': self.device, 'key_id': self.key_id,
                  'audience': CONFIG['audience'], 'state': self.state, 'tenant_name': 'Synthetic tenant',
                  'environment': CONFIG['environment'], 'alias': 'Synthetic Mac',
                  'data_scope': 'version-process-status-only', 'consent_revision': CONSENT,
                  'fingerprint': hashlib.sha256(bytes.fromhex(self.key)).hexdigest()[:16],
                  'pending_expires_at': (NOW + 86400) * 1000, **self.patch}
        if self.lose == path:
            self.lose = None
            raise TransportError()
        return result


@pytest.fixture
def world(tmp_path):
    with Store(tmp_path / 'private', create=True) as store:
        store.write('endpoint.json', CONFIG)
        peer, clock = Peer(), [NOW]
        connector = Connector(store, peer, clock=lambda: clock[0])
        yield store, peer, connector, clock


def consent(world):
    store, peer, connector, clock = world
    status = connector.connect('SYNTHETIC_TOKEN')
    assert connector.save_enrollment_consent(status)
    return store, peer, connector, clock


def test_pending_has_no_send_and_only_approved_identity_activates(world):
    store, peer, connector, clock = consent(world)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_PENDING'
    assert store.read('state.json')['enabled'] is False
    assert connector.tick({}) == 'SDC_DISABLED'
    peer.state = 'admin_approved'
    clock[0] += 301
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_ACTIVE'
    assert store.read('state.json')['enabled'] is True
    assert not any(path in ('/v2/heartbeats', '/v2/inventory/pages') for path, *_ in peer.calls)
    assert 'SYNTHETIC_TOKEN' not in (store.root / 'enrollment-consent.json').read_text()
    assert peer.key not in (store.root / 'enrollment-consent.json').read_text()


@pytest.mark.parametrize('field', ['claim_id', 'device_id', 'key_id', 'audience', 'tenant_name', 'environment', 'alias', 'fingerprint', 'data_scope', 'consent_revision', 'pending_expires_at'])
def test_changed_identity_or_deadline_is_sticky_hold(world, field):
    store, peer, connector, _ = consent(world)
    peer.state = 'admin_approved'
    peer.patch[field] = 'SYNTHETIC_CHANGED'
    with pytest.raises(ValueError):
        connector.poll_enrollment()
    assert store.read('state.json')['enabled'] is False
    assert store.read('state.json')['enrollment_hold']
    count = len(peer.calls)
    peer.patch.clear()
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_DISABLED'
    assert len(peer.calls) == count
    assert all(not path.endswith('/confirm') for path, *_ in peer.calls)


@pytest.mark.parametrize('expiry', [None, True, '1792749600000', NOW * 1000, 2**53, -1])
def test_missing_or_invalid_deadline_never_polls(world, expiry):
    store, peer, connector, _ = world
    status = connector.connect('SYNTHETIC_TOKEN')
    status['pending_expires_at'] = expiry
    if expiry is None:
        assert connector.save_enrollment_consent(status) is False
    else:
        with pytest.raises(ValueError):
            connector.save_enrollment_consent(status)
    count = len(peer.calls)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_CONSENT_REQUIRED'
    assert len(peer.calls) == count
    assert store.read('state.json')['enabled'] is False


@pytest.mark.parametrize('code', [401, 403, 409])
def test_denial_is_never_automatically_released(world, code):
    store, peer, connector, _ = consent(world)
    peer.error = TransportError(code)
    with pytest.raises(TransportError):
        connector.poll_enrollment()
    peer.error = None
    count = len(peer.calls)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_DISABLED'
    assert len(peer.calls) == count
    assert not store.read('state.json')['enabled']


@pytest.mark.parametrize('flag', ['disconnected', 'rotation_id', 'enrollment_hold'])
def test_local_hold_never_dispatches(world, flag):
    store, peer, connector, _ = consent(world)
    state = store.read('state.json')
    state[flag] = True
    store.write('state.json', state)
    count = len(peer.calls)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_DISABLED'
    assert len(peer.calls) == count


def test_receipt_save_failure_precedes_all_confirmation(world, monkeypatch):
    store, peer, connector, _ = world
    status = connector.connect('SYNTHETIC_TOKEN')
    original = store.write
    def write(name, value):
        if name == 'enrollment-consent.json':
            raise OSError('SYNTHETIC_DISK_FAILURE')
        original(name, value)
    monkeypatch.setattr(store, 'write', write)
    with pytest.raises(OSError):
        connector.save_enrollment_consent(status)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_CONSENT_REQUIRED'
    assert all(not path.endswith('/confirm') for path, *_ in peer.calls)


@pytest.mark.parametrize('failure', ['claim_ack', 'credential_save', 'state_save'])
def test_claim_recovery_uses_same_claim_and_key(world, monkeypatch, failure):
    store, peer, connector, _ = world
    original = store.write
    fired = False
    def write(name, value):
        nonlocal fired
        if not fired and peer.claim and ((failure == 'credential_save' and name == 'credential') or (failure == 'state_save' and name == 'state.json')):
            fired = True
            raise OSError('SYNTHETIC_DISK_FAILURE')
        original(name, value)
    monkeypatch.setattr(store, 'write', write)
    if failure == 'claim_ack':
        peer.lose = '/v2/enrollments/claim'
    with pytest.raises((TransportError, OSError)):
        connector.connect('SYNTHETIC_TOKEN')
    prior = store.read('state.json')['claim_id'], store.read('credential')['key']
    assert connector.connect('SYNTHETIC_TOKEN')['state'] == 'claimed'
    assert prior == (store.read('state.json')['claim_id'], store.read('credential')['key'])
    assert [body['key'] for path, body, _ in peer.calls if path.endswith('/claim')] == [prior[1], prior[1]]


@pytest.mark.parametrize('failure', ['confirm_ack', 'active_save'])
def test_confirmation_unknown_recovers_by_status_without_resending_confirm(world, monkeypatch, failure):
    store, peer, connector, clock = consent(world)
    peer.state = 'admin_approved'
    original = store.write
    fired = False
    def write(name, value):
        nonlocal fired
        if failure == 'active_save' and not fired and name == 'state.json' and value.get('state') == 'active':
            fired = True
            raise OSError('SYNTHETIC_DISK_FAILURE')
        original(name, value)
    monkeypatch.setattr(store, 'write', write)
    if failure == 'confirm_ack':
        peer.lose = '/v2/enrollments/confirm'
    with pytest.raises((TransportError, OSError)):
        connector.poll_enrollment()
    assert store.read('state.json')['state'] == 'pending'
    clock[0] += 400
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_ACTIVE'
    assert sum(path.endswith('/confirm') for path, *_ in peer.calls) == 1


def test_receipt_root_and_endpoint_binding_are_checked_without_network(world):
    store, peer, connector, _ = consent(world)
    receipt = store.read('enrollment-consent.json')
    receipt['root_fingerprint'] = '0' * 64
    store.write('enrollment-consent.json', receipt)
    count = len(peer.calls)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_CONSENT_REQUIRED'
    assert len(peer.calls) == count


@pytest.mark.parametrize('field,value', [('accepted', False), ('accepted', 1), ('accepted_at', True), ('accepted_at', -1), ('accepted_at', float('inf')), ('accepted_at', NOW * 1000 + 1), ('extra', True)])
def test_receipt_requires_closed_explicit_finite_consent(world, field, value):
    store, peer, connector, _ = consent(world)
    receipt = store.read('enrollment-consent.json')
    receipt[field] = value
    # JSON itself rejects infinity; a finite but invalid epoch still reaches the validator.
    if value == float('inf'):
        with pytest.raises(ValueError):
            store.write('enrollment-consent.json', receipt)
        return
    store.write('enrollment-consent.json', receipt)
    count = len(peer.calls)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_CONSENT_REQUIRED'
    assert len(peer.calls) == count


@pytest.mark.parametrize('failure', ['confirm_ack', 'active_save'])
def test_unknown_confirmation_can_only_reconcile_active_after_expiry(world, monkeypatch, failure):
    store, peer, connector, clock = consent(world)
    peer.state = 'admin_approved'
    original = store.write
    fired = False
    def write(name, value):
        nonlocal fired
        if failure == 'active_save' and not fired and name == 'state.json' and value.get('state') == 'active':
            fired = True
            raise OSError('SYNTHETIC_DISK_FAILURE')
        original(name, value)
    monkeypatch.setattr(store, 'write', write)
    if failure == 'confirm_ack':
        peer.lose = '/v2/enrollments/confirm'
    with pytest.raises((TransportError, OSError)):
        connector.poll_enrollment()
    assert store.read('state.json')['enrollment_confirmation']
    clock[0] += 86401
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_ACTIVE'
    assert sum(path.endswith('/confirm') for path, *_ in peer.calls) == 1


def test_expired_receipt_without_attempt_never_sends_or_confirms(world):
    _, peer, connector, clock = consent(world)
    peer.state = 'admin_approved'
    clock[0] += 86401
    count = len(peer.calls)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_EXPIRED'
    assert len(peer.calls) == count


def test_expired_unknown_attempt_cannot_start_new_confirmation(world):
    store, peer, connector, clock = consent(world)
    peer.state = 'admin_approved'
    # A request could fail before central confirmation; intent alone never establishes active.
    peer.lose = '/v2/enrollments/confirm'
    with pytest.raises(TransportError):
        connector.poll_enrollment()
    peer.state = 'admin_approved'
    clock[0] += 86401
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_EXPIRED'
    assert not store.read('state.json')['enabled']
    assert sum(path.endswith('/confirm') for path, *_ in peer.calls) == 1


@pytest.mark.parametrize('stop_at', ['entry', 'status', 'confirm'])
def test_owner_stop_is_rechecked_before_confirm_and_active_save(world, stop_at):
    store, peer, connector, _ = consent(world)
    peer.state = 'admin_approved'
    enabled = [stop_at != 'entry']
    connector.admit = lambda: enabled[0]
    original = peer.send
    def send(path, raw, headers=None):
        result = original(path, raw, headers)
        if path.endswith('/' + stop_at):
            enabled[0] = False
        return result
    peer.send = send
    count = len(peer.calls)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_DISABLED'
    assert not store.read('state.json')['enabled']
    if stop_at == 'entry':
        assert len(peer.calls) == count
    elif stop_at == 'status':
        assert all(not path.endswith('/confirm') for path, *_ in peer.calls)
    else:
        assert peer.state == 'active'
        assert store.read('state.json')['enrollment_confirmation']


def test_unexpected_active_without_local_confirmation_intent_is_held(world):
    store, peer, connector, _ = consent(world)
    peer.state = 'active'
    with pytest.raises(ValueError):
        connector.poll_enrollment()
    assert not store.read('state.json')['enabled']
    assert store.read('state.json')['enrollment_hold']


def test_receipt_binds_full_key_digest_in_addition_to_wire_fingerprint(world):
    store, peer, connector, _ = consent(world)
    receipt = store.read('enrollment-consent.json')
    assert receipt['key_sha256'] == hashlib.sha256(bytes.fromhex(peer.key)).hexdigest()
    receipt['key_sha256'] = '0' * 64
    store.write('enrollment-consent.json', receipt)
    count = len(peer.calls)
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_CONSENT_REQUIRED'
    assert len(peer.calls) == count


@pytest.mark.parametrize('code', [429, 503, 500])
def test_pending_backoff_is_persisted_and_separate_from_heartbeat(world, monkeypatch, code):
    store, peer, connector, clock = consent(world)
    monkeypatch.setattr('adapters.sdc.connector.secrets.randbelow', lambda _: 200)
    peer.error = TransportError(code, retry_after=700)
    for failure, delay in enumerate((700, 700, 1200, 1800, 1800), 1):
        before = len(peer.calls)
        with pytest.raises(TransportError):
            connector.poll_enrollment()
        state = store.read('state.json')
        assert state['enrollment_failures'] == min(failure, 4)
        assert state['enrollment_next_due'] == clock[0] + delay
        assert state['sequence'] == 0 and 'sending' not in state and 'failures' not in state
        assert connector.poll_enrollment() == 'SDC_ENROLLMENT_NOT_DUE'
        assert len(peer.calls) == before + 1
        clock[0] += delay
    peer.error = None
    assert connector.poll_enrollment() == 'SDC_ENROLLMENT_PENDING'
    assert store.read('state.json')['enrollment_failures'] == 0
    assert store.read('state.json')['enrollment_next_due'] == clock[0] + 300
