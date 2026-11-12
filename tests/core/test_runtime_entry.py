"""runtime_entry.acquire のv3判定・Q SH保持・fence読取りを合成control rootで検証する。"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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


def _touch(root, name, raw=b''):
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)


def test_legacy_root_with_v1_locks_is_unmanaged(runtime_home):
    """v1 release locks alone prove no v3 migration ran — legacy no-op."""
    root = managed_root(runtime_home)
    _touch(root, 'release-entry.lock')
    _touch(root, 'release-owner.lock')
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert not entry.managed and entry.fd is None
    finally:
        entry.close()


def test_legacy_root_ignores_shadowing_env(runtime_home, monkeypatch):
    root = managed_root(runtime_home)
    _touch(root, 'release-entry.lock')
    monkeypatch.setenv('PYTHONPATH', '/tmp/shadow')
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert not entry.managed
    finally:
        entry.close()


def test_v3_marker_without_q_holds(runtime_home):
    """v3 sidecar beside v1 locks is still a hold — markers beat legacy proof."""
    root = managed_root(runtime_home)
    _touch(root, 'release-entry.lock')
    _touch(root, 'release-migration-v3.json', b'{}')
    with pytest.raises(runtime_entry.RuntimeEntryError, match='RUNTIME_HOLD'):
        runtime_entry.acquire(runtime_home, wait_s=0)


def test_v3_owner_schema_without_q_holds(runtime_home):
    root = managed_root(runtime_home)
    _touch(root, 'release-entry.lock')
    _touch(root, 'release-owner.json',
           b'{"schema_version":"mcs.release-owner.v3"}')
    with pytest.raises(runtime_entry.RuntimeEntryError, match='RUNTIME_HOLD'):
        runtime_entry.acquire(runtime_home, wait_s=0)


def test_real_legacy_gate_root_is_unmanaged(runtime_home):
    """P0再現: v1機構の実経路 release_apply_checks._open_release_gate が
    作るcontrol root(release-entry.lockのみ)はv3未移行としてunmanaged。"""
    import release_apply_checks
    fd = release_apply_checks._open_release_gate(str(runtime_home))
    try:
        # ゲート生存中(LOCK_SH保持)でもlegacy rootはunmanaged
        entry = runtime_entry.acquire(runtime_home, wait_s=0)
        try:
            assert not entry.managed and entry.fd is None
        finally:
            entry.close()
    finally:
        os.close(fd)
    # ゲート終了後もlock fileは残存し同じ判定になる
    assert (runtime_entry.control_root(runtime_home)
            / 'release-entry.lock').exists()
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert not entry.managed and entry.fd is None
    finally:
        entry.close()


def test_real_legacy_gate_root_with_v3_marker_holds(runtime_home):
    """実legacy rootへv3 markerが混在すれば従来どおりfail-closedでhold。"""
    import release_apply_checks
    fd = release_apply_checks._open_release_gate(str(runtime_home))
    os.close(fd)
    _touch(runtime_entry.control_root(runtime_home),
           'release-migration-v3.json', b'{}')
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


@pytest.mark.parametrize('name', ['PYTHONPATH', 'PYTHONHOME'])
def test_managed_acquire_rejects_shadowing_env(runtime_home, monkeypatch, name):
    seed_v3(runtime_home)
    monkeypatch.setenv(name, '/tmp/shadow')
    with pytest.raises(runtime_entry.RuntimeEntryError, match='UNSAFE_PYTHON_ENV'):
        runtime_entry.acquire(runtime_home, wait_s=0)


def test_managed_acquire_allows_empty_and_missing_env(runtime_home, monkeypatch):
    seed_v3(runtime_home)
    monkeypatch.setenv('PYTHONPATH', '')
    monkeypatch.delenv('PYTHONHOME', raising=False)
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    entry.close()


def test_ignore_environment_skips_shadowing_check(runtime_home, monkeypatch):
    """-E/-I makes PYTHON* inert for the interpreter, so the env check is skipped."""
    seed_v3(runtime_home)
    monkeypatch.setenv('PYTHONPATH', '/tmp/shadow')
    monkeypatch.setattr(sys, 'flags', SimpleNamespace(ignore_environment=1))
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    entry.close()


def test_legacy_entry_ignores_shadowing_env(runtime_home, monkeypatch):
    """Unmanaged (pre-v3) devices keep the legacy path regardless of env."""
    monkeypatch.setenv('PYTHONPATH', '/tmp/shadow')
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert not entry.managed
    finally:
        entry.close()


def test_adopt_rejects_shadowing_env(runtime_home):
    seed_v3(runtime_home)
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        fd, env = _dup_env(entry)
        env['PYTHONPATH'] = '/tmp/shadow'
        with pytest.raises(runtime_entry.RuntimeEntryError, match='UNSAFE_PYTHON_ENV'):
            runtime_entry.adopt(runtime_home, env)
        os.close(fd)
    finally:
        entry.close()


@pytest.fixture(autouse=True)
def _reset_current():
    yield
    runtime_entry._CURRENT = None


def test_current_tracks_last_acquire(runtime_home):
    assert runtime_entry.current() is None
    seed_v3(runtime_home)
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert runtime_entry.current() is entry
        env = entry.child_env()
        fd = entry.pass_fds()
        assert fd == (entry.fd,)
        spec = env[runtime_entry.CHILD_FDS_ENV].split(':')
        assert int(spec[0]) == entry.fd
        assert (int(spec[1]), int(spec[2])) == entry.lock_fingerprint
    finally:
        entry.close()
    assert entry.child_env() == {} and entry.pass_fds() == ()


def test_unmanaged_entry_spawn_kwargs_are_empty(runtime_home):
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert runtime_entry.current() is entry
        assert entry.child_env() == {} and entry.pass_fds() == ()
    finally:
        entry.close()


def _dup_env(entry):
    """Simulate a child process: dup the Q fd and describe it for adopt()."""
    fd = os.dup(entry.fd)
    dev, ino = entry.lock_fingerprint
    return fd, {runtime_entry.CHILD_FDS_ENV: f'{fd}:{dev}:{ino}'}


def test_adopt_rejects_foreign_fd(runtime_home):
    seed_v3(runtime_home)
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        fd, env = _dup_env(entry)
        adopted = runtime_entry.adopt(runtime_home, env=env)
        try:
            assert adopted.adopted and adopted.managed
            assert adopted.fd == fd
            assert adopted.lock_fingerprint == entry.lock_fingerprint
            assert adopted.allowed_generation == 7
        finally:
            adopted.close()
        dev, ino = entry.lock_fingerprint
        bogus = {runtime_entry.CHILD_FDS_ENV: f'{fd + 1000}:{dev}:{ino}'}
        with pytest.raises(runtime_entry.RuntimeEntryError, match='RUNTIME_HOLD'):
            runtime_entry.adopt(runtime_home, env=bogus)
        wrong_inode = {runtime_entry.CHILD_FDS_ENV: f'{os.dup(entry.fd)}:{dev}:{ino + 1}'}
        with pytest.raises(runtime_entry.RuntimeEntryError, match='RUNTIME_HOLD'):
            runtime_entry.adopt(runtime_home, env=wrong_inode)
    finally:
        entry.close()


def test_adopt_without_marker_is_legacy(runtime_home):
    seed_v3(runtime_home)
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        adopted = runtime_entry.adopt(runtime_home, env={})
        try:
            assert not adopted.managed and not adopted.adopted
        finally:
            adopted.close()
    finally:
        entry.close()


def test_adopted_close_releases_only_own_reference(runtime_home):
    seed_v3(runtime_home)
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        _fd, env = _dup_env(entry)
        adopted = runtime_entry.adopt(runtime_home, env=env)
        adopted.close()
        # Parent still holds SH: a fresh exclusive attempt must still fail.
        with Store(runtime_entry.control_root(runtime_home)) as store:
            with pytest.raises(BlockingIOError):
                with store.runtime_entry_lock(shared=False):
                    pass
    finally:
        entry.close()


def test_wired_entries_acquire_before_app_imports():
    """各業務入口がapp importより前にruntime_entry.acquireを通ることを静的に固定する。"""
    root = Path(__file__).resolve().parents[2]
    cases = {
        'mcs_standalone/__main__.py': [
            'import mcs_standalone', '_runtime_entry.acquire()',
            'from adapters.common.paths import'],
        'lineworks_adapter/__main__.py': [
            'import _mcs_path', '_runtime_entry.acquire()',
            'from adapters.lineworks.__main__ import main'],
        'adapters/lineworks/__main__.py': [
            '_runtime_entry.acquire()', 'from adapters.common import'],
        'hermes_plugin/__init__.py': [
            'runtime_entry.acquire()', 'ctx.register_command'],
    }
    for rel, markers in cases.items():
        text = (root / rel).read_text()
        positions = [text.index(marker) for marker in markers]
        assert positions == sorted(positions), f'{rel}: acquire must precede app imports'
