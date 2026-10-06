"""External Git diagnostics must not enter user-facing updater errors."""
import subprocess

import pytest

import mcs_update


def test_git_failure_does_not_echo_untrusted_stderr(monkeypatch):
    canary = 'SYNTHETIC_PRIVATE_GIT_DIAGNOSTIC'
    monkeypatch.setattr(mcs_update, '_git', lambda args, timeout: subprocess.CompletedProcess(
        args, 128, '', canary))
    with pytest.raises(mcs_update.UpdateError) as error:
        mcs_update._git_out(['fetch', 'origin'])
    assert 'git_failed' in str(error.value)
    assert 'fetch' in str(error.value)
    assert canary not in str(error.value)


def test_git_wrapper_redacts_failed_process_diagnostics(monkeypatch):
    canary = 'SYNTHETIC_PRIVATE_GIT_DIAGNOSTIC'
    monkeypatch.setattr(mcs_update.subprocess, 'run',
                        lambda args, **kw: subprocess.CompletedProcess(args, 128, '', canary))
    result = mcs_update._git(['merge', '--ff-only', 'synthetic'])
    assert result.returncode == 128
    assert canary not in result.stderr
    with pytest.raises(mcs_update.UpdateError) as error:
        mcs_update.detect_latest()
    assert 'ls-remote' in str(error.value)
    assert canary not in str(error.value)
