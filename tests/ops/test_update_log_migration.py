"""Synthetic update log deployment, prefix recovery, and service ordering."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import mcs_update
import run_log_install as deploy


SOURCE = Path(__file__).resolve().parents[2] / 'mcs/core/run_log.py'


def layout(tmp_path):
    root, checkout, recovery = (tmp_path / name for name in ('runtime', 'checkout', 'recovery'))
    (root / 'data').mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    source = checkout / 'mcs/core/run_log.py'
    source.parent.mkdir(parents=True)
    source.write_bytes(SOURCE.read_bytes())
    source.chmod(0o644)
    return root, checkout, recovery, source


def put(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)


def test_prepare_installs_exact_private_canonical_copy_and_keeps_original(tmp_path):
    root, checkout, recovery, source = layout(tmp_path)
    original = root / 'data/run.log'
    put(original, b'synthetic first\n')
    result = deploy.prepare(root, checkout, recovery)
    assert result['scope'] == 'verified_prefix' and result['legacy_writers_closed'] is False
    assert result['originals_retained'] is True
    pointer = recovery / deploy._pointer(str(root))
    receipt = json.loads(pointer.read_bytes())
    helper = recovery / receipt['helper']
    assert helper.read_bytes() == source.read_bytes()
    assert receipt['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert helper.stat().st_mode & 0o777 == pointer.stat().st_mode & 0o777 == 0o600
    assert recovery.stat().st_mode & 0o777 == 0o700
    assert source.stat().st_mode & 0o777 == 0o644
    assert original.read_bytes() == (root / 'data/mcs.log').read_bytes() == b'synthetic first\n'
    assert list((root / 'data/mcs-log-backups').iterdir())
    assert deploy.prepare(root, checkout, recovery)['imported'] == []
    assert (root / 'data/mcs.log').read_bytes() == b'synthetic first\n'


def test_open_legacy_fd_tail_is_imported_once_without_checkout(tmp_path):
    root, checkout, recovery, _ = layout(tmp_path)
    original = root / 'data/update.log'
    put(original, b'synthetic prefix\n')
    with original.open('ab', buffering=0) as old_writer:
        deploy.prepare(root, checkout, recovery)
        old_writer.write(b'synthetic late tail\n')
    shutil.rmtree(checkout)
    result = deploy.migrate_installed(root, recovery)
    assert result['scope'] == 'verified_prefix' and not result['legacy_writers_closed']
    assert result['imported'] == ['update.log']
    assert (root / 'data/mcs.log').read_bytes() == original.read_bytes()
    assert deploy.migrate_installed(root, recovery)['imported'] == []
    assert not json.loads((root / 'data/.mcs-log-state.json').read_text())['rotation_ready']


def test_custom_roots_share_recovery_directory_without_mixing_logs(tmp_path):
    root, checkout, recovery, _ = layout(tmp_path)
    other = tmp_path / 'other-runtime'
    (other / 'data').mkdir(parents=True, mode=0o700)
    other.chmod(0o700)
    for directory, body in ((root, b'synthetic A\n'), (other, b'synthetic B\n')):
        put(directory / 'data/run.log', body)
        deploy.prepare(directory, checkout, recovery)
        assert (directory / 'data/mcs.log').read_bytes() == body
    assert len(list(recovery.glob('run_log-*.json'))) == 2
    pointer = recovery / deploy._pointer(str(root))
    receipt = json.loads(pointer.read_text())
    receipt['root'] = str(other)
    put(pointer, json.dumps(receipt).encode())
    with pytest.raises(deploy.LogInstallError):
        deploy.migrate_installed(root, recovery)
    assert (other / 'data/mcs.log').read_bytes() == b'synthetic B\n'


@pytest.mark.parametrize('existing_directory', [False, True])
def test_plan_helper_path_is_read_only_before_install(tmp_path, existing_directory):
    root, checkout, recovery, source = layout(tmp_path)
    if existing_directory:
        recovery.mkdir(mode=0o700)
    before = sorted(str(path) for path in tmp_path.rglob('*'))
    expected = deploy.helper_path(root, recovery, checkout=checkout)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert expected == str(recovery / (deploy._pointer(str(root))[:-5] + '-' + digest + '.py'))
    assert sorted(str(path) for path in tmp_path.rglob('*')) == before
    with pytest.raises(deploy.LogInstallError):
        deploy.helper_path(root, recovery, checkout=checkout, require_installed=True)
    assert sorted(str(path) for path in tmp_path.rglob('*')) == before
    deploy.prepare(root, checkout, recovery)
    assert deploy.helper_path(root, recovery, require_installed=True) == expected


def test_helper_accessor_prefers_installed_generation_after_checkout_change(tmp_path):
    root, checkout, recovery, source = layout(tmp_path)
    deploy.prepare(root, checkout, recovery)
    installed = deploy.helper_path(root, recovery, require_installed=True)
    source.write_bytes(source.read_bytes() + b'\n# different synthetic generation\n')
    assert deploy.helper_path(root, recovery, checkout=checkout) == installed
    source.unlink()
    assert deploy.helper_path(root, recovery, checkout=checkout, require_installed=True) == installed


def test_helper_accessor_does_not_fall_back_from_damaged_installed_receipt(tmp_path):
    root, checkout, recovery, _ = layout(tmp_path)
    deploy.prepare(root, checkout, recovery)
    pointer = recovery / deploy._pointer(str(root))
    put(pointer, b'{"version":1}')
    with pytest.raises(deploy.LogInstallError):
        deploy.helper_path(root, recovery, checkout=checkout)
    assert pointer.read_bytes() == b'{"version":1}'


def test_helper_plan_paths_are_separate_for_custom_roots(tmp_path):
    root, checkout, recovery, _ = layout(tmp_path)
    other = tmp_path / 'other'
    other.mkdir(mode=0o700)
    assert deploy.helper_path(root, recovery, checkout=checkout) != deploy.helper_path(
        other, recovery, checkout=checkout)
    assert not recovery.exists()


def test_uninstalled_legacy_recovery_directory_is_untouched(tmp_path):
    recovery = tmp_path / 'legacy-recovery'
    recovery.mkdir(mode=0o755)
    recovery.chmod(0o755)
    assert deploy.migrate_installed(tmp_path / 'old-root', recovery)['scope'] == 'unavailable'
    assert list(recovery.iterdir()) == []
    assert recovery.stat().st_mode & 0o777 == 0o755


def test_ancestor_aliases_use_one_canonical_namespace_without_plan_writes(tmp_path, monkeypatch):
    root, checkout, recovery, _ = layout(tmp_path)
    alias = tmp_path / 'ancestor-alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    alias_root, alias_recovery = alias / root.name, alias / recovery.name
    before = sorted(str(path) for path in tmp_path.rglob('*'))
    expected = deploy.helper_path(root, recovery, checkout=checkout)
    assert deploy.helper_path(alias_root, alias_recovery, checkout=checkout) == expected
    assert sorted(str(path) for path in tmp_path.rglob('*')) == before
    put(root / 'data/run.log', b'synthetic alias prefix\n')
    deploy.prepare(alias_root, checkout, alias_recovery)
    assert deploy.helper_path(alias_root, alias_recovery, require_installed=True) == expected
    pointer = recovery / deploy._pointer(str(root))
    assert json.loads(pointer.read_text())['root'] == str(root)
    assert len(list(recovery.glob('run_log-*.json'))) == 1
    put(root / 'data/update.log', b'synthetic alias tail\n')
    deploy.migrate_installed(alias_root, alias_recovery)
    assert (root / 'data/mcs.log').read_bytes() == b'synthetic alias prefix\nsynthetic alias tail\n'
    # The default recovery folder may have the same macOS ancestor alias.
    canonical_home = tmp_path / 'canonical-home'
    canonical_home.mkdir(mode=0o700)
    alias_home = alias / canonical_home.name
    monkeypatch.setenv('HOME', str(alias_home))
    before = sorted(str(path) for path in tmp_path.rglob('*'))
    planned = deploy.helper_path(alias_root, checkout=checkout)
    assert Path(planned).parent == canonical_home / '.mcs-recovery'
    assert sorted(str(path) for path in tmp_path.rglob('*')) == before


@pytest.mark.parametrize('leaf', ['root', 'recovery'])
@pytest.mark.parametrize('operation', ['plan', 'prepare', 'tail'])
def test_private_leaf_symlinks_remain_rejected(tmp_path, leaf, operation):
    root, checkout, recovery, _ = layout(tmp_path)
    deploy.prepare(root, checkout, recovery)
    link = tmp_path / 'private-leaf-link'
    link.symlink_to(root if leaf == 'root' else recovery, target_is_directory=True)
    target_root = link if leaf == 'root' else root
    target_recovery = link if leaf == 'recovery' else recovery
    before = {path.name: path.read_bytes() for path in recovery.iterdir()}
    with pytest.raises(deploy.LogInstallError):
        if operation == 'plan':
            deploy.helper_path(target_root, target_recovery, checkout=checkout)
        elif operation == 'prepare':
            deploy.prepare(target_root, checkout, target_recovery)
        else:
            deploy.migrate_installed(target_root, target_recovery)
    assert link.is_symlink()
    assert {path.name: path.read_bytes() for path in recovery.iterdir()} == before
    assert (root / 'data/mcs.log').read_bytes() == b''


def test_checkout_ancestor_symlink_contract_is_unchanged(tmp_path):
    root, checkout, recovery, _ = layout(tmp_path)
    alias = tmp_path / 'checkout-alias'
    alias.symlink_to(checkout, target_is_directory=True)
    with pytest.raises(deploy.LogInstallError):
        deploy.helper_path(root, recovery, checkout=alias)
    assert not recovery.exists()


@pytest.mark.parametrize('defect', ['symlink', 'hardlink', 'writable'])
def test_untrusted_source_is_rejected_before_deployment(tmp_path, defect):
    root, checkout, recovery, source = layout(tmp_path)
    if defect == 'writable':
        source.chmod(0o664)
    else:
        moved = source.with_suffix('.saved')
        source.rename(moved)
        source.symlink_to(moved) if defect == 'symlink' else os.link(moved, source)
    with pytest.raises(deploy.LogInstallError):
        deploy.prepare(root, checkout, recovery)
    assert not recovery.exists() and not (root / 'data/mcs.log').exists()


@pytest.mark.parametrize('defect', ['symlink', 'hardlink', 'mode', 'hash', 'missing'])
def test_untrusted_installed_helper_never_executes(tmp_path, defect):
    root, checkout, recovery, _ = layout(tmp_path)
    deploy.prepare(root, checkout, recovery)
    receipt = json.loads((recovery / deploy._pointer(str(root))).read_text())
    helper = recovery / receipt['helper']
    before = (root / 'data/mcs.log').read_bytes()
    if defect in ('symlink', 'hardlink'):
        moved = helper.with_suffix('.saved')
        helper.rename(moved)
        helper.symlink_to(moved) if defect == 'symlink' else os.link(moved, helper)
    elif defect == 'mode':
        helper.chmod(0o644)
    elif defect == 'hash':
        helper.write_bytes(b'raise AssertionError("must not execute")\n')
    else:
        helper.unlink()
    with pytest.raises(deploy.LogInstallError):
        deploy.migrate_installed(root, recovery)
    assert (root / 'data/mcs.log').read_bytes() == before


def test_missing_old_checkout_source_keeps_existing_helper_and_logs(tmp_path):
    root, checkout, recovery, source = layout(tmp_path)
    deploy.prepare(root, checkout, recovery)
    files = {path.name: path.read_bytes() for path in recovery.iterdir()}
    source.unlink()
    assert deploy.prepare(root, checkout, recovery)['scope'] == 'unavailable'
    assert {path.name: path.read_bytes() for path in recovery.iterdir()} == files


def test_interruption_between_helper_and_pointer_publication_is_retryable(monkeypatch, tmp_path):
    root, checkout, recovery, _ = layout(tmp_path)
    put(root / 'data/run.log', b'synthetic retained source\n')
    publish = deploy._publish
    def interrupted(directory, name, raw):
        if name.endswith('.json'):
            raise OSError('synthetic publication interruption')
        publish(directory, name, raw)
    monkeypatch.setattr(deploy, '_publish', interrupted)
    with pytest.raises(deploy.LogInstallError):
        deploy.prepare(root, checkout, recovery)
    assert len(list(recovery.glob('run_log-*.py'))) == 1
    monkeypatch.setattr(deploy, '_publish', publish)
    deploy.prepare(root, checkout, recovery)
    assert (root / 'data/mcs.log').read_bytes() == b'synthetic retained source\n'


def test_cached_new_source_survives_checkout_rollback(monkeypatch, tmp_path):
    root, checkout, recovery, _ = layout(tmp_path)
    source = deploy.source_snapshot(checkout)
    monkeypatch.setattr(mcs_update, 'HOME', str(root))
    monkeypatch.setattr(update_apply, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_cli, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_execution, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_lifecycle_context, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_preflight, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_recovery, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_runtime_migration, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(mcs_update, 'REPO', str(checkout))
    monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(mcs_update, '_RUN_LOG_SOURCE', source)
    monkeypatch.setattr(update_lifecycle_context, '_RUN_LOG_SOURCE', mcs_update._RUN_LOG_SOURCE)
    monkeypatch.setattr(mcs_update, '_RUN_LOG_SOURCE_ERROR', False)
    monkeypatch.setattr(update_lifecycle_context, '_RUN_LOG_SOURCE_ERROR', mcs_update._RUN_LOG_SOURCE_ERROR)
    monkeypatch.setattr(mcs_update, '_service_paths', lambda: SimpleNamespace(recovery_dir=str(recovery)))
    monkeypatch.setattr(update_execution, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_lifecycle_context, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_planning, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_preflight, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_recovery, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_restore, '_service_paths', mcs_update._service_paths)
    mcs_update._prepare_run_log()
    pointer = (recovery / deploy._pointer(str(root))).read_bytes()
    put(root / 'data/run.log', b'synthetic rollback tail\n')
    shutil.rmtree(checkout)
    mcs_update._prepare_run_log()
    assert (recovery / deploy._pointer(str(root))).read_bytes() == pointer
    assert (root / 'data/mcs.log').read_bytes() == b'synthetic rollback tail\n'


def test_only_forward_promotion_changes_helper_generation_and_keeps_logs(tmp_path):
    root, checkout, recovery, source = layout(tmp_path)
    original_source = source.read_bytes()
    deploy.prepare(root, checkout, recovery)
    pointer = recovery / deploy._pointer(str(root))
    first = pointer.read_bytes()
    source.write_bytes(original_source + b'\n# synthetic next generation\n')
    deploy.prepare(root, checkout, recovery)
    assert pointer.read_bytes() == first
    deploy.prepare(root, checkout, recovery, promote=True)
    second = pointer.read_bytes()
    assert second != first
    put(root / 'data/run.log', b'synthetic append after forward upgrade\n')
    deploy.prepare(root, checkout, recovery, source=original_source)
    assert pointer.read_bytes() == second
    assert len(list(recovery.glob('run_log-*.py'))) == 2
    assert (root / 'data/mcs.log').read_bytes() == b'synthetic append after forward upgrade\n'


@pytest.mark.parametrize('fails', [False, True])
def test_service_publication_waits_for_verified_log_prefix(monkeypatch, fails):
    trace = []
    def prepare():
        trace.append('logs')
        if fails:
            raise mcs_update.UpdateError('run_log_prepare_failed')
    monkeypatch.setattr(mcs_update, '_prepare_run_log', prepare)
    monkeypatch.setattr(update_runtime_migration, '_prepare_run_log', mcs_update._prepare_run_log)
    monkeypatch.setattr(mcs_update.owned_process, 'run_owned',
                        lambda *a, **k: trace.append('services') or SimpleNamespace(returncode=0))
    if fails:
        with pytest.raises(mcs_update.UpdateError, match='run_log_prepare_failed'):
            mcs_update._services_reconcile()
    else:
        mcs_update._services_reconcile()
    assert trace == (['logs'] if fails else ['logs', 'services'])


@pytest.mark.parametrize('outcome', ['success', 'timeout_reaped', 'timeout_unreaped'])
def test_postmerge_tail_import_requires_owned_child_reap(monkeypatch, outcome):
    trace = []
    class Child:
        returncode = None
        def communicate(self, timeout=None):
            trace.append('communicate')
            if outcome != 'success':
                raise subprocess.TimeoutExpired('synthetic', timeout)
            self.returncode = 0
            return '', ''
    child = Child()
    def stop(owned):
        assert owned is child
        trace.append('stop')
        if outcome == 'timeout_reaped':
            child.returncode = -9
    monkeypatch.setattr(mcs_update.subprocess, 'Popen', lambda *a, **k: child)
    monkeypatch.setattr(mcs_update, '_stop_update_child', stop)
    monkeypatch.setattr(update_execution, '_stop_update_child', mcs_update._stop_update_child)
    monkeypatch.setattr(update_runtime_migration, '_stop_update_child', mcs_update._stop_update_child)
    monkeypatch.setattr(mcs_update, '_migrate_log_tail', lambda: trace.append('tail'))
    monkeypatch.setattr(update_execution, '_migrate_log_tail', mcs_update._migrate_log_tail)
    monkeypatch.setattr(update_recovery, '_migrate_log_tail', mcs_update._migrate_log_tail)
    monkeypatch.setattr(mcs_update.journal_store, 'load_state',
                        lambda *_: {'stages': [{'stage': 'done'}], 'applying': None})
    mcs_update._run_post_merge('a' * 40, run_lock_fd=None)
    assert trace == (['communicate', 'tail'] if outcome == 'success' else
                     ['communicate', 'stop', 'tail'] if outcome == 'timeout_reaped' else
                     ['communicate', 'stop'])


def test_recovery_reimports_tail_under_both_locks_before_empty_journal_return(monkeypatch):
    trace = []
    monkeypatch.setattr(mcs_update.journal_store, 'load_state', lambda *_: {})
    monkeypatch.setattr(mcs_update, 'acquire_update_lock',
                        lambda: trace.append('update_lock') or os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(update_apply, 'acquire_update_lock', mcs_update.acquire_update_lock)
    monkeypatch.setattr(update_cli, 'acquire_update_lock', mcs_update.acquire_update_lock)
    monkeypatch.setattr(update_recovery, 'acquire_update_lock', mcs_update.acquire_update_lock)
    monkeypatch.setattr(mcs_update, '_acquire_run_lock_wait',
                        lambda **_: trace.append('run_lock') or os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(update_apply, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    monkeypatch.setattr(update_recovery, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    monkeypatch.setattr(mcs_update, '_release_legacy_hold', lambda _: False)
    monkeypatch.setattr(update_apply, '_release_legacy_hold', mcs_update._release_legacy_hold)
    monkeypatch.setattr(update_cli, '_release_legacy_hold', mcs_update._release_legacy_hold)
    monkeypatch.setattr(update_recovery, '_release_legacy_hold', mcs_update._release_legacy_hold)
    monkeypatch.setattr(mcs_update, '_restore_writer_hold_marker', lambda: None)
    monkeypatch.setattr(update_lifecycle_context, '_restore_writer_hold_marker', mcs_update._restore_writer_hold_marker)
    monkeypatch.setattr(update_recovery, '_restore_writer_hold_marker', mcs_update._restore_writer_hold_marker)
    monkeypatch.setattr(mcs_update, '_migrate_log_tail', lambda: trace.append('tail'))
    monkeypatch.setattr(update_execution, '_migrate_log_tail', mcs_update._migrate_log_tail)
    monkeypatch.setattr(update_recovery, '_migrate_log_tail', mcs_update._migrate_log_tail)
    monkeypatch.setattr(mcs_update, '_remove_marker', lambda: trace.append('marker'))
    monkeypatch.setattr(update_apply, '_remove_marker', mcs_update._remove_marker)
    monkeypatch.setattr(update_recovery, '_remove_marker', mcs_update._remove_marker)
    monkeypatch.setattr(update_services, '_remove_marker', mcs_update._remove_marker)
    assert mcs_update.recover_interrupted() == 0
    assert trace == ['update_lock', 'run_lock', 'tail', 'marker']
