"""Synthetic one-shot execution state: no HTTP, Keychain, process, or real Runtime."""
import copy
import hashlib
import json
import os
from pathlib import Path
import pickle
import threading

import pytest

from adapters.sdc import restart_control as control
from adapters.sdc import restart_execution as execution
from adapters.sdc import restart_state as consent
from adapters.sdc.storage import Store

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
        # Assemble the published synthetic receipt independently of candidate builders.
        raw = canonical(V['controls']['accept'])
        state = {'schema_version': consent.SCHEMA, 'root_fingerprint': binding['root_fingerprint'],
                 'owner_generation': 'a' * 32, 'disabled': False, 'expected': V['expected'], 'binding': binding,
                 'preview': V['preview'], 'receipt': V['localReceipt'],
                 'accept': {'request': {'raw': raw, 'body_hash': digest(V['controls']['accept']),
                                        'admitted_at': V['localReceipt']['confirmed_at']}, 'ack': V['acceptance']}}
        store.write(consent.NAME, copy.deepcopy(state))
        yield store, binding


def begin(world, **changes):
    store, binding = world
    options = {'operation': V['operation'], 'approval': V['approval'], 'expected': V['expected'],
               'expected_owner_generation': 'a' * 32, 'binding_supplier': lambda: binding,
               'registration': V['registration'], 'preflight': V['preflight'], 'now_ms': NOW}
    options.update(changes)
    return execution.begin_admit(store, **options)


def admitted(world):
    alive, request = begin(world)
    body = json.loads(request['raw'])
    response = {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER_ID,
                'operation_id': body['operation_id'], 'receipt_id': body['receipt_id'], 'state': 'locally_admitted'}
    execution.record_admit_ack(world[0], alive, request, response, now_ms=NOW)
    return alive, request, response


def granted(world):
    alive, admit, _ = admitted(world)
    request = execution.begin_grant(world[0], alive, binding_supplier=lambda: world[1],
        registration=V['registration'], preflight=V['preflight'], now_ms=NOW)
    admission, body = json.loads(admit['raw']), json.loads(request['raw'])
    grant = copy.deepcopy(V['grant'])
    grant.update({k: admission[k] for k in ('receipt_id', 'admission_id', 'owner_generation')})
    grant['grant_request_id'] = body['grant_request_id']
    return alive, request, grant


def spend(world, alive, grant, starts, **changes):
    def start(identifier):
        starts.append(identifier)
        return NOW + 2
    options = {'grant': grant, 'binding_supplier': lambda: world[1], 'registration': V['registration'],
               'preflight': V['preflight'], 'now_ms': NOW, 'final_check': lambda: NOW + 1, 'start': start}
    options.update(changes)
    return execution.spend_and_start(world[0], alive, **options)


def test_full_same_alive_sequence_fsync_precedes_start_and_grant_is_never_saved(world, monkeypatch):
    store, _ = world
    alive, request, grant = granted(world)
    assert request['body_hash'] == digest(json.loads(request['raw']))
    original = os.fsync
    events, starts = [], []
    def fsync(fd):
        original(fd)
        events.append('directory_synced' if fd == store.fd else 'file_synced')
    def final():
        assert events == ['file_synced', 'directory_synced']
        events.append('final_check')
        return NOW + 1
    def start(identifier):
        assert events[-1] == 'final_check'
        starts.append(identifier)
        events.append('start')
        return NOW + 2
    monkeypatch.setattr(os, 'fsync', fsync)
    result = spend(world, alive, grant, starts, final_check=final, start=start)
    assert len(starts) == 1 and result['started_at'] == NOW + 2 and result['state'] == 'unknown'
    assert result['phase'] == 'SPENT' and result['report_prepared'] and not result['report_accepted']
    assert events == ['file_synced', 'directory_synced', 'final_check', 'start', 'file_synced', 'directory_synced']
    saved = store.read(execution.NAME)
    assert saved['spent']['grant_hash'] == digest(grant) and saved['start_receipt_id'] == starts[0]
    assert json.loads(saved['report']['raw'])['result'] is None
    assert json.loads(saved['report']['raw'])['state_revision'] == 1
    assert 'sdc.support.restart-execution-grant.v1' not in (store.root / execution.NAME).read_text()
    assert 'grant' not in saved and 'key' not in saved and os.fstat(store.fd)
    with pytest.raises(execution.RestartExecutionError, match='OWNER'):
        spend(world, alive, grant, starts)
    assert len(starts) == 1


def test_opaque_owner_cannot_be_serialized_or_restored_from_ids(world):
    alive, request, grant = granted(world)
    with pytest.raises(TypeError, match='OWNER'):
        pickle.dumps(alive)
    owner_string = json.loads(world[0].read(execution.NAME)['admit']['raw'])['owner_generation']
    for counterfeit in (owner_string, execution.status(world[0]), {'owner_generation': owner_string}, object()):
        with pytest.raises(execution.RestartExecutionError, match='OWNER'):
            spend(world, counterfeit, grant, [])
    assert request['raw'] == world[0].read(execution.NAME)['grant_request']['raw']


@pytest.mark.parametrize('phase', ['ADMITTING', 'ADMITTED', 'GRANT_REQUESTED', 'SPENT'])
def test_every_saved_phase_is_metadata_only_after_reopen(world, phase):
    if phase == 'ADMITTING':
        alive, _ = begin(world)
    elif phase == 'ADMITTED':
        alive, _, _ = admitted(world)
    else:
        alive, _, grant = granted(world)
        if phase == 'SPENT':
            spend(world, alive, grant, [])
    store, binding = world
    with Store(store.root) as resumed:
        assert execution.status(resumed)['phase'] == phase
        with pytest.raises(execution.RestartExecutionError):
            begin((resumed, binding))
        with pytest.raises(execution.RestartExecutionError, match='OWNER'):
            execution.begin_grant(resumed, alive, binding_supplier=lambda: binding,
                registration=V['registration'], preflight=V['preflight'], now_ms=NOW)
        request = execution.metadata_request(resumed, control.STATUS, binding_supplier=lambda: binding, now_ms=NOW + 3)
        assert request['raw'] == store.read(execution.NAME)['status_request']['raw']
        for path in (control.ADMIT, control.GRANT, control.POLL):
            with pytest.raises(execution.RestartExecutionError, match='CONFLICT'):
                execution.metadata_request(resumed, path, binding_supplier=lambda: binding, now_ms=NOW + 3)


def test_retired_owner_cannot_accept_late_admit_ack_or_issue_grant(world):
    alive, request = begin(world)
    body = json.loads(request['raw'])
    response = {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER_ID,
                'operation_id': body['operation_id'], 'receipt_id': body['receipt_id'], 'state': 'locally_admitted'}
    execution.retire(world[0], alive)
    execution.retire(world[0], alive)
    with pytest.raises(execution.RestartExecutionError, match='OWNER'):
        execution.record_admit_ack(world[0], alive, request, response, now_ms=NOW)
    assert execution.status(world[0])['phase'] == 'ADMITTING'


def test_unknown_admit_never_generates_another_request_id(world):
    alive, request = begin(world)
    before = world[0].read(execution.NAME)
    with pytest.raises(execution.RestartExecutionError, match='CONFLICT'):
        begin(world)
    assert world[0].read(execution.NAME) == before
    assert request == {k: before['admit'][k] for k in ('raw', 'body_hash')}
    with pytest.raises(execution.RestartExecutionError, match='OWNER'):
        execution.begin_grant(world[0], alive, binding_supplier=lambda: world[1],
            registration=V['registration'], preflight=V['preflight'], now_ms=NOW)


def test_original_grant_request_has_no_second_admission_or_new_id(world):
    alive, request, _ = granted(world)
    before = world[0].read(execution.NAME)
    with pytest.raises(execution.RestartExecutionError, match='OWNER'):
        execution.begin_grant(world[0], alive, binding_supplier=lambda: world[1],
            registration=V['registration'], preflight=V['preflight'], now_ms=NOW)
    assert world[0].read(execution.NAME) == before
    assert request == {k: before['grant_request'][k] for k in ('raw', 'body_hash')}


def test_foreign_thread_cannot_spend_borrowed_owner(world):
    alive, _, grant = granted(world)
    errors, starts = [], []
    def foreign():
        try:
            spend(world, alive, grant, starts)
        except ValueError as error:
            errors.append(str(error))
    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and errors == ['SUPPORT_RESTART_EXECUTION_OWNER'] and not starts
    assert execution.status(world[0])['phase'] == 'GRANT_REQUESTED'


def test_changed_saved_original_grant_bytes_cannot_be_adopted_by_alive_owner(world):
    alive, _, grant = granted(world)
    state = world[0].read(execution.NAME)
    body = json.loads(state['grant_request']['raw'])
    body['grant_request_id'] = OTHER_ID
    state['grant_request'].update(raw=canonical(body), body_hash=digest(body))
    world[0].write(execution.NAME, state)
    with pytest.raises(execution.RestartExecutionError, match='OWNER'):
        spend(world, alive, grant, [])


@pytest.mark.parametrize('field', ['operation_id', 'receipt_id', 'state'])
def test_admit_ack_echo_is_checked_before_grant(world, field):
    alive, request = begin(world)
    body = json.loads(request['raw'])
    response = {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER_ID,
                'operation_id': body['operation_id'], 'receipt_id': body['receipt_id'], 'state': 'locally_admitted'}
    response[field] = 'unknown' if field == 'state' else OTHER_ID
    with pytest.raises(ValueError):
        execution.record_admit_ack(world[0], alive, request, response, now_ms=NOW)
    assert execution.status(world[0])['phase'] == 'ADMITTING'


@pytest.mark.parametrize('phase', ['before_admit', 'before_grant', 'before_spend'])
def test_disable_blocks_next_admission_and_keeps_existing_evidence(world, phase):
    if phase == 'before_admit':
        consent.disable(world[0])
        with pytest.raises(ValueError):
            begin(world)
        assert not (world[0].root / execution.NAME).exists()
    elif phase == 'before_grant':
        alive, _, _ = admitted(world)
        consent.disable(world[0])
        with pytest.raises(ValueError):
            execution.begin_grant(world[0], alive, binding_supplier=lambda: world[1],
                registration=V['registration'], preflight=V['preflight'], now_ms=NOW)
        assert execution.status(world[0])['phase'] == 'ADMITTED'
    else:
        alive, _, grant = granted(world)
        consent.disable(world[0])
        starts = []
        with pytest.raises(ValueError):
            spend(world, alive, grant, starts)
        assert not starts and execution.status(world[0])['phase'] == 'GRANT_REQUESTED'


def test_late_matching_admit_ack_after_disable_never_reenables_execution(world):
    alive, request = begin(world)
    body = json.loads(request['raw'])
    consent.disable(world[0])
    response = {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER_ID,
                'operation_id': body['operation_id'], 'receipt_id': body['receipt_id'], 'state': 'locally_admitted'}
    execution.record_admit_ack(world[0], alive, request, response, now_ms=NOW)
    assert execution.status(world[0])['phase'] == 'ADMITTED' and world[0].read(consent.NAME)['disabled']
    with pytest.raises(ValueError):
        execution.begin_grant(world[0], alive, binding_supplier=lambda: world[1],
            registration=V['registration'], preflight=V['preflight'], now_ms=NOW)


@pytest.mark.parametrize('field', ['secret_sha256', 'pins_sha256', 'claim_id', 'device_id', 'key_id'])
def test_current_binding_change_prevents_final_start(world, field):
    alive, _, grant = granted(world)
    world[1][field] = OTHER_ID if field.endswith('_id') else 'f' * 64
    starts = []
    with pytest.raises(ValueError):
        spend(world, alive, grant, starts)
    assert not starts


@pytest.mark.parametrize('field', ['owner_generation', 'accepted_at'])
def test_primary_ack_and_consent_owner_are_rechecked(world, field):
    alive, _, grant = granted(world)
    current = world[0].read(consent.NAME)
    if field == 'owner_generation':
        current[field] = 'b' * 32
    else:
        current['accept']['ack'][field] += 1
    world[0].write(consent.NAME, current)
    starts = []
    with pytest.raises(ValueError):
        spend(world, alive, grant, starts)
    assert not starts


@pytest.mark.parametrize('field', ['grant_request_id', 'receipt_id', 'admission_id', 'owner_generation',
                                  'payload_hash', 'preview_payload_hash', 'local_change_receipt_hash'])
def test_grant_binds_original_request_and_entire_receipt_chain(world, field):
    alive, _, grant = granted(world)
    grant[field] = OTHER_ID if field.endswith('_id') else 'f' * (32 if field == 'owner_generation' else 64)
    starts = []
    with pytest.raises(ValueError):
        spend(world, alive, grant, starts)
    assert not starts and world[0].read(execution.NAME)['spent'] is None


@pytest.mark.parametrize('failure', ['expiry', 'callback', 'registration'])
def test_post_spent_final_check_failure_is_irreversible_without_start(world, failure):
    alive, _, grant = granted(world)
    registration = copy.deepcopy(V['registration'])
    def final():
        if failure == 'callback':
            raise ValueError(CANARY)
        if failure == 'registration':
            registration['state'] = 'revoked'
            return NOW + 1
        return grant['expires_at']
    starts = []
    with pytest.raises(ValueError) as caught:
        spend(world, alive, grant, starts, registration=registration, final_check=final)
    assert CANARY not in str(caught.value)
    assert execution.status(world[0])['phase'] == 'SPENT' and not starts
    assert not execution.status(world[0])['report_prepared']
    with pytest.raises(execution.RestartExecutionError, match='OWNER'):
        spend(world, alive, grant, starts)


def test_same_locks_cover_spent_fsync_final_check_and_start(world):
    alive, _, grant = granted(world)
    calls = []
    def check_locked():
        with Store(world[0].root) as other:
            for lock in (other.restart_state_lock, other.lock):
                with pytest.raises(BlockingIOError), lock():
                    pytest.fail('critical section released early')
            with pytest.raises(BlockingIOError):
                consent.disable(other)
        calls.append(True)
    def final():
        check_locked()
        return NOW + 1
    def start(_):
        check_locked()
        return NOW + 2
    spend(world, alive, grant, [], final_check=final, start=start)
    assert calls == [True, True]
    consent.disable(world[0])


@pytest.mark.parametrize('failure', ['guard_file', 'guard_directory', 'record_file', 'record_directory', 'replace'])
def test_initial_failure_leaves_hold_guard_and_reopen_cannot_execute(world, monkeypatch, failure):
    store, binding = world
    counts = {'file': 0, 'directory': 0}
    original_sync, original_replace = os.fsync, os.replace
    def fsync(fd):
        kind = 'directory' if fd == store.fd else 'file'
        counts[kind] += 1
        target = ('guard_' if counts[kind] == 1 else 'record_') + kind
        if target == failure:
            raise OSError(CANARY)
        return original_sync(fd)
    def replace(src, dst, **kwargs):
        if dst == execution.NAME and failure == 'replace':
            raise OSError(CANARY)
        return original_replace(src, dst, **kwargs)
    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)
    with pytest.raises(execution.RestartExecutionError, match='UNCERTAIN') as caught:
        begin(world)
    assert CANARY not in str(caught.value) and (store.root / execution.NAME).exists()
    assert not list(store.root.glob('.sdc-*'))
    with Store(store.root) as resumed:
        with pytest.raises(execution.RestartExecutionError, match='CONFLICT'):
            begin((resumed, binding))


def test_guard_creation_failure_is_ram_uncertain_without_claiming_durable_evidence(world, monkeypatch):
    store, _ = world
    original = store._open
    def denied(name, *args, **kwargs):
        if name == execution.NAME:
            raise PermissionError(CANARY)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(store, '_open', denied)
    with pytest.raises(execution.RestartExecutionError, match='UNCERTAIN') as caught:
        begin(world)
    assert CANARY not in str(caught.value) and not (store.root / execution.NAME).exists()
    assert store._restart_execution_uncertain
    with pytest.raises(execution.RestartExecutionError, match='CONFLICT'):
        begin(world)


@pytest.mark.parametrize('phase', ['admit_ack', 'grant', 'spent', 'started'])
@pytest.mark.parametrize('failure', ['file', 'directory', 'replace'])
def test_every_later_write_failure_is_unknown_and_reopen_never_restores_owner(world, monkeypatch, phase, failure):
    store, binding = world
    if phase == 'admit_ack':
        alive, request = begin(world)
        body = json.loads(request['raw'])
        response = {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER_ID,
                    'operation_id': body['operation_id'], 'receipt_id': body['receipt_id'], 'state': 'locally_admitted'}
    elif phase == 'grant':
        alive, _, _ = admitted(world)
    else:
        alive, _, grant = granted(world)
    target = 2 if phase == 'started' else 1
    writes, starts = [], []
    original_write, original_sync, original_replace = store.write, os.fsync, os.replace
    def write(name, value):
        if name == execution.NAME:
            writes.append(True)
        return original_write(name, value)
    def fsync(fd):
        kind = 'directory' if fd == store.fd else 'file'
        if len(writes) == target and kind == failure:
            raise OSError(CANARY)
        return original_sync(fd)
    def replace(src, dst, **kwargs):
        if dst == execution.NAME and len(writes) == target and failure == 'replace':
            raise OSError(CANARY)
        return original_replace(src, dst, **kwargs)
    monkeypatch.setattr(store, 'write', write)
    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)
    with pytest.raises(execution.RestartExecutionError, match='UNCERTAIN'):
        if phase == 'admit_ack':
            execution.record_admit_ack(store, alive, request, response, now_ms=NOW)
        elif phase == 'grant':
            execution.begin_grant(store, alive, binding_supplier=lambda: binding,
                registration=V['registration'], preflight=V['preflight'], now_ms=NOW)
        else:
            spend(world, alive, grant, starts)
    assert len(starts) == (1 if phase == 'started' else 0)
    assert execution.status(store)['uncertain'] and not list(store.root.glob('.sdc-*'))
    with Store(store.root) as resumed:
        with pytest.raises(execution.RestartExecutionError, match='CONFLICT'):
            begin((resumed, binding))
    with pytest.raises(ValueError):
        execution.begin_grant(store, alive, binding_supplier=lambda: binding,
            registration=V['registration'], preflight=V['preflight'], now_ms=NOW)


def test_cancel_during_spent_write_retires_ram_before_write_and_leaves_hold(world, monkeypatch):
    alive, _, grant = granted(world)
    starts = []
    def interrupted(*_):
        assert alive.phase == 'SPENT'
        raise KeyboardInterrupt
    monkeypatch.setattr(world[0], 'write', interrupted)
    with pytest.raises(KeyboardInterrupt):
        spend(world, alive, grant, starts)
    assert not starts and execution.status(world[0])['uncertain']
    with Store(world[0].root) as resumed:
        assert execution.status(resumed)['phase'] == 'GRANT_REQUESTED'
        with pytest.raises(ValueError):
            begin((resumed, world[1]))


@pytest.mark.parametrize('phase', ['report_admission', 'report_ack', 'status_request'])
@pytest.mark.parametrize('failure', ['file', 'directory', 'replace'])
def test_metadata_write_failure_cannot_restore_execution_or_repeat_report(world, monkeypatch, phase, failure):
    alive, _, grant = granted(world)
    starts = []
    spend(world, alive, grant, starts)
    store, binding = world
    if phase == 'report_ack':
        request = execution.metadata_request(store, control.REPORT, binding_supplier=lambda: binding, now_ms=NOW + 3)
        body = json.loads(request['raw'])
        response = {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER_ID,
                    **{k: body[k] for k in ('operation_id', 'receipt_id', 'state', 'state_revision')}}
    original_sync, original_replace = os.fsync, os.replace
    def fsync(fd):
        if ('directory' if fd == store.fd else 'file') == failure:
            raise OSError(CANARY)
        return original_sync(fd)
    def replace(src, dst, **kwargs):
        if dst == execution.NAME and failure == 'replace':
            raise OSError(CANARY)
        return original_replace(src, dst, **kwargs)
    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)
    with pytest.raises(execution.RestartExecutionError, match='UNCERTAIN'):
        if phase == 'report_ack':
            execution.record_metadata_ack(store, control.REPORT, request, response, now_ms=NOW + 4)
        else:
            path = control.REPORT if phase == 'report_admission' else control.STATUS
            execution.metadata_request(store, path, binding_supplier=lambda: binding, now_ms=NOW + 4)
    assert len(starts) == 1 and execution.status(store)['uncertain']
    with pytest.raises(ValueError):
        execution.metadata_request(store, control.REPORT, binding_supplier=lambda: binding, now_ms=NOW + 4)
    with Store(store.root) as resumed:
        assert execution.status(resumed)['phase'] == 'SPENT'
        with pytest.raises(ValueError):
            begin((resumed, binding))


def test_unknown_report_is_fixed_once_and_status_null_never_authorizes_reexecution(world):
    alive, _, grant = granted(world)
    spend(world, alive, grant, [])
    store, binding = world
    request = execution.metadata_request(store, control.REPORT, binding_supplier=lambda: binding, now_ms=NOW + 3)
    report = json.loads(request['raw'])
    assert report['state'] == 'unknown' and report['result'] is None and report['state_revision'] == 1
    with pytest.raises(execution.RestartExecutionError, match='CONFLICT'):
        execution.metadata_request(store, control.REPORT, binding_supplier=lambda: binding, now_ms=NOW + 4)
    request_status = execution.metadata_request(store, control.STATUS, binding_supplier=lambda: binding, now_ms=NOW + 4)
    status_body = json.loads(request_status['raw'])
    response = {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER_ID,
                **{k: status_body[k] for k in ('operation_id', 'receipt_id', 'report_id')},
                'operation': {'state': 'grant_consumed', 'state_revision': 0}, 'report': None}
    assert not execution.record_metadata_ack(store, control.STATUS, request_status, response, now_ms=NOW + 4)['report_accepted']
    response['report'] = {'body_hash': request['body_hash'], 'state': 'unknown', 'state_revision': 1}
    assert execution.record_metadata_ack(store, control.STATUS, request_status, response, now_ms=NOW + 4)['report_accepted']
    assert execution.status(store)['state'] == 'unknown'


def test_late_report_ack_after_disable_is_only_evidence_and_status_is_keyless(world, monkeypatch):
    alive, _, grant = granted(world)
    spend(world, alive, grant, [])
    store, binding = world
    request = execution.metadata_request(store, control.REPORT, binding_supplier=lambda: binding, now_ms=NOW + 3)
    report = json.loads(request['raw'])
    consent.disable(store)
    monkeypatch.setattr(store, '_marker', lambda: pytest.fail('metadata read/ACK must not use credentials'))
    response = {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER_ID,
                **{k: report[k] for k in ('operation_id', 'receipt_id', 'state', 'state_revision')}}
    assert execution.record_metadata_ack(store, control.REPORT, request, response, now_ms=NOW + 4)['report_accepted']
    assert execution.status(store)['state'] == 'unknown' and store.read(consent.NAME)['disabled']
    with pytest.raises(ValueError):
        execution.metadata_request(store, control.STATUS, binding_supplier=lambda: binding, now_ms=NOW + 4)


@pytest.mark.parametrize('field', ['body_hash', 'state', 'state_revision'])
def test_status_report_must_match_the_original_fixed_report(world, field):
    alive, _, grant = granted(world)
    spend(world, alive, grant, [])
    store, binding = world
    report = execution.metadata_request(store, control.REPORT, binding_supplier=lambda: binding, now_ms=NOW + 3)
    request = execution.metadata_request(store, control.STATUS, binding_supplier=lambda: binding, now_ms=NOW + 3)
    body = json.loads(request['raw'])
    evidence = {'body_hash': report['body_hash'], 'state': 'unknown', 'state_revision': 1}
    evidence[field] = {'body_hash': 'f' * 64, 'state': 'succeeded', 'state_revision': 2}[field]
    response = {'schema_version': 'sdc.support.restart-response.v1', 'request_id': OTHER_ID,
                **{k: body[k] for k in ('operation_id', 'receipt_id', 'report_id')},
                'operation': {'state': 'unknown', 'state_revision': 1}, 'report': evidence}
    with pytest.raises(ValueError):
        execution.record_metadata_ack(store, control.STATUS, request, response, now_ms=NOW + 4)
    assert not execution.status(store)['report_accepted']


def test_corruption_missing_and_root_replacement_do_not_create_new_continuations(world, tmp_path):
    alive, _, grant = granted(world)
    store, _ = world
    path = store.root / execution.NAME
    path.write_text('{}')
    with pytest.raises(ValueError):
        execution.status(store)
    with pytest.raises(ValueError):
        spend(world, alive, grant, [])
    with pytest.raises(ValueError):
        begin(world)
    path.unlink()
    with pytest.raises(ValueError):
        spend(world, alive, grant, [])
    with pytest.raises(execution.RestartExecutionError, match='CONFLICT'):
        begin(world)
    retained = tmp_path / 'retained'
    store.root.rename(retained)
    store.root.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        execution.status(store)
    assert not list(store.root.iterdir())
