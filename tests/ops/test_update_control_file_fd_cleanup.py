"""An updater control path that is a directory is corrupt and leaks no fd.
Synthetic temp directories only."""
import os

import pytest

import mcs_update


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_directory_control_file_closes_the_fd(tmp_path):
    before = _open_fds()
    for _ in range(20):
        with pytest.raises(OSError):
            mcs_update._read_control_json(str(tmp_path))
    assert _open_fds() == before
