"""Hold an uncertain opt-out migration until its result is reconciled."""
import os
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


class Receipt:
    """A private, exclusive receipt; cleanup is allowed only after a synced copy."""
    def __init__(self, data):
        self.data = os.path.abspath(data)
        self.root = os.path.dirname(self.data)
        self.directory, self.identity = _open_directory(self.root)
        try:
            self.data_directory, self.data_identity = _open_directory(self.data)
        except BaseException:
            os.close(self.directory)
            raise
        self.fd = None

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
        view = memoryview(BODY)
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
                or held.st_size != len(BODY)):
            raise ValueError('migration_receipt_changed')
        os.lseek(self.fd, 0, os.SEEK_SET)
        if os.read(self.fd, len(BODY) + 1) != BODY:
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
