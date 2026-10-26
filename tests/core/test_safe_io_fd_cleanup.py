"""A bounded no-follow read of a directory raises without leaking its fd.
Synthetic temp directories only."""
import os

import pytest

import safe_io


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_directory_read_fails_and_closes_the_fd(tmp_path):
    before = _open_fds()
    for _ in range(20):
        with pytest.raises(OSError):
            safe_io.read_regular_nofollow(tmp_path, 1024, ValueError)
    assert _open_fds() == before


def test_optional_stat_describes_the_opened_regular_file_and_closes_fd(tmp_path):
    path = tmp_path / "synthetic.json"
    path.write_bytes(b"{}")
    before = _open_fds()
    raw, info = safe_io.read_regular_nofollow(path, 1024, ValueError, with_stat=True)
    assert raw == b"{}" and (info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino)
    assert safe_io.read_regular_nofollow(path, 1024, ValueError) == b"{}"
    assert _open_fds() == before


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversize"])
def test_optional_stat_keeps_regular_nofollow_size_and_fd_cleanup(tmp_path, monkeypatch, kind):
    path = tmp_path / "synthetic.json"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"{}")
        path.symlink_to(target)
    else:
        path.write_bytes(b"x" * 9)
    real_open = os.open
    def nonblocking_open(name, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        return real_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(safe_io.os, "open", nonblocking_open)
    before = _open_fds()
    with pytest.raises((OSError, ValueError)):
        safe_io.read_regular_nofollow(path, 8, ValueError, with_stat=True)
    assert _open_fds() == before
