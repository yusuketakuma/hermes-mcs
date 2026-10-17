"""Update failures release owned descriptors without changing recovery decisions."""
from contextlib import suppress
import errno
import os
import subprocess
from types import SimpleNamespace

import pytest

import mcs_update


def test_unreapable_child_closes_its_parent_side_pipes(monkeypatch):
    pipes = [os.pipe(), os.pipe()]
    streams = [os.fdopen(read, "rb") for read, _write in pipes]

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("synthetic child", 5)

    child = SimpleNamespace(pid=1234, returncode=None, stdout=streams[0], stderr=streams[1],
                            communicate=timeout, wait=timeout)
    signals = []
    monkeypatch.setattr(mcs_update.os, "killpg", lambda *args: signals.append(args))
    try:
        mcs_update._stop_update_child(child)
        assert signals == [(1234, 9)]
        assert all(stream.closed for stream in streams)
    finally:
        for stream in streams:
            stream.close()
        for _read, write in pipes:
            os.close(write)


def test_restore_staging_metadata_failure_closes_descriptor_before_any_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_update, "LEDGER", str(tmp_path / "synthetic-ledger.db"))
    monkeypatch.setattr(mcs_update, "_settle_staging", lambda _directory: None)
    created = []
    real_mkstemp, real_fstat = mcs_update.tempfile.mkstemp, os.fstat

    def tracked_mkstemp(*args, **kwargs):
        result = real_mkstemp(*args, **kwargs)
        created.append(result)
        return result

    def fail(fd):
        if any(fd == opened for opened, _path in created):
            raise OSError("synthetic metadata failure")
        return real_fstat(fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(mcs_update.tempfile, "mkstemp", tracked_mkstemp)
            patch.setattr(mcs_update.os, "fstat", fail)
            with pytest.raises(OSError, match="synthetic metadata failure"):
                mcs_update._replace_database("unused-synthetic-backup", "a" * 64,
                                             lambda: pytest.fail("no database replacement"))
        with pytest.raises(OSError) as error:
            os.fstat(created[0][0])
        assert error.value.errno == errno.EBADF
        assert not (tmp_path / "synthetic-ledger.db").exists()
        assert os.path.getsize(created[0][1]) == 0
    finally:
        for fd, path in created:
            with suppress(OSError):
                os.close(fd)
            os.unlink(path)
