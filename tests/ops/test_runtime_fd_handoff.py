"""Synthetic journal/log stream failures close owned descriptors without starting jobs."""
import errno
import os

import pytest

import mcs_repair
import safe_io
import update_journal
from mcs_standalone import runtime


@pytest.mark.parametrize("operation", ["journal", "child_log", "repair_log"])
@pytest.mark.parametrize("fault", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("closed_first", [False, True])
def test_runtime_stream_failure_preserves_the_failure_and_closes_fd(
        tmp_path, monkeypatch, operation, fault, closed_first):
    source = tmp_path / "journal.json"
    source.write_text('{"v":1}')
    tmp_path.chmod(0o700)
    repair_log = tmp_path / "repair.jsonl"
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda *_a, **_k: pytest.fail("no child before stream transfer"))
    failure = fault("synthetic stream failure")
    opened = []

    def refuse(fd, *_args, **_kwargs):
        opened.append(fd)
        if closed_first:
            os.close(fd)
        raise failure

    def invoke():
        if operation == "journal":
            update_journal.read_control_json(source)
        elif operation == "repair_log":
            mcs_repair._log(repair_log, create=True)
        else:
            host.start("extract-0", "background", 1)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(safe_io.os, "fdopen", refuse)
            if operation == "child_log" and fault is OSError:
                invoke()
                assert host.restarting
            else:
                with pytest.raises(fault) as caught:
                    invoke()
                assert caught.value is failure
        assert len(opened) == 1
        with pytest.raises(OSError) as caught:
            os.fstat(opened[0])
        assert caught.value.errno == errno.EBADF
        assert host.children == {}
        assert source.read_text() == '{"v":1}'
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
