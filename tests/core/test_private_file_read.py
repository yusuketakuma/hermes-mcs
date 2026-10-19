"""Synthetic protected-byte reads retain flags, size fences and descriptor ownership."""
import errno
import os

import pytest

import safe_io


def _private(tmp_path):
    path = tmp_path / "synthetic-private"
    path.write_bytes(b"abc")
    path.chmod(0o600)
    return path


def test_private_read_uses_exact_open_flags_and_closes_success(tmp_path, monkeypatch):
    path = _private(tmp_path)
    real_open, opened = os.open, []

    def record(name, flags):
        fd = real_open(name, flags)
        opened.append((fd, flags))
        return fd

    monkeypatch.setattr(safe_io.os, "open", record)
    assert safe_io.read_private_bytes(path, 3) == b"abc"
    assert opened[0][1] == os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    with pytest.raises(OSError) as error:
        os.fstat(opened[0][0])
    assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit, OSError])
def test_fdopen_baseexception_closes_untransferred_fd(tmp_path, monkeypatch, failure):
    path = _private(tmp_path)
    real_open, opened = os.open, []

    def record(name, flags):
        fd = real_open(name, flags)
        opened.append(fd)
        return fd

    def interrupted(fd, mode, **kwargs):
        assert kwargs == {"closefd": True}
        raise failure("synthetic stream transfer failure")

    monkeypatch.setattr(safe_io.os, "open", record)
    monkeypatch.setattr(safe_io.os, "fdopen", interrupted)
    with pytest.raises(failure):
        safe_io.read_private_bytes(path)
    with pytest.raises(OSError) as error:
        os.fstat(opened[0])
    assert error.value.errno == errno.EBADF


def test_growth_after_fstat_is_still_rejected_and_closed(tmp_path, monkeypatch):
    path = _private(tmp_path)
    real_fdopen, opened = os.fdopen, []

    class GrowingRead:
        def __init__(self, fd, mode, **kwargs):
            assert kwargs == {"closefd": True}
            self.stream = real_fdopen(fd, mode, **kwargs)
            opened.append(fd)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            assert size == 4
            return b"abcd"  # fstat saw 3; post-read bound must also hold

    monkeypatch.setattr(safe_io.os, "fdopen", GrowingRead)
    with pytest.raises(ValueError):
        safe_io.read_private_bytes(path, 3)
    with pytest.raises(OSError) as error:
        os.fstat(opened[0])
    assert error.value.errno == errno.EBADF


def test_missing_private_read_remains_raw_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        safe_io.read_private_bytes(tmp_path / "missing")
