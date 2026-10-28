"""破損した設定の排他的退避と新しい設定の安全な作成。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import stat
import time
from dataclasses import dataclass

import mcs_util
from safe_io import fdopen_owned


class BackupChanged(ValueError):
    pass


class BackupUncertain(OSError):
    def __init__(self, backup_name, parent_unchanged):
        super().__init__('config_backup_result_unknown')
        self.backup_name = backup_name
        self.parent_unchanged = parent_unchanged


@dataclass(frozen=True)
class BackupSnapshot:
    parent: tuple[int, int]
    identity: tuple[int, ...]
    sha256: bytes


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_uid)


def _parent_identity(info):
    return info.st_dev, info.st_ino


def _check_parent(parent, parent_fd, expected, *, private=False):
    opened = os.fstat(parent_fd)
    if (opened.st_uid != os.getuid() or opened.st_mode & (0o077 if private else 0o022)):
        raise ValueError('config_backup_parent_permissions')
    if (_parent_identity(opened) != expected
            or _parent_identity(os.stat(parent)) != expected):
        raise BackupChanged('config_parent_changed')


def _entry(parent_fd, name):
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if info.st_uid != os.getuid():
        raise ValueError('config_backup_owner')
    if stat.S_ISREG(info.st_mode):
        if info.st_size > mcs_util.CONFIG_MAX_BYTES:
            raise ValueError('config_backup_too_large')
        if info.st_nlink != 1:
            raise ValueError('config_backup_hardlink')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if _identity(opened) != _identity(info):
                raise BackupChanged('config_changed')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(mcs_util.CONFIG_MAX_BYTES + 1)
            if len(raw) > mcs_util.CONFIG_MAX_BYTES or _identity(os.fstat(fd)) != _identity(info):
                raise BackupChanged('config_changed')
        finally:
            os.close(fd)
    elif stat.S_ISLNK(info.st_mode):
        raw = os.fsencode(os.readlink(name, dir_fd=parent_fd))
        if len(raw) > mcs_util.CONFIG_MAX_BYTES:
            raise ValueError('config_backup_too_large')
    else:
        raise ValueError('config_backup_type')
    if _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _identity(info):
        raise BackupChanged('config_changed')
    return _identity(info), hashlib.sha256(raw).digest(), raw


def snapshot(path):
    path = Path(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parent = _parent_identity(os.fstat(parent_fd))
        _check_parent(path.parent, parent_fd, parent)
        identity, digest, _ = _entry(parent_fd, path.name)
        _check_parent(path.parent, parent_fd, parent, private=stat.S_ISLNK(identity[2]))
        return BackupSnapshot(parent, identity, digest)
    finally:
        os.close(parent_fd)


def _staged(parent_fd, raw, *, prefix):
    name = prefix + '.stage-' + secrets.token_hex(16) + '.tmp'
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    identity = _parent_identity(os.fstat(fd))
    try:
        with fdopen_owned(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _remove_owned_stage(parent_fd, name, identity)
        raise
    return name, identity


def _remove_owned_stage(parent_fd, name, identity):
    try:
        if _parent_identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) == identity:
            os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _publish_backup(parent_fd, name, raw, is_symlink):
    if is_symlink:
        os.symlink(raw, os.fsencode(name), dir_fd=parent_fd)
        os.fsync(parent_fd)
        return
    temp, identity = _staged(parent_fd, raw, prefix=name.rsplit('.corrupt-', 1)[0])
    try:
        os.link(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        os.fsync(parent_fd)
    finally:
        _remove_owned_stage(parent_fd, temp, identity)


def archive_and_create(path, expected, body):
    if not isinstance(expected, BackupSnapshot) or not isinstance(body, bytes):
        raise ValueError('config_backup_invalid')
    if len(body) > mcs_util.CONFIG_MAX_BYTES:
        raise ValueError('config_too_large')
    path = Path(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    backup = None
    backup_started = False
    private_parent = stat.S_ISLNK(expected.identity[2])
    try:
        try:
            _check_parent(path.parent, parent_fd, expected.parent, private=private_parent)
            source_identity, source_digest, source_raw = _entry(parent_fd, path.name)
            if (source_identity, source_digest) != (expected.identity, expected.sha256):
                raise BackupChanged('config_changed')
            stem = path.name + '.corrupt-' + time.strftime('%Y%m%dT%H%M%S')
            for n in range(1000):
                backup = stem if n == 0 else f'{stem}.{n}'
                backup_started = True
                try:
                    _publish_backup(parent_fd, backup, source_raw, stat.S_ISLNK(source_identity[2]))
                    break
                except FileExistsError:
                    backup_started = False
                    backup = None
            else:
                raise ValueError('config_backup_full')
            _check_parent(path.parent, parent_fd, expected.parent, private=private_parent)
            backup_identity, backup_digest, backup_raw = _entry(parent_fd, backup)
            if (backup_digest != source_digest or backup_raw != source_raw
                    or stat.S_IFMT(backup_identity[2]) != stat.S_IFMT(source_identity[2])
                    or (stat.S_ISREG(backup_identity[2]) and stat.S_IMODE(backup_identity[2]) != 0o600)):
                raise BackupChanged('config_backup_changed')
            temp, temp_identity = _staged(parent_fd, body, prefix=path.name)
            try:
                _check_parent(path.parent, parent_fd, expected.parent, private=private_parent)
                now_identity, now_digest, _ = _entry(parent_fd, path.name)
                if (now_identity, now_digest) != (expected.identity, expected.sha256):
                    raise BackupChanged('config_changed')
                if _entry(parent_fd, backup)[:2] != (backup_identity, backup_digest):
                    raise BackupChanged('config_backup_changed')
                os.replace(temp, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                _remove_owned_stage(parent_fd, temp, temp_identity)
            _check_parent(path.parent, parent_fd, expected.parent, private=private_parent)
            current_identity, current_digest, _ = _entry(parent_fd, path.name)
            if (current_identity[:2] != temp_identity or not stat.S_ISREG(current_identity[2])
                    or stat.S_IMODE(current_identity[2]) != 0o600
                    or current_digest != hashlib.sha256(body).digest()
                    or _entry(parent_fd, backup)[:2] != (backup_identity, backup_digest)):
                raise BackupChanged('config_backup_readback_changed')
            return str(path.parent / backup)
        finally:
            os.close(parent_fd)
    except BaseException:
        if backup_started:
            try:
                parent_unchanged = _parent_identity(os.stat(path.parent)) == expected.parent
            except BaseException:
                parent_unchanged = False
            raise BackupUncertain(backup, parent_unchanged) from None
        raise
