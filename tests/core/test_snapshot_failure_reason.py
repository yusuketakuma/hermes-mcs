"""An unpublished snapshot records which branch refused it, without paths or content. Synthetic DB only."""
import fcntl
import os
import sqlite3
import subprocess
from time import monotonic
from types import SimpleNamespace

import pytest

from ledger import Ledger
import snapshots


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE snapshot_meta(singleton INTEGER PRIMARY KEY, generation_id, generated_at)")
    return str(path)


def _child(monkeypatch, code):
    monkeypatch.setattr(snapshots, "_SNAPSHOT_VALIDATION_CODE", code)


def test_success_leaves_no_failure(db, tmp_path, monkeypatch):
    _child(monkeypatch, "import sys; sys.exit(0)")
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"))
    assert snapshots.last_failure() == {}


def test_synthetic_snapshot_child_uses_each_frozen_clock(tmp_path, monkeypatch, snapshot_child_clock):
    source = str(tmp_path / "synthetic.db")
    ledger = Ledger(source)
    ledger.ensure_patient(1)
    ledger.close()
    clock = [1790000000.25]
    monkeypatch.setattr(snapshots, "time", SimpleNamespace(time=lambda: clock[0]))
    for stamp in (1790000000.25, 1790000060.75):
        clock[0] = stamp
        path = snapshots.publish_snapshot(source, str(tmp_path / "out"))
        assert path is not None
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT generated_at FROM snapshot_meta").fetchone()[0] == stamp


def test_deadline_before_copy(db, tmp_path):
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"), deadline=monotonic() - 1) is None
    assert snapshots.last_failure() == {"reason": "deadline_before_copy"}


@pytest.mark.parametrize("code,reason", [("import sys; sys.exit(1)", "validator_rejected"),
                                         ("import sys; sys.exit(3)", "validator_bootstrap_error"),
                                         ("import sys; sys.exit(4)", "validator_exception"),
                                         ("import sys; sys.exit(5)", "validator_timeout"),
                                         ("import sys; sys.exit(7)", "validator_exit")])
def test_validator_exit_codes_are_told_apart(db, tmp_path, monkeypatch, code, reason):
    _child(monkeypatch, code)
    assert snapshots.publish_snapshot(db, str(tmp_path / "out")) is None
    failure = snapshots.last_failure()
    assert failure["reason"] == reason and failure["exit_code"] == int(code[-2])
    assert failure["capped_by_deadline"] is False and failure["copy_s"] >= 0 and failure["verify_s"] >= 0
    assert all(isinstance(v, (str, int, float, bool)) for v in failure.values())


def test_validator_timeout_reports_budget_and_deadline_cap(db, tmp_path, monkeypatch):
    _child(monkeypatch, "import time; time.sleep(30)")
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"), deadline=monotonic() + 1) is None
    failure = snapshots.last_failure()
    assert failure["reason"] == "validator_timeout" and failure["capped_by_deadline"] is True
    assert failure["budget_s"] <= 1


def test_busy_validation_lock(db, tmp_path, monkeypatch):
    _child(monkeypatch, "import sys; sys.exit(0)")
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    with open(out / ".validation.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert snapshots.publish_snapshot(db, str(out)) is None
    assert snapshots.last_failure()["reason"] == "validation_lock_busy"
    assert snapshots.publish_snapshot(db, str(out))          # the next publish resets the reason
    assert snapshots.last_failure() == {}


def _real_child(*argv):
    import subprocess
    import sys
    return subprocess.run([sys.executable, "-I", "-c", snapshots._SNAPSHOT_VALIDATION_CODE, *argv],
                          capture_output=True, timeout=60).returncode


def test_real_child_separates_bootstrap_errors_exceptions_and_refusals(tmp_path):
    from pathlib import Path
    mcs_root = str(Path(snapshots.__file__).resolve().parents[1])
    far = str(monotonic() + 60)
    assert _real_child(str(tmp_path), str(tmp_path / "x.db"), far) == 3      # no _mcs_path there
    assert _real_child(mcs_root, str(tmp_path / "x.db"), "not-a-number") == 4
    assert _real_child(mcs_root, str(tmp_path / "missing.db"), far) == 1     # explicit refusal


def test_real_child_reports_its_own_deadline_as_timeout(tmp_path):
    from pathlib import Path
    mcs_root = str(Path(snapshots.__file__).resolve().parents[1])
    db = tmp_path / "x.db"
    db.write_bytes(b"not empty")
    assert _real_child(mcs_root, str(db), str(monotonic() - 1)) == 5      # deadline already past


def test_verify_budget_follows_a_slow_copy_within_the_cap(db, tmp_path, monkeypatch):
    seen, clock = {}, {"t": 0.0}

    def verify(path, *, deadline, **_kwargs):
        seen["budget"] = deadline - clock["t"]
        return True
    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", verify)
    monkeypatch.setattr(snapshots, "monotonic", lambda: clock["t"])
    real_connect, opened = snapshots.sqlite3.connect, []

    def connect(*a, **k):
        opened.append(1)
        if len(opened) % 2 == 0:                      # the copy target: the copy "takes" 100 s
            clock["t"] += 100.0
        return real_connect(*a, **k)
    monkeypatch.setattr(snapshots.sqlite3, "connect", connect)
    monkeypatch.setattr(snapshots, "_prepare_in_child",
                        lambda db, tmp, deadline, fd, state: (
                            snapshots._prepare_snapshot(db, tmp, deadline) is None))
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"))
    assert seen["budget"] == 150.0                    # 1.5 x copy, not the ~30 s size floor
    clock["t"] = 0.0
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"), deadline=120.0)
    assert seen["budget"] == 20.0                     # never past the caller's hard deadline


def test_unreaped_copy_is_fenced_and_cannot_publish_late(db, tmp_path, monkeypatch):
    output = tmp_path / "out"
    output.mkdir()
    published = output / "ledger-snapshot.db"
    published.write_bytes(b"synthetic old snapshot")
    monkeypatch.setattr(snapshots, "_PREPARE_CODE", "import time; time.sleep(3)")
    spawn = snapshots.subprocess.Popen
    children = []

    def record(*args, **kwargs):
        child = spawn(*args, **kwargs)
        if args[0][3] == snapshots._PREPARE_CODE:
            children.append((child, child.kill))
            monkeypatch.setattr(child, "kill", lambda: None)
        return child

    monkeypatch.setattr(snapshots.subprocess, "Popen", record)
    try:
        assert snapshots.publish_snapshot(db, str(output), deadline=monotonic() + 0.2) is None
        assert snapshots.last_failure()["reason"] == "copy_timeout"
        assert snapshots.last_failure()["child_unreaped"] is True
        assert len(children) == 1 and children[0][0].poll() is None
        assert len(list(output.glob(".snapshot.*.tmp"))) == 1
        assert snapshots.publish_snapshot(db, str(output), deadline=monotonic() + 5) is None
        assert snapshots.last_failure()["reason"] == "snapshot_lock_busy"
        children[0][0].wait(timeout=5)
        assert published.read_bytes() == b"synthetic old snapshot"
    finally:
        for child, kill in children:
            if child.poll() is None:
                kill()
                child.wait(timeout=5)


def test_expiry_during_candidate_fsync_prevents_replace(db, tmp_path, monkeypatch):
    output = tmp_path / "out"
    output.mkdir()
    published = output / "ledger-snapshot.db"
    published.write_bytes(b"synthetic old snapshot")
    clock = [0.0]
    monkeypatch.setattr(snapshots, "monotonic", lambda: clock[0])
    monkeypatch.setattr(snapshots, "_prepare_in_child",
                        lambda db, tmp, deadline, fd, state:
                        snapshots._prepare_snapshot(db, tmp, deadline) is None)
    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", lambda *a, **kw: True)
    monkeypatch.setattr(snapshots.os, "fsync", lambda fd: clock.__setitem__(0, 10.0))
    assert snapshots.publish_snapshot(db, str(output), deadline=10.0) is None
    assert snapshots.last_failure()["reason"] == "publish_deadline"
    assert published.read_bytes() == b"synthetic old snapshot"


def test_directory_fsync_failure_reports_uncertain_publication(db, tmp_path, monkeypatch):
    output = tmp_path / "out"
    output.mkdir()
    published = output / "ledger-snapshot.db"
    published.write_bytes(b"synthetic old snapshot")
    monkeypatch.setattr(snapshots, "_SNAPSHOT_VALIDATION_CODE", "import sys; sys.exit(0)")

    def fail(_path):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(snapshots, "fsync_dir", fail)
    assert snapshots.publish_snapshot(db, str(output), deadline=monotonic() + 10) is None
    assert snapshots.last_failure()["reason"] == "publication_uncertain"
    assert published.read_bytes() != b"synthetic old snapshot"
    assert snapshots.last_timing() == {}


def test_snapshot_lock_open_failure_has_fixed_reason(db, tmp_path, monkeypatch):
    real_open = snapshots.os.open

    def fail_lock(path, *args, **kwargs):
        if str(path).endswith(".snapshot.lock"):
            raise OSError(13, "synthetic private error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(snapshots.os, "open", fail_lock)
    assert snapshots.publish_snapshot(db, str(tmp_path / "out")) is None
    assert snapshots.last_failure() == {"reason": "snapshot_setup_oserror", "errno": 13}


def test_snapshot_lock_failure_has_fixed_reason(db, tmp_path, monkeypatch):
    def fail_lock(*_args):
        raise OSError(5, "synthetic private error")

    monkeypatch.setattr(snapshots.fcntl, "flock", fail_lock)
    assert snapshots.publish_snapshot(db, str(tmp_path / "out")) is None
    assert snapshots.last_failure() == {"reason": "snapshot_lock_error", "errno": 5}


def test_snapshot_temp_creation_failure_has_fixed_reason(db, tmp_path, monkeypatch):
    def fail_temp(*_args, **_kwargs):
        raise OSError(28, "synthetic private error")

    monkeypatch.setattr(snapshots.tempfile, "mkstemp", fail_temp)
    assert snapshots.publish_snapshot(db, str(tmp_path / "out")) is None
    assert snapshots.last_failure() == {"reason": "snapshot_setup_oserror", "errno": 28}


def test_pending_cleanup_keeps_fence_until_child_exits(db, tmp_path, monkeypatch):
    _child(monkeypatch, "import sys; sys.exit(1)")
    spawn = snapshots.subprocess.Popen
    pending = []

    class CleanupChild:
        def __init__(self, args, **kwargs):
            self.fd = os.dup(kwargs["pass_fds"][0])
            self.waits = []
            pending.append(self)

        def wait(self, timeout=None):
            self.waits.append(timeout)
            raise subprocess.TimeoutExpired("synthetic cleanup", timeout)

    def spawn_cleanup(args, **kwargs):
        if args[3] == snapshots._CLEANUP_CODE:
            return CleanupChild(args, **kwargs)
        return spawn(args, **kwargs)

    monkeypatch.setattr(snapshots.subprocess, "Popen", spawn_cleanup)
    output = tmp_path / "out"
    try:
        assert snapshots.publish_snapshot(db, str(output), deadline=monotonic() + 10) is None
        assert snapshots.last_failure()["reason"] == "validator_rejected"
        assert snapshots.last_failure()["cleanup_pending"] is True
        assert len(pending) == 1 and pending[0].waits == [1]
        with (output / ".snapshot.lock").open() as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.close(pending[0].fd)
            pending[0].fd = None
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for child in pending:
            if child.fd is not None:
                os.close(child.fd)
