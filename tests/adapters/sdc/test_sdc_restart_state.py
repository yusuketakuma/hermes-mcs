"""Synthetic restart consent persistence; no HTTP, Keychain, signatures or Runtime."""
import copy
import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest

from adapters.sdc import restart_state as local
from adapters.sdc.storage import RESTART_STATE_MAX_BYTES, Store

V = json.loads((Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/support/restart-wire-vectors.json').read_text())
NOW = V['now']
OTHER_ID = '00000000-0000-4000-8000-000000000063'
CANARY = 'SYNTHETIC_PRIVATE_NOT_FOR_OUTPUT'


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('ascii')).hexdigest()


@pytest.fixture
def world(tmp_path):
    with Store(tmp_path / 'private', create=True) as store:
        binding = {'root_fingerprint': store.root_fingerprint(), 'monitor_endpoint_hash': '1' * 64,
                   'support_endpoint_hash': '2' * 64, 'pins_sha256': '3' * 64, 'secret_sha256': '4' * 64,
                   **{k: V['expected'][k] for k in ('claim_id', 'device_id', 'key_id')}}
        local.initialize(store)
        yield store, binding


def prepare(world, now=NOW):
    return local.prepare(world[0], binding_supplier=lambda: world[1], now_ms=now)


def confirm(world, snapshot=None, preview=None, expected=None, *, now=NOW, human=True):
    snapshot = prepare(world, now) if snapshot is None else snapshot
    return local.confirm_consent(world[0], copy.deepcopy(V['preview']) if preview is None else preview,
        expected=copy.deepcopy(V['expected']) if expected is None else expected,
        expected_owner_generation=snapshot['owner_generation'], expected_binding=snapshot['current_binding'],
        binding_supplier=lambda: world[1], now_ms=now, confirm_human=human)


def admit(world, saved, now=NOW):
    return local.begin_accept(world[0], expected_owner_generation=saved['owner_generation'],
                              binding_supplier=lambda: world[1], now_ms=now)


def ack(request, *, accepted_at=NOW + 1, expires_at=None, **changes):
    body = json.loads(request['raw'])
    return {'schema_version': 'sdc.support.restart-acceptance.v1',
            **{k: v for k, v in body.items() if k != 'schema_version'},
            'operation_id': V['preview']['operation_id'], 'state': 'accepted',
            'accepted_at': accepted_at, 'expires_at': V['approval']['expires_at'] if expires_at is None else expires_at,
            'request_id': OTHER_ID, **changes}


def recorded(world):
    saved = confirm(world)
    request = admit(world, saved)
    response = ack(request)
    local.record_accept_ack(world[0], request, response, now_ms=NOW + 1)
    return saved, request, response


def test_explicit_initialization_is_disabled_and_never_replaces_state(world):
    store, _ = world
    before = (store.root / local.NAME).read_bytes()
    state = local.status(store, now_ms=NOW)
    assert not state['enabled'] and state['receipt'] is None and not state['uncertain']
    with pytest.raises(local.RestartStateError, match='CONFLICT'):
        local.initialize(store)
    assert (store.root / local.NAME).read_bytes() == before
    assert {p.name for p in store.root.iterdir()} == {local.NAME, 'restart-state.lock'}


def test_receipt_and_accept_ids_are_fixed_together_with_independent_hash_oracle(world, monkeypatch):
    identifiers = iter(['00000000-0000-4000-8000-000000000061', '00000000-0000-4000-8000-000000000062'])
    monkeypatch.setattr(local.uuid, 'uuid4', lambda: uuid.UUID(next(identifiers)))
    before = prepare(world)
    saved = confirm(world, before)
    receipt = saved['receipt']
    assert receipt['local_change_receipt_id'].endswith('061') and receipt['change_accept_id'].endswith('062')
    assert receipt['preview_payload_hash'] == digest(V['preview'])
    assert receipt['receipt_hash'] == digest({k: v for k, v in receipt.items() if k != 'receipt_hash'})
    body = {'schema_version': 'sdc.support.restart-control.v1', 'accept_id': receipt['change_accept_id'],
            'preview_id': V['preview']['preview_id'], 'change_consent_id': V['preview']['change_consent_id'],
            'change_binding_hash': V['preview']['change_binding_hash'], 'local_change_receipt_id': receipt['local_change_receipt_id'],
            'preview_payload_hash': digest(V['preview']), 'local_change_receipt_hash': receipt['receipt_hash']}
    request = saved['accept']['request']
    assert request == {'raw': canonical(body), 'body_hash': digest(body), 'admitted_at': None}
    assert saved['owner_generation'] != before['owner_generation'] and saved['enabled']
    assert receipt['confirmed_at'] == NOW and receipt['expires_at'] == min(V['intent']['expires_at'], V['approval']['expires_at'])
    saved['preview']['tenant'] = 'detached'
    saved['receipt']['params']['service_id'] = 'detached'
    assert local.status(world[0], now_ms=NOW)['preview'] == V['preview']
    assert os.fstat(world[0].fd)


@pytest.mark.parametrize('human', [False, None, 1, 'yes'])
def test_explicit_human_confirmation_is_required(world, human):
    before = world[0].read(local.NAME)
    with pytest.raises(local.RestartStateError, match='HUMAN_REQUIRED'):
        confirm(world, human=human)
    assert world[0].read(local.NAME) == before


@pytest.mark.parametrize('field', list(V['expected']))
@pytest.mark.parametrize('mutation', ['missing', 'mismatch'])
def test_all_independent_expected_fields_are_required_and_match(world, field, mutation):
    expected = copy.deepcopy(V['expected'])
    if mutation == 'missing':
        del expected[field]
    else:
        expected[field] = CANARY
    before = world[0].read(local.NAME)
    with pytest.raises(ValueError) as caught:
        confirm(world, expected=expected)
    assert CANARY not in str(caught.value) and world[0].read(local.NAME) == before


@pytest.mark.parametrize('preview', [None, {}, {'schema_version': 'sdc.support.consent-preview.v1'}, V['operation'], V['localReceipt']])
def test_only_typed_restart_preview_can_be_retained(world, preview):
    with pytest.raises(ValueError):
        local.confirm_consent(world[0], preview, expected=V['expected'],
            expected_owner_generation=prepare(world)['owner_generation'], expected_binding=world[1],
            binding_supplier=lambda: world[1], now_ms=NOW, confirm_human=True)
    assert world[0].read(local.NAME)['receipt'] is None


@pytest.mark.parametrize('field', ['intent_hash', 'change_binding_hash', 'approval_hash', 'preview_id'])
def test_changed_preview_hash_graph_or_expected_identity_is_rejected(world, field):
    value = copy.deepcopy(V['preview'])
    if field == 'approval_hash':
        value['approval'][field] = 'f' * 64
    elif field == 'preview_id':
        value[field] = 'not-a-uuid'
    else:
        value[field] = 'f' * 64
    with pytest.raises(ValueError):
        confirm(world, preview=value)
    assert world[0].read(local.NAME)['receipt'] is None


@pytest.mark.parametrize('field', ['root_fingerprint', 'monitor_endpoint_hash', 'support_endpoint_hash', 'pins_sha256',
                                  'secret_sha256', 'claim_id', 'device_id', 'key_id'])
@pytest.mark.parametrize('phase', ['confirmation', 'accept'])
def test_current_binding_change_rejects_admission(world, field, phase):
    snapshot = prepare(world)
    saved = confirm(world, snapshot) if phase == 'accept' else None
    world[1][field] = OTHER_ID if field.endswith('_id') else 'f' * 64
    before = world[0].read(local.NAME)
    with pytest.raises(local.RestartStateError, match='CONFLICT'):
        admit(world, saved) if saved is not None else confirm(world, snapshot)
    assert world[0].read(local.NAME) == before


def test_stale_owner_and_second_consent_never_replace_pending_evidence(world):
    snapshot = prepare(world)
    saved = confirm(world, snapshot)
    before = world[0].read(local.NAME)
    for stale in (snapshot, prepare(world)):
        with pytest.raises(local.RestartStateError, match='CONFLICT'):
            confirm(world, stale)
    with pytest.raises(local.RestartStateError, match='CONFLICT'):
        admit(world, snapshot)
    assert world[0].read(local.NAME) == before and saved['receipt'] == before['receipt']


def test_supplier_runs_under_state_then_tick_locks_and_failure_is_sanitized(world):
    store, binding = world
    def supplier():
        with Store(store.root) as other:
            for lock in (other.restart_state_lock, other.lock):
                with pytest.raises(BlockingIOError), lock():
                    pytest.fail('supplier outside both locks')
        return binding
    snapshot = local.prepare(store, binding_supplier=supplier, now_ms=NOW)
    saved = local.confirm_consent(store, V['preview'], expected=V['expected'],
        expected_owner_generation=snapshot['owner_generation'], expected_binding=binding,
        binding_supplier=supplier, now_ms=NOW, confirm_human=True)
    before = store.read(local.NAME)
    def unavailable():
        raise ValueError(CANARY)
    with pytest.raises(local.RestartStateError, match='BINDING_UNAVAILABLE') as caught:
        local.begin_accept(store, expected_owner_generation=saved['owner_generation'], binding_supplier=unavailable, now_ms=NOW)
    assert CANARY not in str(caught.value) and store.read(local.NAME) == before
    with Store(store.root) as other, other.restart_state_lock(), other.lock():
        assert os.fstat(store.fd)


def test_unknown_accept_reopens_only_original_admitted_bytes_after_preview_expiry(world):
    store, binding = world
    saved = confirm(world)
    request = admit(world, saved)
    later = V['preview']['expires_at'] + 1
    with Store(store.root) as resumed:
        replay = local.begin_accept(resumed, expected_owner_generation=saved['owner_generation'],
                                    binding_supplier=lambda: binding, now_ms=later)
    assert replay == request and world[0].read(local.NAME)['accept']['ack'] is None
    assert world[0].read(local.NAME)['receipt'] == saved['receipt']
    with pytest.raises(local.RestartStateError, match='EXPIRED'):
        admit(world, saved, now=saved['receipt']['expires_at'])


def test_unadmitted_expired_preview_cannot_start_first_send(world):
    saved = confirm(world)
    before = world[0].read(local.NAME)
    with pytest.raises(local.RestartStateError, match='EXPIRED'):
        admit(world, saved, now=V['preview']['expires_at'])
    assert world[0].read(local.NAME) == before


@pytest.mark.parametrize('at', [V['preview']['not_before'] - 1, V['preview']['expires_at']])
def test_confirmation_requires_original_preview_window(world, at):
    with pytest.raises(ValueError):
        confirm(world, now=at)
    assert world[0].read(local.NAME)['receipt'] is None


def test_first_ack_is_fixed_and_only_http_tracking_id_can_change(world):
    store, _ = world
    saved, request, response = recorded(world)
    before = store.read(local.NAME)
    changed = {**response, 'request_id': '00000000-0000-4000-8000-000000000064'}
    result = local.record_accept_ack(store, request, changed, now_ms=NOW + 2)
    assert result['accept']['ack'] == {k: v for k, v in response.items() if k != 'request_id'}
    assert store.read(local.NAME) == before
    assert local.require_primary_acceptance(store, result['accept']['ack'], now_ms=NOW + 2) == result['accept']['ack']
    with pytest.raises(local.RestartStateError, match='CONFLICT'):
        admit(world, saved, now=NOW + 2)


@pytest.mark.parametrize('field', ['accepted_at', 'expires_at', 'operation_id', 'accept_id', 'preview_id',
                                  'change_consent_id', 'local_change_receipt_id', 'change_binding_hash',
                                  'preview_payload_hash', 'local_change_receipt_hash'])
def test_ack_or_later_acceptance_cannot_replace_primary_fields(world, field):
    store, _ = world
    _, request, response = recorded(world)
    changed = copy.deepcopy(response)
    changed[field] = (response[field] + 1 if field == 'accepted_at' else response[field] - 1 if field == 'expires_at'
                      else OTHER_ID if field.endswith('_id') else 'f' * 64)
    before = store.read(local.NAME)
    with pytest.raises(ValueError):
        local.record_accept_ack(store, request, changed, now_ms=NOW + 2)
    with pytest.raises(ValueError):
        local.require_primary_acceptance(store, {k: v for k, v in changed.items() if k != 'request_id'}, now_ms=NOW + 2)
    assert store.read(local.NAME) == before


@pytest.mark.parametrize('changes', [{'operation_id': OTHER_ID}, {'accepted_at': NOW - 1},
                                    {'accepted_at': V['preview']['expires_at']},
                                    {'expires_at': V['approval']['expires_at'] + 1}])
def test_first_ack_binds_operation_confirmation_preview_and_receipt_expiry(world, changes):
    saved = confirm(world)
    request = admit(world, saved)
    before = world[0].read(local.NAME)
    with pytest.raises(ValueError):
        local.record_accept_ack(world[0], request, ack(request, **changes), now_ms=V['preview']['expires_at'])
    assert world[0].read(local.NAME) == before


def test_ack_without_durable_admission_or_for_different_raw_is_rejected(world):
    saved = confirm(world)
    request = saved['accept']['request']
    with pytest.raises(ValueError):
        local.record_accept_ack(world[0], request, ack(request), now_ms=NOW + 1)
    request = admit(world, saved)
    for changed in ({**request, 'raw': request['raw'] + ' '}, {**request, 'body_hash': 'f' * 64},
                    {**request, 'admitted_at': NOW + 1}):
        with pytest.raises(ValueError):
            local.record_accept_ack(world[0], changed, ack(request), now_ms=NOW + 1)
    assert world[0].read(local.NAME)['accept']['ack'] is None


def test_disable_preserves_pending_ack_without_reenable(world):
    saved = confirm(world)
    request = admit(world, saved)
    before = copy.deepcopy(saved['receipt'])
    local.disable(world[0])
    disabled = world[0].read(local.NAME)
    for action in (lambda: confirm(world), lambda: admit(world, saved)):
        with pytest.raises(local.RestartStateError, match='CONFLICT'):
            action()
    late = local.record_accept_ack(world[0], request, ack(request), now_ms=NOW + 1)
    assert not late['enabled'] and late['disabled'] and late['owner_generation'] == disabled['owner_generation']
    assert late['receipt'] == before and late['accept']['ack'] is not None
    assert local.disable(world[0])['owner_generation'] == disabled['owner_generation']


def test_disabled_empty_state_cannot_be_reenabled(world):
    local.disable(world[0])
    before = world[0].read(local.NAME)
    with pytest.raises(local.RestartStateError, match='CONFLICT'):
        confirm(world)
    with pytest.raises(local.RestartStateError, match='CONFLICT'):
        local.initialize(world[0])
    assert world[0].read(local.NAME) == before


def test_status_and_disable_need_neither_tick_endpoint_nor_keychain(world, monkeypatch):
    store, _ = world
    saved = confirm(world)
    monkeypatch.setattr(store, '_marker', lambda: pytest.fail('credential/endpoint access'))
    monkeypatch.setattr(store, '_keychain', lambda: pytest.fail('Keychain access'))
    with Store(store.root) as other, other.lock():
        assert local.status(store, now_ms=NOW)['receipt'] == saved['receipt']
        assert local.disable(store)['disabled']
    assert not local.status(store, now_ms=NOW)['enabled']


def test_state_lock_contention_fails_without_false_stop_success(world):
    store, _ = world
    before = store.read(local.NAME)
    with Store(store.root) as other, other.restart_state_lock():
        for action in (lambda: local.disable(store), lambda: local.status(store, now_ms=NOW), lambda: prepare(world)):
            with pytest.raises(BlockingIOError):
                action()
    assert store.read(local.NAME) == before


@pytest.mark.parametrize('phase', ['confirmation', 'admission', 'ack', 'disable'])
@pytest.mark.parametrize('failure', ['file_fsync', 'replace', 'directory_fsync'])
def test_write_failures_keep_ram_uncertain_and_never_admit_retry(world, monkeypatch, phase, failure):
    store, _ = world
    snapshot = prepare(world)
    saved = confirm(world, snapshot) if phase != 'confirmation' else None
    request = admit(world, saved) if phase == 'ack' else None
    before = (store.root / local.NAME).read_bytes()
    original_sync, original_replace = os.fsync, os.replace
    fired = []
    def fsync(fd):
        if not fired and ((failure == 'directory_fsync' and fd == store.fd) or (failure == 'file_fsync' and fd != store.fd)):
            fired.append(True)
            raise OSError(CANARY)
        return original_sync(fd)
    def replace(src, dst, **kwargs):
        if dst == local.NAME and failure == 'replace':
            fired.append(True)
            raise OSError(CANARY)
        return original_replace(src, dst, **kwargs)
    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)
    action = {'confirmation': lambda: confirm(world, snapshot), 'admission': lambda: admit(world, saved),
              'ack': lambda: local.record_accept_ack(store, request, ack(request), now_ms=NOW + 1),
              'disable': lambda: local.disable(store)}[phase]
    with pytest.raises(local.RestartStateError, match='UNCERTAIN') as caught:
        action()
    assert CANARY not in str(caught.value) and fired
    state = local.status(store, now_ms=NOW + 1)
    assert state['uncertain'] and not state['enabled'] and os.fstat(store.fd)
    assert not list(store.root.glob('.sdc-*'))
    if failure != 'directory_fsync':
        assert (store.root / local.NAME).read_bytes() == before
    for retry in (action, lambda: prepare(world), lambda: local.disable(store)):
        with pytest.raises(local.RestartStateError, match='UNCERTAIN'):
            retry()
    if phase == 'ack' and failure == 'directory_fsync':
        with pytest.raises(local.RestartStateError, match='UNCERTAIN'):
            local.require_primary_acceptance(store, state['accept']['ack'], now_ms=NOW + 1)


def test_interrupted_write_keeps_uncertain_without_swallowing_cancellation(world, monkeypatch):
    snapshot = prepare(world)
    def interrupted(_):
        raise KeyboardInterrupt()
    monkeypatch.setattr(os, 'fsync', interrupted)
    with pytest.raises(KeyboardInterrupt):
        confirm(world, snapshot)
    assert local.status(world[0], now_ms=NOW)['uncertain']
    with pytest.raises(local.RestartStateError, match='UNCERTAIN'):
        confirm(world, snapshot)


def test_initial_provision_failure_cannot_be_silently_retried(tmp_path, monkeypatch):
    with Store(tmp_path / 'fresh', create=True) as store:
        def failed(_):
            raise OSError(CANARY)
        monkeypatch.setattr(os, 'fsync', failed)
        with pytest.raises(local.RestartStateError, match='UNCERTAIN'):
            local.initialize(store)
        assert not (store.root / local.NAME).exists()
        with pytest.raises(local.RestartStateError, match='UNCERTAIN'):
            local.initialize(store)


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'extra_field', 'receipt_hash', 'accept_raw'])
def test_missing_or_corrupt_state_never_auto_initializes_or_discards_evidence(world, damage):
    saved = confirm(world)
    store, _ = world
    path = store.root / local.NAME
    if damage == 'missing':
        path.unlink()
    elif damage == 'corrupt':
        path.write_text('{')
    else:
        state = store.read(local.NAME)
        if damage == 'extra_field':
            state['unknown_history'] = [CANARY]
        elif damage == 'receipt_hash':
            state['receipt']['receipt_hash'] = 'f' * 64
        else:
            state['accept']['request']['raw'] += ' '
        path.write_text(json.dumps(state))
    before = path.read_bytes() if path.exists() else None
    for action in (lambda: prepare(world), lambda: local.status(store, now_ms=NOW),
                   lambda: local.disable(store), lambda: admit(world, saved)):
        with pytest.raises(ValueError):
            action()
    assert (path.read_bytes() if path.exists() else None) == before


def test_root_path_swap_and_state_copy_cannot_rebind_consent(world, tmp_path):
    store, _ = world
    confirm(world)
    copied = store.read(local.NAME)
    with Store(tmp_path / 'other', create=True) as other:
        other.write(local.NAME, copied)
        with pytest.raises(local.RestartStateError, match='CONFLICT'):
            local.status(other, now_ms=NOW)
    retained = store.root.with_name('retained')
    store.root.rename(retained)
    store.root.mkdir(mode=0o700)
    for action in (lambda: local.status(store, now_ms=NOW), lambda: local.disable(store)):
        with pytest.raises(local.RestartStateError):
            action()
    assert not list(store.root.iterdir())


def test_local_projection_expires_without_mutating_primary_evidence(world):
    saved = confirm(world)
    request = admit(world, saved)
    expires = NOW + 10
    local.record_accept_ack(world[0], request, ack(request, expires_at=expires), now_ms=NOW + 1)
    before = world[0].read(local.NAME)
    assert not local.status(world[0], now_ms=expires)['enabled']
    assert world[0].read(local.NAME) == before
    assert local.require_primary_acceptance(world[0], before['accept']['ack'], now_ms=expires) == before['accept']['ack']


def test_capacity_is_derived_from_wire_records_and_disable_does_not_grow_history(world):
    recorded(world)
    assert RESTART_STATE_MAX_BYTES == 5 * 8192 + 2 * 16384 + 1024
    before = len((world[0].root / local.NAME).read_bytes())
    local.disable(world[0])
    after = len((world[0].root / local.NAME).read_bytes())
    assert after <= before <= RESTART_STATE_MAX_BYTES
