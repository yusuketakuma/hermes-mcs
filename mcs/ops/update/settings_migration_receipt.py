"""Hold an uncertain opt-out migration until its result is reconciled."""
import os
import json
import re
import secrets
import stat

NAME = '.install-options-migration.pending'
BODY = b'{"v":1,"kind":"install-options-migration"}\n'


def _open_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        current = os.stat(path)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('migration_data_unverified')
        return fd, (info.st_dev, info.st_ino)
    except BaseException:
        os.close(fd)
        raise


def pending(data):
    """Any receipt or unverifiable existing directory is a hold."""
    data = os.path.abspath(data)
    root = os.path.dirname(data)
    try:
        fd, identity = _open_directory(root)
    except FileNotFoundError:
        return os.path.lexists(root)
    except (OSError, ValueError):
        return True
    try:
        try:
            os.stat(NAME, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            found = False
        else:
            found = True
        info, current = os.fstat(fd), os.stat(root)
        if (info.st_uid != os.getuid() or info.st_mode & 0o022
                or (current.st_dev, current.st_ino) != identity
                or (info.st_dev, info.st_ino) != identity):
            return True
        if found:
            return True
        try:
            data_fd, _ = _open_directory(data)
        except FileNotFoundError:
            return os.path.lexists(data)
        except (OSError, ValueError):
            return True
        os.close(data_fd)
        current = os.stat(root)
        return (current.st_dev, current.st_ino) != identity
    except OSError:
        return True
    finally:
        os.close(fd)


def read_evidence(data):
    """Read bounded recovery evidence without displaying settings or secrets."""
    data = os.path.abspath(data)
    root = os.path.dirname(data)
    directory, root_identity = _open_directory(root)
    data_fd = None
    try:
        data_fd, data_identity = _open_directory(data)
        fd = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > 2048):
                raise ValueError('migration_evidence_unreadable')
            raw = os.read(fd, 2049)
            after = os.fstat(fd)
            current = os.stat(NAME, dir_fd=directory, follow_symlinks=False)
            def identity(s):
                return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink,
                        s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if len(raw) > 2048 or identity(before) != identity(after) or identity(before) != identity(current):
                raise ValueError('migration_evidence_changed')
        finally:
            os.close(fd)
        import install_options
        evidence = install_options._parse_json(raw)
        fields = {'v', 'kind', 'operation', 'root', 'data', 'legacy_sha256', 'base_sha256'}
        if (type(evidence) is not dict or set(evidence) != fields or type(evidence['v']) is not int or evidence['v'] != 2
                or evidence['kind'] != 'install-options-migration'
                or evidence['root'] != list(root_identity) or evidence['data'] != list(data_identity)
                or any(type(evidence[key]) is not str or re.fullmatch(pattern, evidence[key]) is None
                       for key, pattern in (('operation', '[0-9a-f]{32}'),
                                            ('legacy_sha256', '[0-9a-f]{64}'),
                                            ('base_sha256', '[0-9a-f]{64}')))):
            raise ValueError('migration_evidence_invalid')
        root_now, data_now = os.stat(root), os.stat(data)
        if ((root_now.st_dev, root_now.st_ino) != root_identity
                or (data_now.st_dev, data_now.st_ino) != data_identity):
            raise ValueError('migration_data_changed')
        return evidence, identity(before)
    finally:
        if data_fd is not None:
            os.close(data_fd)
        os.close(directory)


class Receipt:
    """A private, exclusive receipt; cleanup is allowed only after a synced copy."""
    def __init__(self, data, expected_base, legacy_digest):
        self.data = os.path.abspath(data)
        self.root = os.path.dirname(self.data)
        if any(type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None
               for value in (expected_base, legacy_digest)):
            raise ValueError('migration_evidence_invalid')
        self.directory, self.identity = _open_directory(self.root)
        try:
            self.data_directory, self.data_identity = _open_directory(self.data)
        except BaseException:
            os.close(self.directory)
            raise
        self.fd = None
        self.body = (json.dumps({'v': 2, 'kind': 'install-options-migration',
                                 'operation': secrets.token_hex(16),
                                 'root': list(self.identity), 'data': list(self.data_identity),
                                 'legacy_sha256': legacy_digest, 'base_sha256': expected_base},
                                sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')

    def _parent(self):
        held, current = os.fstat(self.directory), os.stat(self.root)
        if (held.st_uid != os.getuid() or held.st_mode & 0o022
                or (held.st_dev, held.st_ino) != self.identity
                or (current.st_dev, current.st_ino) != self.identity):
            raise ValueError('migration_data_changed')
        held, current = os.fstat(self.data_directory), os.stat(self.data)
        if (held.st_uid != os.getuid() or held.st_mode & 0o022
                or (held.st_dev, held.st_ino) != self.data_identity
                or (current.st_dev, current.st_ino) != self.data_identity):
            raise ValueError('migration_data_changed')

    def begin(self):
        self._parent()
        self.fd = os.open(NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=self.directory)
        view = memoryview(self.body)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise OSError('migration_receipt_write')
            view = view[written:]
        os.fsync(self.fd)
        os.fsync(self.directory)
        self._parent()

    def check(self):
        self._parent()
        if self.fd is None:
            raise ValueError('migration_receipt_missing')
        held = os.fstat(self.fd)
        current = os.stat(NAME, dir_fd=self.directory, follow_symlinks=False)
        if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid()
                or stat.S_IMODE(held.st_mode) != 0o600 or held.st_nlink != 1
                or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)
                or held.st_size != len(self.body)):
            raise ValueError('migration_receipt_changed')
        os.lseek(self.fd, 0, os.SEEK_SET)
        if os.read(self.fd, len(self.body) + 1) != self.body:
            raise ValueError('migration_receipt_changed')
        after = os.fstat(self.fd)
        if (held.st_size, held.st_mtime_ns, held.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('migration_receipt_changed')

    def finish(self):
        self.check()
        os.unlink(NAME, dir_fd=self.directory)
        os.fsync(self.directory)
        self._parent()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        os.close(self.data_directory)
        os.close(self.directory)
