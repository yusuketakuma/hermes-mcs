"""Timed-out update children are reaped before journal outcome is resolved."""
import subprocess

import mcs_update
import pytest


@pytest.mark.parametrize("durable_done", [False, True])
def test_post_merge_timeout_reaps_child_before_reading_done(monkeypatch, durable_done):
    calls, killed = [], []

    class Child:
        pid = 1234
        returncode = None

        def communicate(self, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("synthetic-update", timeout)
            self.returncode = -9
            return "", ""

    monkeypatch.setattr(mcs_update.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(mcs_update.os, "killpg", lambda pid, signal: killed.append((pid, signal)))
    monkeypatch.setattr(mcs_update, "load_config", lambda: {})
    state = {"stages": [{"stage": "done"}] if durable_done else [], "applying": None}
    monkeypatch.setattr(mcs_update, "load_state", lambda: state)
    if durable_done:
        mcs_update._run_post_merge("a" * 40)
    else:
        with pytest.raises(mcs_update.UpdateError, match="post_merge_timeout"):
            mcs_update._run_post_merge("a" * 40)
    assert killed == [(1234, 9)]
    assert len(calls) == 2


@pytest.mark.parametrize("operation", ["post_merge", "reinstall"])
@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("cleanup_fault", [None, "kill_missing", "second_interrupt", "pipe_error", "already_exited"])
def test_interrupted_update_reaps_before_propagating_original_failure(
        monkeypatch, operation, failure_type, cleanup_fault):
    failure = failure_type("synthetic interrupted wait")
    calls = []

    class Child:
        pid = 1234
        returncode = None

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if len([call for call in calls if call[0] == "communicate"]) == 1:
                if cleanup_fault == "already_exited":
                    self.returncode = 0
                raise failure
            assert timeout == 5
            if cleanup_fault == "second_interrupt":
                raise KeyboardInterrupt("synthetic cleanup interruption")
            if cleanup_fault == "pipe_error":
                raise UnicodeError("synthetic pipe decode error")
            self.returncode = -9
            return "", ""

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            assert timeout == 5
            self.returncode = -9
            return self.returncode

    def start(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return Child()

    def stop(pid, signum):
        calls.append(("killpg", pid, signum))
        if cleanup_fault == "kill_missing":
            raise ProcessLookupError("synthetic child already gone")

    monkeypatch.setattr(mcs_update.subprocess, "Popen", start)
    monkeypatch.setattr(mcs_update.os, "killpg", stop)
    monkeypatch.setattr(mcs_update, "load_state", lambda: {"applying": {}})
    applying = {"reinstall": True}
    with pytest.raises(failure_type) as caught:
        if operation == "post_merge":
            mcs_update._run_post_merge("a" * 40)
        else:
            mcs_update._reinstall(applying)
    assert caught.value is failure
    assert "reinstall_done" not in applying
    killed = [call for call in calls if call[0] == "killpg"]
    assert killed == ([] if cleanup_fault == "already_exited" else [("killpg", 1234, 9)])
    assert len([call for call in calls if call[0] == "communicate"]) == 2
    if cleanup_fault in ("second_interrupt", "pipe_error"):
        assert calls[-1] == ("wait", 5)


def test_reinstall_timeout_cleanup_errors_do_not_replace_timeout(monkeypatch):
    class Child:
        pid = 1234
        returncode = None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("synthetic-install", timeout)

        def wait(self, timeout=None):
            assert timeout == 5
            raise KeyboardInterrupt("synthetic second interruption")

    monkeypatch.setattr(mcs_update.subprocess, "Popen", lambda *a, **kw: Child())
    monkeypatch.setattr(mcs_update.os, "killpg", lambda *a: (_ for _ in ()).throw(OSError("synthetic failure")))
    with pytest.raises(mcs_update.UpdateError, match="install_failed: timeout"):
        mcs_update._reinstall({"reinstall": True})
