"""Synthetic local consent/CAS oracles; no HTTP, Keychain, process or real state."""
import copy
import json
import os

import pytest

from adapters.sdc import support_state as local
from adapters.sdc.storage import Store
from adapters.sdc.support_contract import hash_consent_binding

NOW = 1792663200000
KEY_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}


def preview(*, now=NOW, **changes):
    value = {'schema_version': 'sdc.support.consent-preview.v1', 'preview_id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
             'product': 'hermes-mcs', 'audience': CONFIG['audience'], 'tenant': 'tenant-a', 'installation': 'installation-a',
             'device_id': '22222222-2222-4222-8222-222222222222', 'environment': 'synthetic',
             'case_id': '33333333-3333-4333-8333-333333333333', 'claim_id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
             'key_id': KEY_ID, 'purpose_code': 'HEALTH_CHECK', 'capability_revision': 'sdc-support-capabilities-v1',
             'diagnostic_ids': ['runtime.status.v1'], 'operator_refs': ['a' * 64], 'approved_by': 'b' * 64,
             'consent_revision': 'sdc-support-consent-v1', 'realm_epoch': 'opaque-9', 'policy_epoch': 1,
             'case_epoch': 1, 'local_consent_epoch': 1, 'device_epoch': 1, 'issued_at': now, 'not_before': now,
             'expires_at': now + 300000, 'consent_expires_at': now + 1800000, **changes}
    value['consent_binding_hash'] = hash_consent_binding(value)
    return value


@pytest.fixture
def world(tmp_path):
    with Store(tmp_path / 'private', create=True) as store:
        store.write('endpoint.json', CONFIG)
        owner = store.enrollment_binding()
        binding = {'root_fingerprint': owner['root_fingerprint'], 'monitor_endpoint_hash': owner['endpoint_hash'],
                   'support_endpoint_hash': '1' * 64, 'pins_sha256': '2' * 64, 'secret_sha256': '3' * 64, 'key_id': KEY_ID}
        yield store, binding


def prepare(world, *, now=NOW, initialize=True):
    store, binding = world
    return local.prepare(store, binding_supplier=lambda: binding, now_ms=now, initialize=initialize)


def confirm(world, snapshot, value=None, *, now=NOW, human=True):
    store, binding = world
    return local.confirm_consent(store, preview() if value is None else value,
                                 expected_owner_generation=snapshot['owner_generation'], expected_binding=snapshot['binding'],
                                 binding_supplier=lambda: binding, now_ms=now, confirm_human=human)


def test_missing_is_hold_and_explicit_first_provision_is_disabled(world):
    store, _ = world
    with pytest.raises(local.SupportStateError, match='SUPPORT_STATE_MISSING'):
        prepare(world, initialize=False)
    assert store.read(local.NAME) is None
    snapshot = prepare(world)
    assert snapshot['enabled'] is False and snapshot['receipt'] is None
    assert store.read(local.NAME)['tombstones'] == []
    assert (store.root / local.NAME).stat().st_mode & 0o777 == 0o600
    assert os.fstat(store.fd)


@pytest.mark.parametrize('raw', [b'{', b'[]', b'{"secret":"SYNTHETIC_PRIVATE"}', b'{"x":1,"x":2}', b'\xff'])
def test_corrupt_state_is_never_repaired_or_echoed(world, raw):
    store, _ = world
    path = store.root / local.NAME
    path.write_bytes(raw)
    path.chmod(0o600)
    for call in (lambda: prepare(world), lambda: local.disable(store, now_ms=NOW)):
        with pytest.raises(local.SupportStateError) as caught:
            call()
        assert 'SYNTHETIC_PRIVATE' not in str(caught.value)
        assert path.read_bytes() == raw


@pytest.mark.parametrize('human', [False, 1, 0, None, 'yes'])
def test_general_flags_and_truthy_values_are_not_human_consent(world, human):
    snapshot = prepare(world)
    before = world[0].read(local.NAME)
    with pytest.raises(local.SupportStateError, match='SUPPORT_HUMAN_REQUIRED'):
        confirm(world, snapshot, human=human)
    assert world[0].read(local.NAME) == before


def test_confirm_is_detached_atomic_and_keeps_borrowed_fd(world):
    store, binding = world
    snapshot, value = prepare(world), preview()
    accepted = confirm(world, snapshot, value)
    assert accepted['enabled'] and accepted['owner_generation'] != snapshot['owner_generation']
    assert accepted['receipt']['accepted'] is True
    receipt_id = accepted['receipt']['local_receipt_id']
    accepted['receipt']['preview']['operator_refs'].append('f' * 64)
    value['operator_refs'].append('e' * 64)
    binding_copy = copy.deepcopy(binding)
    snapshot['binding']['pins_sha256'] = 'e' * 64
    saved = prepare(world, initialize=False)
    assert saved['receipt']['preview']['operator_refs'] == ['a' * 64]
    assert saved['receipt']['local_receipt_id'] == receipt_id
    assert saved['binding'] == binding_copy and os.fstat(store.fd)
    assert set(store.read(local.NAME)) == {'schema_version', 'root_fingerprint', 'owner_generation', 'enabled', 'receipt', 'tombstones'}
    assert 'credential' not in {path.name for path in store.root.iterdir()}


def test_two_previews_and_disable_invalidate_stale_owner_generation(world):
    first, stale = prepare(world), prepare(world, initialize=False)
    accepted = confirm(world, first)
    with pytest.raises(local.SupportStateError, match='SUPPORT_STATE_CONFLICT'):
        confirm(world, stale)
    disabled = local.disable(world[0], now_ms=NOW)
    assert disabled['enabled'] is False and disabled['owner_generation'] != accepted['owner_generation']
    with pytest.raises(local.SupportStateError, match='SUPPORT_STATE_CONFLICT'):
        confirm(world, accepted)
    assert world[0].read(local.NAME)['tombstones']


@pytest.mark.parametrize('field', ['root_fingerprint', 'monitor_endpoint_hash', 'support_endpoint_hash', 'pins_sha256', 'secret_sha256', 'key_id'])
def test_binding_changed_after_display_is_rejected(world, field):
    snapshot = prepare(world)
    world[1][field] = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd' if field == 'key_id' else 'd' * 64
    before = world[0].read(local.NAME)
    with pytest.raises(local.SupportStateError, match='SUPPORT_STATE_CONFLICT'):
        confirm(world, snapshot)
    assert world[0].read(local.NAME) == before


def test_supplier_is_called_only_under_both_locks_and_failure_saves_nothing(world):
    store, binding = world
    calls = []
    def supplier():
        with Store(store.root) as other:
            for lock in (other.support_state_lock, other.lock):
                with pytest.raises(BlockingIOError), lock():
                    pytest.fail('supplier outside lock')
        calls.append(True)
        return binding
    snapshot = local.prepare(store, binding_supplier=supplier, now_ms=NOW, initialize=True)
    accepted = local.confirm_consent(store, preview(), expected_owner_generation=snapshot['owner_generation'],
                                     expected_binding=snapshot['binding'], binding_supplier=supplier, now_ms=NOW, confirm_human=True)
    assert calls == [True, True]
    before = store.read(local.NAME)
    def unavailable():
        raise ValueError('SYNTHETIC_PRIVATE')
    with pytest.raises(local.SupportStateError, match='SUPPORT_BINDING_UNAVAILABLE') as caught:
        local.confirm_consent(store, preview(), expected_owner_generation=accepted['owner_generation'],
                              expected_binding=accepted['binding'], binding_supplier=unavailable, now_ms=NOW, confirm_human=True)
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value) and store.read(local.NAME) == before
    with Store(store.root) as other, other.support_state_lock(), other.lock():
        assert os.fstat(store.fd)


def test_state_lockbusy_is_not_disable_success_and_saves_nothing(world):
    store, _ = world
    snapshot = prepare(world)
    before = store.read(local.NAME)
    with Store(store.root) as other, other.support_state_lock():
        with pytest.raises(BlockingIOError):
            local.disable(store, now_ms=NOW)
        with pytest.raises(BlockingIOError):
            local.prepare(store, binding_supplier=lambda: pytest.fail('busy supplier'), now_ms=NOW)
        with pytest.raises(BlockingIOError):
            confirm(world, snapshot)
    assert store.read(local.NAME) == before
    with Store(store.root) as other, other.support_state_lock(), other.lock():
        assert os.fstat(store.fd)


def test_same_pair_same_binding_is_idempotent_but_other_binding_is_not(world):
    accepted = confirm(world, prepare(world))
    refreshed = preview(now=NOW + 1, consent_expires_at=NOW + 1800000)
    repeated = confirm(world, accepted, refreshed, now=NOW + 1)
    assert repeated['owner_generation'] == accepted['owner_generation']
    assert repeated['receipt']['local_receipt_id'] == accepted['receipt']['local_receipt_id']
    changed = preview(operator_refs=['c' * 64])
    with pytest.raises(local.SupportStateError, match='SUPPORT_STATE_CONFLICT'):
        confirm(world, accepted, changed)
    world[1]['secret_sha256'] = 'e' * 64
    fresh_display = prepare(world, initialize=False)
    assert not fresh_display['enabled']
    with pytest.raises(local.SupportStateError, match='SUPPORT_STATE_CONFLICT'):
        confirm(world, fresh_display)


@pytest.mark.parametrize('case_epoch,local_epoch', [(1, 1), (2, 1), (1, 2)])
def test_disabled_pair_and_partial_epoch_advance_cannot_reenable(world, case_epoch, local_epoch):
    confirm(world, prepare(world))
    local.disable(world[0], now_ms=NOW)
    snapshot = prepare(world, initialize=False)
    with pytest.raises(local.SupportStateError, match='SUPPORT_EPOCH_RETIRED'):
        confirm(world, snapshot, preview(case_epoch=case_epoch, local_consent_epoch=local_epoch))
    assert not world[0].read(local.NAME)['enabled']


def test_both_new_epochs_allow_new_explicit_consent_and_keep_old_tombstone(world):
    confirm(world, prepare(world))
    local.disable(world[0], now_ms=NOW)
    assert confirm(world, prepare(world, initialize=False), preview(case_epoch=2, local_consent_epoch=2))['enabled']
    saved = world[0].read(local.NAME)
    assert len(saved['tombstones']) == 1 and saved['tombstones'][0]['case_epoch'] == 1


@pytest.mark.parametrize('changes', [{'realm_epoch': 'opaque-10'}, {'case_id': 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'}])
def test_new_realm_or_case_is_separate_without_ordering_opaque_realm(world, changes):
    confirm(world, prepare(world), preview(case_epoch=9, local_consent_epoch=9))
    local.disable(world[0], now_ms=NOW)
    assert confirm(world, prepare(world, initialize=False), preview(**changes))['enabled']


def test_expiry_only_changes_read_projection_and_old_pair_cannot_extend_it(world):
    store, _ = world
    confirm(world, prepare(world))
    before = store.read(local.NAME)
    late = NOW + 1800000
    snapshot = prepare(world, now=late, initialize=False)
    assert not snapshot['enabled'] and snapshot['receipt'] == before['receipt']
    assert store.read(local.NAME) == before
    with pytest.raises(local.SupportStateError, match='SUPPORT_CONSENT_EXPIRED'):
        confirm(world, snapshot, preview(now=late), now=late)
    assert confirm(world, snapshot, preview(now=late, case_epoch=2, local_consent_epoch=2), now=late)['enabled']


def test_confirm_before_preview_start_or_after_preview_deadline_is_closed(world):
    snapshot = prepare(world)
    before = world[0].read(local.NAME)
    for at in (NOW - 1, NOW + 300000):
        with pytest.raises(local.SupportStateError):
            confirm(world, snapshot, now=at)
    assert world[0].read(local.NAME) == before


def test_disable_works_with_locked_keychain_without_a_supplier(world):
    store, _ = world
    store.enable_customer()
    store._keychain_factory = lambda *_: pytest.fail('disable must not touch Keychain')
    confirm(world, prepare(world))
    assert local.disable(store, now_ms=NOW)['enabled'] is False
    assert not store.read(local.NAME)['enabled']


@pytest.mark.parametrize('failure', ['confirm_replace', 'disable_replace', 'post_replace_fsync'])
def test_atomic_failures_never_fake_success_or_leave_partial_records(world, monkeypatch, failure):
    store, _ = world
    snapshot = prepare(world)
    if failure == 'disable_replace':
        confirm(world, snapshot)
    path = store.root / local.NAME
    before = path.read_bytes()
    replace, fsync = os.replace, os.fsync
    fired = []
    def replace_file(src, dst, **kwargs):
        if dst == local.NAME and failure != 'post_replace_fsync':
            fired.append(True)
            raise OSError('SYNTHETIC_DISK_FAILURE')
        return replace(src, dst, **kwargs)
    def sync(fd):
        if fd == store.fd and failure == 'post_replace_fsync' and not fired:
            fired.append(True)
            raise OSError('SYNTHETIC_DISK_FAILURE')
        return fsync(fd)
    monkeypatch.setattr(os, 'replace', replace_file)
    monkeypatch.setattr(os, 'fsync', sync)
    with pytest.raises(OSError):
        if failure == 'disable_replace':
            local.disable(store, now_ms=NOW)
        else:
            confirm(world, snapshot)
    assert fired and not list(store.root.glob('.sdc-*'))
    if failure == 'post_replace_fsync':
        assert prepare(world, initialize=False)['enabled']
        with pytest.raises(local.SupportStateError, match='SUPPORT_STATE_CONFLICT'):
            confirm(world, snapshot)
    else:
        assert path.read_bytes() == before
    assert os.fstat(store.fd)


def test_full_tombstones_hold_new_consent_without_evicting_history(world):
    store, _ = world
    snapshot = prepare(world)
    state = store.read(local.NAME)
    state['tombstones'] = [{'scope_sha256': format(index, '064x'), 'case_epoch': 1, 'local_consent_epoch': 1} for index in range(128)]
    store.write(local.NAME, state)
    before = (store.root / local.NAME).read_bytes()
    with pytest.raises(local.SupportStateError, match='SUPPORT_TOMBSTONE_FULL'):
        confirm(world, snapshot)
    assert (store.root / local.NAME).read_bytes() == before


def test_one_slot_is_reserved_so_disable_can_fill_last_tombstone(world):
    store, _ = world
    snapshot = prepare(world)
    state = store.read(local.NAME)
    state['tombstones'] = [{'scope_sha256': format(index, '064x'), 'case_epoch': 1, 'local_consent_epoch': 1} for index in range(127)]
    store.write(local.NAME, state)
    confirm(world, snapshot)
    assert local.disable(store, now_ms=NOW)['enabled'] is False
    assert len(store.read(local.NAME)['tombstones']) == 128
    before = store.read(local.NAME)
    with pytest.raises(local.SupportStateError, match='SUPPORT_TOMBSTONE_FULL'):
        confirm(world, prepare(world, initialize=False), preview(case_epoch=2, local_consent_epoch=2))
    assert store.read(local.NAME) == before


def test_byte_budget_reserves_future_disable_before_enabling(world, monkeypatch):
    store, _ = world
    snapshot = prepare(world)
    accepted = confirm(world, snapshot)
    active = store.read(local.NAME)
    future = {**active, 'enabled': False, 'tombstones': [local._epoch(preview())]}
    limit = len(json.dumps(future, ensure_ascii=True, separators=(',', ':')).encode()) - 1
    # Recreate an explicitly provisioned disabled fixture, not a production repair.
    active.update(enabled=False, receipt=None, tombstones=[], owner_generation=snapshot['owner_generation'])
    store.write(local.NAME, active)
    monkeypatch.setattr(local, 'MAX_BYTES', limit)
    before = store.read(local.NAME)
    with pytest.raises(local.SupportStateError, match='SUPPORT_TOMBSTONE_FULL'):
        confirm(world, snapshot)
    assert store.read(local.NAME) == before and not accepted['receipt']['binding'].get('secret')


def test_copied_self_state_cannot_be_disabled_as_a_different_root(world, tmp_path):
    store, _ = world
    confirm(world, prepare(world))
    raw = (store.root / local.NAME).read_bytes()
    with Store(tmp_path / 'copied', create=True) as copied:
        path = copied.root / local.NAME
        path.write_bytes(raw)
        path.chmod(0o600)
        with pytest.raises(local.SupportStateError, match='SUPPORT_STATE_CONFLICT'):
            local.disable(copied, now_ms=NOW)
        assert path.read_bytes() == raw
    assert (store.root / local.NAME).read_bytes() == raw


@pytest.mark.parametrize('replacement', [False, True])
def test_borrowed_root_renamed_or_replaced_is_not_a_successful_disable(world, replacement):
    store, _ = world
    confirm(world, prepare(world))
    raw = (store.root / local.NAME).read_bytes()
    old = store.root.with_name('old-private')
    store.root.rename(old)
    if replacement:
        store.root.mkdir(mode=0o700)
    with pytest.raises(local.SupportStateError):
        local.disable(store, now_ms=NOW)
    assert (old / local.NAME).read_bytes() == raw
    assert not (store.root / local.NAME).exists()
    assert os.fstat(store.fd)


@pytest.mark.parametrize('customer', [False, True])
@pytest.mark.parametrize('endpoint_problem', ['missing', 'corrupt'])
def test_local_disable_needs_only_root_even_when_monitor_endpoint_is_unusable(world, customer, endpoint_problem):
    store, _ = world
    if customer:
        store.enable_customer()
        store._keychain_factory = lambda *_: pytest.fail('local disable touched Keychain')
    confirm(world, prepare(world))
    endpoint = store.root / 'endpoint.json'
    if endpoint_problem == 'missing':
        endpoint.unlink()
    else:
        endpoint.write_bytes(b'SYNTHETIC_INVALID_ENDPOINT')
    assert local.disable(store, now_ms=NOW)['enabled'] is False
    state = store.read(local.NAME)
    assert state['enabled'] is False and len(state['tombstones']) == 1


def test_local_customer_disable_survives_corrupt_monitor_credential_marker(world):
    store, _ = world
    store.enable_customer()
    store._keychain_factory = lambda *_: pytest.fail('local disable touched Keychain')
    confirm(world, prepare(world))
    (store.root / 'credential-store.json').write_bytes(b'SYNTHETIC_INVALID_MARKER')
    assert local.disable(store, now_ms=NOW)['enabled'] is False
    assert store.read(local.NAME)['enabled'] is False
    # The exception is limited to local support state; credentials remain fail-closed.
    with pytest.raises(ValueError):
        store.read('credential')


def test_disable_is_independent_of_another_store_monitor_http_lock(world):
    store, _ = world
    confirm(world, prepare(world))
    store.write('state.json', {'state': 'active', 'enabled': True, 'key_id': KEY_ID, 'sequence': 7})
    store.write('credential', {'key': 'a1' * 32, 'key_id': KEY_ID})
    monitor_before = {name: (store.root / name).read_bytes() for name in ('state.json', 'credential')}
    with Store(store.root) as other, other.lock():
        assert local.disable(store, now_ms=NOW)['enabled'] is False
        assert store.read(local.NAME)['enabled'] is False
        assert all((store.root / name).read_bytes() == raw for name, raw in monitor_before.items())
    lock = store.root / 'support-state.lock'
    assert lock.stat().st_size == 0 and lock.stat().st_mode & 0o777 == 0o600
    assert os.fstat(store.fd)


@pytest.mark.parametrize('phase', ['initialize', 'prepare', 'confirm'])
def test_monitor_lockbusy_cannot_save_consent_and_releases_state_lock(world, phase):
    store, _ = world
    snapshot = prepare(world) if phase != 'initialize' else None
    before = store.read(local.NAME)
    with Store(store.root) as other, other.lock():
        with pytest.raises(BlockingIOError):
            if phase == 'confirm':
                local.confirm_consent(store, preview(), expected_owner_generation=snapshot['owner_generation'],
                                      expected_binding=snapshot['binding'], binding_supplier=lambda: pytest.fail('busy supplier'),
                                      now_ms=NOW, confirm_human=True)
            else:
                local.prepare(store, binding_supplier=lambda: pytest.fail('busy supplier'), now_ms=NOW, initialize=phase == 'initialize')
        assert store.read(local.NAME) == before
        with other.support_state_lock():
            assert os.fstat(store.fd)
    with Store(store.root) as other, other.support_state_lock(), other.lock():
        pass
