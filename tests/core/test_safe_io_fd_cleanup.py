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
