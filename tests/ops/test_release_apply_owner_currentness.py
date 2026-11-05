"""Synthetic release apply owner currentness contracts; shared helpers preserve isolated runtime state."""
from pathlib import Path
import fcntl
import os
import sqlite3
import subprocess
import time
import pytest
import ledger
import maintenance
import release_apply_owner
from release_apply_owner_testkit import current_release as current_release, installed_current as installed_current, native_product as native_product, native_signing_keys as native_signing_keys, registered_effect as registered_effect
from test_release_dependency_lock import product as product, runtime as runtime
from test_sdc_release_state import native_child as native_child, store as store
from test_sdc_provision import world as world
from test_sdc_provision_profiles import registered as registered


def test_installed_sampler_reads_current_bytes_without_mutating_product_state(installed_current, tmp_path):
    import release_dependency_lock as dependencies
    repo, parameters = installed_current
    roots = (repo, parameters['venv'])
    def snapshot():
        return {str(path): (path.lstat().st_mtime_ns, path.read_bytes())
                for root in roots for path in root.rglob('*') if path.is_file() and not path.is_symlink()}
    before = snapshot()
    db_before = parameters['ledger_path'].read_bytes()
    observed = release_apply_owner._observe_installed(repo, **parameters)
    assert observed['schema'] == 9 and observed['hand_edited'] is False
    assert observed['commit'] == dependencies._git_read(repo, ['rev-parse', 'HEAD']).decode().strip()
    assert observed['lock_sha256'] == dependencies.digest(dependencies.read_lock(repo))
    assert observed['runtime_fingerprint'] == dependencies._read_receipt(parameters['venv'])['runtime_fingerprint']
    assert snapshot() == before and parameters['ledger_path'].read_bytes() == db_before
    assert not release_apply_owner._control_root(tmp_path).exists()
    assert not {'ready', 'authorized', 'updater', 'preconditions', 'grant'} & observed.keys()


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'index_flag', 'filter', 'symlink_db', 'receipt', 'installer', 'fsmonitor'])
def test_installed_sampler_preserves_unknown_and_unsafe_states(installed_current, change):
    import release_dependency_lock as dependencies
    repo, parameters = installed_current
    held = None
    sentinel = repo.parent / 'fsmonitor-executed'
    try:
        if change == 'tracked':
            (repo / 'candidate.py').write_text('synthetic manual edit\n')
        elif change == 'untracked':
            (repo / 'manual.py').write_text('synthetic manual edit\n')
        elif change == 'index_flag':
            subprocess.run(['/usr/bin/git', '-C', str(repo), 'update-index', '--assume-unchanged', 'candidate.py'], check=True)
        elif change == 'filter':
            subprocess.run(['/usr/bin/git', '-C', str(repo), 'config', 'filter.synthetic.clean', 'MUST_NOT_EXECUTE'], check=True)
        elif change == 'symlink_db':
            original = parameters['ledger_path'].with_suffix('.original')
            parameters['ledger_path'].rename(original)
            parameters['ledger_path'].symlink_to(original)
        elif change == 'receipt':
            (parameters['venv'] / dependencies.RECEIPT_NAME).write_text('{}')
        elif change == 'fsmonitor':
            hook = repo.parent / 'synthetic-fsmonitor'
            hook.write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\n')
            hook.chmod(0o700)
            subprocess.run(['/usr/bin/git', '-C', str(repo), 'config', 'core.fsmonitor', str(hook)], check=True)
        else:
            held = os.open(parameters['venv'], os.O_RDONLY | os.O_DIRECTORY)
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if change == 'fsmonitor':
            assert release_apply_owner._observe_installed(repo, **parameters)['hand_edited'] is False
            assert not sentinel.exists()
        elif change in ('tracked', 'untracked'):
            assert release_apply_owner._observe_installed(repo, **parameters)['hand_edited'] is True
        else:
            with pytest.raises(release_apply_owner.ReleaseApplyError):
                release_apply_owner._observe_installed(repo, **parameters)
    finally:
        if held is not None:
            os.close(held)


def test_installed_sampler_rejects_schema_change_during_runtime_observation(installed_current, monkeypatch):
    import release_dependency_lock as dependencies
    repo, parameters = installed_current
    original = dependencies.observe_locked
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        with sqlite3.connect(parameters['ledger_path']) as db:
            db.execute('PRAGMA user_version=10')
        return result
    monkeypatch.setattr(dependencies, 'observe_locked', observe)
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='OBSERVATION_CHANGED'):
        release_apply_owner._observe_installed(repo, **parameters)


def test_installed_sampler_rejects_commit_change_during_observation(installed_current, monkeypatch):
    import release_dependency_lock as dependencies
    repo, parameters = installed_current
    original = dependencies.observe_locked
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        subprocess.run(['/usr/bin/git', '-C', str(repo), '-c', 'user.name=Synthetic',
                        '-c', 'user.email=synthetic@example.invalid', 'commit', '--allow-empty', '-qm', 'changed'], check=True)
        return result
    monkeypatch.setattr(dependencies, 'observe_locked', observe)
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='OBSERVATION_CHANGED'):
        release_apply_owner._observe_installed(repo, **parameters)


def test_installed_sampler_expired_budget_starts_no_process(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_k: pytest.fail('process after deadline'))
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='OBSERVATION_TIMEOUT'):
        release_apply_owner._observe_installed(tmp_path, python=tmp_path, venv=tmp_path,
            runtime_mode='standalone', platform={}, ledger_path=tmp_path, deadline=time.monotonic() - 1)


def test_installed_sampler_reads_existing_wal_without_changing_database(installed_current):
    repo, parameters = installed_current
    path = parameters['ledger_path']
    writer = sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA user_version=10')
        writer.commit()
        wal = Path(str(path) + '-wal')
        before = path.read_bytes(), wal.read_bytes()
        assert release_apply_owner._observe_installed(repo, **parameters)['schema'] == 10
        assert (path.read_bytes(), wal.read_bytes()) == before
    finally:
        writer.close()


def test_installed_sampler_does_not_create_a_missing_wal_index(installed_current):
    repo, parameters = installed_current
    path = parameters['ledger_path']
    Path(str(path) + '-wal').write_bytes(b'synthetic incomplete WAL family')
    shm = Path(str(path) + '-shm')
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='SCHEMA_UNOBSERVED'):
        release_apply_owner._observe_installed(repo, **parameters)
    assert not shm.exists()


def test_currentness_reads_actual_profiles_credentials_and_owner_under_authority_locks(current_release):
    control, monitor, home, now, config = current_release
    with control.release_authority_lock(), monitor.lock():
        value = release_apply_owner._load_release_currentness(control, monitor, runtime_home=home, now_ms=now, deadline=time.monotonic() + 10)
    assert value.bootstrap == config and value.secret == 'a' * 64
    assert value.scope['claim_id'] == monitor.read('state.json')['claim_id']
    assert value.binding['owner_evidence_sha256'] == 'f' * 64
    assert value.secret not in repr(value)
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='CURRENTNESS_HOLD'):
        release_apply_owner._load_release_currentness(control, monitor, runtime_home=home, now_ms=now, deadline=time.monotonic() + 10)


@pytest.mark.parametrize('change', ['root', 'support', 'identity', 'rotation', 'disabled', 'anchor', 'pins', 'deadline'])
def test_currentness_rejects_changed_actual_identity_authority_or_expired_budget(current_release, change):
    control, monitor, home, now, config = current_release
    deadline = time.monotonic() + 10
    if change == 'root':
        config['control_root_fingerprint'] = '0' * 64
    elif change == 'support':
        config['support_snapshot_sha256'] = '0' * 64
    elif change == 'anchor':
        config['initial_trust_sha256'] = '0' * 64
    elif change == 'pins':
        config['registration_pins'] = config['operation_pins']
    elif change == 'deadline':
        deadline = time.monotonic() + 1
    with control.release_authority_lock(), monitor.lock():
        control.write('release-bootstrap.json', config)
        state = monitor.read('state.json')
        if change == 'identity':
            state['key_id'] = '00000009-0000-4000-8000-000000000001'
        elif change == 'rotation':
            state['rotation_id'] = '00000009-0000-4000-8000-000000000001'
        elif change == 'disabled':
            state['enabled'] = False
        if change in ('identity', 'rotation', 'disabled'):
            monitor.write('state.json', state)
        with pytest.raises(release_apply_owner.ReleaseApplyError, match='CURRENTNESS_HOLD'):
            release_apply_owner._load_release_currentness(control, monitor, runtime_home=home, now_ms=now, deadline=deadline)


@pytest.mark.parametrize('failure', [None, 'missing', 'malformed', 'symlink', 'hardlink', 'mode', 'sidecar', 'changed', 'expired'])
def test_existing_backup_observation_only_reads_a_verified_unchanged_static_file(tmp_path, monkeypatch, failure):
    import hashlib
    directory = tmp_path / 'backups'
    directory.mkdir(mode=0o700)
    backup = directory / 'ledger-20261026.db'
    ledger.Ledger(str(backup)).close()
    backup.chmod(0o600)
    raw = backup.read_bytes()
    if failure == 'missing':
        backup.unlink()
    elif failure == 'malformed':
        backup.write_bytes(b'synthetic malformed backup')
    elif failure == 'symlink':
        other = tmp_path / 'original.db'
        backup.rename(other)
        backup.symlink_to(other)
    elif failure == 'hardlink':
        os.link(backup, tmp_path / 'other.db')
    elif failure == 'mode':
        backup.chmod(0o644)
    elif failure == 'sidecar':
        Path(str(backup) + '-wal').write_bytes(b'synthetic WAL')
    elif failure == 'changed':
        valid = maintenance._valid
        def change_after_validation(path, deadline):
            result = valid(path, deadline)
            backup.write_bytes(backup.read_bytes() + b'synthetic changed bytes')
            return result
        monkeypatch.setattr(maintenance, '_valid', change_after_validation)
    deadline = time.monotonic() + (-1 if failure == 'expired' else 10)
    if failure is None:
        before = backup.stat()
        observed = release_apply_owner._observe_existing_backup(directory, deadline=deadline)
        assert observed == {'backup_verified': True, 'backup_sha256': hashlib.sha256(raw).hexdigest()}
        assert backup.read_bytes() == raw and backup.stat().st_mtime_ns == before.st_mtime_ns
        assert list(directory.iterdir()) == [backup]
        assert str(directory) not in str(observed)
    else:
        with pytest.raises(release_apply_owner.ReleaseApplyError, match='BACKUP_UNVERIFIED'):
            release_apply_owner._observe_existing_backup(directory, deadline=deadline)
        assert not Path(str(backup) + '.ok').exists()
