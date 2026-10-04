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
