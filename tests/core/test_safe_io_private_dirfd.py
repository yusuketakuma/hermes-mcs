"""Private leaf reads own their file descriptor and only borrow the directory FD."""
import errno
import os

import pytest

import safe_io

LEAF = 'fixed-private.json'


@pytest.fixture
def borrowed(tmp_path, monkeypatch):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    original = os.fstat(dir_fd)
    opened, calls = [], []
    real_open = os.open

    def record(path, flags, *args, **kwargs):
        assert path == LEAF and kwargs == {'dir_fd': dir_fd}
        assert flags & os.O_NONBLOCK  # A FIFO regression must fail before it can block.
        calls.append((path, kwargs['dir_fd']))
        fd = real_open(path, flags, *args, **kwargs)
        opened.append((fd, os.fstat(fd)))
        return fd

    def watch():
        monkeypatch.setattr(safe_io.os, 'open', record)

    def assert_ownership(count):
        assert calls == [(LEAF, dir_fd)] and len(opened) == count
        current = os.fstat(dir_fd)
        assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
        for fd, _ in opened:
            with pytest.raises(OSError) as failure:
                os.fstat(fd)
            assert failure.value.errno == errno.EBADF

    yield directory, dir_fd, watch, assert_ownership
    # Reclaim only test-owned descriptors if an ownership assertion failed.
    for fd, expected in opened:
        try:
            current = os.fstat(fd)
        except OSError:
            continue
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.close(fd)
    os.close(dir_fd)


def private_file(directory, raw=b'abc'):
    path = directory / LEAF
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def test_relative_leaf_uses_borrowed_directory_independently_of_cwd(borrowed, tmp_path, monkeypatch):
    directory, dir_fd, watch, assert_ownership = borrowed
    private_file(directory)
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    private_file(unrelated, b'wrong-directory')
    monkeypatch.chdir(unrelated)
    watch()
    assert safe_io.read_private_bytes(LEAF, 3, dir_fd=dir_fd) == b'abc'
    assert_ownership(1)


def test_oversize_leaf_closes_owned_fd_without_closing_directory(borrowed):
    directory, dir_fd, watch, assert_ownership = borrowed
    private_file(directory, b'abcd')
    watch()
    with pytest.raises(ValueError):
        safe_io.read_private_bytes(LEAF, 3, dir_fd=dir_fd)
    assert_ownership(1)


@pytest.mark.parametrize('kind', ['symlink', 'fifo'])
def test_unsafe_leaf_is_rejected_without_taking_directory_ownership(borrowed, kind):
    directory, dir_fd, watch, assert_ownership = borrowed
    if kind == 'symlink':
        target = directory / 'target'
        target.write_bytes(b'abc')
        target.chmod(0o600)
        (directory / LEAF).symlink_to(target)
    else:
        os.mkfifo(directory / LEAF, 0o600)
    watch()
    if kind == 'symlink':
        with pytest.raises(OSError) as failure:
            safe_io.read_private_bytes(LEAF, 3, dir_fd=dir_fd)
        assert failure.value.errno == errno.ELOOP
        assert_ownership(0)  # O_NOFOLLOW rejects before a leaf FD is acquired.
    else:
        with pytest.raises(ValueError):
            safe_io.read_private_bytes(LEAF, 3, dir_fd=dir_fd)
        assert_ownership(1)


@pytest.mark.parametrize('error_kind', [OSError, KeyboardInterrupt])
def test_read_exception_preserves_instance_and_closes_only_owned_leaf(borrowed, monkeypatch, error_kind):
    directory, dir_fd, watch, assert_ownership = borrowed
    private_file(directory)
    error = error_kind('synthetic read failure')
    real_fdopen = os.fdopen

    class InterruptedRead:
        def __init__(self, fd, mode, **kwargs):
            self.stream = real_fdopen(fd, mode, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            assert size == 4
            raise error

    watch()
    monkeypatch.setattr(safe_io.os, 'fdopen', InterruptedRead)
    with pytest.raises(error_kind) as failure:
        safe_io.read_private_bytes(LEAF, 3, dir_fd=dir_fd)
    assert failure.value is error
    assert_ownership(1)
