"""Queue publication reclaims its own descriptor when stream transfer fails."""
import errno
import os

import pytest

import mcs_requests
import safe_io


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt, SystemExit])
def test_enqueue_pretransfer_failure_closes_owned_fd_and_keeps_queue_empty(tmp_path, monkeypatch, failure):
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    request = {"version": 1, "cmd": "request.create",
               "command_id": "00000000-0000-4000-8000-000000000001",
               "actor": "synthetic operator", "human_confirmed": True,
               "project_id": 1, "source_message_id": 1, "source_hash": "a" * 64,
               "title": "synthetic task", "reason": "synthetic confirmation"}
    error = failure("synthetic stream transfer failure")
    owned = []

    def fail(fd, *args, **kwargs):
        owned.append(fd)
        raise error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(safe_io.os, "fdopen", fail)
            with pytest.raises(failure) as caught:
                mcs_requests.enqueue(request, str(inbox))
        assert caught.value is error
        assert len(owned) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(owned[0])
        assert closed.value.errno == errno.EBADF
        assert list(inbox.glob("*.json")) == []
        assert list(inbox.glob("*.tmp")) == []
        with mcs_requests.queue_lock(str(inbox), wait=False) as acquired:
            assert acquired
    finally:
        for fd in owned:
            try:
                os.close(fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
