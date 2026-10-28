"""Coordinate standalone credential users without enabling credential replacement."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from functools import wraps
import fcntl
import os
from pathlib import Path
import stat


class CredentialGateUnavailable(Exception):
    """A fixed code: no credential use was started by this reader."""


@contextmanager
def _reader(root):
    data = Path(root).expanduser().resolve() / "data"
    directory = fd = None
    try:
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = data.resolve(strict=True)
        directory = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError
        fd = os.open("standalone-credentials.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size):
            raise ValueError
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        current = os.stat("standalone-credentials.lock", dir_fd=directory, follow_symlinks=False)
        parent_now = data.stat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino) \
                or (parent.st_dev, parent.st_ino) != (parent_now.st_dev, parent_now.st_ino):
            raise ValueError
        try:
            os.stat("standalone-credentials.hold", dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)
        raise CredentialGateUnavailable("credential_gate_unavailable") from None
    try:
        yield
    finally:
        os.close(fd)
        os.close(directory)


async def _settle_after_cancel(awaitable):
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
        if not task.cancelled():
            task.exception()
        raise


def protected(call):
    """Keep the read gate until the connection or send and its cleanup finish."""
    @wraps(call)
    async def wrapped(root, *args, **kwargs):
        with _reader(root):
            return await _settle_after_cancel(call(root, *args, **kwargs))
    return wrapped
