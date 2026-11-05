"""Independent recovery closes unadopted FDs, including its frozen child."""
import errno
import hashlib
import os
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from test_mcs_recover import rec

__all__ = ["rec"]


def _close_if_open(fd):
    try:
        os.close(fd)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise


@pytest.mark.parametrize("site", ["tracked_read", "atomic_json", "restore", "frozen_json"])
@pytest.mark.parametrize("fault_type", [OSError, ValueError, KeyboardInterrupt])
def test_independent_recovery_closes_fd_before_stream_adoption(
        rec, tmp_path, monkeypatch, site, fault_type):
    namespace = None
    if site == "frozen_json":
        namespace = {"__name__": "synthetic_frozen"}
        exec(compile(rec.mcs_recovery_gateway._GATEWAY_RESTART_PROGRAM.rsplit("sys.exit(", 1)[0],
                     "<synthetic_frozen>", "exec"), namespace)
    target = tmp_path / "target.json"
    target.write_bytes(b'{"synthetic":"previous"}')
    expected = target.read_bytes()
    repo = tmp_path / "tracked"
    repo.mkdir()
    tracked = repo / "synthetic.txt"
    tracked.write_bytes(b"fully synthetic\n")
    blob = hashlib.sha1(b"blob 16\0fully synthetic\n", usedforsecurity=False).hexdigest()
    tree = "100644 blob " + blob + "\tsynthetic.txt\0"
    index = "H 100644 " + blob + " 0\tsynthetic.txt\0"
    monkeypatch.setattr(rec.mcs_recovery_core, "REPO", str(repo))
    monkeypatch.setattr(rec.mcs_recovery_core, "_git", lambda args: SimpleNamespace(
        returncode=0, stdout=tree if args[0] == "ls-tree" else
        index if args[0] == "ls-files" else ""))
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(rec.mcs_recovery_database, "_settle_staging", lambda directory: None)
    monkeypatch.setattr(rec.sqlite3, "connect", lambda *args, **kwargs: pytest.fail(
        "pre-adoption failure must not open any database"))
    monkeypatch.setattr(rec.mcs_recovery_core, "_load_state", lambda: pytest.fail(
        "pre-adoption failure must not read the restore journal"))
    real_open, real_mkstemp = os.open, rec.tempfile.mkstemp
    attempted = []
    created = []
    replaced = []
    before_replace = []
    fault = fault_type("synthetic stream adoption failure")
    with ExitStack() as resources:
        keeper = real_open(tmp_path / "keeper", os.O_CREAT | os.O_RDWR, 0o600)
        resources.callback(_close_if_open, keeper)

        def remember(fd):
            if fd not in created:
                created.append(fd)
                resources.callback(_close_if_open, fd)

        def owned_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if os.fspath(path) == str(tracked):
                remember(fd)
            return fd

        def mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            remember(fd)
            return fd, path

        def fail_adoption(fd, mode, **kwargs):
            attempted.append((fd, mode))
            raise fault

        monkeypatch.setattr(rec.os, "open", owned_open)
        monkeypatch.setattr(rec.tempfile, "mkstemp", mkstemp)
        monkeypatch.setattr(rec.os, "fdopen", fail_adoption)
        monkeypatch.setattr(rec.os, "replace", lambda *args: replaced.append(args))
        if site == "tracked_read" and fault_type in (OSError, ValueError):
            assert rec.mcs_recovery_core._local_changes_outside("synthetic-prev", "synthetic-target") is None
        else:
            with pytest.raises(fault_type) as caught:
                if site == "tracked_read":
                    rec.mcs_recovery_core._local_changes_outside("synthetic-prev", "synthetic-target")
                elif site == "restore":
                    rec.mcs_recovery_database._replace_database(str(tmp_path / "unused.db"), "0" * 64,
                                          lambda: before_replace.append(True))
                else:
                    writer = namespace["_atomic_json"] if namespace else rec.mcs_recovery_gateway._atomic_json
                    writer(str(target), {"synthetic": "new"}, ".synthetic.")
            assert caught.value is fault
        assert len(created) == 1 and attempted == [(created[0],
                "rb" if site == "tracked_read" else "wb" if site == "restore" else "w")]
        with pytest.raises(OSError) as closed:
            os.fstat(created[0])
        assert closed.value.errno == errno.EBADF
        assert os.fstat(keeper).st_size == 0
        assert replaced == [] and before_replace == []
        assert target.read_bytes() == expected
        assert list(tmp_path.glob(".synthetic.*")) == []
        assert list(tmp_path.glob(".restore.*")) == []
