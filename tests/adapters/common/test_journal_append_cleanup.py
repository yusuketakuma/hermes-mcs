"""A journal append that fails before writing closes its descriptor and
writes nothing — repeated I/O faults must not exhaust the worker's FDs.
Synthetic temp directories only."""
import os
from pathlib import Path

import pytest

from adapters.common import journal


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.mark.parametrize("failing", ["fstat", "pread", "fdopen"])
def test_failure_before_the_write_closes_the_fd(tmp_path, monkeypatch, failing):
    path = journal.append(str(tmp_path), "w1", {"phase": "claimed", "attempt": "a"})
    before = Path(path).read_bytes()
    if failing == "fdopen":
        def boom(*_a, **_k):
            raise OSError(5, "synthetic EIO")
        monkeypatch.setattr(journal.os, "fdopen", boom)
    else:
        real = getattr(os, failing)

        def boom(fd, *a, **k):
            if a or failing == "fstat":
                raise OSError(5, "synthetic EIO")
            return real(fd, *a, **k)
        monkeypatch.setattr(journal.os, failing, boom)
    opened = _open_fds()
    for _ in range(20):
        with pytest.raises(OSError):
            journal.append(str(tmp_path), "w1", {"phase": "started", "attempt": "a"})
    monkeypatch.undo()
    assert _open_fds() == opened
    assert Path(path).read_bytes() == before          # nothing was written


def test_a_torn_tail_still_gets_its_own_line(tmp_path):
    path = journal.append(str(tmp_path), "w1", {"phase": "claimed", "attempt": "a"})
    with open(path, "ab") as handle:
        handle.write(b'{"torn":')
    journal.append(str(tmp_path), "w1", {"phase": "started", "attempt": "a"})
    lines = Path(path).read_bytes().split(b"\n")
    assert lines[1] == b'{"torn":' and b'"started"' in lines[2]
