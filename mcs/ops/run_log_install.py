"""Install and verify a root-bound, independent copy of the canonical runtime-log helper."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid


MAX_SOURCE = 2 * 1024 * 1024


class LogInstallError(RuntimeError):
    """A fixed deployment or migration failure that leaves original logs intact."""


def _canonical_leaf(path):
    path = Path(os.path.abspath(os.fspath(path)))
    if path.is_symlink():
        raise LogInstallError('run_log_root_mismatch')
    # macOS exposes standard ancestor aliases such as /var -> /private/var.
    # Only resolve the parent: the private leaf itself must never be a link.
    return os.fspath(path.parent.resolve() / path.name)


def _root(root):
    return _canonical_leaf(root)


def _checked(fd, *, private=True, directory=False):
    info = os.fstat(fd)
    if (info.st_uid != os.getuid() or info.st_mode & 0o022
            or (not stat.S_ISDIR(info.st_mode) if directory else
                not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
            or private and stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)):
        raise LogInstallError('run_log_helper_untrusted')
    return info


def _read(fd, limit, *, private=True):
    before = _checked(fd, private=private)
    if before.st_size > limit:
        raise LogInstallError('run_log_helper_size')
    pieces, size = [], 0
    while True:
        raw = os.read(fd, min(65536, limit + 1 - size))
        if not raw:
            break
        pieces.append(raw)
        size += len(raw)
        if size > limit:
            raise LogInstallError('run_log_helper_size')
    after = _checked(fd, private=private)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise LogInstallError('run_log_helper_changed')
    return b''.join(pieces)


def source_snapshot(checkout):
    """Capture trusted source bytes before a checkout can be reset by rollback."""
    path = Path(os.path.abspath(checkout), 'mcs', 'core', 'run_log.py')
    if path.resolve() != path:
        raise LogInstallError('run_log_helper_untrusted')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        return _read(fd, MAX_SOURCE, private=False)
    finally:
        os.close(fd)


@contextmanager
def _directory(path, *, write=True):
    parts = Path(path).absolute().parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parts[1:]):
            if write and index == len(parts) - 2:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        _checked(fd, directory=True)
        if not write:
            yield fd
            return
        lock = os.open('.run-log-install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                       | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=fd)
        try:
            _checked(lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            info = os.fstat(lock)
            current = os.stat('.run-log-install.lock', dir_fd=fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise LogInstallError('run_log_helper_changed')
            yield fd
        finally:
            os.close(lock)
    finally:
        os.close(fd)


def _read_at(directory, name, limit=MAX_SOURCE):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory)
    try:
        return _read(fd, limit)
    finally:
        os.close(fd)


def _publish(directory, name, raw):
    temporary = '.run-log-install-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        _checked(fd)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise LogInstallError('run_log_helper_write_failed')
            view = view[count:]
        os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _pointer(root):
    return 'run_log-' + hashlib.sha256(os.fsencode(root)).hexdigest() + '.json'


def _receipt(directory, root):
    value = json.loads(_read_at(directory, _pointer(root), 4096))
    if (not isinstance(value, dict) or set(value) != {'version', 'root', 'sha256', 'helper'}
            or type(value['version']) is not int or value['version'] != 1 or value['root'] != root
            or not isinstance(value['sha256'], str) or len(value['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in value['sha256'])
            or value['helper'] != _pointer(root)[:-5] + '-' + value['sha256'] + '.py'):
        raise LogInstallError('run_log_helper_receipt_invalid')
    try:
        raw = _read_at(directory, value['helper'])
    except FileNotFoundError:
        raise LogInstallError('run_log_helper_missing') from None
    if hashlib.sha256(raw).hexdigest() != value['sha256']:
        raise LogInstallError('run_log_helper_hash_mismatch')
    return value, raw


def _migrate(root, receipt, raw):
    module = {'__name__': '_mcs_private_run_log', '__file__': receipt['helper']}
    exec(compile(raw, receipt['helper'], 'exec'), module)
    data_dir = os.path.join(root, 'data')
    module['initialize'](data_dir)
    result = module['migrate'](data_dir)
    return {**result, 'scope': 'verified_prefix', 'legacy_writers_closed': False}


def helper_path(root, recovery_dir=None, *, checkout=None, require_installed=False):
    """Read an installed helper, or calculate its immutable path for a read-only plan."""
    root = _root(root)
    recovery_dir = _canonical_leaf(recovery_dir if recovery_dir is not None
                                   else os.path.expanduser('~/.mcs-recovery'))
    try:
        try:
            with _directory(recovery_dir, write=False) as directory:
                try:
                    receipt, _ = _receipt(directory, root)
                except FileNotFoundError:
                    receipt = None
        except FileNotFoundError:
            receipt = None
        if receipt is not None:
            return os.path.join(recovery_dir, receipt['helper'])
        if require_installed:
            raise LogInstallError('run_log_helper_missing')
        checkout = Path(__file__).resolve().parents[2] if checkout is None else checkout
        source = source_snapshot(checkout)
        if source is None:
            raise LogInstallError('run_log_source_unavailable')
        digest = hashlib.sha256(source).hexdigest()
        return os.path.join(recovery_dir, _pointer(root)[:-5] + '-' + digest + '.py')
    except Exception:
        raise LogInstallError('run_log_helper_unverified') from None


def migrate_installed(root, recovery_dir):
    """Re-import a verified finite tail without depending on the runtime checkout."""
    # Older installations have no pointer. Do not inspect or modify their
    # recovery directory, and do not impose new deployment checks on them.
    root = _root(root)
    recovery_dir = _canonical_leaf(recovery_dir)
    if not os.path.lexists(os.path.join(recovery_dir, _pointer(root))):
        return {'scope': 'unavailable', 'legacy_writers_closed': False}
    try:
        with _directory(recovery_dir) as directory:
            try:
                receipt, raw = _receipt(directory, root)
            except FileNotFoundError:
                return {'scope': 'unavailable', 'legacy_writers_closed': False}
            return _migrate(root, receipt, raw)
    except Exception:
        raise LogInstallError('run_log_migration_failed') from None


def prepare(root, checkout, recovery_dir, *, source=None, promote=False):
    """Publish canonical bytes and import verified prefixes before changing services."""
    root = _root(root)
    recovery_dir = _canonical_leaf(recovery_dir)
    try:
        source = source_snapshot(checkout) if source is None else source
        if source is None:
            return {'scope': 'unavailable', 'legacy_writers_closed': False}
        digest = hashlib.sha256(source).hexdigest()
        receipt = {'version': 1, 'root': root, 'sha256': digest,
                   'helper': _pointer(root)[:-5] + '-' + digest + '.py'}
        with _directory(recovery_dir) as directory:
            try:
                previous, previous_source = _receipt(directory, root)
            except FileNotFoundError:
                previous = None
            if previous is not None and not promote:
                return _migrate(root, previous, previous_source)
            try:
                saved = _read_at(directory, receipt['helper'])
            except FileNotFoundError:
                _publish(directory, receipt['helper'], source)
            else:
                if saved != source:
                    raise LogInstallError('run_log_helper_hash_mismatch')
            if previous != receipt:
                _publish(directory, _pointer(root), json.dumps(receipt, sort_keys=True).encode())
            checked, raw = _receipt(directory, root)
            return _migrate(root, checked, raw)
    except Exception:
        raise LogInstallError('run_log_prepare_failed') from None


if __name__ == '__main__':
    try:
        if (len(sys.argv) in (5, 6) and sys.argv[1] == '--helper-path'
                and (len(sys.argv) == 5 or sys.argv[5] == '--require-installed')):
            print(helper_path(sys.argv[2], sys.argv[4], checkout=sys.argv[3],
                              require_installed=len(sys.argv) == 6))
        elif len(sys.argv) == 4:
            prepare(*sys.argv[1:], promote=True)
        else:
            raise LogInstallError('run_log_install_arguments')
    except LogInstallError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
