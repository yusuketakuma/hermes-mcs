"""破損した設定の排他的退避と新しい設定の安全な作成。"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
import sys
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


def _check_parent(parent, parent_fd, expected):
    if (_parent_identity(os.fstat(parent_fd)) != expected
            or _parent_identity(os.stat(parent)) != expected):
        raise BackupChanged('config_parent_changed')


def _entry(parent_fd, name):
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if info.st_uid != os.getuid():
        raise ValueError('config_backup_owner')
    if stat.S_ISREG(info.st_mode):
        if info.st_size > mcs_util.CONFIG_MAX_BYTES:
            raise ValueError('config_backup_too_large')
        if info.st_nlink != 1 and stat.S_IMODE(info.st_mode) != 0o600:
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
    return _identity(info), hashlib.sha256(raw).digest()


def snapshot(path):
    path = Path(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parent = _parent_identity(os.fstat(parent_fd))
        _check_parent(path.parent, parent_fd, parent)
        identity, digest = _entry(parent_fd, path.name)
        _check_parent(path.parent, parent_fd, parent)
        return BackupSnapshot(parent, identity, digest)
    finally:
        os.close(parent_fd)


def _rename_exclusive(parent_fd, source, destination):
    if sys.platform == 'darwin':
        name, flags = 'renameatx_np', 4
    elif sys.platform.startswith('linux'):
        name, flags = 'renameat2', 1
    else:
        raise OSError(errno.ENOTSUP, 'exclusive_rename_unsupported')
    fn = getattr(ctypes.CDLL(None, use_errno=True), name, None)
    if fn is None:
        raise OSError(errno.ENOTSUP, 'exclusive_rename_unsupported')
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), flags):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def archive_and_create(path, expected, body):
    if not isinstance(expected, BackupSnapshot) or not isinstance(body, bytes):
        raise ValueError('config_backup_invalid')
    if len(body) > mcs_util.CONFIG_MAX_BYTES:
        raise ValueError('config_too_large')
    path = Path(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    backup = None
    try:
        _check_parent(path.parent, parent_fd, expected.parent)
        if _entry(parent_fd, path.name) != (expected.identity, expected.sha256):
            raise BackupChanged('config_changed')
        stem = path.name + '.corrupt-' + time.strftime('%Y%m%dT%H%M%S')
        for n in range(1000):
            backup = stem if n == 0 else f'{stem}.{n}'
            try:
                _rename_exclusive(parent_fd, path.name, backup)
                break
            except FileExistsError:
                backup = None
        else:
            raise ValueError('config_backup_full')
        try:
            moved, digest = _entry(parent_fd, backup)
            if moved[:5] + moved[6:] != expected.identity[:5] + expected.identity[6:] or digest != expected.sha256:
                raise BackupChanged('config_moved_changed')
            _check_parent(path.parent, parent_fd, expected.parent)
            if stat.S_ISREG(moved[2]):
                fd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                try:
                    current = _identity(os.fstat(fd))
                    if current != moved:
                        raise BackupChanged('config_moved_changed')
                    if stat.S_IMODE(moved[2]) != 0o600:
                        if os.fstat(fd).st_nlink != 1:
                            raise ValueError('config_backup_hardlink')
                        os.fchmod(fd, 0o600)
                        os.fsync(fd)
                finally:
                    os.close(fd)
            os.fsync(parent_fd)
            _check_parent(path.parent, parent_fd, expected.parent)
            temp = '.setup-' + secrets.token_hex(16) + '.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            staging = _parent_identity(os.fstat(fd))
            try:
                with fdopen_owned(fd, 'wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                _check_parent(path.parent, parent_fd, expected.parent)
                os.link(temp, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                os.fsync(parent_fd)
            finally:
                try:
                    if _parent_identity(os.stat(temp, dir_fd=parent_fd, follow_symlinks=False)) == staging:
                        os.unlink(temp, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            _check_parent(path.parent, parent_fd, expected.parent)
            return str(path.parent / backup)
        except (OSError, ValueError):
            try:
                _check_parent(path.parent, parent_fd, expected.parent)
                parent_unchanged = True
            except (OSError, ValueError):
                parent_unchanged = False
            raise BackupUncertain(backup, parent_unchanged) from None
    finally:
        os.close(parent_fd)
