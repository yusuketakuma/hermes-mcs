"""Cloud locks transfer only acquired descriptors and close every failed open."""
import errno
import os
from types import SimpleNamespace

import pytest

import cloud_backlog as backlog


def capture_open(monkeypatch):
    real_open, real_fstat = os.open, os.fstat
    monkeypatch.setattr(backlog, 'os', SimpleNamespace(**vars(os)))
    monkeypatch.setattr(backlog, 'fcntl', SimpleNamespace(**vars(backlog.fcntl)))
    opened, captured_flags = [], []
    def opened_lock(path, flags, mode=0o777, **kwargs):
        assert flags & os.O_NOFOLLOW and flags & os.O_CLOEXEC
        fd = real_open(path, flags, mode, **kwargs)
        opened.append(fd)
        captured_flags.append(flags)
        return fd
    monkeypatch.setattr(backlog.os, 'open', opened_lock)
    return opened, real_fstat, captured_flags


def assert_closed(opened, fstat):
    assert opened
    for fd in opened:
        with pytest.raises(OSError) as caught:
            fstat(fd)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize('stage', ['fstat', 'flock'])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_failed_lock_closes_fd_and_preserves_exception(tmp_path, monkeypatch, stage, error_type):
    opened, fstat, _flags = capture_open(monkeypatch)
    failure = error_type('synthetic lock failure')
    def failed(*args):
        raise failure
    monkeypatch.setattr(backlog.os if stage == 'fstat' else backlog.fcntl,
                        stage, failed)
    with pytest.raises(error_type) as caught:
        backlog._lock(tmp_path / 'synthetic.lock')
    assert caught.value is failure
    assert_closed(opened, fstat)


def test_busy_lock_still_returns_none_and_closes_fd(tmp_path, monkeypatch):
    opened, fstat, _flags = capture_open(monkeypatch)
    def busy(*args):
        raise BlockingIOError(errno.EAGAIN, 'synthetic busy')
    monkeypatch.setattr(backlog.fcntl, 'flock', busy)
    assert backlog._lock(tmp_path / 'synthetic.lock') is None
    assert_closed(opened, fstat)


def test_successful_lock_transfers_fd_to_caller(tmp_path, monkeypatch):
    opened, fstat, flags = capture_open(monkeypatch)
    fd = backlog._lock(tmp_path / 'synthetic.lock')
    try:
        assert opened == [fd] and fstat(fd).st_nlink == 1
        assert flags[0] & os.O_NONBLOCK
    finally:
        os.close(fd)
    assert_closed(opened, fstat)


def test_slot_scan_failure_closes_admission_and_every_open_slot(tmp_path, monkeypatch):
    opened, fstat, _flags = capture_open(monkeypatch)
    real_flock = backlog.fcntl.flock
    calls = []
    failure = OSError('synthetic slot scan failure')
    def flock(fd, operation):
        calls.append(fd)
        if len(calls) == 3:
            raise failure
        return real_flock(fd, operation)
    monkeypatch.setattr(backlog.fcntl, 'flock', flock)
    with pytest.raises(OSError) as caught:
        with backlog.request_slot(tmp_path, 2):
            pytest.fail('failed scan cannot admit a request')
    assert caught.value is failure and len(opened) == 3
    assert_closed(opened, fstat)
