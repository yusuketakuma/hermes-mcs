"""Only synthetic file locks; interruptions cannot retain the owned descriptor."""
from contextlib import contextmanager
import errno
import os

import pytest

import llm_availability as availability


@contextmanager
def tracked_lock(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-circuit.json"
    made = []
    real_open = os.open
    real_flock = availability.fcntl.flock
    try:
        def opened(name, *args, **kwargs):
            fd = real_open(name, *args, **kwargs)
            if name == f"{path}.lock":
                made.append(fd)
            return fd

        with monkeypatch.context() as patch:
            patch.setattr(availability.os, "open", opened)
            yield path, made, patch, real_flock
    finally:
        for fd in made:
            try:
                os.close(fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def assert_closed(fd):
    with pytest.raises(OSError) as error:
        os.fstat(fd)
    assert error.value.errno == errno.EBADF


def assert_lock_released(path, real_flock):
    with open(f"{path}.lock", "r+b") as probe:
        real_flock(probe.fileno(), availability.fcntl.LOCK_EX | availability.fcntl.LOCK_NB)


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_flock_interruption_before_yield_closes_owned_fd(tmp_path, monkeypatch, capsys, failure):
    with tracked_lock(tmp_path, monkeypatch) as (path, made, patch, real_flock):
        original = failure("synthetic private exception detail")
        entered = []

        def interrupted(fd, mode):
            assert fd == made[0] and mode == availability.fcntl.LOCK_EX
            raise original

        patch.setattr(availability.fcntl, "flock", interrupted)
        with pytest.raises(failure) as caught:
            with availability._circuit_lock(path):
                entered.append(True)
        assert caught.value is original and entered == []
        assert len(made) == 1
        assert_closed(made[0])
        assert_lock_released(path, real_flock)
        assert not path.exists()
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""


def test_flock_oserror_still_fails_open_and_closes_fd_after_yield(tmp_path, monkeypatch):
    with tracked_lock(tmp_path, monkeypatch) as (path, made, patch, real_flock):
        def unavailable(_fd, _mode):
            raise OSError("synthetic lock unavailable")

        patch.setattr(availability.fcntl, "flock", unavailable)
        entered = []
        with availability._circuit_lock(path):
            entered.append(True)
            assert os.fstat(made[0])
        assert entered == [True] and len(made) == 1
        assert_closed(made[0])
        assert_lock_released(path, real_flock)
        assert not path.exists()


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt, SystemExit])
def test_open_failure_has_no_owned_fd_and_retains_failopen_rule(tmp_path, monkeypatch, failure):
    with tracked_lock(tmp_path, monkeypatch) as (path, made, patch, _real_flock):
        original = failure("synthetic open refusal")
        entered = []

        def unavailable(*_args, **_kwargs):
            raise original

        patch.setattr(availability.os, "open", unavailable)
        if failure is OSError:
            with availability._circuit_lock(path):
                entered.append(True)
            assert entered == [True]
        else:
            with pytest.raises(failure) as caught:
                with availability._circuit_lock(path):
                    entered.append(True)
            assert caught.value is original and entered == []
        assert made == [] and not path.exists()


@pytest.mark.parametrize("failure", [None, OSError, KeyboardInterrupt, SystemExit])
def test_lock_stays_owned_through_body_and_closes_on_every_exit(tmp_path, monkeypatch, failure):
    with tracked_lock(tmp_path, monkeypatch) as (path, made, _patch, real_flock):
        original = failure("synthetic body refusal") if failure is not None else None
        entered = []

        def body():
            with availability._circuit_lock(path):
                entered.append(True)
                assert os.fstat(made[0])
                if original is not None:
                    raise original

        if failure is None:
            body()
        else:
            with pytest.raises(failure) as caught:
                body()
            assert caught.value is original
        assert entered == [True] and len(made) == 1
        assert_closed(made[0])
        assert_lock_released(path, real_flock)
        assert not path.exists()
