"""Recovery releases owned resources and requires proof before retiring services."""
from contextlib import suppress
import errno
import os
from pathlib import Path
import subprocess

import pytest

from test_mcs_recover import rec

__all__ = ["rec"]


def test_restore_metadata_failure_closes_descriptor_before_copy(rec, tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "LEDGER", str(tmp_path / "synthetic-ledger.db"))
    monkeypatch.setattr(rec, "_settle_staging", lambda _directory: None)
    created = []
    real_mkstemp, real_fstat = rec.tempfile.mkstemp, os.fstat

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
            patch.setattr(rec.tempfile, "mkstemp", tracked_mkstemp)
            patch.setattr(rec.os, "fstat", fail)
            with pytest.raises(OSError, match="synthetic metadata failure"):
                rec._replace_database("unused-synthetic-backup", "a" * 64,
                                      lambda: pytest.fail("no database replacement"))
        with pytest.raises(OSError) as error:
            os.fstat(created[0][0])
        assert error.value.errno == errno.EBADF
        assert not Path(rec.LEDGER).exists()
        # Without metadata proof, neither the empty path nor another file is deleted.
        assert os.path.getsize(created[0][1]) == 0
    finally:
        for fd, path in created:
            with suppress(OSError):
                os.close(fd)
            os.unlink(path)


@pytest.mark.parametrize("status", [None, 0, 1, 124, 127, 113])
def test_retirement_requires_verified_service_absence(rec, tmp_path, monkeypatch, status):
    agents = Path(rec.AGENTS_DIR)
    agents.mkdir()
    label = "ai.mcs.extract-retired"
    obsolete = agents / (label + ".plist")
    obsolete.write_text("synthetic owned service")
    foreign = agents / "unrelated.service.plist"
    foreign.write_text("synthetic foreign service")
    monkeypatch.setattr(rec, "_runtime_mode", lambda: "standalone")
    monkeypatch.setattr(rec, "_runtime_config", lambda: {"backup": {"enabled": False}})
    monkeypatch.setattr(rec, "_setup_python", lambda: None)
    calls = []

    def run(args):
        calls.append(args)
        if args[0] == "bootout":
            return subprocess.CompletedProcess(args, 0)
        assert args[0] == "print"
        return None if status is None else subprocess.CompletedProcess(args, status)

    monkeypatch.setattr(rec, "_launchctl", run)
    problems = rec._reconcile_membership({"agents": [], "cron": []})
    assert [call[0] for call in calls] == ["bootout", "print"]
    assert obsolete.exists() is (status != 113)
    assert foreign.read_text() == "synthetic foreign service"
    assert any(label in problem for problem in problems) is (status != 113)
