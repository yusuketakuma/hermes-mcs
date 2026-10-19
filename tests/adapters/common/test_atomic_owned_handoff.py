"""Synthetic owned-FD faults preserve adapter publication and lazy bootstrap."""
import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.common import paths


def _close_all(fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("already_closed", [False, True])
def test_atomic_owned_handoff_fault_preserves_original_and_destination(
        tmp_path, monkeypatch, error_type, already_closed):
    import safe_io

    target = tmp_path / "published.json"
    target.write_bytes(b"previous")
    original = error_type("synthetic handoff fault")
    captured = []
    acquire = paths.tempfile.mkstemp

    def record(*args, **kwargs):
        fd, name = acquire(*args, **kwargs)
        captured.append(fd)
        return fd, name

    def fail(fd, mode, **kwargs):
        assert fd == captured[-1] and mode == "wb"
        if already_closed:
            os.close(fd)
        raise original

    try:
        monkeypatch.setattr(paths.tempfile, "mkstemp", record)
        monkeypatch.setattr(safe_io.os, "fdopen", fail)
        with pytest.raises(error_type) as got:
            paths.atomic_write(str(target), b"replacement")
        assert got.value is original
        assert len(captured) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(captured[0])
        assert closed.value.errno == errno.EBADF
        assert target.read_bytes() == b"previous"
        # Existing cleanup catches only OSError; interrupts retain their staging
        # pathname, while the owned raw descriptor is still closed in all cases.
        assert len(list(tmp_path.glob(".atomic-*.tmp"))) == (
            0 if error_type is OSError else 1)
    finally:
        _close_all(captured)


def test_atomic_owned_handoff_success_closes_and_publishes(tmp_path, monkeypatch):
    captured = []
    acquire = paths.tempfile.mkstemp

    def record(*args, **kwargs):
        fd, name = acquire(*args, **kwargs)
        captured.append(fd)
        return fd, name

    try:
        monkeypatch.setattr(paths.tempfile, "mkstemp", record)
        target = str(tmp_path / "published.json")
        assert paths.atomic_write(target, b"replacement", mode=0o600) == target
        assert Path(target).read_bytes() == b"replacement"
        assert Path(target).stat().st_mode & 0o777 == 0o600
        assert list(tmp_path.glob(".atomic-*.tmp")) == []
        assert len(captured) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(captured[0])
        assert closed.value.errno == errno.EBADF
    finally:
        _close_all(captured)


def test_atomic_write_bootstraps_checkout_only_when_called_outside_repo(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    code = """
import sys
from pathlib import Path
repo, target = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo))
for name in ('discord', 'slack_sdk', 'slack_bolt'):
    sys.modules[name] = None
from adapters.common import paths, summary, mcs_api
assert 'safe_io' not in sys.modules and '_mcs_path' not in sys.modules
assert 'mcs_requests' not in sys.modules and 'notify_digest' not in sys.modules
paths.atomic_write(str(target), b'synthetic')
assert Path(sys.modules['safe_io'].__file__).resolve() == repo / 'mcs/core/safe_io.py'
assert 'mcs_requests' not in sys.modules and 'notify_digest' not in sys.modules
assert target.read_bytes() == b'synthetic'
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(repo), str(tmp_path / "published")],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
