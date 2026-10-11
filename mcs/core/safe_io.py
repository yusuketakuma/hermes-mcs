"""Crash-safe file primitives shared by core, ingest, notify and ops modules.

Holds the one implementation of each safety-critical file pattern that
several modules used to keep private copies of: atomic replace through
an fsynced temp file, directory fsync after a rename or link.  Every
helper keeps the exact flags, modes, fsync points and cleanup of the
copies it replaced; options exist only where those copies differed.
Stdlib only and free of side effects at import, so any entry point can
use it.
"""
import os
import stat
import tempfile
from contextlib import suppress


def fsync_dir(path, *, require_dir: bool = True) -> None:
    """fsync a directory so a rename or link inside it survives a crash.

    ``require_dir=False`` opens with plain ``O_RDONLY`` (no
    ``O_DIRECTORY``), as the call sites written that way always did."""
    flags = os.O_RDONLY | os.O_DIRECTORY if require_dir else os.O_RDONLY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: str, writer, mode: int | None = None,
                 tmp_prefix: str = ".atomic.", *, binary: bool = False,
                 tmp_suffix: str = ".tmp", makedirs: bool = True,
                 require_dir: bool = True) -> None:
    """tmp -> fsync -> [chmod] -> os.replace -> dir fsync: consumers see
    the whole old file or the whole new one, and a mid-write crash never
    leaves a torn file behind (tmp is unlinked on failure).  ``writer``
    receives the open file object: UTF-8 text mode, or bytes mode when
    ``binary``.  ``makedirs=False`` requires the parent to exist;
    ``require_dir`` is passed to the final :func:`fsync_dir`."""
    parent = os.path.dirname(path) or "."
    if makedirs:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=tmp_prefix,
                               suffix=tmp_suffix)
    try:
        with (os.fdopen(fd, "wb") if binary
              else os.fdopen(fd, "w", encoding="utf-8")) as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(parent, require_dir=require_dir)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def create_exclusive(path, mode: int = 0o600, *, dir_fd: int | None = None,
                     nofollow: bool = True) -> int:
    """Create ``path`` write-only and return its fd, failing if it exists.

    ``O_WRONLY | O_CREAT | O_EXCL`` with ``mode``, plus ``O_NOFOLLOW``
    unless ``nofollow=False``; ``dir_fd`` is passed only when given."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if nofollow:
        flags |= os.O_NOFOLLOW
    if dir_fd is None:
        return os.open(path, flags, mode)
    return os.open(path, flags, mode, dir_fd=dir_fd)


def read_regular_nofollow(path, limit: int, error, *,
                          check_size: bool = True) -> bytes:
    """Read at most ``limit + 1`` bytes of a regular file, never following
    a symlink or blocking on a FIFO.

    Opens ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` and raises ``error()``
    unless fstat shows a regular file (and, with ``check_size``, a size
    within ``limit``).  The caller rejects a result longer than ``limit``
    with its own error, since the file may grow after the fstat."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or (check_size and st.st_size > limit):
            raise error()
        return stream.read(limit + 1)
