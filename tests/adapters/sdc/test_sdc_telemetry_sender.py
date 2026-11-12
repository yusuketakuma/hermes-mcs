"""Use synthetic private state and a fake HTTPS peer for telemetry delivery."""

import pytest

from adapters.sdc.protocol import sign, strict_json
from adapters.sdc.storage import Store
from adapters.sdc.telemetry import SECTIONS
from adapters.sdc.telemetry_sender import (CONSENT, INTERVAL, POLICY_SCHEMA, TelemetrySender,
                                           endpoint_hash, initial_status, validate_policy)
from adapters.sdc.transport import TransportError

NOW = 1792713600
CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
DEVICE = '11111111-1111-4111-8111-111111111111'
KEY_ID = '22222222-2222-4222-8222-222222222222'
REVISION = '33333333-3333-4333-8333-333333333333'
REQUEST_ID = '44444444-4444-4444-8444-444444444444'
KEY = 'a1' * 32
FACTS = {'mode': 'standalone', 'mcs_process': 'running'}
SECTIONS_ALL = sorted(SECTIONS)


class Peer:
    def __init__(self):
        self.calls, self.error, self.reply = [], None, None

    def send(self, path, raw, headers):
        self.calls.append((path, raw, headers))
        expected = sign(CONFIG, path, raw, {'device_id': DEVICE, 'key_id': KEY_ID}, KEY,
                        now=int(headers['X-SDC-Time']), nonce=headers['X-SDC-Nonce'])
        assert headers == expected
        if self.error:
            raise self.error
        report = strict_json(raw)
        reply = {'code': 'accepted', 'request_id': REQUEST_ID, 'report_id': report['report_id'],
                 'sections': sorted(report['sections'])}
        return self.reply(reply) if callable(self.reply) else self.reply or reply


def sections_for(policy_names):
    return {name: {'schema_version': f'sdc.telemetry.{name}.v1', 'sentinel': name}
            for name in policy_names if name != 'logs'} | (
        {'logs': {'schema_version': 'sdc.telemetry.logs.v1', 'window_seconds': 3600, 'entries': [], 'dropped': 0}}
        if 'logs' in policy_names else {})


@pytest.fixture
def world(tmp_path):
    with Store(tmp_path / 'private', create=True) as store:
        store.write('endpoint.json', CONFIG)
        store.write('credential', {'key': KEY, 'key_id': KEY_ID})
        store.write('state.json', {'state': 'active', 'enabled': True, 'device_id': DEVICE, 'key_id': KEY_ID,
                                   'clock_status': 'ok', 'clock_checked_at': NOW, 'sequence': 7})
        policy = {'schema_version': POLICY_SCHEMA, 'enabled': True, 'consent_revision': CONSENT,
                  'sections': SECTIONS_ALL, 'revision': REVISION, 'approved_at': NOW * 1000,
                  'device_id': DEVICE, 'endpoint_hash': endpoint_hash(CONFIG)}
        store.write('telemetry-policy.json', policy)
        store.write('telemetry-status.json', initial_status(REVISION))
        peer, clock, collected = Peer(), [NOW], []
        def collector(facts, state, *, data_dir, clock, only=None):
            collected.append((facts, state, data_dir, only))
            return sections_for(SECTIONS_ALL)
        sender = TelemetrySender(store, peer, clock=lambda: clock[0], data_dir=str(tmp_path / 'data'), collector=collector)
        yield store, peer, clock, collected, sender


def advance(store, clock, seconds):
    clock[0] += seconds
    state = store.read('state.json')
    state['clock_checked_at'] = clock[0]
    store.write('state.json', state)


def test_disabled_without_policy(tmp_path):
    with Store(tmp_path / 'private', create=True) as store:
        store.write('endpoint.json', CONFIG)
        store.write('state.json', {'state': 'active', 'enabled': True, 'device_id': DEVICE,
                                   'clock_status': 'ok', 'clock_checked_at': NOW})
        sender = TelemetrySender(store, Peer(), clock=lambda: NOW, data_dir=str(tmp_path))
        assert sender.tick(FACTS) == 'SDC_TELEMETRY_DISABLED'


def test_sends_one_report_and_backs_off(world):
    store, peer, clock, collected, sender = world
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_ACCEPTED'
    assert len(peer.calls) == len(collected) == 1
    path, raw, _headers = peer.calls[0]
    assert path == '/v2/device/telemetry'
    report = strict_json(raw)
    assert report['schema_version'] == 'sdc.device-telemetry.v1' and report['sequence'] == 1
    assert set(report['sections']) == set(SECTIONS_ALL)
    status = store.read('telemetry-status.json')
    assert status['sequence'] == 1 and status['last_result'] == 'SDC_TELEMETRY_ACCEPTED'
    assert status['next_due'] == NOW + INTERVAL
    # Heartbeat sequence/state is untouched by the telemetry sender.
    assert store.read('state.json')['sequence'] == 7
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_BACKOFF'
    assert len(peer.calls) == 1


def test_consent_sections_filter_collected_sections(world, tmp_path):
    store, peer, clock, collected, sender = world
    policy = store.read('telemetry-policy.json')
    policy['sections'] = ['hardware', 'mcs']
    store.write('telemetry-policy.json', policy)
    store.write('telemetry-status.json', initial_status(policy['revision']))
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_ACCEPTED'
    report = strict_json(peer.calls[0][1])
    assert set(report['sections']) == {'hardware', 'mcs'}


def test_gates_waiting_clock_and_runtime(world):
    store, peer, clock, collected, sender = world
    state = store.read('state.json')
    state['clock_status'] = 'skewed'
    store.write('state.json', state)
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_CLOCK_UNTRUSTED'
    state['clock_status'] = 'ok'
    state['enabled'] = False
    store.write('state.json', state)
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_WAITING'
    state['enabled'] = True
    store.write('state.json', state)
    assert sender.tick({'mode': 'unknown'}) == 'SDC_TELEMETRY_RUNTIME_UNKNOWN'
    assert peer.calls == []


def test_retryable_error_backs_off_and_terminal_error_stops(world):
    store, peer, clock, collected, sender = world
    peer.error = TransportError(503, 'SDC_STORAGE_UNAVAILABLE', 30)
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_RETRY_LATER'
    assert store.read('telemetry-status.json')['next_due'] > NOW
    advance(store, clock, INTERVAL + 1)
    peer.error = TransportError(403, 'SDC_REQUEST_DENIED')
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_REQUEST_DENIED'
    status = store.read('telemetry-status.json')
    assert status['stopped'] is True and status['last_result'] == 'SDC_TELEMETRY_REQUEST_DENIED'
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_REQUEST_DENIED'
    assert len(peer.calls) == 2


def test_protocol_error_on_mismatched_response(world):
    store, peer, clock, collected, sender = world
    peer.reply = lambda reply: {**reply, 'report_id': '55555555-5555-4555-8555-555555555555'}
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_PROTOCOL_ERROR'
    assert store.read('telemetry-status.json')['stopped'] is True


def test_duplicate_response_is_accepted(world):
    store, peer, clock, collected, sender = world
    peer.reply = lambda reply: {**reply, 'code': 'duplicate'}
    assert sender.tick(FACTS) == 'SDC_TELEMETRY_ACCEPTED'
    assert store.read('telemetry-status.json')['sequence'] == 1


def test_validate_policy_rejects_bad_sections():
    base = {'schema_version': POLICY_SCHEMA, 'enabled': True, 'consent_revision': CONSENT,
            'sections': ['hardware'], 'revision': REVISION, 'approved_at': NOW * 1000,
            'device_id': DEVICE, 'endpoint_hash': 'a' * 64}
    assert validate_policy(dict(base))['sections'] == ['hardware']
    for bad in ([], ['hardware', 'hardware'], ['unknown'], 'hardware', None):
        policy = dict(base, sections=bad)
        try:
            validate_policy(policy)
        except ValueError:
            pass
        else:
            raise AssertionError(f'accepted {bad!r}')
    policy = dict(base, consent_revision='other')
    try:
        validate_policy(policy)
    except ValueError:
        pass
    else:
        raise AssertionError('accepted wrong consent')
