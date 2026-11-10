"""External Git diagnostics must not enter user-facing updater errors."""
import update_apply
import update_cli
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import owned_process
import update_discovery
import subprocess

import pytest

import mcs_update


def test_git_failure_does_not_echo_untrusted_stderr(monkeypatch):
    canary = 'SYNTHETIC_PRIVATE_GIT_DIAGNOSTIC'
    monkeypatch.setattr(mcs_update, '_git', lambda args, timeout: subprocess.CompletedProcess(
        args, 128, '', canary))
    monkeypatch.setattr(update_apply, '_git', mcs_update._git)
    monkeypatch.setattr(update_cli, '_git', mcs_update._git)
    monkeypatch.setattr(update_planning, '_git', mcs_update._git)
    monkeypatch.setattr(update_preflight, '_git', mcs_update._git)
    monkeypatch.setattr(update_recovery, '_git', mcs_update._git)
    monkeypatch.setattr(update_repository, '_git', mcs_update._git)
    monkeypatch.setattr(update_restore, '_git', mcs_update._git)
    monkeypatch.setattr(update_runtime_migration, '_git', mcs_update._git)
    with pytest.raises(mcs_update.UpdateError) as error:
        mcs_update._git_out(['fetch', 'origin'])
    assert 'git_failed' in str(error.value)
    assert 'fetch' in str(error.value)
    assert canary not in str(error.value)


def test_git_wrapper_redacts_failed_process_diagnostics(monkeypatch):
    canary = 'SYNTHETIC_PRIVATE_GIT_DIAGNOSTIC'
    # the owned-session runner is the process boundary of _git
    monkeypatch.setattr(owned_process, 'run_owned',
                        lambda args, timeout, env, stdin=None:
                        subprocess.CompletedProcess(args, 128, '', canary))
    result = mcs_update._git(['merge', '--ff-only', 'synthetic'])
    assert result.returncode == 128
    assert canary not in result.stderr
    with pytest.raises(update_discovery.DiscoveryError) as error:
        update_discovery.detect_latest(checkout=mcs_update.REPO, environment=mcs_update.os.environ)
    assert 'ls-remote' in str(error.value)
    assert canary not in str(error.value)


@pytest.mark.parametrize('canary', [
    'SYNTHETIC_PRIVATE_SPAWN /synthetic/private/path token=synthetic',
    '合成の非公開起動情報\n合成の資格情報',
])
def test_git_spawn_failure_uses_fixed_code_at_both_entry_points(monkeypatch, canary):
    def refused(*args, **kwargs):
        raise OSError(canary)

    monkeypatch.setattr(owned_process, 'run_owned', refused)
    with pytest.raises(update_discovery.DiscoveryError) as discovery:
        update_discovery.git(['status'], checkout='/synthetic/repo', environment={})
    assert str(discovery.value) == 'git_spawn_failed'
    with pytest.raises(mcs_update.UpdateError) as updater:
        mcs_update._git(['status'])
    assert str(updater.value) == 'git_spawn_failed'
    assert canary not in str(discovery.value) + str(updater.value)
