"""Borrowed SQLite/descriptor lifetime and stale-generation refusal use synthetic resources."""
import errno
import os
import sqlite3

import pytest

import writer_lock


@pytest.fixture
def resources(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: 1)
    path = str(tmp_path / "run.lock")
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE synthetic(value)")
    fd = writer_lock.acquire_run_lock(path)
    try:
        yield db, fd, path
    finally:
        db.close()
        os.close(fd)


def test_active_transaction_refuses_unlock_without_committing_or_closing(resources):
    db, fd, path = resources
    db.execute("INSERT INTO synthetic VALUES(1)")
    with pytest.raises(RuntimeError, match="transport_inside_db_transaction"):
        with writer_lock.unlocked_transport(db, fd):
            pytest.fail("transport must not run inside a writer transaction")
    assert db.in_transaction
    assert db.execute("SELECT value FROM synthetic").fetchone() == (1,)
    assert writer_lock.acquire_run_lock(path) is None
    os.fstat(fd)
    db.rollback()
    assert db.execute("SELECT count(*) FROM synthetic").fetchone() == (0,)


@pytest.mark.parametrize("failure", [False, True])
def test_transport_borrows_connection_and_fd_across_normal_and_error_exit(resources, failure):
    db, fd, path = resources
    def transport():
        with writer_lock.unlocked_transport(db, fd):
            other = writer_lock.acquire_run_lock(path)
            assert other is not None
            os.close(other)
            if failure:
                raise ValueError("synthetic transport error")
    if failure:
        with pytest.raises(ValueError, match="synthetic transport error"):
            transport()
    else:
        transport()
    assert not db.in_transaction
    assert db.execute("SELECT count(*) FROM synthetic").fetchone() == (0,)
    assert writer_lock.acquire_run_lock(path) is None
    os.fstat(fd)


@pytest.mark.parametrize("cause", ["marker", "code"])
def test_wait_closes_only_the_stale_trial_descriptor(tmp_path, monkeypatch, cause):
    clock = [0.0]
    monkeypatch.setattr(writer_lock.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(writer_lock.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: 2 if cause == "code" else 1)
    marker = tmp_path / "marker"
    if cause == "marker":
        marker.write_text("synthetic")
    path = str(tmp_path / "run.lock")
    opened = []
    original = writer_lock.acquire_run_lock
    def acquire(path):
        fd = original(path)
        opened.append(fd)
        return fd
    monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire)
    fresh = None
    try:
        assert writer_lock.wait_run_lock(path, wait_s=1, expected_code_stamp=1, marker_path=str(marker)) is None
        with pytest.raises(OSError) as error:
            os.fstat(opened[0])
        assert error.value.errno == errno.EBADF
        fresh = original(path)
        assert fresh is not None
    finally:
        # Capture includes descriptors the tested implementation should close;
        # always clean up even if an earlier assertion catches a regression.
        for fd in set(opened + ([fresh] if fresh is not None else [])):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise



def test_no_lock_retains_existing_noop_even_inside_a_transaction(resources):
    db, fd, path = resources
    db.execute("INSERT INTO synthetic VALUES(1)")
    with writer_lock.unlocked_transport(db, None):
        assert db.in_transaction
    assert db.in_transaction
    assert writer_lock.acquire_run_lock(path) is None
    os.fstat(fd)


def test_single_baseexception_identity_is_shared_by_callers():
    import run_check
    assert run_check.RunLockLost is writer_lock.RunLockLost
    assert issubclass(writer_lock.RunLockLost, BaseException)
    assert not issubclass(writer_lock.RunLockLost, Exception)
