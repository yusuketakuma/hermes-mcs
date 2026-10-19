"""Owned rotation descriptors close on failed stream adoption across repeated passes."""
import errno
import os

import pytest

import maintenance
import safe_io


@pytest.mark.parametrize("fault", [OSError, KeyboardInterrupt, SystemExit])
def test_rotation_handoff_failure_releases_each_pass_and_recovers(tmp_path, monkeypatch, fault):
    log = tmp_path / "synthetic.log"
    raw = b"synthetic rotation contents\n"
    log.write_bytes(raw)
    monkeypatch.setattr(maintenance, "LOG_MAX", 1)
    real_open, real_fdopen = open, os.fdopen
    counts = []
    for _window in range(4):
        completed = 0
        for _pass in range(5):
            owned = []
            error = fault("synthetic stream failure")

            def reject_fd(fd, *args, **kwargs):
                owned.append(fd)
                raise error

            def open_file(file, *args, **kwargs):
                if isinstance(file, int):
                    return reject_fd(file, *args, **kwargs)
                return real_open(file, *args, **kwargs)

            try:
                with monkeypatch.context() as scoped:
                    scoped.setattr(maintenance, "open", open_file, raising=False)
                    scoped.setattr(safe_io.os, "fdopen", reject_fd)
                    if fault is OSError:
                        assert maintenance.rotate_log([str(log)]) is None
                    else:
                        with pytest.raises(fault) as caught:
                            maintenance.rotate_log([str(log)])
                        assert caught.value is error
                assert len(owned) == 1
                with pytest.raises(OSError) as caught:
                    os.fstat(owned[0])
                assert caught.value.errno == errno.EBADF
                assert log.read_bytes() == raw
                completed += 1
            finally:
                for fd in owned:
                    try:
                        os.close(fd)
                    except OSError as cleanup:
                        assert cleanup.errno == errno.EBADF
        counts.append(completed)
    assert counts == [5, 5, 5, 5]
    assert os.fdopen is real_fdopen
    maintenance.rotate_log([str(log)])
    assert log.read_bytes() == b""
    assert (tmp_path / "synthetic.log.1").read_bytes() == raw
