"""Independent synthetic runner oracles: no crypto process, Keychain or live I/O."""
import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.sdc import support_client as client, support_state as local
from adapters.sdc.storage import Store
from adapters.sdc.support_control import POLL, ADMIT, GRANT, REPORT, STATUS
from adapters.sdc.transport import TransportError

CONTRACTS = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/support'
VECTORS = json.loads((CONTRACTS / 'signature-vectors.json').read_text())
CONTROL = json.loads((CONTRACTS / 'control-vectors.json').read_text())
NOW = VECTORS['now']
OPERATION = VECTORS['vectors'][0]['payload']
APPROVAL = VECTORS['approval']
KEY = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
CLAIM = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
CANARY = 'SYNTHETIC_PRIVATE_DO_NOT_ECHO'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def digest(value, omitted=()):
    return hashlib.sha256(encoded({k: v for k, v in value.items() if k not in omitted})).hexdigest()


def envelope(value, purpose):
    return {'alg': 'PS256', 'kid': 'synthetic', 'purpose': purpose,
            'payload': base64.urlsafe_b64encode(encoded(value)).rstrip(b'=').decode('ascii'), 'signature': 'A' * 512}


def preview():
    keys = ('product', 'audience', 'tenant', 'installation', 'device_id', 'environment', 'case_id', 'purpose_code',
            'capability_revision', 'consent_revision', 'realm_epoch', 'policy_epoch', 'device_epoch', 'case_epoch', 'local_consent_epoch')
    value = {k: OPERATION[k] for k in keys}
    value.update(schema_version='sdc.support.consent-preview.v1', preview_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                 claim_id=CLAIM, key_id=KEY, diagnostic_ids=['runtime.status.v1'], operator_refs=[OPERATION['requested_by']],
                 approved_by='f' * 64, issued_at=NOW, not_before=NOW, expires_at=NOW + 300000, consent_expires_at=NOW + 1800000)
    value['consent_binding_hash'] = digest(value, ('preview_id', 'issued_at', 'not_before', 'expires_at', 'consent_binding_hash'))
    return value


def result(diagnostic_id, generated_at):
    return {'schema_version': 'sdc.support.diagnostic-result.v1', 'diagnostic_id': diagnostic_id, 'generated_at': generated_at,
            'observed_at': None, 'status': 'unknown', 'duration_ms': None, 'error_code': 'SOURCE_UNKNOWN',
            'data': {'mode': 'unknown', 'process': 'unknown', 'installed_commit': None, 'loaded_commit': None,
                     'installed_version': None, 'loaded_version': None}}


@pytest.fixture
def world(tmp_path):
    with Store(tmp_path / 'runner-private', create=True) as store:
        monitor = {'origin': 'https://monitor.invalid', 'audience': OPERATION['audience'], 'environment': OPERATION['environment']}
        store.write('endpoint.json', monitor)
        store.write('state.json', {'state': 'active', 'enabled': True, 'claim_id': CLAIM, 'device_id': OPERATION['device_id'], 'key_id': KEY})
        store.write('credential', {'key_id': KEY, 'key': '1' * 64})
        binding = store.enrollment_binding()
        store.write('support-bootstrap.json', {'schema_version': client.BOOTSTRAP, 'root_fingerprint': binding['root_fingerprint'],
                    'monitor_endpoint_hash': binding['endpoint_hash'], 'support_endpoint': {**monitor, 'origin': 'https://support.invalid'},
                    'pins': [{'synthetic': True}]})
        w = SimpleNamespace(store=store, wall=NOW, mono=100.0, calls=[], providers=[], pin_purposes=[],
                            before={}, operation=copy.deepcopy(OPERATION), approval=copy.deepcopy(APPROVAL), grant_change=None,
                            empty=False, report_error=False, status_report='match', status_state='succeeded', status_revision=1)
        def advance(ms):
            w.wall += ms
            w.mono += ms / 1000
        w.advance = advance
        def check(_pins, *, purpose, now_ms):
            w.pin_purposes.append(purpose)
        w.check = check
        current = client._supplier(store, check, lambda: w.wall / 1000, {})
        snap = local.prepare(store, binding_supplier=current, now_ms=NOW, initialize=True)
        snap = local.confirm_consent(store, preview(), expected_owner_generation=snap['owner_generation'], expected_binding=snap['binding'],
                                     binding_supplier=current, now_ms=NOW, confirm_human=True)
        request = local.begin_accept(store, expected_owner_generation=snap['owner_generation'], binding_supplier=current, now_ms=NOW)
        body = json.loads(request['raw'])
        local.record_accept_ack(store, request, {**body, 'case_id': OPERATION['case_id'], 'state': 'accepted',
                                               'expires_at': NOW + 1800000, 'request_id': KEY})
        w.pin_purposes.clear()
        def verify(raw, **kwargs):
            value = json.loads(raw)
            if kwargs['purpose'] == 'operation':
                assert value['purpose'] == 'operation'
            return json.loads(base64.urlsafe_b64decode(value['payload'] + '=' * (-len(value['payload']) % 4)))
        w.verify = verify
        def unlocked():
            with Store(store.root) as other:
                with other.support_state_lock(), other.lock():
                    pass
                with pytest.raises(BlockingIOError):
                    with other.support_session_lock():
                        pass
        def send(path, raw, headers):
            unlocked()
            state = store.read(local.NAME)
            record = state['runner']['poll'] if path == POLL else state['runner']['active'][
                {ADMIT: 'admit', GRANT: 'grant', REPORT: 'report', STATUS: 'status'}[path]]
            assert record['raw'].encode('ascii') == raw
            w.calls.append((path, raw, headers))
            request = json.loads(raw)
            if path in w.before:
                w.before[path](request)
            base = {'schema_version': 'sdc.support.v1', 'request_id': KEY}
            if path == POLL:
                return {**base, 'poll_id': request['poll_id'], 'operation': None if w.empty else envelope(w.operation, 'operation'),
                        'approval': None if w.empty else w.approval}
            if path == ADMIT:
                return {**base, 'operation_id': request['operation_id'], 'receipt_id': request['receipt_id'], 'state': 'locally_admitted'}
            if path == GRANT:
                grant = copy.deepcopy(VECTORS['vectors'][1]['payload'])
                grant.update({k: request[k] for k in ('grant_request_id', 'operation_id', 'payload_hash', 'receipt_id', 'admission_id')})
                grant.update(owner_generation=state['runner']['active']['alive_owner'], issued_at=w.wall // 1000 * 1000,
                             not_before=w.wall // 1000 * 1000, expires_at=min(w.wall // 1000 * 1000 + 30000, w.approval['expires_at']))
                if w.grant_change:
                    grant.update(w.grant_change)
                return {**base, 'operation_id': request['operation_id'], 'state': 'grant_consumed', 'grant': envelope(grant, 'execution-grant')}
            if path == REPORT:
                if w.report_error:
                    raise TransportError()
                return {**base, 'operation_id': request['operation_id'], 'receipt_id': request['receipt_id'],
                        'state': request['state'], 'state_revision': request['state_revision']}
            report = state['runner']['active']['report']
            metadata = None
            if w.status_report != 'null' and report is not None:
                report_body = json.loads(report['raw'])
                metadata = {'body_hash': report['body_hash'] if w.status_report == 'match' else '0' * 64,
                            'state': report_body['state'], 'state_revision': report_body['state_revision']}
            return {**base, 'operation_id': request['operation_id'], 'receipt_id': request['receipt_id'], 'report_id': request['report_id'],
                    'report': metadata, 'operation': {'state': w.status_state, 'state_revision': w.status_revision}}
        w.transport = lambda _: SimpleNamespace(send=send)
        def provider(diagnostic_id, *, generated_at_ms):
            unlocked()
            assert store.read(local.NAME)['runner']['active']['phase'] == 'STARTED'
            w.providers.append(diagnostic_id)
            return result(diagnostic_id, generated_at_ms)
        w.provider = provider
        yield w


def run(w, **kwargs):
    return client.run_once(w.store, verifier=w.verify, check_pins=w.check, transport_factory=w.transport,
                           provider=kwargs.pop('provider', w.provider), clock=lambda: w.wall / 1000, monotonic=lambda: w.mono, **kwargs)


def test_normal_command_completion_is_not_system_health(world):
    assert run(world) == {'state': 'report_accepted', 'report_state': 'succeeded'}
    assert [p for p, _, _ in world.calls] == [POLL, ADMIT, GRANT, REPORT]
    report = json.loads(world.calls[-1][1])
    assert report['result']['status'] == 'unknown' and report['result']['observed_at'] is None
    assert world.providers == ['runtime.status.v1']
    saved = world.store.read(local.NAME)['runner']
    assert saved['active'] is None and len(saved['terminal']) == 1
    assert {'operation', 'execution-grant'} <= set(world.pin_purposes)


def test_empty_poll_observation_is_not_execution(world):
    world.empty = True
    assert run(world) == {'state': 'empty'}
    assert [p for p, _, _ in world.calls] == [POLL] and not world.providers


@pytest.mark.parametrize('field,value', [('tenant', 'other'), ('installation', 'other'), ('realm_epoch', 'other'),
                                        ('device_epoch', 2), ('purpose_code', 'CONFIG_MISMATCH'), ('requested_by', '0' * 64)])
def test_signed_foreign_operation_is_not_admitted(world, field, value):
    world.operation[field] = value
    world.operation['payload_hash'] = digest(world.operation, ('payload_hash',))
    with pytest.raises(ValueError):
        run(world)
    assert [p for p, _, _ in world.calls] == [POLL] and not world.providers


@pytest.mark.parametrize('field,value', [('approval_kind', 'preauthorized'), ('intent_hash', '0' * 64),
                                        ('case_epoch', 2), ('approved_by', '0' * 64)])
def test_approval_must_match_signed_operation(world, field, value):
    world.approval[field] = value
    world.approval['approval_hash'] = digest(world.approval, ('approval_hash',))
    with pytest.raises(ValueError):
        run(world)
    assert len(world.calls) == 1 and not world.providers


@pytest.mark.parametrize('field,value', [('receipt_id', KEY), ('admission_id', KEY), ('owner_generation', '0' * 32),
                                        ('grant_request_id', KEY), ('realm_epoch', 'other'), ('approval_hash', '0' * 64)])
def test_validly_signed_wrong_grant_never_starts_provider(world, field, value):
    world.grant_change = {field: value}
    with pytest.raises(ValueError):
        run(world)
    assert [p for p, _, _ in world.calls] == [POLL, ADMIT, GRANT] and not world.providers
    assert world.store.read(local.NAME)['runner']['active']['phase'] == 'GRANTING'


@pytest.mark.parametrize('change', ['backward', 'forward', 'elapsed'])
def test_clock_jump_and_elapsed_budget_are_separate_holds(world, change):
    def clock_change(_):
        if change == 'elapsed':
            world.advance(30000)
        else:
            world.wall += -1000 if change == 'backward' else 1000
    world.before[GRANT] = clock_change
    with pytest.raises(ValueError):
        run(world)
    assert not world.providers


def test_grant_fsync_cost_uses_original_monotonic_baseline(world, monkeypatch):
    write = world.store.write
    def delayed(name, value):
        if name == local.NAME and value.get('runner', {}).get('active') and value['runner']['active']['phase'] == 'GRANTING':
            world.advance(30000)
        return write(name, value)
    monkeypatch.setattr(world.store, 'write', delayed)
    with pytest.raises(ValueError):
        run(world)
    assert not world.providers


@pytest.mark.parametrize('phase,network,providers', [('POLL', 0, 0), ('ADMITTING', 1, 0), ('GRANTING', 2, 0),
                                                    ('STARTED', 3, 0), ('RESULT', 3, 1), ('REPORTING', 3, 1)])
def test_each_durable_boundary_fails_closed(world, monkeypatch, phase, network, providers):
    write = world.store.write
    def failed(name, value):
        runner = value.get('runner', {})
        active = runner.get('active')
        matches = runner.get('poll') is not None if phase == 'POLL' else active is not None and active['phase'] == phase
        if name == local.NAME and matches:
            raise OSError(CANARY)
        return write(name, value)
    monkeypatch.setattr(world.store, 'write', failed)
    with pytest.raises(OSError):
        run(world)
    assert len(world.calls) == network and len(world.providers) == providers
    assert CANARY not in (world.store.root / local.NAME).read_text()


def test_admit_ack_loss_is_orphan_hold_without_new_ids(world):
    world.before[ADMIT] = lambda _: (_ for _ in ()).throw(TransportError())
    with pytest.raises(TransportError):
        run(world)
    original = world.store.read(local.NAME)
    assert run(world) == {'state': 'orphan_hold'}
    saved = world.store.read(local.NAME)
    assert saved['runner']['active']['admit'] == original['runner']['active']['admit']
    assert saved['runner']['active']['grant'] is None and saved['runner']['active']['phase'] == 'ADMITTING'
    assert [p for p, _, _ in world.calls] == [POLL, ADMIT, STATUS] and not world.providers


@pytest.mark.parametrize('phase', ['GRANTING', 'STARTED', 'RESULT', 'REPORTING'])
def test_new_process_never_reuses_grant_or_reexecutes_started(world, monkeypatch, phase):
    write = world.store.write
    def crash(name, value):
        write(name, value)
        active = value.get('runner', {}).get('active')
        if name == local.NAME and active and active['phase'] == phase:
            raise OSError('synthetic after-commit interruption')
    monkeypatch.setattr(world.store, 'write', crash)
    with pytest.raises(OSError):
        run(world)
    monkeypatch.setattr(world.store, 'write', write)
    count, provider_count = len(world.calls), len(world.providers)
    world.status_report = 'null'
    assert run(world) == {'state': 'result_unknown'}
    assert [p for p, _, _ in world.calls[count:]] == [STATUS] and len(world.providers) == provider_count


@pytest.mark.parametrize('status_report,state', [('match', 'report_reconciled'), ('null', 'result_unknown')])
def test_ack_lost_uses_metadata_and_keeps_raw_report(world, status_report, state):
    world.report_error, world.status_report = True, status_report
    assert run(world)['state'] == state
    assert [p for p, _, _ in world.calls] == [POLL, ADMIT, GRANT, REPORT, STATUS]
    assert len(world.providers) == 1
    if status_report == 'null':
        original = world.store.read(local.NAME)['runner']['active']['report']
        assert run(world)['state'] == 'result_unknown'
        assert world.store.read(local.NAME)['runner']['active']['report'] == original
        assert world.calls[-1][0] == STATUS and len(world.providers) == 1


def test_status_body_hash_mismatch_is_not_acceptance(world):
    world.report_error, world.status_report = True, 'other'
    with pytest.raises(ValueError):
        run(world)
    assert world.store.read(local.NAME)['runner']['active']['report'] is not None
    assert len(world.providers) == 1


def test_case_expired_reconciliation_sends_no_diagnostic_bytes(world):
    world.report_error, world.status_report = True, 'null'
    assert run(world)['state'] == 'result_unknown'
    raw_report = world.store.read(local.NAME)['runner']['active']['report']['raw']
    world.advance(1800001)
    assert run(world)['state'] == 'result_unknown'
    assert world.calls[-1][0] == STATUS and len(world.providers) == 1
    assert world.store.read(local.NAME)['runner']['active']['report']['raw'] == raw_report


@pytest.mark.parametrize('stage', [POLL, ADMIT, GRANT])
def test_stop_during_request_prevents_next_request_or_provider(world, stage):
    world.before[stage] = lambda _: client.stop(world.store, now_ms=world.wall)
    with pytest.raises(ValueError):
        run(world)
    assert world.calls[-1][0] == stage and not world.providers


def test_stop_inside_provider_keeps_unknown_and_status_network_zero(world):
    def provider(diagnostic_id, *, generated_at_ms):
        world.providers.append(diagnostic_id)
        client.stop(world.store, now_ms=world.wall)
        return result(diagnostic_id, generated_at_ms)
    with pytest.raises(ValueError):
        run(world, provider=provider)
    active = world.store.read(local.NAME)['runner']['active']
    assert active['phase'] == 'RESULT' and json.loads(active['report']['raw'])['state'] == 'unknown'
    calls = len(world.calls)
    with pytest.raises(ValueError):
        run(world)
    assert len(world.calls) == calls and len(world.providers) == 1


@pytest.mark.parametrize('kind', ['exception', 'extra', 'wrong_diagnostic', 'late'])
def test_provider_error_or_late_output_is_never_healthy_or_reexecuted(world, kind):
    def provider(diagnostic_id, *, generated_at_ms):
        world.providers.append(diagnostic_id)
        if kind == 'exception':
            raise ValueError(CANARY)
        value = result(diagnostic_id, generated_at_ms)
        if kind == 'extra':
            value['private'] = CANARY
        elif kind == 'wrong_diagnostic':
            value['diagnostic_id'] = 'notification.health.v1'
        else:
            world.advance(30000)
        return value
    assert run(world, provider=provider)['report_state'] == 'unknown'
    body = json.loads(world.calls[-1][1])
    assert body['result'] is None and body['state'] == 'unknown'
    assert CANARY not in (world.store.root / local.NAME).read_text() and CANARY not in world.calls[-1][1].decode()
    assert len(world.providers) == 1


def test_duplicate_operation_is_not_admitted_again(world):
    run(world)
    with pytest.raises(ValueError):
        run(world)
    assert [p for p, _, _ in world.calls].count(ADMIT) == 1 and len(world.providers) == 1


@pytest.mark.parametrize('name', ['support_session_lock', 'support_state_lock', 'lock'])
def test_nonblocking_locks_never_turn_wait_into_grant_budget(world, name):
    with Store(world.store.root) as other, getattr(other, name)():
        with pytest.raises(BlockingIOError):
            run(world)
    assert not world.calls and not world.providers


def test_missing_provider_and_missing_ack_are_not_poll_authority(world):
    with pytest.raises(ValueError):
        run(world, provider=None)
    state = world.store.read(local.NAME)
    state['client']['accept']['ack'] = None
    world.store.write(local.NAME, state)
    with pytest.raises(ValueError):
        run(world)
    assert not world.calls


def test_purpose_pins_required_before_poll(world):
    def check(_pins, *, purpose, now_ms):
        if purpose == 'operation':
            raise ValueError(CANARY)
    world.check = check
    with pytest.raises(ValueError):
        run(world)
    assert not world.calls and not world.providers


def test_reservation_and_terminal_bounds_hold_before_admit(world, monkeypatch):
    monkeypatch.setattr(local, 'REPORT_STORAGE_RESERVE', local.MAX_BYTES)
    with pytest.raises(ValueError):
        run(world)
    assert [p for p, _, _ in world.calls] == [POLL] and not world.providers


def test_full_terminal_evidence_is_never_evicted(world, monkeypatch):
    monkeypatch.setattr(local, 'MAX_OPERATION_RECEIPTS', 1)
    run(world)
    saved = world.store.read(local.NAME)
    count = len(world.calls)
    with pytest.raises(ValueError):
        run(world)
    assert len(world.calls) == count and world.store.read(local.NAME) == saved


def test_unknown_persisted_phase_is_never_repaired(world):
    world.before[ADMIT] = lambda _: (_ for _ in ()).throw(TransportError())
    with pytest.raises(TransportError):
        run(world)
    state = world.store.read(local.NAME)
    state['runner']['active']['phase'] = CANARY
    world.store.write(local.NAME, state)
    count = len(world.calls)
    with pytest.raises(ValueError):
        run(world)
    assert len(world.calls) == count


def test_whole_caller_budget_interrupts_before_next_admission(world):
    def budget():
        if world.calls:
            raise TimeoutError(CANARY)
    with pytest.raises(TimeoutError):
        run(world, budget=budget)
    assert [p for p, _, _ in world.calls] == [POLL] and not world.providers


def test_shared_control_vectors_are_fixed_public_shape_evidence():
    assert CONTROL['schema_version'] == 'sdc.support.control-vectors.v1'
    from adapters.sdc.support_control import validate_request
    for vector in CONTROL['positive_requests']:
        if vector['path'] in (POLL, ADMIT, GRANT, REPORT, STATUS):
            assert validate_request(vector['path'], vector['raw_json'])['schema_version'] == 'sdc.support.v1'


def test_preempted_clock_sampling_interval_is_not_a_clock_jump():
    # Anchor wall read occurs between monotonic 10..12; current wall read at
    # mono13 is inside 12..14. A two-second uncertainty must not be called a jump.
    values = iter([12.0, 14.0])
    grant = {'issued_at': NOW, 'not_before': NOW, 'expires_at': NOW + 30000}
    client._grant_clock(lambda: (NOW + 1000) / 1000, lambda: next(values), (10.0, NOW, 12.0), grant)


def test_sampling_uncertainty_still_consumes_conservative_start_budget():
    values = iter([12.0, 40.0])
    grant = {'issued_at': NOW, 'not_before': NOW, 'expires_at': NOW + 30000}
    with pytest.raises(ValueError):
        client._grant_clock(lambda: (NOW + 1000) / 1000, lambda: next(values), (10.0, NOW, 12.0), grant)


def test_local_status_projects_only_operation_metadata(world):
    world.report_error, world.status_report = True, 'null'
    run(world)
    calls = len(world.calls)
    status = client.status(world.store, now_ms=world.wall)
    assert status['operation']['phase'] == 'REPORTING' and status['operation_receipts'] == 0
    assert len(world.calls) == calls and 'result' not in encoded(status).decode()
    assert 'secret' not in encoded(status).decode()


def leave_pregrant_unknown(world):
    world.before[ADMIT] = lambda _: (_ for _ in ()).throw(TransportError())
    with pytest.raises(TransportError):
        run(world)
    active = world.store.read(local.NAME)['runner']['active']
    assert active['phase'] == 'ADMITTING' and active['grant'] is None and active['report'] is None
    return active


@pytest.mark.parametrize('terminal', ['canceled', 'expired'])
def test_known_pregrant_terminal_releases_only_never_granted_slot(world, terminal):
    active = leave_pregrant_unknown(world)
    world.status_state, world.status_revision, world.status_report = terminal, 0, 'null'
    if terminal == 'expired':
        world.advance(1800001)
    assert run(world) == {'state': 'pregrant_terminal', 'terminal_state': terminal}
    saved = world.store.read(local.NAME)['runner']
    assert saved['active'] is None and len(saved['terminal']) == 1
    row = saved['terminal'][0]
    admit = json.loads(active['admit']['raw'])
    assert row['kind'] == 'pregrant_terminal' and row['not_started'] is True
    assert row['receipt_id'] == admit['receipt_id'] and row['admission_id'] == admit['admission_id']
    assert row['admit_body_hash'] == active['admit']['body_hash'] and row['state'] == terminal and row['state_revision'] == 0
    assert [p for p, _, _ in world.calls] == [POLL, ADMIT, STATUS] and not world.providers


@pytest.mark.parametrize('state,revision', [('locally_admitted', 0), ('grant_consumed', 0), ('running', 0),
                                          ('unknown', 0), ('canceled', 1), ('expired', 1), ('succeeded', 1)])
def test_null_report_and_nonterminal_or_nonzero_revision_never_release_pregrant(world, state, revision):
    active = leave_pregrant_unknown(world)
    world.status_state, world.status_revision, world.status_report = state, revision, 'null'
    assert run(world) == {'state': 'orphan_hold'}
    saved = world.store.read(local.NAME)['runner']
    assert saved['active']['admit'] == active['admit'] and saved['active']['grant'] is None and not saved['terminal']
    assert not world.providers


def test_admit_crash_before_request_cannot_invent_known_receipt(world, monkeypatch):
    write = world.store.write
    def crash(name, value):
        write(name, value)
        active = value.get('runner', {}).get('active')
        if name == local.NAME and active and active['phase'] == 'ADMITTING':
            raise OSError('synthetic crash before admit send')
    monkeypatch.setattr(world.store, 'write', crash)
    with pytest.raises(OSError):
        run(world)
    monkeypatch.setattr(world.store, 'write', write)
    assert [p for p, _, _ in world.calls] == [POLL]
    world.before[STATUS] = lambda _: (_ for _ in ()).throw(TransportError(403, 'SDC_REQUEST_DENIED'))
    with pytest.raises(TransportError):
        run(world)
    saved = world.store.read(local.NAME)['runner']
    assert saved['active']['phase'] == 'ADMITTING' and saved['active']['grant'] is None and not saved['terminal']
    assert not world.providers


@pytest.mark.parametrize('change', ['secret', 'device', 'endpoint', 'stop'])
def test_pregrant_status_needs_current_binding_and_local_enabled(world, change):
    leave_pregrant_unknown(world)
    world.status_state, world.status_revision, world.status_report = 'canceled', 0, 'null'
    if change == 'secret':
        world.store.write('credential', {'key_id': KEY, 'key': '2' * 64})
    elif change == 'device':
        world.store.write('state.json', {**world.store.read('state.json'), 'device_id': KEY})
    elif change == 'endpoint':
        world.store.write('endpoint.json', {**world.store.read('endpoint.json'), 'origin': 'https://other.invalid'})
    else:
        client.stop(world.store, now_ms=world.wall)
    count = len(world.calls)
    with pytest.raises(ValueError):
        run(world)
    assert len(world.calls) == count and world.store.read(local.NAME)['runner']['active'] is not None


def test_stop_inflight_pregrant_status_keeps_slot_and_never_reenables(world):
    leave_pregrant_unknown(world)
    world.status_state, world.status_revision, world.status_report = 'canceled', 0, 'null'
    world.before[STATUS] = lambda _: client.stop(world.store, now_ms=world.wall)
    with pytest.raises(ValueError):
        run(world)
    saved = world.store.read(local.NAME)
    assert not saved['enabled'] and saved['runner']['active']['phase'] == 'ADMITTING' and not saved['runner']['terminal']
    assert not world.providers


@pytest.mark.parametrize('phase', ['GRANTING', 'STARTED'])
def test_central_terminal_cannot_erase_a_locally_requested_grant_or_started_intent(world, monkeypatch, phase):
    write = world.store.write
    def crash(name, value):
        write(name, value)
        active = value.get('runner', {}).get('active')
        if name == local.NAME and active and active['phase'] == phase:
            raise OSError('synthetic post-commit crash')
    monkeypatch.setattr(world.store, 'write', crash)
    with pytest.raises(OSError):
        run(world)
    monkeypatch.setattr(world.store, 'write', write)
    world.status_state, world.status_revision, world.status_report = 'expired', 0, 'null'
    assert run(world) == {'state': 'result_unknown'}
    saved = world.store.read(local.NAME)['runner']
    assert saved['active']['phase'] == phase and not saved['terminal'] and not world.providers


def test_pregrant_terminal_evidence_prevents_same_operation_redispatch(world):
    leave_pregrant_unknown(world)
    world.status_state, world.status_revision, world.status_report = 'canceled', 0, 'null'
    assert run(world)['state'] == 'pregrant_terminal'
    with pytest.raises(ValueError):
        run(world)
    assert [p for p, _, _ in world.calls].count(ADMIT) == 1 and not world.providers


def test_nonnull_report_is_not_pregrant_nonexecution_evidence(world):
    leave_pregrant_unknown(world)
    world.status_state, world.status_revision = 'canceled', 0
    transport = world.transport
    def factory(endpoint):
        original = transport(endpoint)
        def send(path, raw, headers):
            response = original.send(path, raw, headers)
            if path == STATUS:
                response['report'] = {'body_hash': '0' * 64, 'state': 'unknown', 'state_revision': 1}
            return response
        return SimpleNamespace(send=send)
    world.transport = factory
    assert run(world) == {'state': 'orphan_hold'}
    assert world.store.read(local.NAME)['runner']['active']['phase'] == 'ADMITTING'


def test_pregrant_terminal_save_failure_keeps_same_receipt_and_status_bytes(world, monkeypatch):
    original = leave_pregrant_unknown(world)
    world.status_state, world.status_revision, world.status_report = 'expired', 0, 'null'
    write = world.store.write
    def fail(name, value):
        runner = value.get('runner', {})
        if name == local.NAME and runner.get('active') is None and runner.get('terminal'):
            raise OSError(CANARY)
        return write(name, value)
    monkeypatch.setattr(world.store, 'write', fail)
    with pytest.raises(OSError):
        run(world)
    saved = world.store.read(local.NAME)['runner']
    assert saved['active']['admit'] == original['admit'] and not saved['terminal']
    status_raw = world.calls[-1][1]
    monkeypatch.setattr(world.store, 'write', write)
    assert run(world)['state'] == 'pregrant_terminal'
    assert world.calls[-1][1] == status_raw and not world.providers


def watch(w, *, interval=60, wait=None, budget=lambda: None, limit_deadline=lambda _: None):
    def sleeping(seconds):
        assert 0 < seconds <= 1
        # Session ownership lasts through sleep; monitoring/state locks do not.
        with Store(w.store.root) as other:
            with other.support_state_lock(), other.lock():
                pass
            with pytest.raises(BlockingIOError):
                with other.support_session_lock():
                    pass
        w.advance(round(seconds * 1000))
        if wait:
            wait(seconds)
    return client.watch(w.store, verifier=w.verify, check_pins=w.check, transport_factory=w.transport,
                        provider=w.provider, clock=lambda: w.wall / 1000, monotonic=lambda: w.mono,
                        interval=interval, wait=sleeping, budget=budget, limit_deadline=limit_deadline)


@pytest.mark.parametrize('interval,count', [(30, 10), (60, 5)])
def test_watch_owns_full_session_and_stops_at_300_seconds(world, interval, count):
    world.empty = True
    limits = []
    outcome = watch(world, interval=interval, limit_deadline=limits.append)
    assert outcome['state'] == 'watch_finished' and outcome['reason'] == 'deadline' and outcome['passes'] == count
    assert len(world.calls) == count and all(p == POLL for p, _, _ in world.calls)
    assert world.mono == 400 and limits == [NOW + 1800000] and not world.providers
    with world.store.support_session_lock():
        pass


def test_watch_empty_then_approved_operation_runs_provider_once(world):
    world.empty = True
    def pending(_):
        if world.wall >= NOW + 60000:
            world.empty = False
    world.before[POLL] = pending
    def reported(_):
        world.empty = True
        world.before[POLL] = lambda _: None
    world.before[REPORT] = reported
    outcome = watch(world)
    assert outcome['passes'] == 5 and len(world.providers) == 1
    assert [p for p, _, _ in world.calls].count(ADMIT) == 1


def test_watch_unknown_receipt_returns_without_sleep_or_new_poll(world):
    world.report_error, world.status_report = True, 'null'
    def no_wait(_):
        pytest.fail('unknown must end watch')
    outcome = watch(world, wait=no_wait)
    assert outcome['state'] == 'result_unknown' and outcome['passes'] == 1
    assert world.mono == 100 and len(world.providers) == 1
    assert [p for p, _, _ in world.calls].count(POLL) == 1


def test_watch_pregrant_terminal_allows_next_period_only(world):
    leave_pregrant_unknown(world)
    world.status_state, world.status_revision, world.status_report = 'canceled', 0, 'null'
    world.empty = True
    outcome = watch(world)
    assert outcome['passes'] == 5 and world.calls[2][0] == STATUS
    assert [p for p, _, _ in world.calls[3:]] == [POLL] * 4 and not world.providers


def test_watch_waiting_stop_has_no_next_network_and_reports_session_inflight(world):
    world.empty = True
    stops = []
    def stopped(_):
        if not stops:
            stops.append(client.stop(world.store, now_ms=int(world.wall)))
    outcome = watch(world, wait=stopped)
    assert outcome['reason'] == 'stopped_or_owner_changed' and outcome['passes'] == 1
    assert stops == [{'state': 'stopped', 'foreground_ended': False}]
    assert [p for p, _, _ in world.calls] == [POLL]
    assert client.stop(world.store, now_ms=int(world.wall))['foreground_ended'] is True


def test_watch_never_switches_to_new_consent_owner(world):
    world.empty = True
    def change_owner(_):
        state = world.store.read(local.NAME)
        state['owner_generation'] = '9' * 32
        world.store.write(local.NAME, state)
    outcome = watch(world, wait=change_owner)
    assert outcome['reason'] == 'stopped_or_owner_changed' and len(world.calls) == 1


def test_watch_original_expiry_is_shorter_than_session_upper_bound(world):
    world.empty = True
    # Match the existing signed expiry by starting near its end, without renewal.
    world.advance(1800000 - 20000)
    outcome = watch(world)
    assert outcome['reason'] == 'deadline' and outcome['passes'] == 1
    assert world.wall == NOW + 1800000 and len(world.calls) == 1


def test_watch_parent_budget_ends_before_new_poll_and_preserves_durable_state(world):
    world.empty = True
    def parent():
        if world.mono >= 102:
            raise TimeoutError(CANARY)
    with pytest.raises(TimeoutError):
        watch(world, budget=parent)
    assert len(world.calls) == 1 and world.store.read(local.NAME)['receipt']['accepted'] is True


def test_watch_delay_never_catches_up_missed_periods(world):
    world.empty = True
    call_times = []
    def slow(_):
        call_times.append(world.mono)
        world.advance(45000)
    world.before[POLL] = slow
    watch(world, interval=30)
    assert call_times == [100.0, 175.0, 250.0, 325.0]


@pytest.mark.parametrize('error', [EOFError, KeyboardInterrupt, OSError])
def test_watch_wait_interruption_keeps_receipt_and_releases_session(world, error):
    world.empty = True
    original = world.store.read(local.NAME)['receipt']
    def interrupted(_):
        raise error(CANARY)
    with pytest.raises(error):
        watch(world, wait=interrupted)
    assert world.store.read(local.NAME)['receipt'] == original and len(world.calls) == 1
    with world.store.support_session_lock():
        pass


@pytest.mark.parametrize('interval', [0, 1, 29, 31, 61, True, '30'])
def test_watch_interval_is_closed_before_network(world, interval):
    with pytest.raises(ValueError):
        watch(world, interval=interval)
    assert not world.calls and not world.providers


def test_watch_owner_change_between_idle_check_and_pass_cannot_be_adopted(world, monkeypatch):
    original = client._run_pass
    def race(*args, **kwargs):
        state = world.store.read(local.NAME)
        state['owner_generation'] = '8' * 32
        world.store.write(local.NAME, state)
        return original(*args, **kwargs)
    monkeypatch.setattr(client, '_run_pass', race)
    with pytest.raises(ValueError):
        watch(world)
    assert not world.calls and not world.providers
