"""runtime_entry.acquire のv3判定・Q SH保持・fence読取りを合成control rootで検証する。"""
import os

import pytest

import runtime_entry
from adapters.sdc import release_auto_state as auto_state
from adapters.sdc.storage import Store

U = '12345678-1234-4234-8234-123456789abc'
H = 'a' * 64


def owner_value(store):
    ref = {'release_id': H, 'code_seq': 1, 'source_commit': 'b' * 40,
           'artifact_sha256': 'c' * 64, 'artifact_size': 1024}
    return {
        'schema_version': 'mcs.release-owner.v3',
        'scope': {'product': 'hermes-mcs', 'tenant': 'tenant-a', 'installation': 'inst-a',
                  'device_id': U, 'realm_epoch': 'realm-1', 'environment': 'synthetic', 'stream_id': 'st'},
        'root_fingerprint': store.root_fingerprint(),
        'repository_fingerprint': 'd' * 64, 'activation_sha256': 'e' * 64,
        'control_closure_sha256': 'f' * 64,
        'revision': 1, 'execution_revision': 1,
        'trust_json': '{}', 'legacy_evidence': [], 'operation_evidence': [],
        'lock_fingerprints': {k: '0' * 64 for k in 'GUQR CAMO'.replace(' ', '')},
        'installed_source_manifest_sha256': '1' * 64, 'verified_source_manifest_sha256': '2' * 64,
        'profile': {'profile_id': 'auto-mini', 'profile_sha256': '3' * 64, 'runtime_mode': 'standalone',
                    'os': 'macos', 'cpu': 'arm64', 'python': [3, 14], 'control_closure_sha256': '4' * 64},
        'consent': None,
        'highest_seen': ref, 'installed': ref, 'highest_installed': ref, 'verified_installed': ref,
        'runtime_generation': 7, 'bindings': [{'code_seq': 1, 'release_id': H}],
        'active': None, 'spent': [], 'outcomes': [], 'outbox': [], 'prepare_budget': [],
    }


@pytest.fixture
def runtime_home(tmp_path):
    return tmp_path / 'mcs-home'


def managed_root(runtime_home):
    root = runtime_entry.control_root(runtime_home)
    root.mkdir(mode=0o700, parents=True)
    os.chmod(root.parent, 0o700)
    return root


def seed_v3(runtime_home, *, owner=True):
    root = managed_root(runtime_home)
    with Store(root) as store:
        store.install_runtime_entry_lock()
        if owner:
            store.write(auto_state.OWNER_NAME, owner_value(store))
    return root


def test_legacy_device_without_control_root(runtime_home):
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert not entry.managed and entry.fd is None and entry.owner is None
    finally:
        entry.close()


def test_control_root_without_q_holds(runtime_home):
    managed_root(runtime_home)
    with pytest.raises(runtime_entry.RuntimeEntryError, match='RUNTIME_HOLD'):
        runtime_entry.acquire(runtime_home, wait_s=0)


def test_missing_owner_holds(runtime_home):
    seed_v3(runtime_home, owner=False)
    with pytest.raises(runtime_entry.RuntimeEntryError, match='UNKNOWN_BASELINE'):
        runtime_entry.acquire(runtime_home, wait_s=0)


def test_v3_acquire_holds_q_and_exposes_fence(runtime_home):
    seed_v3(runtime_home)
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert entry.managed and entry.fd is not None
        assert entry.allowed_generation == 7
        assert entry.lock_fingerprint is not None
        assert entry.intent is None
        st = os.fstat(entry.fd)
        assert entry.lock_fingerprint == (st.st_dev, st.st_ino)
    finally:
        entry.close()
    again = runtime_entry.acquire(runtime_home, wait_s=0)
    again.close()


def test_q_exclusive_blocks_shared_until_released(runtime_home):
    seed_v3(runtime_home)
    with Store(runtime_entry.control_root(runtime_home)) as store:
        with store.runtime_entry_lock():
            with pytest.raises(runtime_entry.RuntimeEntryError, match='RUNTIME_HOLD'):
                runtime_entry.acquire(runtime_home, wait_s=0)
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    entry.close()


def test_two_shared_entries_coexist(runtime_home):
    seed_v3(runtime_home)
    first = runtime_entry.acquire(runtime_home, wait_s=0)
    second = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert first.managed and second.managed
    finally:
        first.close()
        second.close()
