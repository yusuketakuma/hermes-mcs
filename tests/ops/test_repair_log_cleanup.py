"""Audit-log metadata failures must release the descriptor before returning."""
from contextlib import suppress
import errno
import os

import pytest

import mcs_repair


def test_failed_audit_log_metadata_read_closes_the_descriptor(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "synthetic-audit.jsonl"
    opened = []
    real_open, real_fstat = os.open, os.fstat

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail(fd):
        if fd in opened:
            raise OSError("synthetic metadata failure")
        return real_fstat(fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(mcs_repair.os, "open", tracked_open)
            patch.setattr(mcs_repair.os, "fstat", fail)
            with pytest.raises(OSError, match="synthetic metadata failure"):
                mcs_repair._log(path, create=True)
        with pytest.raises(OSError) as error:
            os.fstat(opened[0])
        assert error.value.errno == errno.EBADF
        assert path.read_bytes() == b""
    finally:
        for fd in opened:
            with suppress(OSError):
                os.close(fd)
