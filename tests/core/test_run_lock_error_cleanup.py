"""Run-lock acquisition errors release the descriptor opened for the attempt."""
import errno
import os
from contextlib import suppress

import pytest

import mcs_util
import mcs_update


@pytest.mark.parametrize('kind', ['run', 'update'])
@pytest.mark.parametrize('failure', [OSError(errno.EIO, 'synthetic_lock_error'), KeyboardInterrupt()])
def test_failed_run_lock_acquisition_closes_descriptor(tmp_path, monkeypatch, failure, kind):
    monkeypatch.setattr(mcs_update, 'UPDATE_LOCK', str(tmp_path / 'synthetic-update.lock'))
    opened = []
    real_open = os.open

    def capture_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail_lock(*args, **kwargs):
        raise failure

    monkeypatch.setattr(mcs_util.os, 'open', capture_open)
    monkeypatch.setattr(mcs_util.fcntl, 'flock', fail_lock)
    try:
        with pytest.raises(type(failure)):
            (mcs_util.acquire_run_lock(str(tmp_path / 'synthetic.lock')) if kind == 'run'
             else mcs_update.acquire_update_lock())
        with pytest.raises(OSError) as error:
            os.fstat(opened[0])
        assert error.value.errno == errno.EBADF
    finally:
        for fd in opened:
            with suppress(OSError):
                os.close(fd)
