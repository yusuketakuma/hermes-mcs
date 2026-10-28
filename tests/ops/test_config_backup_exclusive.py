"""Synthetic exclusive backup, parent binding and publication failure cases."""
import ctypes
import errno
import os
from pathlib import Path

import pytest

import config_backup
import mcs_util


def _invalid(tmp_path, body=b'{synthetic-invalid'):
    path = tmp_path / 'config.json'
    path.write_bytes(body)
    path.chmod(0o600)
    return path, config_backup.snapshot(path)


def test_archive_and_create_preserves_original_private_file(tmp_path):
    path, initial = _invalid(tmp_path)
    backup = Path(config_backup.archive_and_create(path, initial, b'{}\n'))
    assert backup.read_bytes() == b'{synthetic-invalid'
    assert backup.stat().st_mode & 0o777 == 0o600
    assert path.read_bytes() == b'{}\n' and path.stat().st_mode & 0o777 == 0o600


def test_archive_moves_symlink_not_its_target(tmp_path):
    target = tmp_path / 'synthetic-target'
    target.write_text('untouched')
    path = tmp_path / 'config.json'
    path.symlink_to(target.name)
    backup = Path(config_backup.archive_and_create(path, config_backup.snapshot(path), b'{}'))
    assert backup.is_symlink() and os.readlink(backup) == target.name
    assert target.read_text() == 'untouched' and path.read_bytes() == b'{}'


@pytest.mark.parametrize('kind', ['file', 'symlink'])
def test_archive_never_overwrites_an_existing_backup(tmp_path, monkeypatch, kind):
    path, initial = _invalid(tmp_path)
    monkeypatch.setattr(config_backup.time, 'strftime', lambda *_: 'synthetic')
    existing = tmp_path / 'config.json.corrupt-synthetic'
    if kind == 'file':
        existing.write_bytes(b'keep')
    else:
        target = tmp_path / 'synthetic-target'
        target.write_bytes(b'keep')
        existing.symlink_to(target.name)
    backup = Path(config_backup.archive_and_create(path, initial, b'{}'))
    assert existing.read_bytes() == b'keep'
    assert existing.is_symlink() is (kind == 'symlink')
    assert backup.name.endswith('.1') and backup.read_bytes() == b'{synthetic-invalid'


def test_replacement_immediately_before_rename_is_retained(tmp_path, monkeypatch):
    path, initial = _invalid(tmp_path)
    rename = config_backup._rename_exclusive
    def concurrent(fd, src, dst):
        path.rename(tmp_path / 'previous')
        path.write_bytes(b'newer-config')
        path.chmod(0o600)
        rename(fd, src, dst)
    monkeypatch.setattr(config_backup, '_rename_exclusive', concurrent)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert (tmp_path / error.value.backup_name).read_bytes() == b'newer-config'
    assert error.value.parent_unchanged is True
    assert (tmp_path / 'previous').read_bytes() == b'{synthetic-invalid'
    assert not path.exists()


def test_concurrent_creator_after_rename_is_never_overwritten(tmp_path, monkeypatch):
    path, initial = _invalid(tmp_path)
    rename = config_backup._rename_exclusive
    def concurrent(fd, src, dst):
        rename(fd, src, dst)
        path.write_bytes(b'newer-config')
    monkeypatch.setattr(config_backup, '_rename_exclusive', concurrent)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert path.read_bytes() == b'newer-config'
    assert error.value.parent_unchanged is True
    assert (tmp_path / error.value.backup_name).read_bytes() == b'{synthetic-invalid'


def test_swapped_parent_is_not_followed_after_rename(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    path, initial = _invalid(root)
    rename = config_backup._rename_exclusive
    def swap(fd, src, dst):
        root.rename(tmp_path / 'original-root')
        root.mkdir()
        (root / 'config.json').write_bytes(b'newer-config')
        rename(fd, src, dst)
    monkeypatch.setattr(config_backup, '_rename_exclusive', swap)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert path.read_bytes() == b'newer-config'
    assert error.value.parent_unchanged is False
    assert (tmp_path / 'original-root' / error.value.backup_name).read_bytes() == b'{synthetic-invalid'


def test_changed_bytes_with_restored_mtime_are_detected_after_rename(tmp_path, monkeypatch):
    path, initial = _invalid(tmp_path, b'old-invalid')
    rename = config_backup._rename_exclusive
    def corrupt(fd, src, dst):
        rename(fd, src, dst)
        backup = tmp_path / dst
        backup.write_bytes(b'new-invalid')
        os.utime(backup, ns=(initial.identity[4], initial.identity[4]))
    monkeypatch.setattr(config_backup, '_rename_exclusive', corrupt)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert (tmp_path / error.value.backup_name).read_bytes() == b'new-invalid'
    assert not path.exists()


@pytest.mark.parametrize('kind', ['large', 'unreadable', 'fifo'])
def test_unverifiable_original_is_never_moved(tmp_path, monkeypatch, kind):
    path = tmp_path / 'config.json'
    if kind == 'fifo':
        os.mkfifo(path)
    else:
        path.write_bytes(b'0123456789')
    if kind == 'large':
        monkeypatch.setattr(mcs_util, 'CONFIG_MAX_BYTES', 9)
    if kind == 'unreadable':
        original_open = os.open
        def denied(name, flags, *args, **kwargs):
            if name == 'config.json' and kwargs.get('dir_fd') is not None:
                raise PermissionError(errno.EACCES, 'synthetic')
            return original_open(name, flags, *args, **kwargs)
        monkeypatch.setattr(config_backup.os, 'open', denied)
    with pytest.raises((OSError, ValueError)):
        config_backup.snapshot(path)
    assert os.path.lexists(path) and not list(tmp_path.glob('config.json.corrupt-*'))


def test_unsupported_platform_never_modifies_file(tmp_path, monkeypatch):
    path, initial = _invalid(tmp_path)
    monkeypatch.setattr(config_backup.sys, 'platform', 'unsupported')
    with pytest.raises(OSError) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert error.value.errno == errno.ENOTSUP
    assert path.read_bytes() == b'{synthetic-invalid' and not list(tmp_path.glob('config.json.corrupt-*'))


def test_linux_missing_renameat2_never_modifies_file(tmp_path, monkeypatch):
    path, initial = _invalid(tmp_path)
    monkeypatch.setattr(config_backup.sys, 'platform', 'linux')
    monkeypatch.setattr(config_backup.ctypes, 'CDLL', lambda *_a, **_k: object())
    with pytest.raises(OSError) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert error.value.errno == errno.ENOTSUP
    assert path.read_bytes() == b'{synthetic-invalid' and not list(tmp_path.glob('config.json.corrupt-*'))


@pytest.mark.parametrize('platform,symbol,flag', [('darwin', 'renameatx_np', 4), ('linux', 'renameat2', 1)])
def test_exclusive_rename_dispatches_exact_native_contract(monkeypatch, platform, symbol, flag):
    calls = []
    class Function:
        def __call__(self, *args):
            calls.append(args)
            ctypes.set_errno(errno.EEXIST)
            return -1
    function = Function()
    class Library:
        pass
    library = Library()
    setattr(library, symbol, function)
    monkeypatch.setattr(config_backup.sys, 'platform', platform)
    monkeypatch.setattr(config_backup.ctypes, 'CDLL', lambda *_a, **_k: library)
    with pytest.raises(FileExistsError):
        config_backup._rename_exclusive(123, 'config.json', 'backup')
    assert calls == [(123, b'config.json', 123, b'backup', flag)]


@pytest.mark.parametrize('directory_sync', [1, 2])
def test_directory_sync_failure_leaves_recoverable_entries(tmp_path, monkeypatch, directory_sync):
    path, initial = _invalid(tmp_path)
    sync = os.fsync
    directories = []
    def failed(fd):
        import stat
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories.append(True)
            if len(directories) == directory_sync:
                raise OSError('synthetic directory sync failure')
        return sync(fd)
    monkeypatch.setattr(config_backup.os, 'fsync', failed)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert (tmp_path / error.value.backup_name).read_bytes() == b'{synthetic-invalid'
    if directory_sync == 1:
        assert not path.exists()
    else:
        assert path.read_bytes() == b'{}'


def _init_world(monkeypatch, tmp_path):
    import mcs_setup
    from test_mcs_setup import _init_env
    _init_env(monkeypatch, tmp_path, {})
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: None)
    path = tmp_path / 'c.json'
    path.write_text('{broken')
    return mcs_setup, path


def test_init_uses_exclusive_backup_without_overwriting_new_creator(tmp_path, monkeypatch, capsys):
    setup, path = _init_world(monkeypatch, tmp_path)
    rename = config_backup._rename_exclusive
    def concurrent(fd, src, dst):
        rename(fd, src, dst)
        path.write_text('{"winner":true}')
    monkeypatch.setattr(config_backup, '_rename_exclusive', concurrent)
    assert setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    assert path.read_text() == '{"winner":true}'
    (backup,) = tmp_path.glob('c.json.corrupt-*')
    assert backup.read_text() == '{broken'
    out = capsys.readouterr().out
    assert '保存結果を確認できません' in out
    assert f'移動先の候補 {backup}' in out


def test_init_unsupported_exclusive_rename_keeps_original(tmp_path, monkeypatch):
    setup, path = _init_world(monkeypatch, tmp_path)
    def unavailable(*args):
        raise OSError(errno.ENOTSUP, 'synthetic')
    monkeypatch.setattr(config_backup, '_rename_exclusive', unavailable)
    assert setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    assert path.read_text() == '{broken' and not list(tmp_path.glob('c.json.corrupt-*'))


def test_init_rejects_oversize_before_secret_or_backup(tmp_path, monkeypatch):
    setup, path = _init_world(monkeypatch, tmp_path)
    original = b'{' + b'x' * mcs_util.CONFIG_MAX_BYTES
    path.write_bytes(original)
    monkeypatch.setattr(setup, '_init_store_secrets', lambda *_: pytest.fail('secret mutation'))
    assert setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    assert path.read_bytes() == original and not list(tmp_path.glob('c.json.corrupt-*'))


def test_init_parent_swap_after_rename_never_writes_new_parent(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'home'
    root.mkdir()
    setup, path = _init_world(monkeypatch, root)
    rename = config_backup._rename_exclusive
    def swap(fd, src, dst):
        root.rename(tmp_path / 'old-home')
        root.mkdir()
        (root / 'c.json').write_text('{"winner":true}')
        rename(fd, src, dst)
    monkeypatch.setattr(config_backup, '_rename_exclusive', swap)
    assert setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    assert path.read_text() == '{"winner":true}'
    (backup,) = (tmp_path / 'old-home').glob('c.json.corrupt-*')
    assert backup.read_text() == '{broken'
    out = capsys.readouterr().out
    assert '保存場所が変わった可能性' in out and '移動したフォルダー' in out
    assert '移動先の候補' not in out
