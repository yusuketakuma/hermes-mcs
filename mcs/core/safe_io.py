"""Crash-safe file primitives shared by core, ingest, notify and ops modules.

Holds the one implementation of each safety-critical file pattern that
several modules used to keep private copies of: atomic replace through
an fsynced temp file, directory fsync after a rename or link, exclusive
creation of a new file, and a bounded read that refuses links.  Every
helper keeps the exact flags, modes, fsync points and cleanup of the
copies it replaced; options exist only where those copies differed.
Stdlib only and free of side effects at import, so any entry point can
use it.
"""
import errno
import json
import os
import stat
import tempfile
from pathlib import Path
from contextlib import suppress


def fdopen_owned(fd: int, mode: str, **stream_kwargs):
    """Take ownership of raw ``fd`` and return its owning stream, or close on failure.

    The caller must pass only an owned descriptor (duplicate borrowed FDs first).
    Flags and metadata checks remain with the caller. On success the caller owns
    and closes the stream; closefd is always true and must not be overridden.
    """
    try:
        return os.fdopen(fd, mode, closefd=True, **stream_kwargs)
    except BaseException:
        try:
            os.close(fd)
        except OSError as cleanup_error:
            if cleanup_error.errno != errno.EBADF:
                raise
        raise


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
        with (fdopen_owned(fd, "wb") if binary
              else fdopen_owned(fd, "w", encoding="utf-8")) as f:
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
    stream = fdopen_owned(fd, "rb")
    with stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or (check_size and st.st_size > limit):
            raise error()
        return stream.read(limit + 1)


def publish_new_bytes(path, raw):
    """Publish a fully synced private file without replacing an existing destination."""
    path = Path(path)
    fd, staging = tempfile.mkstemp(dir=path.parent, prefix=".alias-", suffix=".tmp")
    try:
        with fdopen_owned(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # link is an atomic, exclusive publication. A crash before it leaves
        # only staging; a later failure leaves complete, recoverable JSON.
        os.link(staging, path)
        fsync_dir(path.parent)
    finally:
        with suppress(OSError):
            os.unlink(staging)


def publish_tmp(tmp: str, dest: str, mode: int | None = None) -> None:
    """[chmod] -> fsync tmp -> os.replace -> dir fsync for a finished tmp
    file: readers see the whole old ``dest`` or the whole new one."""
    if mode is not None:
        os.chmod(tmp, mode)
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dest)
    fsync_dir(os.path.dirname(dest) or ".")



def atomic_publish_text(path: str, text: str) -> None:
    """tmp -> fsync -> os.replace -> dir fsync: consumers either see the
    whole previous file or the whole new one — a mid-write crash never
    leaves a torn JSON behind (same contract as the backup chain)."""
    atomic_write(path, lambda f: f.write(text), tmp_prefix=".pub.")


def read_private_bytes(path, limit: int = 16384, *, dir_fd: int | None = None) -> bytes:
    """Read a bounded regular current-UID private file without links or FIFO waits.

    A supplied directory descriptor is borrowed; this function closes only its file.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if dir_fd is None:
        fd = os.open(path, flags)
    else:
        fd = os.open(path, flags, dir_fd=dir_fd)
    stream = fdopen_owned(fd, "rb")
    with stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid() or metadata.st_size > limit):
            raise ValueError
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError
        return raw

def read_json_nofollow(path, limit: int):
    """Decode one complete bounded regular JSON document without links or FIFO waits."""
    raw = read_regular_nofollow(path, limit, ValueError)
    if len(raw) > limit:
        raise ValueError("status_too_large")
    return json.loads(raw.decode("utf-8"))
