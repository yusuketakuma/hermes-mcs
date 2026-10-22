"""Keep Connector credentials and bounded state outside the repository."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat

NAMES = frozenset({'endpoint.json', 'credential', 'state.json', 'tick.lock'})
MAX_FILE = 8 * 1024 * 1024
LIMITS = {'state.json': MAX_FILE, 'credential': 4096, 'endpoint.json': 4096, 'tick.lock': 0}


class Store:
    def __init__(self, root=None, *, create=False):
        self.root = Path(root or Path.home() / '.mcs-security').absolute()
        repo = Path(__file__).resolve().parents[2]
        if self.root.is_relative_to(repo) or '..' in self.root.parts:
            raise ValueError('SDC_PRIVATE_ROOT_REQUIRED')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, part in enumerate(self.root.parts[1:]):
                if create and index == len(self.root.parts) - 2:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
                raise ValueError('SDC_PRIVATE_ROOT_REQUIRED')
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _name(self, name):
        if name not in NAMES:
            raise ValueError('SDC_INVALID_FILE')

    def _open(self, name, flags=os.O_RDONLY):
        self._name(name)
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > LIMITS[name] or st.st_nlink != 1:
                raise ValueError('SDC_PRIVATE_FILE_REQUIRED')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, name, default=None):
        try:
            fd = self._open(name)
        except FileNotFoundError:
            return default
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(LIMITS[name] + 1)
        if len(raw) > LIMITS[name]:
            raise ValueError('SDC_FILE_TOO_LARGE')
        from .protocol import strict_json
        return strict_json(raw)

    def write(self, name, value):
        self._name(name)
        try:
            os.close(self._open(name))
        except FileNotFoundError:
            pass
        raw = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
        if len(raw) > LIMITS[name]:
            raise ValueError('SDC_FILE_TOO_LARGE')
        temp = '.sdc-' + secrets.token_hex(16)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    @contextmanager
    def lock(self):
        fd = self._open('tick.lock', os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def remove_credential(self):
        os.close(self._open('credential'))
        os.unlink('credential', dir_fd=self.fd)
        os.fsync(self.fd)
