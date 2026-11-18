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
        with writer_lock.capture_writer_refusal() as diagnostic:
            assert writer_lock.wait_run_lock(path, wait_s=1, expected_code_stamp=1, marker_path=str(marker)) is None
        assert diagnostic == {
            'writer_reason': 'update_marker_present' if cause == 'marker' else 'source_generation_changed',
            'writer_stage': 'update_restore_fence' if cause == 'marker' else 'generation_fence'}
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


def test_wait_expiry_reports_actual_lock_refusal_without_replacing_outer_capture(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(writer_lock.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(writer_lock.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    trials = []
    monkeypatch.setattr(writer_lock, 'acquire_run_lock', lambda path: trials.append(path))
    with writer_lock.capture_writer_refusal() as outer:
        with writer_lock.capture_writer_refusal() as inner:
            assert writer_lock.wait_run_lock(str(tmp_path / 'run.lock'), wait_s=1,
                expected_code_stamp=1, marker_path=str(tmp_path / 'marker')) is None
        assert inner == {'writer_reason': 'run_lock_not_acquired', 'writer_stage': 'write_lock'}
        assert outer['writer_reason'] == 'writer_refusal_unclassified'
    assert trials and 1 <= clock[0] < 1.3



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


def _restart_intent(tmp_path, **overrides):
    import json as _json
    started = int(__import__('time').time())
    row = {"schema": "gateway-restart-intent/1",
           "request_id": "a" * 32,
           "helper_pid": 4242,
           "helper_started_at": started,
           "not_after": started + 660}
    row.update(overrides)
    (tmp_path / writer_lock.RESTART_INTENT_NAME).write_text(_json.dumps(row))
    return row


def _fake_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(writer_lock.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(writer_lock.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return clock


def test_wait_yields_early_for_a_live_restart_intent(tmp_path, monkeypatch):
    row = _restart_intent(tmp_path)
    monkeypatch.setattr(writer_lock, "_helper_process_started_at",
                        lambda pid, **kw: row["helper_started_at"])
    _fake_clock(monkeypatch)
    trials = []
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda path: trials.append(path))
    with writer_lock.capture_writer_refusal() as diagnostic:
        assert writer_lock.wait_run_lock(str(tmp_path / "run.lock"), wait_s=10,
            expected_code_stamp=1, marker_path=str(tmp_path / "marker")) is None
    assert diagnostic == {"writer_reason": "restart_intent_active",
                          "writer_stage": "write_lock"}
    assert trials == []


@pytest.mark.parametrize("fault", ["schema", "expired", "dead_pid", "started_at",
                                   "request_id", "expected_pid", "absent"])
def test_stale_or_forged_restart_intent_never_shortens_the_wait(tmp_path, monkeypatch, fault):
    if fault != "absent":
        if fault == "schema":
            _restart_intent(tmp_path, schema="other/0")
        elif fault == "expired":
            import time as _time
            _restart_intent(tmp_path, helper_started_at=int(_time.time()) - 700,
                            not_after=int(_time.time()) - 1)
        elif fault == "request_id":
            _restart_intent(tmp_path, request_id="not-a-request")
        elif fault == "expected_pid":
            _restart_intent(tmp_path, expected_pid=-1)
    started = int(__import__('time').time())
    if fault == "dead_pid":
        _restart_intent(tmp_path, helper_started_at=started)
        monkeypatch.setattr(writer_lock, "_helper_process_started_at", lambda pid, **kw: None)
    elif fault == "started_at":
        _restart_intent(tmp_path, helper_started_at=started)
        monkeypatch.setattr(writer_lock, "_helper_process_started_at", lambda pid, **kw: started + 1)
    else:
        monkeypatch.setattr(writer_lock, "_helper_process_started_at", lambda pid, **kw: started)
    _fake_clock(monkeypatch)
    trials = []
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda path: trials.append(path))
    with writer_lock.capture_writer_refusal() as diagnostic:
        assert writer_lock.wait_run_lock(str(tmp_path / "run.lock"), wait_s=1,
            expected_code_stamp=1, marker_path=str(tmp_path / "marker")) is None
    assert diagnostic == {"writer_reason": "run_lock_not_acquired",
                          "writer_stage": "write_lock"}
    assert trials


def test_restart_intent_appearing_mid_wait_ends_it(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda path: None)
    clock = _fake_clock(monkeypatch)
    slept = [0]

    def sleep(seconds):
        slept[0] += 1
        if slept[0] == 2:
            _restart_intent(tmp_path)
        clock[0] += seconds

    monkeypatch.setattr(writer_lock.time, "sleep", sleep)
    started = int(__import__('time').time())
    monkeypatch.setattr(writer_lock, "_helper_process_started_at",
                        lambda pid, **kw: started)
    with writer_lock.capture_writer_refusal() as diagnostic:
        assert writer_lock.wait_run_lock(str(tmp_path / "run.lock"), wait_s=10,
            expected_code_stamp=1, marker_path=str(tmp_path / "marker")) is None
    assert diagnostic["writer_reason"] == "restart_intent_active"
    assert slept[0] == 2


def test_lock_won_as_the_intent_appears_is_given_back(tmp_path, monkeypatch):
    row = _restart_intent(tmp_path)
    monkeypatch.setattr(writer_lock, "_helper_process_started_at",
                        lambda pid, **kw: row["helper_started_at"])
    checked = [0]

    def probe(path, limit, error, **kw):
        checked[0] += 1
        # First poll reads nothing yet; the second (post-acquire) finds it.
        if checked[0] == 1:
            raise OSError("no intent yet")
        import json as _json
        return _json.dumps(row).encode()

    monkeypatch.setattr(writer_lock, "read_regular_nofollow", probe)
    _fake_clock(monkeypatch)
    fd = writer_lock.acquire_run_lock(str(tmp_path / "run.lock"))
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda path: fd)
    with writer_lock.capture_writer_refusal() as diagnostic:
        assert writer_lock.wait_run_lock(str(tmp_path / "run.lock"), wait_s=10,
            expected_code_stamp=1, marker_path=str(tmp_path / "marker")) is None
    assert diagnostic["writer_reason"] == "restart_intent_active"
    with pytest.raises(OSError):
        os.fstat(fd)
