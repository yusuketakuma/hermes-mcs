"""Synthetic independent oracles for diagnostic consent, immutable sends and cancellation."""
import hashlib
import json
import os
import uuid

import pytest

from adapters.sdc import diagnostic_state as local
from adapters.sdc.diagnostic_contract import DiagnosticContractError
from adapters.sdc.storage import Store

NOW = 1800000000000
DAY = 86400000
IDENTIFIERS = [f'00000000-0000-4000-8000-{n:012d}' for n in range(1, 8)]


def target(store):
    return {'root_fingerprint': store.root_fingerprint(), 'monitor_endpoint_hash': 'a' * 64,
            'support_endpoint_hash': 'b' * 64, 'root_id': IDENTIFIERS[0], 'tenant': 'self',
            'installation': 'fixture', 'realm_epoch': 'realm-1', 'device_epoch': 1,
            'device_id': IDENTIFIERS[1], 'claim_id': IDENTIFIERS[2], 'key_id': IDENTIFIERS[3],
            'environment': 'test', 'audience': 'fixture'}


@pytest.fixture
def world(tmp_path):
    with Store(tmp_path / 'private', create_new=True,
               keychain_factory=lambda **_: pytest.fail('credential accessed')) as store:
        local.enable(store, binding=target(store), now_ms=NOW, expires_at=NOW + 30 * DAY,
                     confirm_human=True, reason='synthetic-consent')
        yield store


def saved(store):
    return store.read(local.NAME)


def event(at=NOW, *, component='runtime', count=1):
    return {'component': component, 'stage': 'run', 'code': 'UNAVAILABLE', 'severity': 'warning',
            'bucket_start': at // 600000 * 600000, 'count': count}


def status_reply(request, *, state='active', epoch=1, expires=NOW + 20 * DAY, accepted=False):
    ids = json.loads(request['raw'])['batch_ids']
    return {'schema_version': 'sdc.diagnostics.status.v1', 'request_id': IDENTIFIERS[4],
            'consent': {'state': state, 'consent_epoch': epoch if state == 'active' else None,
                        'expires_at': expires if state == 'active' else None},
            'batches': [{'batch_id': i, 'state': 'accepted' if accepted else 'unknown',
                         'content_state': 'stored' if accepted else None} for i in ids]}


def permit(store, *, at=NOW, epoch=1):
    request = local.admit(store, current=target(store), now_ms=at)
    assert request['path'] == local.STATUS
    assert json.loads(request['raw'])['batch_ids'] == []
    return local.record_response(store, request, status_reply(request, epoch=epoch),
                                 current=target(store), now_ms=at)


def sample(events=None, *, at=NOW, signature='c' * 64, dropped=0):
    return {'events': [event(at)] if events is None else events, 'dropped_events': dropped,
            'partial': False, 'observed_at': at,
            'source_marks': {'runtime': {'observed_at': at, 'signature': signature}}}


def collected(store, *, at=NOW, events=None):
    token = permit(store, at=at)
    local.collect(store, token, sample(events, at=at), current=target(store), now_ms=at)


def receipt(request, *, at=NOW + 300000):
    body = json.loads(request['raw'])
    return {'schema_version': 'sdc.diagnostics.receipt.v1', 'request_id': IDENTIFIERS[4],
            'batch_id': body['batch_id'], 'state': 'accepted', 'content_state': 'stored',
            'consent_epoch': body['consent_epoch'], 'event_count': len(body['events']),
            'dropped_events': body['dropped_events'], 'partial': body['partial'],
            'accepted_at': at, 'content_expires_at': at + 7 * DAY}


def report_request(store, *, at=NOW + 300000):
    request = local.admit(store, current=target(store), now_ms=at)
    assert request['path'] == local.REPORT
    return request


def test_explicit_anchor_and_no_credential_or_implicit_repair(world):
    anchor = world.read(local.ANCHOR)
    assert anchor['anchor_id'] == saved(world)['anchor_id']
    assert os.stat(world.root / local.ANCHOR).st_mode & 0o777 == 0o600
    os.unlink(world.root / local.NAME)
    with Store(world.root) as reopened:
        with pytest.raises(local.DiagnosticStateError, match='STATE_MISSING'):
            local.enable(reopened, binding=target(reopened), now_ms=NOW, expires_at=NOW + DAY,
                         confirm_human=True, reason='do-not-repair')
    assert world.read(local.ANCHOR) == anchor


def test_managed_bundle_hash_is_local_only_and_old_base_binding_requires_explicit_renew(world):
    old = saved(world)
    current = {**target(world), 'support_bundle_hash': 'e' * 64}
    assert local.validate_binding(current) == current
    assert local.admit(world, current=current, now_ms=NOW) is None
    held = saved(world)
    assert held['enabled'] is False and held['binding'] == old['binding']
    assert held['receipt'] == old['receipt'] and held['result'] == 'epoch_changed'
    with pytest.raises(local.DiagnosticStateError, match='HUMAN_REQUIRED'):
        local.renew(world, binding=current, now_ms=NOW + 1, expires_at=NOW + DAY,
                    reason='no-automatic-migration')
    local.renew(world, binding=current, now_ms=NOW + 1, expires_at=NOW + DAY,
                confirm_human=True, reason='explicit-managed-renew')
    renewed = saved(world)
    assert renewed['binding'] == current and renewed['consent_history'][-1]['binding'] == old['binding']
    assert renewed['receipt']['local_receipt_id'] != old['receipt']['local_receipt_id']
    assert renewed['consent_epoch'] is None and not renewed['queue'] and renewed['pending'] is None
    request = local.admit(world, current=current, now_ms=NOW + 1)
    assert request['path'] == local.STATUS and b'support_bundle_hash' not in request['raw']
    assert json.loads(request['raw']) == {'schema_version': 'sdc.diagnostics.status-request.v1', 'batch_ids': []}


@pytest.mark.parametrize('extra,error,code', [
    ({'support_bundle_hash': None}, DiagnosticContractError, 'SDC_DIAGNOSTIC_INVALID_CONTRACT'),
    ({'support_bundle_hash': 'not-a-hash'}, DiagnosticContractError, 'SDC_DIAGNOSTIC_INVALID_CONTRACT'),
    ({'support_bundle_hash': 'e' * 64, 'unknown': True}, local.DiagnosticStateError, 'SDC_DIAGNOSTIC_STATE_INVALID'),
    ({'unknown': 'e' * 64}, local.DiagnosticStateError, 'SDC_DIAGNOSTIC_STATE_INVALID')])
def test_binding_optional_managed_field_remains_closed(world, extra, error, code):
    with pytest.raises(error, match='^' + code + '$') as caught:
        local.validate_binding({**target(world), **extra})
    assert type(caught.value) is error and caught.value.code == code


def test_managed_bundle_replacement_cannot_reuse_old_receipt_or_rebind_pending(world):
    first = {**target(world), 'support_bundle_hash': 'e' * 64}
    local.renew(world, binding=first, now_ms=NOW + 1, expires_at=NOW + DAY,
                confirm_human=True, reason='explicit-managed-renew')
    request = local.admit(world, current=first, now_ms=NOW + 1)
    token = local.record_response(world, request, status_reply(request), current=first, now_ms=NOW + 1)
    local.collect(world, token, sample(at=NOW + 1), current=first, now_ms=NOW + 1)
    request = local.admit(world, current=first, now_ms=NOW + 300001)
    before = saved(world)
    replacement = {**first, 'support_bundle_hash': 'f' * 64}
    assert local.admit(world, current=replacement, now_ms=NOW + 600001) is None
    held = saved(world)
    assert held['enabled'] is False and held['pending'] is None and held['binding'] == first
    assert held['receipt'] == before['receipt'] and held['history'][-1]['body_hash'] == request['body_hash']
    assert held['history'][-1]['batch_id'] == request['batch_id']


def test_failed_first_state_write_keeps_exclusive_anchor(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    with Store(root, create_new=True) as store:
        monkeypatch.setattr(store, 'write', lambda *_: (_ for _ in ()).throw(OSError('SYNTHETIC_SECRET')))
        with pytest.raises(local.DiagnosticStateError, match='^SDC_DIAGNOSTIC_STORAGE_UNCERTAIN$'):
            local.enable(store, binding=target(store), now_ms=NOW, expires_at=NOW + DAY,
                         confirm_human=True, reason='fixture')
    with Store(root) as store:
        assert store.read(local.ANCHOR)
        with pytest.raises(local.DiagnosticStateError, match='STATE_MISSING'):
            local.status(store, now_ms=NOW)


@pytest.mark.parametrize('where', ['read', 'anchor', 'state_write', 'readback'])
def test_storage_deadline_preserves_original_instance_and_uncertain_boundary(tmp_path, monkeypatch, where):
    with Store(tmp_path / 'private', create_new=True) as store:
        deadline = TimeoutError('synthetic-parent-deadline')
        original_read, original_write = store.read, store.write
        original_anchor = store.create_diagnostic_anchor
        wrote = False
        def read(name):
            if where == 'read' or where == 'readback' and wrote:
                raise deadline
            return original_read(name)
        def write(name, value):
            nonlocal wrote
            if where == 'state_write':
                raise deadline
            result = original_write(name, value)
            wrote = True
            return result
        def anchor(value):
            result = original_anchor(value)
            if where == 'anchor':
                raise deadline
            return result
        monkeypatch.setattr(store, 'read', read)
        monkeypatch.setattr(store, 'write', write)
        monkeypatch.setattr(store, 'create_diagnostic_anchor', anchor)
        with pytest.raises(TimeoutError) as caught:
            local.enable(store, binding=target(store), now_ms=NOW, expires_at=NOW + DAY,
                         confirm_human=True, reason='deadline-boundary')
        assert caught.value is deadline
        assert bool(getattr(store, '_diagnostic_uncertain', False)) is (where != 'read')
        assert (original_read(local.ANCHOR) is not None) is (where != 'read')
        assert (original_read(local.NAME) is not None) is (where == 'readback')


@pytest.mark.parametrize('confirm,reason', [(False, 'fixture'), (1, 'fixture'), (True, ''), (True, '\nprivate')])
def test_bad_human_admission_creates_nothing(tmp_path, confirm, reason):
    with Store(tmp_path / 'private', create_new=True) as store:
        with pytest.raises(ValueError, match='^SDC_DIAGNOSTIC_'):
            local.enable(store, binding=target(store), now_ms=NOW, expires_at=NOW + DAY,
                         confirm_human=confirm, reason=reason)
        assert store.read(local.ANCHOR) is None and store.read(local.NAME) is None


@pytest.mark.parametrize('field,value', [('device_epoch', True), ('realm_epoch', 1), ('tenant', 'private/path'),
                                         ('key_id', 'bad'), ('root_id', IDENTIFIERS[1].upper()), ('extra', 'secret')])
def test_binding_closed_types(world, field, value):
    binding = target(world)
    if field == 'root_id':
        value = 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'
    binding[field] = value
    with pytest.raises(ValueError, match='^SDC_DIAGNOSTIC_'):
        local.admit(world, current=binding, now_ms=NOW)


def test_empty_status_does_not_persist_sampling_capability(world):
    token = permit(world)
    assert token is not None and saved(world)['source_marks'] == {}
    assert not any(key in saved(world) for key in ('permit', 'grant', 'secret', 'key'))
    with Store(world.root) as other:
        with pytest.raises(local.DiagnosticStateError, match='STATE_CONFLICT'):
            local.collect(other, token, sample(), current=target(other), now_ms=NOW)
    local.collect(world, token, sample(), current=target(world), now_ms=NOW)
    with pytest.raises(local.DiagnosticStateError, match='STATE_CONFLICT'):
        local.collect(world, token, sample(), current=target(world), now_ms=NOW)
    assert len(saved(world)['queue']) == 1


def test_cancel_during_sampling_and_late_status_cannot_enable(world):
    request = local.admit(world, current=target(world), now_ms=NOW)
    local.disable(world, now_ms=NOW)
    assert local.record_response(world, request, status_reply(request), current=target(world), now_ms=NOW) is None
    assert saved(world)['enabled'] is False
    with pytest.raises(local.DiagnosticStateError, match='RETIRED'):
        local.enable(world, binding=target(world), now_ms=NOW, expires_at=NOW + 30 * DAY,
                     confirm_human=True, reason='cannot-revive')


@pytest.mark.parametrize('kind', ['inactive', 'expired', 'epoch'])
def test_central_inactive_expired_or_changed_epoch_never_rebinds_queue(world, kind):
    collected(world)
    request = report_request(world)
    check = local.admit(world, current=target(world), now_ms=NOW + 600000)
    kwargs = {'state': 'inactive'} if kind == 'inactive' else {'expires': NOW} if kind == 'expired' else {'epoch': 2}
    local.record_response(world, check, status_reply(check, **kwargs), current=target(world), now_ms=NOW + 600000)
    state = saved(world)
    assert state['enabled'] is False and state['pending'] is None and state['queue'] == []
    assert state['history'][0]['batch_id'] == request['batch_id']
    assert state['history'][0]['consent_epoch'] == 1 and state['history'][0]['result'] == 'unknown'


def test_unknown_status_precedes_each_same_byte_replay_and_attempts_survive_reopen(world):
    collected(world)
    original = report_request(world)
    at = NOW + 600000
    for expected in (2, 3, 4):
        with Store(world.root) as other:
            request = local.admit(other, current=target(other), now_ms=at)
            assert request['path'] == local.STATUS
            assert json.loads(request['raw'])['batch_ids'] == [original['batch_id']]
            local.record_response(other, request, status_reply(request), current=target(other), now_ms=at)
            again = local.admit(other, current=target(other), now_ms=at + 300000)
            assert again['raw'] == original['raw'] and again['body_hash'] == original['body_hash']
            assert saved(other)['pending']['attempts'] == expected
        at += 600000
    request = local.admit(world, current=target(world), now_ms=at)
    local.record_response(world, request, status_reply(request), current=target(world), now_ms=at)
    assert saved(world)['pending'] is None
    assert saved(world)['history'][0]['attempts'] == 4
    assert saved(world)['history'][0]['result'] == 'unknown'
    assert 'raw' not in saved(world)['history'][0]


def test_status_accepted_is_metadata_not_a_fabricated_full_receipt(world):
    collected(world)
    original = report_request(world)
    request = local.admit(world, current=target(world), now_ms=NOW + 600000)
    local.record_response(world, request, status_reply(request, accepted=True), current=target(world), now_ms=NOW + 600000)
    row = saved(world)['history'][0]
    assert row['batch_id'] == original['batch_id'] and row['result'] == 'accepted'
    assert row['receipt'] is None and local.status(world, now_ms=NOW + 600000)['last_receipt'] is None


@pytest.mark.parametrize('field,value', [('batch_id', IDENTIFIERS[6]), ('consent_epoch', 2),
                                         ('event_count', 2), ('dropped_events', 1), ('partial', True)])
def test_all_receipt_join_fields_fixed(world, field, value):
    collected(world)
    request = report_request(world)
    response = receipt(request)
    response[field] = value
    with pytest.raises(ValueError, match='^SDC_DIAGNOSTIC_'):
        local.record_response(world, request, response, current=target(world), now_ms=NOW + 300000)
    assert saved(world)['pending']['attempts'] == 1 and not saved(world)['history']


def test_cancelled_inflight_preserves_id_and_accepts_evidence_without_reenabling(world):
    collected(world)
    request = report_request(world)
    local.disable(world, now_ms=NOW + 300001)
    row = saved(world)['history'][0]
    assert row['body_hash'] == hashlib.sha256(request['raw']).hexdigest() and row['result'] == 'unknown'
    local.record_response(world, request, receipt(request), current=target(world), now_ms=NOW + 300002)
    status = local.status(world, now_ms=NOW + 300002)
    assert status['enabled'] is False and status['result'] == 'cancelled'
    assert status['last_receipt'] == receipt(request)


def test_1001_rows_drop_oldest_dto_not_sum_of_count(world):
    token = permit(world)
    events = [event(NOW, count=100000)] + [event(NOW + 600000) for _ in range(1000)]
    local.collect(world, token, sample(events, at=NOW + 600000), current=target(world), now_ms=NOW + 600000)
    state = saved(world)
    assert len(state['queue']) == 1000 and state['dropped_events'] == 1
    assert all(row['count'] == 1 for row in state['queue'])


def test_65_distinct_rows_form_64_then_one_without_raw_mutation(world):
    from adapters.sdc.diagnostic_contract import CODES, STAGES
    events = [{**event(), 'stage': stage, 'code': code} for stage in STAGES for code in CODES][:65]
    collected(world, events=events)
    first = report_request(world)
    body = json.loads(first['raw'])
    assert len(body['events']) == 64 and len(first['raw']) <= 16384
    assert len(saved(world)['queue']) == 1
    local.record_response(world, first, receipt(first), current=target(world), now_ms=NOW + 300000)
    second = report_request(world, at=NOW + 600000)
    assert json.loads(second['raw'])['events'] == events[64:]
    assert second['batch_id'] != first['batch_id']


def test_seven_day_pending_body_expiry_preserves_unknown_without_new_id(world):
    collected(world)
    first = report_request(world)
    local.status(world, now_ms=NOW + 7 * DAY + 1)
    state = saved(world)
    assert state['pending'] is None and state['queue'] == []
    assert state['history'][0]['batch_id'] == first['batch_id']
    assert state['history'][0]['result'] == 'unknown' and 'raw' not in state['history'][0]


def test_same_mark_cannot_reinsert_but_delayed_different_source_can(world):
    collected(world)
    first = report_request(world)
    local.record_response(world, first, receipt(first), current=target(world), now_ms=NOW + 300000)
    token = permit(world, at=NOW + 600000)
    with pytest.raises(local.DiagnosticStateError, match='STATE_CONFLICT'):
        local.collect(world, token, sample(at=NOW + 600000), current=target(world), now_ms=NOW + 600000)
    token = permit(world, at=NOW + 900000)
    delayed = sample([event(NOW, component='storage')], at=NOW + 900000)
    delayed['source_marks'] = {'storage': {'observed_at': NOW + 1, 'signature': 'd' * 64}}
    local.collect(world, token, delayed, current=target(world), now_ms=NOW + 900000)
    assert saved(world)['source_marks']['runtime']['observed_at'] == NOW
    assert saved(world)['source_marks']['storage']['observed_at'] == NOW + 1


def test_same_source_timestamp_cannot_change_classification_and_recount(world):
    collected(world)
    request = report_request(world)
    local.record_response(world, request, receipt(request), current=target(world), now_ms=NOW + 300000)
    token = permit(world, at=NOW + 600000)
    changed = sample(at=NOW + 600000, signature='d' * 64)
    changed['source_marks']['runtime']['observed_at'] = NOW
    with pytest.raises(local.DiagnosticStateError, match='STATE_CONFLICT'):
        local.collect(world, token, changed, current=target(world), now_ms=NOW + 600000)
    assert saved(world)['queue'] == [] and saved(world)['source_marks']['runtime']['signature'] == 'c' * 64


@pytest.mark.parametrize('change', ['unknown_source', 'extra_field', 'future', 'secret_signature'])
def test_closed_source_marks(world, change):
    token = permit(world)
    result = sample()
    if change == 'unknown_source':
        result['source_marks']['private-file'] = result['source_marks'].pop('runtime')
    elif change == 'extra_field':
        result['source_marks']['runtime']['raw'] = 'SYNTHETIC_SECRET'
    elif change == 'future':
        result['source_marks']['runtime']['observed_at'] = NOW + 1
    else:
        result['source_marks']['runtime']['signature'] = 'SYNTHETIC_SECRET'
    with pytest.raises(ValueError, match='^SDC_DIAGNOSTIC_') as caught:
        local.collect(world, token, result, current=target(world), now_ms=NOW)
    assert 'SYNTHETIC_SECRET' not in str(caught.value) and not saved(world)['queue']


def test_unknown_history_capacity_allows_only_metadata_reconciliation(world):
    collected(world)
    request = report_request(world)
    local.disable(world, now_ms=NOW + 300001)
    state = saved(world)
    exemplar = state['history'][0]
    state['history'] = [{**exemplar, 'batch_id': str(uuid.uuid4())} for _ in range(1000)]
    state.update(enabled=True, cancelled_at=None, pending=None, queue=[], generation=state['receipt']['generation'])
    world.write(local.NAME, state)
    check = local.admit(world, current=target(world), now_ms=NOW + 600000)
    assert check['path'] == local.STATUS and len(json.loads(check['raw'])['batch_ids']) == 16
    token = local.record_response(world, check, status_reply(check), current=target(world), now_ms=NOW + 600000)
    assert token is None and saved(world)['pending'] is None
    assert len(saved(world)['history']) == 1000 and saved(world)['result'] == 'capacity'
    assert request['batch_id'] != saved(world)['history'][0]['batch_id']


def test_byte_budget_drops_oldest_rows_and_preserves_fixed_limit(world, monkeypatch):
    assert local.MAX_BYTES == 1048576
    initial = saved(world)
    initial['revision'] = 996
    world.write(local.NAME, initial)
    token = permit(world)
    original = saved(world)
    # A smaller injected bound makes the byte boundary independently observable.
    ceiling = len(json.dumps(original, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()) + 1300
    monkeypatch.setattr(local, 'MAX_BYTES', ceiling)
    # Distinct tuples cannot collapse into one event when the next batch is fixed.
    events = [event(NOW + i * 600000, count=i + 1) for i in range(100)]
    at = NOW + 99 * 600000
    local.collect(world, token, sample(events, at=at), current=target(world), now_ms=at)
    record = saved(world)
    raw = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    assert len(raw) <= ceiling
    assert 0 < len(record['queue']) < 100
    assert record['dropped_events'] == 100 - len(record['queue'])
    assert record['queue'] == events[record['dropped_events']:]
    assert record['revision'] == 999
    request = report_request(world, at=at + 300000)
    assert request['revision'] == 1000
    body = json.loads(request['raw'])
    assert body['events'] == events[body['dropped_events']:]
    assert body['partial'] is True and body['events']
    assert len(json.dumps(saved(world), ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()) <= ceiling
    check = local.admit(world, current=target(world), now_ms=at + 600000)
    assert check['path'] == local.STATUS
    local.record_response(world, check, status_reply(check), current=target(world), now_ms=at + 600000)
    replay = report_request(world, at=at + 900000)
    assert replay['raw'] == request['raw'] and replay['batch_id'] == request['batch_id']
    result = local.disable(world, now_ms=at + 900001)
    assert result['enabled'] is False and result['unknown_batches'] == 1
    assert saved(world)['history'][0]['body_hash'] == request['body_hash']
    assert len(json.dumps(saved(world), ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()) <= ceiling


def test_drop_counter_exhaustion_stops_without_clamping_or_reenabling(world):
    token = permit(world)
    with pytest.raises(local.DiagnosticStateError, match='CAPACITY'):
        local.collect(world, token, sample(dropped=1000000), current=target(world), now_ms=NOW)
    state = saved(world)
    assert state['enabled'] is False and state['result'] == 'capacity'
    assert state['dropped_events'] == 0 and state['queue'] == []
    assert local.admit(world, current=target(world), now_ms=NOW + 300000) is None


def test_cancel_has_reserved_counter_room_and_does_not_sum_event_counts(world):
    token = permit(world)
    local.collect(world, token, sample([event(count=100000)], dropped=999999),
                  current=target(world), now_ms=NOW)
    result = local.disable(world, now_ms=NOW + 1)
    assert result['enabled'] is False and result['dropped_events'] == 1000000


def test_receipt_row_limit_evicts_only_accepted_before_unknown(world):
    collected(world)
    request = report_request(world)
    local.record_response(world, request, receipt(request), current=target(world), now_ms=NOW + 300000)
    value = saved(world)
    accepted = value['history'][0]
    unknown = {**accepted, 'receipt': None, 'result': 'unknown', 'content_state': None}
    late = {**accepted, 'batch_id': IDENTIFIERS[6], 'receipt': None, 'recorded_at': NOW + 500000}
    value['history'] = [late] + [{**unknown, 'batch_id': str(uuid.uuid4())} for _ in range(998)] + [accepted]
    world.write(local.NAME, value)
    check = local.admit(world, current=target(world), now_ms=NOW + 600000)
    assert check['path'] == local.STATUS and len(saved(world)['history']) == 999
    assert sum(row['result'] == 'unknown' for row in saved(world)['history']) == 998
    assert any(row['batch_id'] == late['batch_id'] for row in saved(world)['history'])
    assert all(row['batch_id'] != accepted['batch_id'] for row in saved(world)['history'])


def test_central_expiry_never_extends_on_a_later_active_observation(world):
    first = local.admit(world, current=target(world), now_ms=NOW)
    local.record_response(world, first, status_reply(first, expires=NOW + DAY),
                          current=target(world), now_ms=NOW)
    second = local.admit(world, current=target(world), now_ms=NOW + 300000)
    local.record_response(world, second, status_reply(second, expires=NOW + 5 * DAY),
                          current=target(world), now_ms=NOW + 300000)
    assert saved(world)['central_expires_at'] == NOW + DAY
    assert local.status(world, now_ms=NOW + DAY)['enabled'] is False


def test_accepted_history_expires_but_unknown_never_evicted(world):
    collected(world)
    request = report_request(world)
    local.record_response(world, request, receipt(request), current=target(world), now_ms=NOW + 300000)
    state = saved(world)
    unknown = {**state['history'][0], 'batch_id': IDENTIFIERS[6], 'result': 'unknown',
               'receipt': None, 'content_state': None}
    state['history'].append(unknown)
    world.write(local.NAME, state)
    local.status(world, now_ms=NOW + 8 * DAY)
    assert saved(world)['history'] == [unknown]


@pytest.mark.parametrize('corrupt', ['raw', 'body_hash', 'attempts', 'epoch', 'anchor'])
def test_corrupt_persistence_cannot_rebuild_send_authority(world, corrupt):
    collected(world)
    report_request(world)
    value = saved(world)
    if corrupt == 'anchor':
        os.unlink(world.root / local.ANCHOR)
    else:
        key = 'consent_epoch' if corrupt == 'epoch' else corrupt
        value['pending'][key] = {'raw': '{}', 'body_hash': '0' * 64, 'attempts': 5, 'consent_epoch': 2}[key]
        world.write(local.NAME, value)
    with pytest.raises(ValueError, match='^SDC_DIAGNOSTIC_'):
        local.status(world, now_ms=NOW + 600000)


@pytest.mark.parametrize('previous', ['enabled', 'disabled', 'expired'])
def test_explicit_renew_starts_new_local_receipt_without_transferring_old_payload(world, previous):
    collected(world)
    request = report_request(world)
    old = saved(world)
    at = NOW + 600000 if previous != 'expired' else NOW + 30 * DAY
    if previous == 'disabled':
        local.disable(world, now_ms=at)
    elif previous == 'expired':
        assert not local.status(world, now_ms=at)['enabled']
    result = local.renew(world, binding=target(world), now_ms=at, expires_at=at + DAY,
                         confirm_human=True, reason='explicit-renew')
    value = saved(world)
    assert result['enabled'] and result['consent_epoch'] is None
    assert value['anchor_id'] == old['anchor_id']
    assert value['receipt']['local_receipt_id'] != old['receipt']['local_receipt_id']
    assert value['receipt']['accepted_at'] == at and value['generation'] != old['generation']
    assert value['queue'] == [] and value['pending'] is None and value['source_marks'] == {}
    row = value['history'][0]
    assert row['batch_id'] == request['batch_id'] and row['body_hash'] == request['body_hash']
    assert row['local_receipt_id'] == old['receipt']['local_receipt_id'] and row['generation'] == request['generation']
    assert row['result'] == 'unknown' and 'raw' not in row
    assert value['consent_history'][0]['receipt'] == old['receipt']
    check = local.admit(world, current=target(world), now_ms=at)
    assert json.loads(check['raw'])['batch_ids'] == [request['batch_id']]
    token = local.record_response(world, check, status_reply(check, epoch=1, expires=at + DAY),
                                  current=target(world), now_ms=at)
    assert token is not None and token.since_ms == at  # Same still-current central epoch is sufficient.
    assert saved(world)['consent_epoch'] == 1 and saved(world)['pending'] is None


@pytest.mark.parametrize('field,value', [('tenant', 'other'), ('installation', 'other'),
                                         ('device_id', IDENTIFIERS[6]), ('claim_id', IDENTIFIERS[6]),
                                         ('root_id', IDENTIFIERS[6]), ('support_endpoint_hash', 'e' * 64),
                                         ('monitor_endpoint_hash', 'e' * 64), ('audience', 'other'),
                                         ('environment', 'other')])
def test_renew_cannot_transfer_the_consent_to_another_scope(world, field, value):
    before = (world.root / local.NAME).read_bytes()
    current = target(world)
    current[field] = value
    with pytest.raises(local.DiagnosticStateError, match='STATE_CONFLICT'):
        local.renew(world, binding=current, now_ms=NOW + 1, expires_at=NOW + DAY,
                    confirm_human=True, reason='wrong-scope')
    assert (world.root / local.NAME).read_bytes() == before


def test_renew_accepts_independently_supplied_current_key_and_epochs_same_scope(world):
    current = {**target(world), 'key_id': IDENTIFIERS[6], 'realm_epoch': 'realm-2', 'device_epoch': 2}
    local.renew(world, binding=current, now_ms=NOW + 1, expires_at=NOW + DAY,
                confirm_human=True, reason='current-authority')
    assert saved(world)['binding'] == current
    assert saved(world)['consent_history'][0]['binding'] == target(world)
    assert saved(world)['consent_epoch'] is None


@pytest.mark.parametrize('confirm,reason', [(False, 'fixture'), (1, 'fixture'), (True, ''), (True, '\nsecret')])
def test_renew_requires_new_explicit_human_confirmation(world, confirm, reason):
    before = (world.root / local.NAME).read_bytes()
    with pytest.raises(ValueError, match='^SDC_DIAGNOSTIC_'):
        local.renew(world, binding=target(world), now_ms=NOW + 1, expires_at=NOW + DAY,
                    confirm_human=confirm, reason=reason)
    assert (world.root / local.NAME).read_bytes() == before


@pytest.mark.parametrize('late_kind', ['report', 'status', 'sample'])
def test_previous_generation_cannot_authorize_or_complete_the_new_receipt(world, late_kind):
    token = permit(world)
    if late_kind != 'sample':
        local.collect(world, token, sample(), current=target(world), now_ms=NOW)
        report = report_request(world)
        check = local.admit(world, current=target(world), now_ms=NOW + 600000)
    old = saved(world)
    local.renew(world, binding=target(world), now_ms=NOW + 600001, expires_at=NOW + DAY,
                confirm_human=True, reason='new-receipt')
    fresh = saved(world)['receipt']
    if late_kind == 'report':
        local.record_response(world, report, receipt(report), current=target(world), now_ms=NOW + 600002)
        summary = local.status(world, now_ms=NOW + 600002)
        assert summary['last_receipt_local_receipt_id'] == old['receipt']['local_receipt_id']
        assert summary['last_receipt_generation'] == report['generation']
    elif late_kind == 'status':
        assert local.record_response(world, check, status_reply(check, epoch=2, accepted=True),
                                      current=target(world), now_ms=NOW + 600002) is None
    else:
        assert token.used is False
        with pytest.raises(local.DiagnosticStateError, match='STATE_CONFLICT'):
            local.sample_context(world, token, current=target(world), now_ms=NOW + 600002)
    value = saved(world)
    assert value['receipt'] == fresh and value['consent_epoch'] is None and value['result'] == 'waiting'
    assert value['queue'] == [] and value['pending'] is None and value['source_marks'] == {}


def history_world(store, count):
    collected(store)
    report_request(store)
    local.disable(store, now_ms=NOW + 300001)
    value = saved(store)
    prototype = value['history'][0]
    value['history'] = [{**prototype, 'batch_id': str(uuid.uuid4())} for _ in range(count)]
    store.write(local.NAME, value)
    return {row['batch_id'] for row in value['history']}


def test_unfittable_single_batch_persists_capacity_stop_and_preserves_unknown_metadata(world, monkeypatch):
    history_world(world, 1)
    at = NOW + 600000
    local.renew(world, binding=target(world), now_ms=at, expires_at=NOW + DAY,
                confirm_human=True, reason='metadata-retained')
    check = local.admit(world, current=target(world), now_ms=at)
    token = local.record_response(world, check, status_reply(check), current=target(world), now_ms=at)
    local.collect(world, token, sample(at=at), current=target(world), now_ms=at)
    before = saved(world)
    ceiling = len(json.dumps(before, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()) + 100
    monkeypatch.setattr(local, 'MAX_BYTES', ceiling)
    assert local.admit(world, current=target(world), now_ms=at + 300000) is None
    after = saved(world)
    assert after['enabled'] is False and after['result'] == 'capacity'
    assert after['queue'] == [] and after['pending'] is None and after['dropped_events'] == 1
    assert after['history'] == before['history'] and after['consent_history'] == before['consent_history']
    assert len(json.dumps(after, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()) <= ceiling
    with Store(world.root) as reopened:
        assert local.admit(reopened, current=target(reopened), now_ms=at + 600000) is None
        assert saved(reopened)['result'] == 'capacity' and saved(reopened)['history'] == before['history']


@pytest.mark.parametrize('kind', ['unknown', 'accepted'])
def test_renew_never_evicts_unknown_or_receipt_metadata_to_make_capacity(world, kind):
    history_world(world, 1000)
    if kind == 'accepted':
        value = saved(world)
        for row in value['history']:
            row.update(result='accepted', content_state='stored')
        world.write(local.NAME, value)
    before = (world.root / local.NAME).read_bytes()
    with pytest.raises(local.DiagnosticStateError, match='CAPACITY'):
        local.renew(world, binding=target(world), now_ms=NOW + 600000, expires_at=NOW + DAY,
                    confirm_human=True, reason='no-capacity')
    assert (world.root / local.NAME).read_bytes() == before


def test_unknown_history_uses_fair_sixteen_id_status_without_payload_reconstruction(world):
    identifiers = history_world(world, 40)
    local.renew(world, binding=target(world), now_ms=NOW + 600000, expires_at=NOW + DAY,
                confirm_human=True, reason='reconcile-metadata')
    seen = set()
    for index in range(3):
        at = NOW + (index + 2) * 300000
        check = local.admit(world, current=target(world), now_ms=at)
        ids = json.loads(check['raw'])['batch_ids']
        assert check['path'] == local.STATUS and len(ids) == len(set(ids)) == 16
        seen.update(ids)
        local.record_response(world, check, status_reply(check), current=target(world), now_ms=at)
        assert saved(world)['pending'] is None and saved(world)['queue'] == []
    assert seen == identifiers
    assert all(row['result'] == 'unknown' and 'raw' not in row for row in saved(world)['history'])


def test_status_keeps_pending_first_and_joins_each_history_id_separately(world):
    history_world(world, 20)
    at = NOW + 600000
    local.renew(world, binding=target(world), now_ms=at, expires_at=NOW + DAY,
                confirm_human=True, reason='new-and-old')
    check = local.admit(world, current=target(world), now_ms=at)
    token = local.record_response(world, check, status_reply(check), current=target(world), now_ms=at)
    local.collect(world, token, sample(at=at), current=target(world), now_ms=at)
    report = report_request(world, at=at + 300000)
    check = local.admit(world, current=target(world), now_ms=at + 600000)
    ids = json.loads(check['raw'])['batch_ids']
    assert ids[0] == report['batch_id'] and len(ids) == 16
    response = status_reply(check)
    accepted = ids[3]
    response['batches'][3].update(state='accepted', content_state='expired')
    local.record_response(world, check, response, current=target(world), now_ms=at + 600000)
    value = saved(world)
    assert value['pending']['batch_id'] == report['batch_id'] and value['pending']['phase'] == 'replay'
    assert next(row for row in value['history'] if row['batch_id'] == accepted)['result'] == 'accepted'
    assert sum(row['result'] == 'accepted' for row in value['history']) == 1
    assert all(row['receipt'] is None for row in value['history'])
