"""Synthetic durability and identity checks for migration holds."""
import os
import stat
import pytest

import settings_migration_receipt as receipt


def test_exclusive_private_receipt_is_removed_only_after_finish(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    assert not receipt.pending(data)
    item = receipt.Receipt(data)
    try:
        item.begin()
        path = tmp_path / receipt.NAME
        assert receipt.pending(data)
        assert path.read_bytes() == receipt.BODY
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        other = receipt.Receipt(data)
        try:
            with pytest.raises(FileExistsError):
                other.begin()
        finally:
            other.close()
        item.finish()
        assert not receipt.pending(data)
    finally:
        item.close()


def test_begin_directory_sync_failure_keeps_the_created_receipt(monkeypatch, tmp_path):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    item = receipt.Receipt(data)
    original = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('synthetic directory fsync failure')
        return original(fd)

    monkeypatch.setattr(receipt.os, 'fsync', fail_directory)
    try:
        with pytest.raises(OSError):
            item.begin()
        assert (tmp_path / receipt.NAME).read_bytes() == receipt.BODY
        assert receipt.pending(data)
    finally:
        item.close()


def test_changed_parent_is_detected_and_original_receipt_is_preserved(tmp_path):
    data, moved = tmp_path / 'data', tmp_path / 'old-data'
    data.mkdir(mode=0o700)
    item = receipt.Receipt(data)
    try:
        item.begin()
        data.rename(moved)
        data.mkdir(mode=0o700)
        with pytest.raises(ValueError, match='migration_data_changed'):
            item.finish()
        assert (tmp_path / receipt.NAME).read_bytes() == receipt.BODY
        assert receipt.pending(data)
    finally:
        item.close()


@pytest.mark.parametrize('kind', ['regular', 'directory', 'dangling_symlink'])
def test_any_existing_receipt_kind_blocks_without_reading_or_removing(tmp_path, kind):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    path = tmp_path / receipt.NAME
    if kind == 'regular':
        path.write_text('synthetic')
    elif kind == 'directory':
        path.mkdir()
    else:
        path.symlink_to('missing-synthetic')
    assert receipt.pending(data)
    assert os.path.lexists(path)


def test_group_writable_parent_is_unverified_even_without_receipt(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    data.chmod(0o770)
    assert receipt.pending(data)
    with pytest.raises(ValueError):
        receipt.Receipt(data)


def test_parent_permissions_change_keeps_existing_receipt(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    item = receipt.Receipt(data)
    try:
        item.begin()
        data.chmod(0o770)
        with pytest.raises(ValueError, match='migration_data_changed'):
            item.finish()
        assert (tmp_path / receipt.NAME).read_bytes() == receipt.BODY
    finally:
        item.close()


def test_installer_entry_refuses_a_pending_receipt(tmp_path, capsys):
    import install_options
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    (tmp_path / receipt.NAME).write_bytes(receipt.BODY)
    config = tmp_path / 'config.json'
    config.write_text('{}')
    legacy = data / 'install_options.json'
    before = config.read_bytes()
    assert install_options.main(['inspect', str(config), str(legacy), '-']) == 1
    assert config.read_bytes() == before
    assert '移行結果を確認できません' in capsys.readouterr().err
