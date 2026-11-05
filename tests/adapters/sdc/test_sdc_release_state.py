"""明示した合成基線と公開署名fixtureで、H/bindingの保存を検証する。"""
import release_apply_checks
import copy
import fcntl
import json
import os
from pathlib import Path
import time

import pytest

from adapters.sdc import release_contract as contract
from adapters.sdc import release_state as state
from adapters.sdc.storage import Store
import release_apply_owner
import update_journal

ROOT = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/releases'
SIGNATURES = json.loads((ROOT / 'signature-vectors.json').read_bytes())
VECTORS = {row['name']: row['value'] for row in json.loads((ROOT / 'release-vectors.json').read_bytes())['positive']}
APPLY = json.loads((ROOT / 'apply-vectors.json').read_bytes())


def baseline():
    trust = copy.deepcopy(SIGNATURES['trusts']['rotated'])
    current = copy.deepcopy(VECTORS['State'])
    current.update(SIGNATURES['scope'], trust_digest=contract.hash_release_trust(trust),
                   root_version=trust['root_version'], metadata_epoch=trust['metadata_epoch'])
    current['highest_seen'] = copy.deepcopy(current['highest_installed'])
    bindings = [dict(stream_id=current['stream_id'], **copy.deepcopy(current[name]))
                for name in ('installed', 'highest_installed')]
    installed = {**current['installed'], 'commit': 'a' * 40, 'lock_sha256': 'b' * 64,
                 'schema': 1, 'profile_id': 'synthetic', 'profile_sha256': 'c' * 64,
                 'runtime_fingerprint': 'd' * 64}
    return dict(trust=trust, state=current, baseline=installed, bindings=bindings,
                repo_fingerprint='e' * 64, owner_evidence_sha256='f' * 64)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / 'private-release', create=True) as value:
        yield value


def bootstrap(store, **supplied):
    with store.release_entry_lock():
        return state._bootstrap_verified(store, **supplied)


def test_missing_baseline_is_not_initialized_by_status(store):
    assert not state.legacy_blocked(store)
    with pytest.raises(ValueError, match='UNKNOWN_BASELINE'):
        state.status(store)
    assert store.read(state.NAME) is None
    assert not (store.root / 'release-owner.lock').exists()


def test_explicit_complete_synthetic_baseline_and_no_overwrite(store):
    supplied = baseline()
    bootstrap(store, **supplied)
    actual = state.status(store)
    assert actual['baseline'] == supplied['baseline'] and actual['revision'] == 1
    assert not actual['uncertain'] and not (store.root / 'credential').exists()
    assert state.legacy_blocked(store)
    assert store.read(state.ACTIVATION)['initial_baseline_sha256'] == store._fingerprint(supplied['baseline'])
    with pytest.raises(ValueError, match='ALREADY_INITIALIZED'):
        bootstrap(store, **supplied)


@pytest.mark.parametrize('mutate', [
    lambda value: value['state'].update(installed=None),
    lambda value: value.update(bindings=[]),
    lambda value: value['bindings'].pop(),
    lambda value: value['baseline'].update(release_id='0' * 64),
    lambda value: value.update(repo_fingerprint='invalid'),
])
def test_incomplete_or_mismatching_baseline_never_saved(store, mutate):
    supplied = baseline()
    mutate(supplied)
    with pytest.raises(ValueError):
        bootstrap(store, **supplied)
    assert store.read(state.NAME) is None


def test_authenticated_higher_observation_is_durable_without_install_or_preflight(store):
    bootstrap(store, **baseline())
    before = state.status(store)
    result = state.observe(store, SIGNATURES['attestations']['current']['envelope'], now_ms=SIGNATURES['now'])
    after = state.status(store)
    assert result['highest_seen_raised'] and after['revision'] == 2
    assert after['state']['highest_seen']['code_seq'] == 42
    assert after['state']['installed'] == before['state']['installed']
    assert after['state']['highest_installed'] == before['state']['highest_installed']
    assert len(after['bindings']) == 3 and after['baseline'] == before['baseline']
    # A later capacity/artifact/preflight rejection has no authority to undo observation.
    with Store(store.root) as reopened:
        assert state.status(reopened)['state']['highest_seen'] == after['state']['highest_seen']
    revision = after['revision']
    state.observe(store, SIGNATURES['attestations']['current']['envelope'], now_ms=SIGNATURES['now'])
    assert state.status(store)['revision'] == revision


def test_bad_signature_and_same_sequence_conflict_leave_bytes_unchanged(store):
    bootstrap(store, **baseline())
    raw = (store.root / state.NAME).read_bytes()
    forged = copy.deepcopy(SIGNATURES['attestations']['current']['envelope'])
    forged['signature'] = 'A' * len(forged['signature'])
    with pytest.raises(ValueError):
        state.observe(store, forged, now_ms=SIGNATURES['now'])
    assert (store.root / state.NAME).read_bytes() == raw
    saved = store.read(state.NAME)
    saved['bindings'].append({'stream_id': saved['state']['stream_id'], 'code_seq': 42, 'release_id': '0' * 64})
    saved['state']['highest_seen'] = {'code_seq': 42, 'release_id': '0' * 64}
    store.write(state.NAME, saved)
    raw = (store.root / state.NAME).read_bytes()
    with pytest.raises(ValueError):
        state.observe(store, SIGNATURES['attestations']['current']['envelope'], now_ms=SIGNATURES['now'])
    assert (store.root / state.NAME).read_bytes() == raw


def test_uncertain_publish_does_not_retry_same_store(store, monkeypatch):
    original = store.write
    calls = []
    def write_then_fail(name, value, **kwargs):
        calls.append(name)
        original(name, value, **kwargs)
        if name == state.NAME:
            raise OSError('synthetic directory fsync uncertainty')
    monkeypatch.setattr(store, 'write', write_then_fail)
    with pytest.raises(OSError):
        bootstrap(store, **baseline())
    assert state.status(store)['uncertain']
    with pytest.raises(ValueError, match='UNCERTAIN'):
        state.observe(store, SIGNATURES['attestations']['current']['envelope'], now_ms=SIGNATURES['now'])
    assert calls == [state.ACTIVATION, state.NAME]


def test_private_root_binding_and_capacity_never_truncate_history(store, monkeypatch):
    bootstrap(store, **baseline())
    raw = (store.root / state.NAME).read_bytes()
    monkeypatch.setattr(state, 'MAX_FILE', len(raw) - 1)
    with pytest.raises(ValueError, match='CAPACITY_EXCEEDED'):
        state.observe(store, SIGNATURES['attestations']['current']['envelope'], now_ms=SIGNATURES['now'])
    assert (store.root / state.NAME).read_bytes() == raw


def test_recreated_lock_or_copied_root_is_not_authority(store, tmp_path):
    bootstrap(store, **baseline())
    saved = store.read(state.NAME)
    with Store(tmp_path / 'other-private-root', create=True) as other:
        other.write(state.NAME, saved)
        with pytest.raises(ValueError, match='ROOT_MISMATCH'):
            state.status(other)
    (store.root / 'release-owner.lock').rename(store.root / 'synthetic-old-lock')
    with pytest.raises(ValueError, match='LOCK_MISMATCH'):
        state.status(store)


def test_root_recovery_updates_trust_without_erasing_realm_floors_or_bindings(store):
    bootstrap(store, **baseline())
    before = state.status(store)
    trust = state.update_trust(store, [SIGNATURES['proofs']['recovery']], now_ms=SIGNATURES['now'])
    after = state.status(store)
    assert trust == SIGNATURES['trusts']['recovery'] and after['trust'] == trust
    assert after['bindings'] == before['bindings'] and after['baseline'] == before['baseline']
    for key in (*contract.STATE_POINTS, 'realm_epoch'):
        assert after['state'][key] == before['state'][key]
    assert after['state']['metadata_epoch'] == before['state']['metadata_epoch'] + 1


def test_marker_fences_legacy_when_owner_publish_failed_or_owner_file_was_lost(store, monkeypatch):
    original = store.write
    def fail_owner(name, value, **kwargs):
        if name == state.NAME:
            raise OSError('synthetic owner publish failure')
        original(name, value, **kwargs)
    monkeypatch.setattr(store, 'write', fail_owner)
    with pytest.raises(OSError):
        bootstrap(store, **baseline())
    assert store.read(state.NAME) is None and state.legacy_blocked(store)
    with Store(store.root) as reopened:
        with pytest.raises(ValueError, match='ALREADY_INITIALIZED'):
            bootstrap(reopened, **baseline())
    assert store.read(state.NAME) is None


def test_activation_is_immutable_and_missing_or_changed_marker_holds(store):
    bootstrap(store, **baseline())
    marker = store.read(state.ACTIVATION)
    with pytest.raises(ValueError, match='IMMUTABLE'):
        store.write(state.ACTIVATION, marker)
    path = store.root / state.ACTIVATION
    path.rename(store.root / 'synthetic-saved-activation')
    assert state.legacy_blocked(store)
    with pytest.raises(ValueError, match='UNKNOWN_BASELINE'):
        state.status(store)
    changed = copy.deepcopy(marker)
    changed['initial_baseline_sha256'] = '0' * 64
    store.write(state.ACTIVATION, changed)
    with pytest.raises(ValueError, match='ACTIVATION_MISMATCH'):
        state.status(store)


def spend_baseline():
    supplied = baseline()
    witness = copy.deepcopy(APPLY['controls']['grant']['state'])
    supplied['trust'] = copy.deepcopy(VECTORS['Trust root version 1'])
    supplied['state'] = witness
    supplied['bindings'] = [dict(stream_id=witness['stream_id'], **witness[key])
                            for key in ('installed', 'highest_installed', 'highest_seen')]
    preconditions = APPLY['controls']['grant']['preconditions']
    supplied['baseline'].update(witness['installed'], commit=preconditions['current_commit'],
                                lock_sha256=preconditions['installed_lock_sha256'], schema=preconditions['installed_schema'])
    return supplied


def spend():
    return {'operation': copy.deepcopy(APPLY['operation']), 'grant': copy.deepcopy(APPLY['grant']),
            'admission': copy.deepcopy(APPLY['admission']), 'grant_envelope_sha256': '6' * 64,
            'witness_state': copy.deepcopy(APPLY['controls']['grant']['state']),
            'preconditions': copy.deepcopy(APPLY['controls']['grant']['preconditions']),
            'start_receipt_id': '00000000-0000-4000-8000-000000000099', 'started_at': APPLY['grant']['not_before']}


@pytest.fixture
def native_child(store, tmp_path, monkeypatch):
    home, repo = tmp_path / 'runtime', tmp_path / 'repository'
    data = home / 'data'
    data.mkdir(parents=True)
    repo.mkdir()
    supplied = spend_baseline()
    supplied['repo_fingerprint'] = release_apply_owner._repository_fingerprint(repo)
    bootstrap(store, **supplied)
    spent = state._consume_and_start(store, **spend())
    ref = {'schema_version': 'mcs.release-reference.v1',
           'root_fingerprint': store.root_fingerprint(), 'repo_fingerprint': supplied['repo_fingerprint'],
           'operation_id': spent['operation']['operation_id'], 'grant_id': spent['grant']['grant_id'],
           'plan_sha256': spent['operation']['plan_hash'], 'release_id': spent['operation']['release_id'],
           'target_code_seq': spent['operation']['target_code_seq'], 'target_commit': '1' * 40,
           'artifact_sha256': '2' * 64, 'lock_sha256': '3' * 64, 'target_schema': 1,
           'start_receipt_id': spent['start_receipt_id'], 'started_at': spent['started_at']}
    raw, receipt = release_apply_owner._child_ticket(ref)
    journal = {**update_journal.default_state(), 'release_ref': ref,
               'applying': {'sha': ref['target_commit'], 'release_ref': ref, 'release_child': receipt}}
    path = data / 'update_state.json'
    update_journal.save_state(path, journal)
    monkeypatch.setattr(release_apply_owner, '_control_root', lambda _home: store.root)
    monkeypatch.setattr(release_apply_checks, '_control_root', release_apply_owner._control_root)
    update = os.open(data / 'update.lock', os.O_CREAT | os.O_RDWR, 0o600)
    run = os.open(data / 'run.lock', os.O_CREAT | os.O_RDWR, 0o600)
    reader, writer = os.pipe()
    try:
        os.write(writer, raw)
        os.close(writer)
        writer = None
        fcntl.flock(update, fcntl.LOCK_EX)
        fcntl.flock(run, fcntl.LOCK_EX)
        with store.release_entry_lock() as gate:
            yield dict(home=home, repo=repo, path=path, journal=journal,
                       parameters={'ticket_fd': reader, 'locks': (gate, update, run),
                                   'deadline': time.monotonic() + 2})
    finally:
        for fd in (reader, writer, run, update):
            if fd is not None:
                os.close(fd)


def test_native_child_adopts_once_without_shared_gate_or_owner_state_changes(native_child, store, monkeypatch):
    child = native_child
    before = state.status(store)
    monkeypatch.setattr(release_apply_owner, '_open_release_gate',
                        lambda *_: pytest.fail('exclusive child tried legacy shared G'))
    monkeypatch.setattr(release_apply_checks, '_open_release_gate', release_apply_owner._open_release_gate)
    result = release_apply_owner._adopt_post_merge(child['home'], child['repo'], **child['parameters'])
    assert result['applying']['release_child']['phase'] == 'consumed'
    assert update_journal.load_state(child['path']) == result
    assert state.status(store) == before
    with pytest.raises(ValueError, match='CHILD_HOLD'):
        release_apply_owner._adopt_post_merge(child['home'], child['repo'], **child['parameters'])
    assert state.status(store) == before


@pytest.mark.parametrize('failure', ['other_repo', 'other_spent', 'reinstall', 'migration', 'missing_pipe'])
def test_native_child_rejects_cross_binding_or_runtime_change_without_journal_write(native_child, failure):
    child = native_child
    if failure == 'other_repo':
        child['repo'] = child['repo'].parent
    elif failure == 'other_spent':
        child['journal']['release_ref']['start_receipt_id'] = '00000000-0000-4000-8000-000000000001'
    elif failure == 'reinstall':
        child['journal']['applying']['reinstall'] = True
    elif failure == 'migration':
        child['journal']['applying']['runtime_migration'] = {'synthetic': True}
    else:
        child['parameters']['ticket_fd'] = child['parameters']['locks'][0]
    update_journal.save_state(child['path'], child['journal'])
    before = child['path'].read_bytes()
    with pytest.raises(ValueError, match='CHILD_HOLD'):
        release_apply_owner._adopt_post_merge(child['home'], child['repo'], **child['parameters'])
    assert child['path'].read_bytes() == before


def test_native_child_uncertain_consumption_never_returns_permission(native_child, monkeypatch):
    child = native_child
    publish = update_journal.save_state

    def uncertain(path, value):
        publish(path, value)
        raise OSError('synthetic uncertain fsync')

    monkeypatch.setattr(update_journal, 'save_state', uncertain)
    with pytest.raises(OSError, match='uncertain'):
        release_apply_owner._adopt_post_merge(child['home'], child['repo'], **child['parameters'])
    assert update_journal.load_state(child['path'])['applying']['release_child']['phase'] == 'consumed'
    with pytest.raises(ValueError, match='CHILD_HOLD'):
        release_apply_owner._adopt_post_merge(child['home'], child['repo'], **child['parameters'])


def test_native_child_deadline_still_applies_after_durable_consumption(native_child, monkeypatch):
    child = native_child
    publish = update_journal.save_state

    def slow_publish(path, value):
        publish(path, value)
        monkeypatch.setattr(release_apply_owner.time, 'monotonic',
                            lambda: child['parameters']['deadline'] + 1)

    monkeypatch.setattr(update_journal, 'save_state', slow_publish)
    with pytest.raises(ValueError, match='CHILD_HOLD'):
        release_apply_owner._adopt_post_merge(child['home'], child['repo'], **child['parameters'])
    assert update_journal.load_state(child['path'])['applying']['release_child']['phase'] == 'consumed'


def test_spent_and_started_fact_are_one_publish_and_saved_records_never_resume(store, monkeypatch):
    bootstrap(store, **spend_baseline())
    original = store.write
    saved = []
    def record(name, value, **kwargs):
        if name == state.NAME:
            saved.append(copy.deepcopy(value))
        original(name, value, **kwargs)
    monkeypatch.setattr(store, 'write', record)
    started = state._consume_and_start(store, **spend())
    assert len(saved) == 1
    assert saved[0]['spent'] == [started]
    assert saved[0]['state']['unresolved'] == {'operation_id': APPLY['operation']['operation_id'],
                                               'plan_sha256': APPLY['operation']['plan_hash'], 'phase': 'unknown'}
    assert saved[0]['spent'][0]['start_receipt_id'] == spend()['start_receipt_id']
    with Store(store.root) as reopened:
        assert state.status(reopened)['spent'] == [started]
        with pytest.raises(ValueError, match='STATE_CHANGED'):
            state._consume_and_start(reopened, **spend())
    assert len(saved) == 1


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(started_at=APPLY['grant']['expires_at']),
    lambda value: value['grant'].update(owner_generation='0' * 32),
    lambda value: value['grant'].update(state_sha256='0' * 64),
    lambda value: value['preconditions'].update(free_bytes=1),
    lambda value: value['witness_state']['highest_seen'].update(release_id='0' * 64),
])
def test_invalid_or_changed_grant_witness_cannot_publish_spent(store, mutate):
    bootstrap(store, **spend_baseline())
    original = (store.root / state.NAME).read_bytes()
    request = spend()
    mutate(request)
    with pytest.raises(ValueError):
        state._consume_and_start(store, **request)
    assert (store.root / state.NAME).read_bytes() == original


def test_unknown_commit_publish_holds_spent_and_never_reenters(store, monkeypatch):
    bootstrap(store, **spend_baseline())
    original = store.write
    def uncertain(name, value, **kwargs):
        original(name, value, **kwargs)
        if name == state.NAME:
            raise OSError('synthetic after replace/directory-fsync uncertainty')
    monkeypatch.setattr(store, 'write', uncertain)
    with pytest.raises(OSError):
        state._consume_and_start(store, **spend())
    assert state.status(store)['uncertain'] and state.status(store)['spent']
    with pytest.raises(ValueError, match='UNCERTAIN'):
        state._consume_and_start(store, **spend())


def report(sequence=1, *, phase='unknown', result=True):
    value = copy.deepcopy(APPLY['controls']['report'])
    value.update(sequence=sequence, state=phase, report_id=f'00000000-0000-4000-8000-{sequence:012d}')
    if result:
        value['result'].update(start_receipt_id=spend()['start_receipt_id'],
                                started_at=spend()['started_at'], verified_at=None, readiness='unknown',
                                installed_release_id=None, installed_code_seq=None, error_code='RESULT_UNKNOWN')
    else:
        value['result'] = None
    return value


def test_report_bytes_are_durable_idempotent_and_never_resume_or_clear_j(store):
    bootstrap(store, **spend_baseline())
    state._consume_and_start(store, **spend())
    before = state.status(store)
    value = report()
    saved = state._queue_report(store, value, now_ms=value['observed_at'])
    assert saved == state._queue_report(store, value, now_ms=value['observed_at'])
    after = state.status(store)
    assert after['revision'] == before['revision'] + 1
    assert after['state'] == before['state'] and after['spent'] == before['spent']
    with Store(store.root) as reopened:
        assert state.pending_reports(reopened) == [saved]
        assert json.loads(saved['raw']) == value
        with pytest.raises(ValueError, match='STATE_CHANGED'):
            state._consume_and_start(reopened, **spend())


def test_report_ack_converges_after_restart_without_changing_j_or_execution_facts(store):
    bootstrap(store, **spend_baseline())
    state._consume_and_start(store, **spend())
    value = report()
    saved = state._queue_report(store, value, now_ms=value['observed_at'])
    before = state.status(store)
    with Store(store.root) as reopened:
        accepted = state._acknowledge_report(reopened, report_id=value['report_id'],
                                             outcome_sha256=saved['sha256'])
        assert accepted == {**saved, 'acknowledged': True}
        after = state.status(reopened)
        assert after == {**before, 'revision': before['revision'] + 1, 'outbox': [accepted]}
        assert state.pending_reports(reopened) == []
        assert state._acknowledge_report(reopened, report_id=value['report_id'],
                                         outcome_sha256=saved['sha256']) == accepted
        assert state.status(reopened) == after
        assert state._queue_report(reopened, value, now_ms=value['observed_at']) == accepted


@pytest.mark.parametrize('unknown', [False, True])
def test_report_ack_mismatch_or_unknown_never_discards_pending_bytes(store, unknown):
    bootstrap(store, **spend_baseline())
    state._consume_and_start(store, **spend())
    value = report()
    saved = state._queue_report(store, value, now_ms=value['observed_at'])
    before = state.status(store)
    with pytest.raises(ValueError, match='REPORT_ACK_'):
        state._acknowledge_report(store, report_id=report(2)['report_id'] if unknown else value['report_id'],
                                   outcome_sha256=saved['sha256'] if unknown else '0' * 64)
    assert state.status(store) == before


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(sequence=3),
    lambda value: value.update(report_id=report()['report_id'], observed_at=value['observed_at'] + 1),
    lambda value: value.update(state='running'),
    lambda value: value.update(result=None),
    lambda value: value['result'].update(started_at=spend()['started_at'] + 1),
    lambda value: value['result'].update(readiness='ready'),
    lambda value: value['result'].update(installed_release_id=APPLY['operation']['release_id'],
                                         installed_code_seq=APPLY['operation']['target_code_seq']),
])
def test_bad_or_regressing_report_never_changes_saved_bytes(store, mutate):
    bootstrap(store, **spend_baseline())
    state._consume_and_start(store, **spend())
    first = report()
    state._queue_report(store, first, now_ms=first['observed_at'])
    raw = (store.root / state.NAME).read_bytes()
    value = report(2)
    mutate(value)
    with pytest.raises(ValueError):
        state._queue_report(store, value, now_ms=value['observed_at'])
    assert (store.root / state.NAME).read_bytes() == raw


def installed_observation():
    core = VECTORS['Attestation']['core']
    supplied = spend_baseline()['baseline']
    return {'commit': core['commit'], 'lock_sha256': core['lock_sha256'], 'schema': core['schema']['target'],
            **{key: supplied[key] for key in ('profile_id', 'profile_sha256', 'runtime_fingerprint')}}


def test_matching_authenticated_observation_advances_i_f_without_readiness_or_clearing_j(store, monkeypatch):
    import sdc_support_signatures
    monkeypatch.setattr(sdc_support_signatures, 'verify_release_attestation',
                        lambda *_a, **_k: copy.deepcopy(VECTORS['Attestation']))
    bootstrap(store, **spend_baseline())
    state._consume_and_start(store, **spend())
    before = state.status(store)
    point = state._observe_installed(store, {}, observed=installed_observation(), now_ms=APPLY['now'] + 20000)
    after = state.status(store)
    assert after['state']['installed'] == after['state']['highest_installed'] == point
    assert after['state']['highest_seen'] == before['state']['highest_seen']
    assert after['state']['unresolved'] == before['state']['unresolved']
    assert after['spent'] == before['spent'] and after['outbox'] == []
    assert after['baseline']['lock_sha256'] == VECTORS['Attestation']['core']['lock_sha256']
    value = report()
    value['result'].update(installed_release_id=point['release_id'], installed_code_seq=point['code_seq'])
    saved = state._queue_report(store, value, now_ms=value['observed_at'])
    assert json.loads(saved['raw'])['result']['verified_at'] is None
    assert json.loads(saved['raw'])['result']['readiness'] == 'unknown'
    revision = state.status(store)['revision']
    state._observe_installed(store, {}, observed=installed_observation(), now_ms=APPLY['now'] + 20000)
    assert state.status(store)['revision'] == revision


@pytest.mark.parametrize('field', ['commit', 'lock_sha256', 'schema', 'profile_id', 'profile_sha256', 'runtime_fingerprint'])
def test_inexact_observation_cannot_infer_installation_from_return_code_or_journal(store, monkeypatch, field):
    import sdc_support_signatures
    monkeypatch.setattr(sdc_support_signatures, 'verify_release_attestation',
                        lambda *_a, **_k: copy.deepcopy(VECTORS['Attestation']))
    bootstrap(store, **spend_baseline())
    state._consume_and_start(store, **spend())
    before = (store.root / state.NAME).read_bytes()
    observation = installed_observation()
    observation[field] = True if field == 'schema' else 'wrong'
    with pytest.raises(ValueError, match='INSTALLATION_NOT_OBSERVED'):
        state._observe_installed(store, {}, observed=observation, now_ms=APPLY['now'] + 20000)
    assert (store.root / state.NAME).read_bytes() == before


def test_bootstrap_requires_exclusive_entry_lifetime_and_rejects_replaced_inode(store):
    with pytest.raises(ValueError, match='ENTRY_LOCK_REQUIRED'):
        state._bootstrap_verified(store, **baseline())
    with store.release_entry_lock(shared=True), pytest.raises(ValueError, match='ENTRY_LOCK_REQUIRED'):
        state._bootstrap_verified(store, **baseline())
    with store.release_entry_lock():
        path = store.root / 'release-entry.lock'
        path.rename(store.root / 'synthetic-saved-entry-lock')
        path.touch(mode=0o600)
        with pytest.raises(ValueError, match='ENTRY_LOCK_MISMATCH'):
            state._bootstrap_verified(store, **baseline())
    assert store.read(state.NAME) is None and store.read(state.ACTIVATION) is None


def test_entry_lock_replacement_never_rebases_existing_owner(store):
    bootstrap(store, **baseline())
    path = store.root / 'release-entry.lock'
    path.rename(store.root / 'synthetic-saved-entry-lock')
    path.touch(mode=0o600)
    with pytest.raises(ValueError, match='ENTRY_LOCK_MISMATCH'):
        state.status(store)


def test_release_bootstrap_storage_is_separate_metadata_without_credential_or_activation(store, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('bootstrap metadata reached credentials or monitor projection')
    monkeypatch.setattr(store, '_marker', forbidden)
    monkeypatch.setattr(store, 'monitor_endpoint', forbidden)
    assert store.read('release-bootstrap.json') is None
    # The storage layer is not the bootstrap validator or an initialization permit.
    saved = {'schema_version': 'mcs.release-bootstrap.v1', 'synthetic_metadata': True}
    store.write('release-bootstrap.json', saved)
    assert store.read('release-bootstrap.json') == saved
    assert store.read(state.NAME) is None and store.read(state.ACTIVATION) is None
    assert not state.legacy_blocked(store)
    assert not (store.root / 'credential').exists()
    assert not (store.root / 'support-bootstrap.json').exists()


def test_oversized_release_bootstrap_keeps_previous_record(store):
    from adapters.sdc.storage import LIMITS
    assert LIMITS['release-bootstrap.json'] == 2 * 16384 + 4096
    store.write('release-bootstrap.json', {'synthetic': True})
    before = (store.root / 'release-bootstrap.json').read_bytes()
    with pytest.raises(ValueError):
        store.write('release-bootstrap.json', {'synthetic': 'x' * LIMITS['release-bootstrap.json']})
    assert (store.root / 'release-bootstrap.json').read_bytes() == before


@pytest.mark.parametrize('kind', ['observe', 'trust', 'installed'])
@pytest.mark.parametrize('changed', [False, True])
def test_crypto_releases_short_state_lock_and_rejects_a_changed_snapshot(store, monkeypatch, kind, changed):
    import sdc_support_signatures as signatures
    if kind == 'installed':
        # Typed installation fixture only; real signed attestations are covered by observe/trust.
        bootstrap(store, **spend_baseline())
        state._consume_and_start(store, **spend())
        original = lambda *_a, **_k: copy.deepcopy(VECTORS['Attestation'])  # noqa: E731
        method = 'verify_release_attestation'
        def action():
            return state._observe_installed(store, {}, observed=installed_observation(), now_ms=APPLY['now'] + 20000)
    elif kind == 'trust':
        bootstrap(store, **baseline())
        method = 'verify_release_root_chain'
        original = getattr(signatures, method)
        def action():
            return state.update_trust(store, [SIGNATURES['proofs']['recovery']], now_ms=SIGNATURES['now'])
    else:
        bootstrap(store, **baseline())
        method = 'verify_release_attestation'
        original = getattr(signatures, method)
        def action():
            return state.observe(store, SIGNATURES['attestations']['current']['envelope'], now_ms=SIGNATURES['now'])
    concurrent = []
    calls = []
    def verify(*args, **kwargs):
        # A fresh OFD cannot acquire this nonblocking flock if the caller still holds S.
        with store.release_owner_lock():
            calls.append('S is free')
        result = original(*args, **kwargs)
        if changed:
            with store.release_owner_lock():
                value = store.read(state.NAME)
                value['revision'] += 1
                store.write(state.NAME, value)
                concurrent.append(copy.deepcopy(value))
        return result
    monkeypatch.setattr(signatures, method, verify)
    if changed:
        with pytest.raises(ValueError, match='RELEASE_OWNER_STATE_CHANGED'):
            action()
        assert store.read(state.NAME) == concurrent[0]
    else:
        action()
    assert calls == ['S is free']
