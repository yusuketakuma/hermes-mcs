"""Recover synthetic settings migrations without exposing their values."""
import json
import os
from pathlib import Path

import pytest
import config_edit
import install_options
import safe_io
import release_apply_owner
import settings_migration_receipt as receipt
import settings_migration_reconcile as recovery
import update_journal
import writer_lock


@pytest.fixture
def pending(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    config = tmp_path / 'config.json'
    config.write_text('{"unknown": {"keep": true}}')
    config.chmod(0o600)
    legacy = data / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    legacy.chmod(0o600)
    base = install_options.inspect(config, legacy)
    _, digest = install_options._legacy(legacy)
    item = receipt.Receipt(data, base, digest)
    item.begin()
    item.close()
    return tmp_path, config, legacy


@pytest.mark.parametrize('published', [False, True])
def test_explicit_recovery_preserves_config_and_legacy(pending, published):
    root, config, legacy = pending
    if published:
        config.write_text('{"unknown": {"keep": true}, "install_args": ["--no-llm"]}')
    original = config.read_bytes(), legacy.read_bytes()
    assert recovery.reconcile(root, 'synthetic review', confirm_human=True) == ('published' if published else 'not_published')
    assert (config.read_bytes(), legacy.read_bytes()) == original
    assert not (root / receipt.NAME).exists()
    audits = list(root.glob('.install-options-migration.reconcile.*'))
    assert len(audits) == 1
    assert json.loads(audits[0].read_bytes())['reason'] == 'synthetic review'


def test_status_is_read_only_and_does_not_print_values(pending, capsys):
    root, config, legacy = pending
    before = config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()
    assert recovery.main(['--root', str(root)]) == 0
    output = capsys.readouterr().out
    assert '--no-llm' not in output and '設定へのコピー前' in output
    assert (config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()) == before
    assert not list(root.glob('.install-options-migration.reconcile.*'))


@pytest.mark.parametrize('which', ['update.lock', 'run.lock'])
def test_active_writer_refuses_recovery_without_mutating(pending, which):
    root, config, legacy = pending
    held = writer_lock.acquire_run_lock(str(root / 'data' / which))
    try:
        before = config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()
        with pytest.raises(ValueError, match='busy'):
            recovery.reconcile(root, 'synthetic review', confirm_human=True)
        assert (config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()) == before
        assert not list(root.glob('.install-options-migration.reconcile.*'))
    finally:
        os.close(held)


def test_legacy_change_preserves_pending_without_creating_audit(pending):
    root, config, legacy = pending
    legacy.write_text('{"v": 1, "install_args": []}')
    before = config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()
    with pytest.raises(ValueError, match='legacy_changed'):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()) == before
    assert not list(root.glob('.install-options-migration.reconcile.*'))


@pytest.mark.parametrize('shape', ['invalid', 'old_version', 'oversize', 'symlink', 'hardlink'])
def test_unverified_pending_record_is_preserved(pending, shape):
    root, config, legacy = pending
    marker = root / receipt.NAME
    if shape == 'invalid':
        marker.write_bytes(b'{"v":2,"v":2}')
    elif shape == 'old_version':
        marker.write_bytes(receipt.BODY)
    elif shape == 'oversize':
        marker.write_bytes(b' ' * 2049)
    elif shape == 'symlink':
        marker.unlink()
        marker.symlink_to('synthetic-missing')
    else:
        os.link(marker, root / 'synthetic-hardlink')
    before = config.read_bytes(), legacy.read_bytes()
    assert recovery.main(['--root', str(root), '--confirm-human', '--reason', 'synthetic review']) == 1
    assert (config.read_bytes(), legacy.read_bytes()) == before
    assert os.path.lexists(marker)
    assert not list(root.glob('.install-options-migration.reconcile.*'))


def test_changed_data_directory_stays_held(pending):
    root, config, legacy = pending
    data = root / 'data'
    old = root / 'old-data'
    original = legacy.read_bytes()
    data.rename(old)
    data.mkdir(mode=0o700)
    (data / 'install_options.json').write_bytes(original)
    with pytest.raises(ValueError, match='migration_evidence_invalid'):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (root / receipt.NAME).exists() and config.read_bytes()
    assert not list(root.glob('.install-options-migration.reconcile.*'))


def test_sync_failure_after_republication_keeps_pending(pending, monkeypatch):
    root, config, legacy = pending
    config.write_text('{"unknown": {"keep": true}, "install_args": ["--no-llm"]}')
    before = config.read_bytes(), legacy.read_bytes()
    original = safe_io.fsync_dir
    count = []

    def fail_after_config_rename(path, **kwargs):
        if Path(path) == root:
            count.append(True)
            if len(count) == 2:
                raise OSError('synthetic config fsync failure')
        return original(path, **kwargs)

    monkeypatch.setattr(safe_io, 'fsync_dir', fail_after_config_rename)
    with pytest.raises(OSError, match='synthetic config fsync failure'):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (config.read_bytes(), legacy.read_bytes()) == before
    assert (root / receipt.NAME).exists()
    assert len(list(root.glob('.install-options-migration.reconcile.*'))) == 1


def test_invalid_reason_and_unverified_evidence_never_expose_values(pending, capsys):
    root, config, legacy = pending
    before = config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()
    assert recovery.main(['--root', str(root), '--confirm-human', '--reason', '']) == 1
    output = capsys.readouterr()
    assert '--no-llm' not in output.out + output.err
    assert (config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()) == before


def test_busy_config_editor_refuses_without_audit(pending):
    root, config, legacy = pending
    with config_edit._edit_lock(config):
        with pytest.raises(config_edit.ConfigConflict):
            recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (root / receipt.NAME).exists()
    assert not list(root.glob('.install-options-migration.reconcile.*'))


def test_programmatic_recovery_also_requires_human_confirmation(pending):
    root, _, _ = pending
    with pytest.raises(ValueError, match='human_confirmation_required'):
        recovery.reconcile(root, 'synthetic review')
    assert (root / receipt.NAME).exists()
    assert not list(root.glob('.install-options-migration.reconcile.*'))


@pytest.mark.parametrize('stage', ['lock', 'settings_migration'])
def test_premerge_journal_is_preserved_for_separate_recovery(pending, stage):
    root, _, _ = pending
    path = root / 'data/update_state.json'
    state = update_journal.default_state()
    state['stages'] = [{'stage': stage, 'at': 1}]
    update_journal.save_state(path, state)
    original = path.read_bytes()
    assert recovery.reconcile(root, 'synthetic review', confirm_human=True) == 'not_published'
    assert path.read_bytes() == original


def test_postmerge_journal_prevents_reconciliation(pending):
    root, config, legacy = pending
    path = root / 'data/update_state.json'
    state = update_journal.default_state()
    state['stages'] = [{'stage': 'post_merge', 'at': 1}]
    update_journal.save_state(path, state)
    original = config.read_bytes(), legacy.read_bytes(), path.read_bytes(), (root / receipt.NAME).read_bytes()
    with pytest.raises(ValueError, match='update_journal_unverified'):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (config.read_bytes(), legacy.read_bytes(), path.read_bytes(), (root / receipt.NAME).read_bytes()) == original
    assert not list(root.glob('.install-options-migration.reconcile.*'))


def test_restore_marker_prevents_reconciliation(pending):
    root, config, legacy = pending
    marker = root / 'data/restore_pending.json'
    marker.write_text('{"phase": "awaiting_consent"}')
    original = config.read_bytes(), legacy.read_bytes(), marker.read_bytes(), (root / receipt.NAME).read_bytes()
    with pytest.raises(ValueError, match='restore_hold'):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (config.read_bytes(), legacy.read_bytes(), marker.read_bytes(), (root / receipt.NAME).read_bytes()) == original
    assert not list(root.glob('.install-options-migration.reconcile.*'))


def test_release_owner_fence_is_not_bypassed(pending):
    root, config, legacy = pending
    with release_apply_owner._control_store(root) as store:
        store.write('release-owner-activation.json', {'synthetic': True})
    original = config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()
    with pytest.raises(ValueError, match='release_entry_hold'):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()) == original
    assert not list(root.glob('.install-options-migration.reconcile.*'))


def test_orphaned_release_change_is_not_bypassed(pending):
    root, config, legacy = pending
    with release_apply_owner._legacy_scope(root):
        control = release_apply_owner._control_root(root)
        (control / 'release-change.json').write_text('{"synthetic": true}')
    original = config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()
    with pytest.raises(ValueError, match='release_control_hold'):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()) == original
    assert not list(root.glob('.install-options-migration.reconcile.*'))


def test_audit_sync_failure_preserves_pending_and_config(pending, monkeypatch):
    root, config, legacy = pending
    original = config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()
    monkeypatch.setattr(safe_io, 'fsync_dir', lambda *_a, **_k: (_ for _ in ()).throw(OSError('synthetic audit fsync')))
    with pytest.raises(OSError, match='synthetic audit fsync'):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()) == original


def test_symlinked_update_lock_is_not_followed(pending):
    root, config, legacy = pending
    foreign = root / 'synthetic-foreign'
    foreign.write_bytes(b'preserve')
    (root / 'data/update.lock').symlink_to(foreign)
    original = config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()
    with pytest.raises(OSError):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert foreign.read_bytes() == b'preserve'
    assert (config.read_bytes(), legacy.read_bytes(), (root / receipt.NAME).read_bytes()) == original
    assert not list(root.glob('.install-options-migration.reconcile.*'))


def test_directory_sync_failure_after_unlink_keeps_audit_and_data(pending, monkeypatch):
    root, config, legacy = pending
    config.write_text('{"unknown": {"keep": true}, "install_args": ["--no-llm"]}')
    original = config.read_bytes(), legacy.read_bytes()
    root_info = root.stat()
    real_fsync = os.fsync

    def fail_after_removal(fd):
        info = os.fstat(fd)
        if ((info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino)
                and not (root / receipt.NAME).exists()):
            raise OSError('synthetic removal fsync failure')
        return real_fsync(fd)

    monkeypatch.setattr(recovery.os, 'fsync', fail_after_removal)
    with pytest.raises(OSError, match='synthetic removal fsync failure'):
        recovery.reconcile(root, 'synthetic review', confirm_human=True)
    assert (config.read_bytes(), legacy.read_bytes()) == original
    assert not (root / receipt.NAME).exists()
    assert len(list(root.glob('.install-options-migration.reconcile.*'))) == 1
