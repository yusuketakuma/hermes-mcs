"""Use synthetic private state and a fake HTTPS peer for inventory delivery."""
import copy
import json

import pytest

from adapters.sdc.inventory import collect_inventory
from adapters.sdc.inventory_sender import CONSENT, POLICY_SCHEMA, InventorySender, endpoint_hash, initial_status
from adapters.sdc.protocol import sign, strict_json
from adapters.sdc.storage import Store
from adapters.sdc.transport import TransportError

NOW = 1792713600
CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
DEVICE = '11111111-1111-4111-8111-111111111111'
KEY_ID = '22222222-2222-4222-8222-222222222222'
REVISION = '33333333-3333-4333-8333-333333333333'
REQUEST_ID = '44444444-4444-4444-8444-444444444444'
KEY = 'a1' * 32
FACTS = {'mode': 'standalone', 'mcs_commit': 'b' * 40, 'python_version': '3.14.0'}


class Distribution:
    def __init__(self, name):
        self.metadata, self.version = {'Name': name}, '1.2.3'

    def read_text(self, _):
        return None


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
        page = strict_json(raw)
        reply = {'code': 'accepted' if page['page'] == page['page_count'] else 'stored', 'request_id': REQUEST_ID,
                 'snapshot_id': page['snapshot_id'], 'state': ('complete' if page['complete'] else 'partial') if page['page'] == page['page_count'] else 'receiving'}
        return self.reply(reply) if callable(self.reply) else self.reply or reply


@pytest.fixture
def world(tmp_path):
    with Store(tmp_path / 'private', create=True) as store:
        store.write('endpoint.json', CONFIG)
        store.write('credential', {'key': KEY, 'key_id': KEY_ID})
        store.write('state.json', {'state': 'active', 'enabled': True, 'device_id': DEVICE, 'key_id': KEY_ID,
                                   'clock_status': 'ok', 'clock_checked_at': NOW, 'sequence': 7})
        names = [f'synthetic-public-{index:03d}' for index in range(110)]
        policy = {'schema_version': POLICY_SCHEMA, 'enabled': True, 'consent_revision': CONSENT,
                  'public_packages': names, 'lock_entries': None, 'revision': REVISION, 'approved_at': NOW * 1000,
                  'device_id': DEVICE, 'endpoint_hash': endpoint_hash(CONFIG)}
        store.write('inventory-policy.json', policy)
        store.write('inventory-status.json', initial_status(REVISION))
        peer, clock, collections = Peer(), [NOW], []
        def collector(**kwargs):
            collections.append(kwargs)
            return collect_inventory(**kwargs, python_version='3.14.0', distributions=[Distribution(name) for name in names])
        sender = InventorySender(store, peer, clock=lambda: clock[0], collector=collector)
        yield store, peer, clock, collections, sender


def advance(store, clock, seconds):
    clock[0] += seconds
    state = store.read('state.json')
    state['clock_checked_at'] = clock[0]
    store.write('state.json', state)


def test_sends_one_page_per_tick_and_recollects_only_after_six_hours(world):
    store, peer, clock, collections, sender = world
    before = store.read('state.json')
    assert sender.tick(FACTS) == 'SDC_INVENTORY_STORED'
    assert len(peer.calls) == len(collections) == 1
    assert strict_json(peer.calls[0][1])['page'] == 1
    assert sender.tick(FACTS) == 'SDC_INVENTORY_ACCEPTED'
    assert len(peer.calls) == 2 and len(collections) == 1
    assert sender.tick(FACTS) == 'SDC_INVENTORY_BACKOFF'
    assert len(peer.calls) == 2
    assert store.read('state.json') == before
    advance(store, clock, 6 * 3600)
    assert sender.tick(FACTS) == 'SDC_INVENTORY_STORED'
    assert len(peer.calls) == 3 and len(collections) == 2


def test_ack_loss_reuses_exact_bytes_and_id_with_fresh_hmac_nonce(world):
    store, peer, clock, collections, sender = world
    peer.error = TransportError()
    assert sender.tick(FACTS) == 'SDC_INVENTORY_RETRY_LATER'
    original = peer.calls[0]
    assert sender.tick(FACTS) == 'SDC_INVENTORY_BACKOFF'
    peer.error = None
    advance(store, clock, 300)
    assert sender.tick(FACTS) == 'SDC_INVENTORY_STORED'
    assert peer.calls[1][1] == original[1]
    assert peer.calls[1][2]['X-SDC-Nonce'] != original[2]['X-SDC-Nonce']
    assert len(collections) == 1


def test_incomplete_snapshot_remains_partial_after_terminal_receipt(world):
    store, _, _, _, sender = world
    facts = {**FACTS, 'mcs_commit': None}
    assert sender.tick(facts) == 'SDC_INVENTORY_STORED'
    assert sender.tick(facts) == 'SDC_INVENTORY_PARTIAL'
    assert store.read('inventory-status.json')['last_result'] == 'SDC_INVENTORY_PARTIAL'


def test_known_configuration_change_waits_for_pending_pages_then_refreshes_next_tick(world):
    _, peer, _, collections, sender = world
    assert sender.tick(FACTS) == 'SDC_INVENTORY_STORED'
    changed = {**FACTS, 'mcs_commit': 'c' * 40}
    assert sender.tick(changed) == 'SDC_INVENTORY_ACCEPTED'
    assert len(collections) == 1
    assert sender.tick(changed) == 'SDC_INVENTORY_STORED'
    assert len(collections) == 2
    assert strict_json(peer.calls[0][1])['snapshot_id'] != strict_json(peer.calls[2][1])['snapshot_id']
    assert strict_json(peer.calls[2][1])['source_revision'] == 'c' * 40


def test_unknown_configuration_does_not_trigger_every_tick_collection(world):
    _, peer, _, collections, sender = world
    sender.tick(FACTS)
    sender.tick(FACTS)
    assert sender.tick({**FACTS, 'mcs_commit': None, 'python_version': None}) == 'SDC_INVENTORY_BACKOFF'
    assert len(collections) == 1 and len(peer.calls) == 2


@pytest.mark.parametrize('previous_runtime', [{'mode': 'hermes', 'python_version': '3.14.0'}, {'mode': 'standalone', 'python_version': '3.13.0'}])
def test_known_runtime_or_python_change_refreshes_before_six_hours(world, previous_runtime):
    store, _, _, collections, sender = world
    sender.tick(FACTS)
    sender.tick(FACTS)
    status = store.read('inventory-status.json')
    status['runtime'] = previous_runtime
    store.write('inventory-status.json', status)
    assert sender.tick(FACTS) == 'SDC_INVENTORY_STORED'
    assert len(collections) == 2


def test_failed_change_refresh_keeps_its_backoff_instead_of_recollecting_each_tick(world):
    store, peer, _, collections, sender = world
    sender.tick(FACTS)
    sender.tick(FACTS)
    attempts = []
    def failed(**_):
        attempts.append(1)
        raise ValueError('synthetic inventory failure')
    sender.collector = failed
    changed = {**FACTS, 'mcs_commit': 'c' * 40}
    assert sender.tick(changed) == 'SDC_INVENTORY_UNAVAILABLE'
    assert store.read('inventory-status.json')['next_due'] == NOW + 6 * 3600
    assert sender.tick(changed) == 'SDC_INVENTORY_BACKOFF'
    assert attempts == [1]
    assert len(collections) == 1 and len(peer.calls) == 2


@pytest.mark.parametrize('changes', [{'enabled': False}, {'state': 'suspended'}, {'central_suspended': True}, {'clock_status': 'unknown'}, {'clock_checked_at': NOW - 901}, {'rotation_id': REVISION}])
def test_untrusted_or_inactive_device_performs_no_collection_or_fetch(world, changes):
    store, peer, _, collections, sender = world
    store.write('state.json', {**store.read('state.json'), **changes})
    sender.tick(FACTS)
    assert collections == peer.calls == []


def test_disabled_policy_unknown_runtime_and_changed_target_perform_no_fetch(world):
    store, peer, _, collections, sender = world
    policy = store.read('inventory-policy.json')
    store.write('inventory-policy.json', {**policy, 'enabled': False})
    assert sender.tick(FACTS) == 'SDC_INVENTORY_DISABLED'
    store.write('inventory-policy.json', policy)
    assert sender.tick({**FACTS, 'mode': 'unknown'}) == 'SDC_INVENTORY_RUNTIME_UNKNOWN'
    store.write('endpoint.json', {**CONFIG, 'audience': 'sdc:another'})
    assert sender.tick(FACTS) == 'SDC_INVENTORY_WAITING'
    assert collections == peer.calls == []


@pytest.mark.parametrize('status', [403, 409])
def test_permanent_denial_stops_only_inventory(world, status):
    store, peer, clock, _, sender = world
    before = copy.deepcopy(store.read('state.json'))
    peer.error = TransportError(status, 'SYNTHETIC_PRIVATE_ERROR')
    assert sender.tick(FACTS) == 'SDC_INVENTORY_REQUEST_DENIED'
    advance(store, clock, 3600)
    sender.tick(FACTS)
    assert len(peer.calls) == 1
    assert store.read('state.json')['enabled'] is before['enabled'] is True
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(store.read('inventory-status.json'))


def test_429_honors_bounded_separate_retry_budget(world):
    store, peer, clock, _, sender = world
    peer.error = TransportError(429, retry_after=600)
    assert sender.tick(FACTS) == 'SDC_INVENTORY_RETRY_LATER'
    assert store.read('inventory-status.json')['next_due'] == NOW + 600
    advance(store, clock, 300)
    assert sender.tick(FACTS) == 'SDC_INVENTORY_BACKOFF'
    assert len(peer.calls) == 1


def test_cancellation_and_clock_expiry_after_collection_prevent_dispatch(world):
    store, peer, clock, _, sender = world
    collect = sender.collector
    def canceled(**kwargs):
        result = collect(**kwargs)
        policy = store.read('inventory-policy.json')
        policy['enabled'] = False
        store.write('inventory-policy.json', policy)
        return result
    sender.collector = canceled
    assert sender.tick(FACTS) == 'SDC_INVENTORY_WAITING'
    assert peer.calls == []
    policy = store.read('inventory-policy.json')
    policy['enabled'] = True
    store.write('inventory-policy.json', policy)
    clock[0] += 901
    assert sender.tick(FACTS) == 'SDC_INVENTORY_CLOCK_UNTRUSTED'
    assert peer.calls == []


@pytest.mark.parametrize('reply', [{'code': 'stored', 'snapshot_id': DEVICE, 'state': 'receiving', 'request_id': REQUEST_ID}, {'code': 'accepted', 'snapshot_id': DEVICE, 'state': 'applied', 'request_id': REQUEST_ID}])
def test_mismatched_or_invalid_receipt_never_advances_the_page(world, reply):
    store, peer, _, _, sender = world
    peer.reply = reply
    assert sender.tick(FACTS) == 'SDC_INVENTORY_PROTOCOL_ERROR'
    assert store.read('inventory-status.json')['next_page'] == 0
    assert store.read('inventory-status.json')['stopped'] is True


@pytest.mark.parametrize('mutate', [lambda reply: {**reply, 'request_id': 'not-a-uuid'},
                                   lambda reply: {**reply, 'request_id': '44444444-4444-1444-8444-444444444444'},
                                   lambda reply: {key: value for key, value in reply.items() if key != 'request_id'},
                                   lambda reply: {**reply, 'unexpected': 'SYNTHETIC_PRIVATE_ERROR'}])
def test_success_receipt_requires_uuid4_request_id_and_rejects_unknown_fields(world, mutate):
    store, peer, _, _, sender = world
    peer.reply = mutate
    assert sender.tick(FACTS) == 'SDC_INVENTORY_PROTOCOL_ERROR'
    assert store.read('inventory-status.json')['next_page'] == 0
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(store.read('inventory-status.json'))


def test_corrupted_spool_is_never_transmitted(world):
    store, peer, clock, _, sender = world
    peer.error = TransportError()
    sender.tick(FACTS)
    spool = store.read('inventory-spool.json')
    page = json.loads(spool['pages'][0])
    page['runtime']['private_path'] = 'SYNTHETIC_PRIVATE_PATH'
    spool['pages'][0] = json.dumps(page)
    store.write('inventory-spool.json', spool)
    advance(store, clock, 300)
    assert sender.tick(FACTS) == 'SDC_INVENTORY_PROTOCOL_ERROR'
    assert len(peer.calls) == 1


def test_crash_between_spool_and_status_fsync_recovers_reserved_bytes(world, monkeypatch):
    store, peer, _, collections, sender = world
    write = store.write
    fail = [True]
    def interrupted(name, value):
        if name == 'inventory-status.json' and fail[0]:
            fail[0] = False
            raise OSError('synthetic fsync failure')
        write(name, value)
    monkeypatch.setattr(store, 'write', interrupted)
    with pytest.raises(OSError):
        sender.tick(FACTS)
    reserved = store.read('inventory-spool.json')['pages'][0].encode()
    assert peer.calls == []
    assert sender.tick(FACTS) == 'SDC_INVENTORY_STORED'
    assert peer.calls[0][1] == reserved
    assert len(collections) == 1


def test_deadline_is_not_converted_to_a_retry_or_new_collection(world):
    _, peer, _, _, sender = world
    def expired(**_):
        raise TimeoutError('SYNTHETIC_PRIVATE_DEADLINE')
    sender.collector = expired
    with pytest.raises(TimeoutError):
        sender.tick(FACTS)
    assert peer.calls == []


def test_inventory_exception_cannot_turn_a_successful_heartbeat_into_failure(world, monkeypatch, capsys):
    import sdc_cli
    import sys
    store, _, _, _, _ = world
    order = []
    class Heartbeat:
        def __init__(self, *_):
            pass
        def tick(self, _):
            order.append('heartbeat')
    class Inventory:
        def __init__(self, *_):
            pass
        def tick(self, _):
            order.append('inventory')
            raise TimeoutError('SYNTHETIC_PRIVATE_ERROR')
    monkeypatch.setattr(sdc_cli, 'Connector', Heartbeat)
    monkeypatch.setattr(sdc_cli, 'InventorySender', Inventory)
    monkeypatch.setattr(sdc_cli, 'Transport', lambda _: object())
    monkeypatch.setattr(sdc_cli, 'facts', lambda: FACTS)
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', lambda: {'runtime_mode': 'standalone'})
    monkeypatch.setattr(sdc_cli.mcs_runtime, 'python_executable', lambda _: sys.executable)
    monkeypatch.setattr(sdc_cli.time, 'sleep', lambda _: None)
    assert sdc_cli.tick(store.root) == 0
    assert order == ['heartbeat', 'inventory']
    assert capsys.readouterr().err == 'SDC_INVENTORY_UNAVAILABLE\n'


def test_stale_launchd_interpreter_cannot_collect_after_successful_heartbeat(world, monkeypatch, capsys):
    import sdc_cli
    store, _, _, _, _ = world
    order = []
    class Heartbeat:
        def __init__(self, *_):
            pass
        def tick(self, _):
            order.append('heartbeat')
    monkeypatch.setattr(sdc_cli, 'Connector', Heartbeat)
    monkeypatch.setattr(sdc_cli, 'InventorySender', lambda *_: pytest.fail('mismatched runtime must not collect'))
    monkeypatch.setattr(sdc_cli, 'Transport', lambda _: object())
    monkeypatch.setattr(sdc_cli, 'facts', lambda: FACTS)
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', lambda: {'runtime_mode': 'standalone'})
    monkeypatch.setattr(sdc_cli.mcs_runtime, 'python_executable', lambda _: '/synthetic/other-venv/bin/python')
    monkeypatch.setattr(sdc_cli.time, 'sleep', lambda _: None)
    assert sdc_cli.tick(store.root) == 0
    assert order == ['heartbeat']
    assert capsys.readouterr().err == 'SDC_INVENTORY_RUNTIME_UNKNOWN\n'
