"""release_auto_bridge のsidecar分類・v1 blocker・manifest/baseline/intent・drive位相を合成Storeで検証する。"""
import hashlib
import os

import pytest

import release_auto_bridge as bridge
from adapters.sdc import release_auto_migration as migration
from adapters.sdc import release_auto_state as auto_state
from adapters.sdc.storage import Store

H = 'a' * 64
U = '12345678-1234-4234-8234-123456789abc'


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / 'private-bridge', create=True) as value:
        yield value


def put(store, name, value):
    store.write(name, value)


def test_classify_empty_and_locks_skipped(store):
    store.install_runtime_entry_lock()
    assert bridge.classify_sidecars(store) == []


def test_classify_sidecars_rows_and_hashes(store):
    put(store, 'release-owner.json', {'schema_version': 'mcs.release-owner.v1', 'spent': [], 'outbox': []})
    put(store, 'release-successor.json', {'x': 1})
    rows = bridge.classify_sidecars(store)
    by_id = {r['logical_id']: r for r in rows}
    assert set(by_id) == {'release-owner.json', 'release-successor.json'}
    raw = store.read_migration_raw('release-owner.json')
    assert by_id['release-owner.json']['before_sha256'] == hashlib.sha256(raw).hexdigest()
    assert by_id['release-owner.json']['disposition'] == 'translate_verified'
    assert by_id['release-successor.json']['disposition'] == 'archive_exact'


def test_classify_unlisted_and_unresolved_refuse(store):
    fd = os.open('mystery.json', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=store.fd)
    os.write(fd, b'{}')
    os.close(fd)
    with pytest.raises(bridge.BridgeError, match='UNLISTED_SIDECAR'):
        bridge.classify_sidecars(store)
    os.unlink('mystery.json', dir_fd=store.fd)
    put(store, 'release-start-intent.json', {'schema_version': 'mcs.start-intent.v3'})
    with pytest.raises(bridge.BridgeError, match='UNRESOLVED_RELEASE_START_INTENT_JSON'):
        bridge.classify_sidecars(store)


@pytest.mark.parametrize('doc,code', [
    ({'schema_version': 'mcs.release-owner.v1', 'spent': [{'x': 1}], 'outbox': []}, 'V1_SPENT_PRESENT'),
    ({'schema_version': 'mcs.release-owner.v1', 'spent': [], 'outbox': [{'acknowledged': False}]}, 'V1_UNKNOWN_PRESENT'),
    ({'schema_version': 'mcs.release-owner.v3'}, 'ALREADY_V3'),
    ({'schema_version': 'other'}, 'OWNER_SCHEMA_UNKNOWN'),
    ('not-a-dict', 'V1_UNREADABLE'),
])
def test_check_v1_blockers_owner(store, doc, code):
    if isinstance(doc, str):
        fd = os.open('release-owner.json', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=store.fd)
        os.write(fd, doc.encode())
        os.close(fd)
    else:
        put(store, 'release-owner.json', doc)
    with pytest.raises(bridge.BridgeError, match=code):
        bridge.check_v1_blockers(store)


def test_check_v1_blockers_pending_records(store):
    put(store, 'release-owner.json', {'schema_version': 'mcs.release-owner.v1', 'spent': [], 'outbox': []})
    assert bridge.check_v1_blockers(store) is True
    put(store, 'release-successor.json', {'x': 1})
    with pytest.raises(bridge.BridgeError, match='V1_SUCCESSOR_PENDING'):
        bridge.check_v1_blockers(store)
    os.unlink('release-successor.json', dir_fd=store.fd)
    put(store, 'release-start-intent.json', {'schema_version': 'mcs.start-intent.v3'})
    with pytest.raises(bridge.BridgeError, match='INTENT_PENDING'):
        bridge.check_v1_blockers(store)


def manifest_kwargs(store):
    return dict(entrypoints=['mcs_update.py'],
                files=[{'path': 'mcs/core/runtime_entry.py', 'mode': '100644', 'sha256': H}],
                dependency_policy_sha256='b' * 64, recovery_generation_sha256='c' * 64)


def test_build_control_manifest_valid_and_invalid(store):
    result = bridge.build_control_manifest(**manifest_kwargs(store))
    assert result['manifest']['schema_version'] == 'mcs.release-control-manifest.v3'
    assert len(result['sha256']) == 64
    bad = manifest_kwargs(store)
    bad['files'] = [{'path': 'x', 'mode': '100644', 'sha256': 'zz'}]
    with pytest.raises(bridge.BridgeError, match='MANIFEST_INVALID'):
        bridge.build_control_manifest(**bad)


def owner_kwargs(store):
    return dict(scope={'product': 'hermes-mcs', 'tenant': 't', 'installation': 'i',
                       'device_id': U, 'realm_epoch': 'r', 'environment': 'synthetic', 'stream_id': 's'},
                root_fingerprint=store.root_fingerprint(),
                repository_fingerprint='d' * 64, activation_sha256='e' * 64,
                control_closure_sha256='f' * 64,
                profile={'profile_id': 'auto-mini', 'profile_sha256': '3' * 64,
                         'runtime_mode': 'standalone', 'os': 'macos', 'cpu': 'arm64',
                         'python': [3, 14], 'control_closure_sha256': '4' * 64},
                lock_fingerprints={k: '0' * 64 for k in 'GUQR CAMO'.replace(' ', '')},
                installed_source_manifest_sha256='1' * 64,
                verified_source_manifest_sha256='2' * 64,
                baseline={'release_id': H, 'code_seq': 1, 'source_commit': 'b' * 40,
                          'artifact_sha256': 'c' * 64, 'artifact_size': 1024})


def test_initial_owner_value_validates(store):
    value = bridge.initial_owner_value(**owner_kwargs(store))
    assert value['schema_version'] == 'mcs.release-owner.v3' and value['revision'] == 1
    bad = owner_kwargs(store)
    bad['scope'] = {'product': 'hermes-mcs'}
    with pytest.raises(bridge.BridgeError, match='BASELINE_INVALID'):
        bridge.initial_owner_value(**bad)


def migration_kwargs(store):
    return dict(root_fingerprint=store.root_fingerprint(), repository_fingerprint='d' * 64,
                source_schema='mcs.release-owner.v1', bridge_sha256='e' * 64,
                control_closure_sha256='f' * 64, recovery_generation_sha256='0' * 64,
                external_floor_sha256='9' * 64,
                files=[{'logical_id': 'release-owner.json', 'before_sha256': H,
                        'size': 10, 'disposition': 'translate_verified'}],
                human_receipt_id=U, created_at=1000)


def test_plan_migration_phase_and_validation(store):
    intent = bridge.plan_migration(**migration_kwargs(store))
    assert intent['phase'] == 'prepared' and intent['target_schema'] == 'mcs.release-owner.v3'
    bad = migration_kwargs(store)
    bad['human_receipt_id'] = 'not-a-uuid'
    with pytest.raises(bridge.BridgeError, match='INTENT_INVALID'):
        bridge.plan_migration(**bad)


def test_drive_happy_path_and_resume(store):
    intent = bridge.plan_migration(**migration_kwargs(store))
    calls = []
    owner_doc = bridge.initial_owner_value(**owner_kwargs(store))

    def archive(name, before):
        calls.append(('archive', name, before))

    def install_owner():
        store.write(auto_state.OWNER_NAME, owner_doc)
        calls.append(('owner',))

    def install_q():
        store.install_runtime_entry_lock()
        calls.append(('q',))

    def verify():
        calls.append(('verify',))
        return True

    result = bridge.drive(store, intent, archive=archive, install_owner=install_owner,
                          install_q=install_q, verify=verify)
    assert result['phase'] == 'verified'
    assert [c[0] for c in calls] == ['archive', 'owner', 'q', 'verify']
    assert migration.load(store)['phase'] == 'verified'
    os.stat('runtime-entry.lock', dir_fd=store.fd)


def test_drive_resumes_from_recorded_phase(store):
    intent = bridge.plan_migration(**migration_kwargs(store))
    migration.prepare(store, intent)
    migration.advance(store, migration_id=intent['migration_id'], phase='old_archived')
    calls = []
    owner_doc = bridge.initial_owner_value(**owner_kwargs(store))
    result = bridge.drive(store, intent,
                          archive=lambda n, b: calls.append('archive'),
                          install_owner=lambda: (store.write(auto_state.OWNER_NAME, owner_doc), calls.append('owner')),
                          install_q=lambda: (store.install_runtime_entry_lock(), calls.append('q')),
                          verify=lambda: calls.append('verify') or True)
    assert result['phase'] == 'verified'
    assert 'archive' not in calls


def test_drive_verify_failure_holds(store):
    intent = bridge.plan_migration(**migration_kwargs(store))
    owner_doc = bridge.initial_owner_value(**owner_kwargs(store))
    with pytest.raises(bridge.BridgeError, match='VERIFY_FAILED'):
        bridge.drive(store, intent, archive=lambda n, b: None,
                     install_owner=lambda: store.write(auto_state.OWNER_NAME, owner_doc),
                     install_q=lambda: store.install_runtime_entry_lock(),
                     verify=lambda: False)
    assert migration.load(store)['phase'] == 'owner_replaced'
