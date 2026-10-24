"""Synthetic source/mark oracles; no live health, Keychain, DB, HTTP or process."""
import copy
import hashlib
import json
import os

import pytest

from adapters.sdc.diagnostic_contract import COMPONENTS, LIMITS, validate_diagnostic
import sdc_diagnostic_events as provider

NOW = 1800000000000
SINCE = NOW - 60000
CANARY = 'SYNTHETIC_PATIENT_SECRET_PATH_HOST_ID_DO_NOT_COPY'


def health(*reasons, at=NOW, **extra):
    return {'at': at / 1000, 'overall': 'ok', 'run_status': 'ok',
            'state_reasons': list(reasons), 'disk_low': False,
            'backup': {'state': 'disabled', 'reasons': []}, 'notify': {'state': 'ok'}, **extra}


def runtime(*, at=NOW, process='running', **extra):
    return {'schema_version': 'sdc.support.diagnostic-result.v1', 'diagnostic_id': 'runtime.status.v1',
            'observed_at': at, 'status': 'partial', 'data': {'mode': 'standalone', 'process': process}, **extra}


def sample(tmp_path, *, report=None, witness=None, previous=None, since=SINCE, now=NOW, connector=None, **options):
    return provider.collect_events(trusted_root=tmp_path, mode='standalone',
        previous_marks={} if previous is None else previous, since_ms=since, now_ms=now,
        health_reader=lambda _: report, diagnostic_collector=lambda *a, **k: witness,
        connector_observation=connector, **options)


@pytest.mark.parametrize('reason,expected', [
    ('run_failed', ('scheduler', 'run', 'UNKNOWN', 'error')),
    ('stage_errors', ('scheduler', 'run', 'UNKNOWN', 'warning')),
    ('run_deadline_exceeded', ('scheduler', 'run', 'TIMEOUT', 'error')),
    ('collection_incomplete', ('scheduler', 'receive', 'UNKNOWN', 'warning')),
    ('session_expired', ('connectivity', 'receive', 'REJECTED', 'warning')),
    ('notification_failed', ('notification', 'send', 'UNKNOWN', 'error')),
    ('notification_deferred', ('notification', 'send', 'UNKNOWN', 'info')),
    ('notification_parked', ('notification', 'send', 'UNKNOWN', 'warning')),
    ('notification_held', ('notification', 'send', 'UNKNOWN', 'warning')),
    ('notification_pending', ('notification', 'send', 'UNKNOWN', 'info')),
    ('disk_low', ('storage', 'run', 'RESOURCE_LIMIT', 'warning')),
    ('ledger_relation_violations', ('storage', 'verify', 'INTEGRITY_FAILURE', 'error')),
    ('backup_not_verified', ('storage', 'verify', 'UNKNOWN', 'warning')),
    ('backup_deferred_deadline', ('storage', 'verify', 'TIMEOUT', 'warning')),
])
def test_fixed_health_producer_mapping(reason, expected, tmp_path):
    result = sample(tmp_path, report=health(reason))
    assert result['events'] == [dict(zip(('component', 'stage', 'code', 'severity'), expected),
                                      bucket_start=NOW // 600000 * 600000, count=1)]
    validate_diagnostic('Event', result['events'][0], now_ms=NOW)
    assert result['partial'] is True and result['dropped_events'] == 0


def test_one_row_counts_one_classified_observation_not_patients_failures_or_reason_duplicates(tmp_path):
    report = health('notification_parked', 'notification_held', 'notification_parked',
                    notify={'pending': 100000, 'failed': 50000, 'patient': CANARY})
    result = sample(tmp_path, report=report)
    assert result['events'] == [{'component': 'notification', 'stage': 'send', 'code': 'UNKNOWN',
                                'severity': 'warning', 'bucket_start': NOW, 'count': 1}]
    assert CANARY not in json.dumps(result)


def test_fixed_backup_invalid_is_config_evidence_not_generic_health_failure(tmp_path):
    result = sample(tmp_path, report=health(backup={'state': 'invalid', 'reasons': ['backup_config_invalid']}))
    assert result['events'] == [{'component': 'config', 'stage': 'verify', 'code': 'INVALID_STATE',
                                'severity': 'error', 'bucket_start': NOW, 'count': 1}]


def test_safe_field_fallbacks_do_not_invent_cause_or_crash(tmp_path):
    result = sample(tmp_path, report=health(run_status='failed', disk_low=True, backup=None))
    assert {(row['component'], row['code']) for row in result['events']} == {
        ('scheduler', 'UNKNOWN'), ('storage', 'RESOURCE_LIMIT')}
    assert 'config' not in result['source_marks']
    assert all(row['code'] != 'CRASH' for row in result['events'])


@pytest.mark.parametrize('notify', [{'state': 'incomplete'}, {'state': 'parked'}, {'state': CANARY}])
def test_observed_notification_classification_without_a_cause_is_unknown(notify, tmp_path):
    result = sample(tmp_path, report=health(notify=notify))
    assert len(result['events']) == 1 and result['events'][0]['component'] == 'notification'
    assert result['events'][0]['code'] == 'UNKNOWN' and result['partial']


def test_missing_notification_field_does_not_publish_a_completed_notification_mark(tmp_path):
    result = sample(tmp_path, report=health(notify=None))
    assert 'notification' not in result['source_marks'] and result['partial']


def test_code_change_guard_is_not_an_update_outcome_or_a_failure(tmp_path):
    result = sample(tmp_path, report=health('code_changed', run_status='partial'))
    assert result['events'] == [] and 'update' not in result['source_marks'] and result['partial']


def test_unknown_reason_and_connector_label_never_enter_dto_or_hash(tmp_path):
    first = sample(tmp_path, report=health(CANARY), connector={'observed_at': NOW, 'code': CANARY})
    second = sample(tmp_path, report=health('different-private-label'),
                    connector={'observed_at': NOW, 'code': 'another-private-label'})
    assert first == second
    assert first['events'] and all(row['code'] == 'UNKNOWN' for row in first['events'])
    assert CANARY not in json.dumps(first)


@pytest.mark.parametrize('code,expected', [
    ('SDC_ACCEPTED', None), ('SDC_EXPLICIT_RESUME', None),
    ('SDC_RETRY_LATER', ('UNKNOWN', 'warning')),
    ('SDC_REQUEST_DENIED', ('REJECTED', 'error')),
    ('SDC_TIME_SKEW', ('INVALID_STATE', 'warning')),
])
def test_only_injected_fixed_connector_observation_is_used(code, expected, tmp_path):
    result = sample(tmp_path, connector={'observed_at': NOW - 1, 'code': code, 'credential': CANARY})
    assert set(result['source_marks']) == {'connector'}
    if expected is None:
        assert result['events'] == []
    else:
        assert [(row['code'], row['severity']) for row in result['events']] == [expected]


def test_unobserved_sources_neither_emit_fake_failures_nor_advance_marks(tmp_path):
    result = sample(tmp_path)
    assert result == {'events': [], 'dropped_events': 0, 'partial': True, 'observed_at': NOW, 'source_marks': {}}
    good = sample(tmp_path, report=health(), witness=runtime())
    assert good['events'] == [] and good['partial'] is True
    assert set(good['source_marks']) == {'scheduler', 'notification', 'config', 'runtime'}
    assert 'storage' not in good['source_marks'] and 'connectivity' not in good['source_marks']
    assert 'update' not in good['source_marks'] and 'connector' not in good['source_marks']
    assert set(provider.SOURCE_COVERAGE) == set(COMPONENTS)


@pytest.mark.parametrize('at', [None, True, NOW + 1, NOW - 15001])
def test_runtime_missing_future_or_stale_witness_is_not_a_crash_or_success(at, tmp_path):
    result = sample(tmp_path, witness=runtime(at=at))
    assert result['events'] == [] and result['source_marks'] == {} and result['partial']


def test_observed_but_unclear_runtime_is_unknown_with_actual_source_time(tmp_path):
    result = sample(tmp_path, witness=runtime(at=NOW - 3, process=CANARY))
    assert result['events'][0]['component'] == 'runtime' and result['events'][0]['code'] == 'UNKNOWN'
    assert result['source_marks']['runtime']['observed_at'] == NOW - 3
    assert CANARY not in json.dumps(result)


def test_equal_occurrence_reread_and_reason_reordering_count_nothing(tmp_path):
    original = health('disk_low', 'notification_failed')
    first = sample(tmp_path, report=original)
    reordered = health('notification_failed', 'disk_low')
    assert sample(tmp_path, report=reordered)['source_marks'] == first['source_marks']
    second = sample(tmp_path, report=reordered, previous=first['source_marks'], now=NOW + 1000)
    assert second['events'] == [] and second['source_marks'] == {}
    assert second['dropped_events'] == 0


def test_same_timestamp_conflict_does_not_rewrite_mark_or_double_count(tmp_path):
    first = sample(tmp_path, report=health('notification_parked'))
    result = sample(tmp_path, report=health('notification_failed'), previous=first['source_marks'])
    assert result['events'] == [] and result['source_marks'] == {} and result['partial']


def test_new_producer_observation_can_count_one_but_assembly_clock_alone_cannot(tmp_path):
    first = sample(tmp_path, report=health('disk_low'))
    result = sample(tmp_path, report=health('disk_low', at=NOW + 1), previous=first['source_marks'], now=NOW + 1)
    assert len(result['events']) == 1 and result['events'][0]['count'] == 1
    assert result['source_marks']['storage']['signature'] != first['source_marks']['storage']['signature']


def test_later_assembly_does_not_suppress_delayed_independent_source(tmp_path):
    first = sample(tmp_path, witness=runtime())
    later = sample(tmp_path, report=health('disk_low', at=NOW - 30000), previous=first['source_marks'], now=NOW + 1)
    assert later['events'][0]['component'] == 'storage'
    assert later['source_marks']['storage']['observed_at'] == NOW - 30000
    assert 'runtime' not in later['source_marks']


def test_older_component_observation_does_not_roll_its_mark_back(tmp_path):
    first = sample(tmp_path, report=health('disk_low'))
    result = sample(tmp_path, report=health('disk_low', at=NOW - 1), previous=first['source_marks'])
    assert result['events'] == [] and result['source_marks'] == {} and result['partial']


@pytest.mark.parametrize('at', [SINCE - 1, NOW + 1])
def test_first_read_excludes_preconsent_and_future_history_without_marking_complete(at, tmp_path):
    result = sample(tmp_path, report=health('disk_low', at=at), connector={'observed_at': at, 'code': 'SDC_REQUEST_DENIED'})
    assert result['events'] == [] and result['source_marks'] == {} and result['dropped_events'] == 0


def test_expired_rows_drop_once_in_dto_row_units_and_preserve_source_marks(tmp_path):
    at = NOW - LIMITS['max_age_ms'] - 1
    report = health('notification_parked', 'notification_held', 'disk_low', at=at)
    first = sample(tmp_path, report=report, since=at - 1)
    assert first['events'] == [] and first['dropped_events'] == 2  # duplicate classification is one row
    assert first['source_marks']['storage']['observed_at'] == at
    again = sample(tmp_path, report=report, since=at - 1, previous=first['source_marks'])
    assert again['dropped_events'] == 0 and again['source_marks'] == {}


def test_bucket_freshness_uses_wire_boundary_not_unrounded_source_time(tmp_path):
    now = NOW + 123
    at = now - LIMITS['max_age_ms']
    result = sample(tmp_path, report=health('disk_low', at=at), since=at, now=now)
    assert result['events'] == [] and result['dropped_events'] == 1


def test_private_fields_and_values_do_not_even_affect_marks(tmp_path):
    clean = health('disk_low')
    dirty = {**clean, 'run_id': CANARY, 'errors': [CANARY], 'patient_context': {CANARY: 100000},
             'notify': {'state': 'ok', 'pending': 88888, 'url': CANARY}, 'disk_free_mb': 123,
             'raw_log': CANARY, 'host': CANARY, 'credentials': {CANARY: CANARY}}
    assert sample(tmp_path, report=clean) == sample(tmp_path, report=dirty)
    mark = sample(tmp_path, report=clean)['source_marks']['storage']
    safe = {'component': 'storage', 'observed_at': NOW,
            'classifications': [('run', 'RESOURCE_LIMIT', 'warning')]}
    raw = json.dumps(safe, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    assert mark['signature'] == hashlib.sha256(raw).hexdigest()


def test_unselected_private_object_is_not_traversed_or_serialized(tmp_path):
    class Private:
        def __repr__(self):
            pytest.fail('unselected object was inspected')

    clean = health('disk_low')
    assert sample(tmp_path, report={**clean, 'private': Private()}) == sample(tmp_path, report=clean)


@pytest.mark.parametrize('report', [None, [], {}, {'at': NOW / 1000}, health(*(['disk_low'] * 65)),
    health('x' * 129), health(state_reasons=[{}]), health(at=-1), health(at=float('nan'))])
def test_malformed_or_overlarge_source_is_partial_not_trimmed(report, tmp_path):
    result = sample(tmp_path, report=report)
    assert result['events'] == [] and result['source_marks'] == {} and result['partial']


@pytest.mark.parametrize('changes', [
    {'trusted_root': 'relative'}, {'trusted_root': '/synthetic/../private'}, {'mode': 'unknown'},
    {'since_ms': True}, {'now_ms': SINCE - 1}, {'previous_marks': {'private-label': {}}},
    {'previous_marks': {'runtime': {'observed_at': NOW + 1, 'signature': 'a' * 64}}},
    {'previous_marks': {'runtime': {'observed_at': NOW, 'signature': CANARY}}},
    {'previous_marks': {'runtime': {'observed_at': NOW, 'signature': 'a' * 64, 'extra': CANARY}}},
])
def test_invalid_context_and_clock_rollback_stop_before_any_source_read(changes, tmp_path):
    calls = []
    args = dict(trusted_root=tmp_path, mode='standalone', previous_marks={}, since_ms=SINCE,
                now_ms=NOW, health_reader=lambda _: calls.append('health'),
                diagnostic_collector=lambda *a, **k: calls.append('witness'))
    args.update(changes)
    with pytest.raises(ValueError, match='^SDC_DIAGNOSTIC_PROVIDER_INVALID$'):
        provider.collect_events(**args)
    assert calls == []


def test_input_marks_and_injected_sources_are_not_mutated(tmp_path):
    report = health('disk_low')
    previous = {'update': {'observed_at': SINCE, 'signature': 'a' * 64}}
    before = copy.deepcopy((report, previous))
    sample(tmp_path, report=report, previous=previous)
    assert (report, previous) == before


def test_source_errors_are_fixed_but_parent_cancellation_propagates(tmp_path):
    def fail(*args, **kwargs):
        raise ValueError(CANARY)

    args = dict(trusted_root=tmp_path, mode='standalone', previous_marks={}, since_ms=SINCE,
                now_ms=NOW, health_reader=fail, diagnostic_collector=fail)
    result = provider.collect_events(**args)
    assert result['source_marks'] == {} and CANARY not in json.dumps(result)

    def deadline(*args, **kwargs):
        raise TimeoutError('synthetic deadline')

    with pytest.raises(TimeoutError):
        provider.collect_events(**{**args, 'health_reader': deadline})


def test_fixed_health_file_only_and_hermes_never_calls_standalone_collector(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'health.json').write_text(json.dumps(health('disk_low', errors=[CANARY])))
    (data / 'run.log').write_text(CANARY)

    def forbidden(*args, **kwargs):
        pytest.fail('Hermes must not borrow a standalone witness')

    result = provider.collect_events(trusted_root=tmp_path, mode='hermes', previous_marks={},
                                    since_ms=SINCE, now_ms=NOW, diagnostic_collector=forbidden)
    assert result['events'][0]['code'] == 'RESOURCE_LIMIT'
    assert 'runtime' not in result['source_marks'] and CANARY not in json.dumps(result)


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'fifo', 'oversized', 'duplicate', 'corrupt', 'data-link'])
def test_fixed_health_file_boundary_never_follows_or_blocks(kind, tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    path = data / 'health.json'
    if kind == 'symlink':
        target = tmp_path / 'private'
        target.write_text(CANARY)
        path.symlink_to(target)
    elif kind == 'directory':
        path.mkdir()
    elif kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'oversized':
        path.write_bytes(b' ' * (provider.MAX_HEALTH_BYTES + 1))
    elif kind == 'duplicate':
        path.write_text('{"at":1,"at":2}')
    elif kind == 'data-link':
        data.rmdir()
        target = tmp_path / 'private-directory'
        target.mkdir()
        (target / 'health.json').write_text(json.dumps(health('disk_low')))
        data.symlink_to(target, target_is_directory=True)
    else:
        path.write_text(CANARY)
    result = provider.collect_events(trusted_root=tmp_path, mode='hermes', previous_marks={}, since_ms=SINCE, now_ms=NOW)
    assert result['events'] == [] and result['source_marks'] == {} and result['partial']


def test_existing_passive_runtime_collector_is_used_without_active_probe(tmp_path, monkeypatch):
    import sdc_support_diagnostics
    calls = []

    def collect(diagnostic_id, **options):
        calls.append((diagnostic_id, options))
        return runtime(at=NOW - 10)

    monkeypatch.setattr(sdc_support_diagnostics, 'collect_diagnostic', collect)
    result = provider.collect_events(trusted_root=tmp_path, mode='standalone', previous_marks={},
                                    since_ms=SINCE, now_ms=NOW, health_reader=lambda _: None)
    assert calls == [('runtime.status.v1', {'generated_at_ms': NOW, 'root': tmp_path, 'mode': 'standalone'})]
    assert result['events'] == [] and result['source_marks']['runtime']['observed_at'] == NOW - 10


def test_passive_owner_witness_ids_and_unrelated_fields_never_enter_marks(tmp_path, monkeypatch):
    import socket
    import sqlite3
    import subprocess
    import mcs_util
    from adapters.sdc.keychain import KeychainCredentialStore
    from mcs_standalone import service

    def forbidden(*args, **kwargs):
        pytest.fail('provider attempted active I/O or a private configuration read')

    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(mcs_util, 'load_config', forbidden)
    monkeypatch.setattr(KeychainCredentialStore, 'read', forbidden)
    witness = {'updated_at': NOW / 1000, 'started_at': SINCE / 1000,
               'generation': '00000000000040008000000000000001', 'private': CANARY}

    def status(root, *, now):
        assert root == tmp_path and now == NOW / 1000
        return witness

    monkeypatch.setattr(service, 'status', status)
    args = dict(trusted_root=tmp_path, mode='standalone', previous_marks={}, since_ms=SINCE,
                now_ms=NOW, health_reader=lambda _: None)
    first = provider.collect_events(**args)
    witness.update(generation='00000000000040008000000000000002', private=object())
    assert provider.collect_events(**args) == first
    assert set(first['source_marks']) == {'runtime'} and first['events'] == [] and first['partial']
