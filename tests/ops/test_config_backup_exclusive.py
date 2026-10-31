"""Synthetic private backup, source races, and interruption recovery."""
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

import _mcs_path
import config_backup
import mcs_util


def _invalid(tmp_path, body=b'{synthetic-invalid'):
    path = tmp_path / 'config.json'
    path.write_bytes(body)
    path.chmod(0o600)
    return path, config_backup.snapshot(path)


def test_archive_and_create_preserves_independent_private_copy(tmp_path):
    path, initial = _invalid(tmp_path)
    backup = Path(config_backup.archive_and_create(path, initial, b'{}\n'))
    assert backup.read_bytes() == b'{synthetic-invalid'
    assert backup.stat().st_mode & 0o777 == 0o600
    assert (backup.stat().st_dev, backup.stat().st_ino) != initial.identity[:2]
    assert path.read_bytes() == b'{}\n' and path.stat().st_mode & 0o777 == 0o600


def test_abandoned_config_staging_name_is_git_ignored():
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(['git', 'check-ignore', '-q', '--', 'config.json.stage-synthetic.tmp'],
                            cwd=repo, capture_output=True, timeout=10)
    assert result.returncode == 0


def test_archive_copies_symlink_not_its_target(tmp_path):
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
    assert existing.read_bytes() == b'keep' and existing.is_symlink() is (kind == 'symlink')
    assert backup.name.endswith('.1') and backup.read_bytes() == b'{synthetic-invalid'


@pytest.mark.parametrize('change', ['newer', 'same_bytes_replacement', 'mtime_restored'])
def test_concurrent_source_change_before_replace_is_retained(tmp_path, monkeypatch, change):
    path, initial = _invalid(tmp_path, b'old-invalid')
    publish = config_backup._publish_backup
    def concurrent(fd, name, raw, symlink):
        publish(fd, name, raw, symlink)
        if change == 'same_bytes_replacement':
            other = tmp_path / 'other'
            other.write_bytes(path.read_bytes())
            other.chmod(0o600)
            other.replace(path)
        else:
            path.write_bytes(b'new-invalid')
            if change == 'mtime_restored':
                os.utime(path, ns=(initial.identity[4], initial.identity[4]))
    monkeypatch.setattr(config_backup, '_publish_backup', concurrent)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert path.read_bytes() == (b'old-invalid' if change == 'same_bytes_replacement' else b'new-invalid')
    assert (tmp_path / error.value.backup_name).read_bytes() == b'old-invalid'


def test_source_change_during_copy_is_not_published(tmp_path, monkeypatch):
    path, initial = _invalid(tmp_path, b'old-invalid')
    entry = config_backup._entry
    calls = []
    def concurrent(fd, name):
        result = entry(fd, name)
        if name == path.name:
            calls.append(True)
            if len(calls) == 1:
                path.write_bytes(b'new-invalid')
        return result
    monkeypatch.setattr(config_backup, '_entry', concurrent)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert path.read_bytes() == b'new-invalid'
    assert (tmp_path / error.value.backup_name).read_bytes() == b'old-invalid'


def test_swapped_parent_is_not_followed(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    path, initial = _invalid(root)
    publish = config_backup._publish_backup
    def swap(fd, name, raw, symlink):
        publish(fd, name, raw, symlink)
        root.rename(tmp_path / 'original-root')
        root.mkdir()
        (root / 'config.json').write_bytes(b'newer-config')
    monkeypatch.setattr(config_backup, '_publish_backup', swap)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert error.value.parent_unchanged is False and path.read_bytes() == b'newer-config'
    assert (tmp_path / 'original-root' / 'config.json').read_bytes() == b'{synthetic-invalid'
    assert (tmp_path / 'original-root' / error.value.backup_name).read_bytes() == b'{synthetic-invalid'


def test_changed_backup_prevents_config_publication(tmp_path, monkeypatch):
    path, initial = _invalid(tmp_path)
    stage = config_backup._staged
    def concurrent(fd, raw, **kwargs):
        result = stage(fd, raw, **kwargs)
        if raw == b'{}':
            next(tmp_path.glob('config.json.corrupt-*')).write_bytes(b'newer-backup')
        return result
    monkeypatch.setattr(config_backup, '_staged', concurrent)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert path.read_bytes() == b'{synthetic-invalid'
    assert (tmp_path / error.value.backup_name).read_bytes() == b'newer-backup'


@pytest.mark.parametrize('kind', ['large', 'unreadable', 'fifo'])
def test_unverifiable_original_is_never_copied(tmp_path, monkeypatch, kind):
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


@pytest.mark.parametrize('mode', [0o600, 0o644])
def test_hardlinked_original_is_not_copied_or_chmodded(tmp_path, mode):
    path = tmp_path / 'config.json'
    path.write_bytes(b'{invalid')
    path.chmod(mode)
    other = tmp_path / 'other-link'
    os.link(path, other)
    with pytest.raises(ValueError, match='config_backup_hardlink'):
        config_backup.snapshot(path)
    assert path.read_bytes() == other.read_bytes() == b'{invalid'
    assert other.stat().st_mode & 0o777 == mode
    assert not list(tmp_path.glob('config.json.corrupt-*'))


@pytest.mark.parametrize('kind,mode', [('regular', 0o770), ('symlink', 0o755)])
def test_unsafe_parent_permissions_reject_backup(tmp_path, kind, mode):
    root = tmp_path / 'home'
    root.mkdir(mode=0o700)
    path = root / 'config.json'
    if kind == 'regular':
        path.write_bytes(b'{invalid')
    else:
        target = tmp_path / 'target'
        target.write_bytes(b'untouched')
        path.symlink_to(target)
    root.chmod(mode)
    with pytest.raises(ValueError, match='config_backup_parent_permissions'):
        config_backup.snapshot(path)
    assert os.path.lexists(path) and not list(root.glob('config.json.corrupt-*'))
    if kind == 'symlink':
        assert target.read_bytes() == b'untouched'


@pytest.mark.parametrize('directory_sync', [1, 2])
def test_directory_sync_failure_preserves_backup_and_config(tmp_path, monkeypatch, directory_sync):
    path, initial = _invalid(tmp_path)
    sync = os.fsync
    directories = []
    def failed(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories.append(True)
            if len(directories) == directory_sync:
                raise OSError('synthetic directory sync failure')
        return sync(fd)
    monkeypatch.setattr(config_backup.os, 'fsync', failed)
    with pytest.raises(config_backup.BackupUncertain) as error:
        config_backup.archive_and_create(path, initial, b'{}')
    assert (tmp_path / error.value.backup_name).read_bytes() == b'{synthetic-invalid'
    assert path.read_bytes() == (b'{synthetic-invalid' if directory_sync == 1 else b'{}')


def _init_world(monkeypatch, tmp_path):
    import mcs_setup
    from test_mcs_setup import _init_env
    _init_env(monkeypatch, tmp_path, {})
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: None)
    path = tmp_path / 'c.json'
    path.write_text('{broken')
    return mcs_setup, path


def test_init_detects_competing_edit_without_overwriting(tmp_path, monkeypatch, capsys):
    setup, path = _init_world(monkeypatch, tmp_path)
    publish = config_backup._publish_backup
    def concurrent(fd, name, raw, symlink):
        publish(fd, name, raw, symlink)
        path.write_text('{"winner":true}')
    monkeypatch.setattr(config_backup, '_publish_backup', concurrent)
    assert setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    assert path.read_text() == '{"winner":true}'
    (backup,) = tmp_path.glob('c.json.corrupt-*')
    assert backup.read_text() == '{broken'
    out = capsys.readouterr().out
    assert '保存結果を確認できません' in out and f'退避先の候補 {backup}' in out


def test_init_help_explains_backup_limits(capsys):
    import mcs_setup
    with pytest.raises(SystemExit) as result:
        mcs_setup.main(['init', '--help'])
    assert result.value.code == 0
    out = capsys.readouterr().out
    assert '1 MiB超・読取不能・FIFO' in out and '複数ハードリンク' in out
    assert 'コピーや保存に失敗した場合' in out
    assert '排他的な退避が未対応' not in out


def test_init_rejects_oversize_before_secret_or_backup(tmp_path, monkeypatch):
    setup, path = _init_world(monkeypatch, tmp_path)
    original = b'{' + b'x' * mcs_util.CONFIG_MAX_BYTES
    path.write_bytes(original)
    monkeypatch.setattr(setup, '_init_store_secrets', lambda *_: pytest.fail('secret mutation'))
    assert setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    assert path.read_bytes() == original and not list(tmp_path.glob('c.json.corrupt-*'))


def test_init_parent_swap_never_writes_new_parent(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'home'
    root.mkdir()
    setup, path = _init_world(monkeypatch, root)
    publish = config_backup._publish_backup
    def swap(fd, name, raw, symlink):
        publish(fd, name, raw, symlink)
        root.rename(tmp_path / 'old-home')
        root.mkdir()
        (root / 'c.json').write_text('{"winner":true}')
    monkeypatch.setattr(config_backup, '_publish_backup', swap)
    assert setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    assert path.read_text() == '{"winner":true}'
    (backup,) = (tmp_path / 'old-home').glob('c.json.corrupt-*')
    assert backup.read_text() == '{broken'
    out = capsys.readouterr().out
    assert '保存場所が変わった可能性' in out and '退避先の候補' not in out


@pytest.mark.parametrize('phase', ['backup', 'replace_before', 'replace_after', 'backup_fsync', 'replace_fsync', 'parent_close', 'replace_eio_before', 'replace_eio_after'])
def test_init_reports_uncertain_backup_for_interrupted_phase(tmp_path, monkeypatch, capsys, phase):
    setup, path = _init_world(monkeypatch, tmp_path)
    publish, replace, fsync, close = config_backup._publish_backup, os.replace, os.fsync, os.close
    published, directories = [], []
    def publish_fault(fd, name, raw, symlink):
        publish(fd, name, raw, symlink)
        published.append(True)
        if phase == 'backup':
            raise KeyboardInterrupt
    def replace_fault(*args, **kwargs):
        if phase == 'replace_before':
            raise KeyboardInterrupt
        if phase == 'replace_eio_before':
            raise OSError(errno.EIO, 'synthetic')
        result = replace(*args, **kwargs)
        if phase == 'replace_after':
            raise KeyboardInterrupt
        if phase == 'replace_eio_after':
            raise OSError(errno.EIO, 'synthetic')
        return result
    def sync_fault(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories.append(True)
            if phase == 'backup_fsync' and len(directories) == 1:
                raise KeyboardInterrupt
            if phase == 'replace_fsync' and len(directories) == 2:
                raise KeyboardInterrupt
        return fsync(fd)
    def close_fault(fd):
        if phase == 'parent_close' and published and stat.S_ISDIR(os.fstat(fd).st_mode):
            close(fd)
            raise KeyboardInterrupt
        return close(fd)
    monkeypatch.setattr(config_backup, '_publish_backup', publish_fault)
    monkeypatch.setattr(config_backup.os, 'replace', replace_fault)
    monkeypatch.setattr(config_backup.os, 'fsync', sync_fault)
    monkeypatch.setattr(config_backup.os, 'close', close_fault)
    assert setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    out = capsys.readouterr().out
    assert '保存結果を確認できません' in out and '退避先の候補' in out
    assert '今回の変更は設定ファイルに保存していません' not in out
    (backup,) = tmp_path.glob('c.json.corrupt-*')
    assert backup.read_text() == '{broken'
    if phase in ('replace_after', 'replace_fsync', 'parent_close', 'replace_eio_after'):
        assert json.loads(path.read_text())['mcs_login_id'] == 'synthetic'
    else:
        assert path.read_text() == '{broken'


@pytest.mark.parametrize('phase', ['before_backup', 'during_backup', 'after_backup', 'before_replace', 'after_replace'])
def test_process_exit_never_leaves_original_config_missing(tmp_path, monkeypatch, phase):
    _, path = _init_world(monkeypatch, tmp_path)
    code = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _mcs_path
import config_backup
path = Path(sys.argv[2])
phase = sys.argv[3]
publish = config_backup._publish_backup
stage = config_backup._staged
replace = os.replace
def interrupted_publish(fd, name, raw, symlink):
    if phase == 'before_backup':
        os._exit(73)
    publish(fd, name, raw, symlink)
    if phase == 'after_backup':
        os._exit(73)
def interrupted_stage(fd, raw, **kwargs):
    result = stage(fd, raw, **kwargs)
    if phase == 'during_backup' and raw == b'{broken':
        os._exit(73)
    return result
def interrupted_replace(*args, **kwargs):
    if phase == 'before_replace':
        os._exit(73)
    result = replace(*args, **kwargs)
    if phase == 'after_replace':
        os._exit(73)
    return result
config_backup._publish_backup = interrupted_publish
config_backup._staged = interrupted_stage
os.replace = interrupted_replace
config_backup.archive_and_create(path, config_backup.snapshot(path), b'{}')
'''
    result = subprocess.run([sys.executable, '-B', '-c', code,
                             str(Path(_mcs_path.__file__).parent), str(path), phase],
                            cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode == 73, result.stderr
    assert path.exists()
    assert path.read_bytes() == (b'{}' if phase == 'after_replace' else b'{broken')
    backups = list(tmp_path.glob('c.json.corrupt-*'))
    if phase in ('before_backup', 'during_backup'):
        assert not backups
        if phase == 'during_backup':
            (staged,) = tmp_path.glob('c.json.stage-*.tmp')
            assert staged.read_bytes() == b'{broken' and staged.stat().st_mode & 0o777 == 0o600
    else:
        assert len(backups) == 1 and backups[0].read_bytes() == b'{broken'
