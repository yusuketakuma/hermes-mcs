"""Synthetic mcs update release boundary contracts; shared helpers preserve isolated runtime state."""
from ops_testkit import patch_lifecycle_lookup
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import owned_process
import update_discovery
import release_apply_owner
import os
import subprocess
import time
from pathlib import Path
import pytest
import mcs_update
import mcs_util
from ops_testkit import _git, _make_repo, _mk_schema
from mcs_update_testkit import prepared_run_log as prepared_run_log, updater as updater


@pytest.mark.parametrize('authority', [False, True, 7, 'serialized-token', {}, 'unregistered'])
def test_private_release_capability_refusal_precedes_all_legacy_effects(updater, monkeypatch, authority):
    if authority == 'unregistered':
        authority = release_apply_owner._EffectCapability()
    def forbidden(*_args, **_kwargs):
        pytest.fail('release refusal reached legacy state, locks or cleanup')
    for name in ('acquire_update_lock', '_acquire_run_lock_wait', 'recover_interrupted',
                 '_remove_marker', '_enqueue_notice', '_report', '_apply_runtime_gate_errors'):
        patch_lifecycle_lookup(monkeypatch, updater, name, forbidden)
    monkeypatch.setattr(updater.journal_store, 'load_state', forbidden)
    monkeypatch.setattr(updater.mcs_util, 'load_config', forbidden)
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='EFFECT_HOLD'):
        updater._apply_pipeline(None, '1' * 40, None, _release_cap=authority)


@pytest.mark.parametrize('native', [False, True])
def test_release_post_merge_requires_native_adoption_and_never_calls_legacy(updater, tmp_path, monkeypatch, native):
    saved = {'applying': {'sha': 'a' * 40, 'release_ref': {'synthetic': True}},
             'release_ref': {'synthetic': True}}
    monkeypatch.setattr(updater.journal_store, 'load_state', lambda *_: saved)
    monkeypatch.setenv(updater._UPDATE_PM_ENV, 'a' * 40)
    monkeypatch.delenv(release_apply_owner.CHILD_FDS_ENV, raising=False)
    monkeypatch.setattr(updater, '_call_legacy', lambda *_: pytest.fail('release fell back to legacy'))
    monkeypatch.setattr(update_apply, '_call_legacy', updater._call_legacy)
    monkeypatch.setattr(update_cli, '_call_legacy', updater._call_legacy)
    monkeypatch.setattr(update_lifecycle_context, '_call_legacy', updater._call_legacy)
    calls = []
    monkeypatch.setattr(updater, '_post_merge',
                        lambda value, **kwargs: calls.append((value, kwargs)) or 0)
    monkeypatch.setattr(update_cli, '_post_merge', updater._post_merge)
    if not native:
        assert updater.cmd_post_merge() == 2
        assert calls == []
        return
    fds = [os.open(tmp_path / f'lock-{i}', os.O_CREAT | os.O_RDWR, 0o600) for i in range(3)]
    reader, writer = os.pipe()
    try:
        monkeypatch.setenv(release_apply_owner.CHILD_FDS_ENV, ':'.join(map(str, (reader, *fds))))
        consumed = {**saved, 'applying': {**saved['applying'], 'release_child': {'phase': 'consumed'}}}

        def adopt(home, repo, *, ticket_fd, locks, deadline):
            assert (home, repo) == (updater.HOME, updater.REPO)
            assert ticket_fd == reader and locks == tuple(fds)
            assert time.monotonic() < deadline <= time.monotonic() + 2
            return consumed

        monkeypatch.setattr(release_apply_owner, '_adopt_post_merge', adopt)
        assert updater.cmd_post_merge() == 0
        assert calls == [(consumed, {'native_provider_migration': True})]
        for descriptor in fds:
            os.fstat(descriptor)  # The child scope borrows; it never closes native descriptors.
    finally:
        for descriptor in (*fds, reader, writer):
            os.close(descriptor)


def test_detect_latest_prerelease_excluded(updater, monkeypatch):
    def fake_git(args, timeout=30, **_lif05_paths):
        class R:
            returncode = 0
            stdout = (("a" * 40) + "\trefs/tags/v1.2.0\n"
                      + ("b" * 40) + "\trefs/tags/v1.3.0-rc1\n"
                      + ("c" * 40) + "\trefs/tags/v1.3.0-rc1^{}\n")
            stderr = ""
        return R()
    monkeypatch.setattr(update_discovery, "git", fake_git)
    tag, sha = update_discovery.detect_latest(checkout=mcs_update.REPO, environment=mcs_update.os.environ)
    assert tag == "v1.2.0"
    tag, sha = update_discovery.detect_latest(include_prerelease=True, checkout=mcs_update.REPO, environment=mcs_update.os.environ)
    # opt-in picks the prerelease — and peels it to the commit sha
    assert tag == "v1.3.0-rc1"
    assert sha == "c" * 40


def test_prerelease_numeric_components_follow_semver_order():
    names = ["v1.2.0-rc.2", "v1.2.0-rc.10", "v1.2.0"]
    assert sorted(reversed(names), key=mcs_update.update_policy.version_key) == names
    assert mcs_update.update_policy.version_key(["invalid"]) is None


def test_deferred_update_notice_refetches_release_context(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, 'detect_latest', lambda pre, **_lif05_paths: ("v1.1.0", "a" * 40))
    monkeypatch.setattr(update_discovery, 'current_version', lambda**_lif05_paths: ("v1.0.0", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    monkeypatch.setattr(update_cli, 'scan_pending_approvals', updater.scan_pending_approvals)
    monkeypatch.setattr(updater, "_git", lambda args, **kw:
                        subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", ""))
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    fetched, notices = [], []
    monkeypatch.setattr(updater, "fetch_notes", lambda tag: fetched.append(tag) or "synthetic release notes")
    monkeypatch.setattr(update_cli, 'fetch_notes', updater.fetch_notes)
    monkeypatch.setattr(updater, "impact_summary", lambda current, tag: ["synthetic restart required"])
    monkeypatch.setattr(update_cli, 'impact_summary', updater.impact_summary)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **kw:
                        notices.append(text) or len(notices) > 1)
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    assert updater.cmd_check() == 0
    assert updater.journal_store.load_state(updater.STATE_PATH)["latest_tag"] == "v1.1.0"
    assert updater.journal_store.load_state(updater.STATE_PATH).get("notified_at") != "v1.1.0"
    assert updater.cmd_check() == 0
    assert fetched == ["v1.1.0", "v1.1.0"]
    assert "synthetic release notes" in notices[1]
    assert "synthetic restart required" in notices[1]
    assert updater.journal_store.load_state(updater.STATE_PATH)["notified_at"] == "v1.1.0"
    assert updater.cmd_check() == 0
    assert len(fetched) == 2 and len(notices) == 2


@pytest.mark.parametrize('marker', ['release-owner.json', 'release-owner-activation.json'])
@pytest.mark.parametrize('entry', ['apply', 'rollback', 'recover', 'reinstall_done', 'check'])
def test_release_activation_blocks_legacy_before_journal_or_cleanup(updater, tmp_path, monkeypatch, marker, entry):
    import release_apply_owner
    # pytest can reuse a removed tmp_path; the private census must belong to this test user too.
    user_home = tmp_path / 'synthetic-user-home'
    user_home.mkdir(mode=0o700)
    monkeypatch.setenv('HOME', str(user_home))
    root = release_apply_owner._control_root(updater.HOME)
    with release_apply_owner._control_store(updater.HOME) as store:
        store.write(marker, {'synthetic': True})
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {'sdc': {'state_root': str(root)}, 'update': {'mode': 'notify'}})
    monkeypatch.setattr(updater, '_apply_runtime_gate_errors', lambda *_: [])
    monkeypatch.setattr(update_apply, '_apply_runtime_gate_errors', updater._apply_runtime_gate_errors)
    monkeypatch.setattr(updater, 'journal', lambda *_: pytest.fail('legacy journal forbidden'))
    monkeypatch.setattr(update_apply, 'journal', updater.journal)
    monkeypatch.setattr(update_recovery, 'journal', updater.journal)
    monkeypatch.setattr(update_runtime_migration, 'journal', updater.journal)
    monkeypatch.setattr(updater, '_remove_marker', lambda: pytest.fail('cleanup forbidden'))
    monkeypatch.setattr(update_apply, '_remove_marker', updater._remove_marker)
    monkeypatch.setattr(update_recovery, '_remove_marker', updater._remove_marker)
    monkeypatch.setattr(update_services, '_remove_marker', updater._remove_marker)
    monkeypatch.setattr(updater, '_acquire_run_lock_wait', lambda **_: pytest.fail('legacy run lock forbidden'))
    monkeypatch.setattr(update_apply, '_acquire_run_lock_wait', updater._acquire_run_lock_wait)
    monkeypatch.setattr(update_recovery, '_acquire_run_lock_wait', updater._acquire_run_lock_wait)
    before = Path(updater.STATE_PATH).read_bytes() if Path(updater.STATE_PATH).exists() else None
    if entry == 'apply':
        result = updater.apply('v1.1.0', 'a' * 40, None)
    elif entry == 'rollback':
        result = updater.rollback()
    elif entry == 'reinstall_done':
        result = updater.reinstall_done()
    elif entry == 'check':
        monkeypatch.setattr(update_discovery, 'detect_latest', lambda *_, **__: ('v1.1.0', 'a' * 40))
        monkeypatch.setattr(update_discovery, 'current_version', lambda **_: ('v1.0.0', 'b' * 40))
        monkeypatch.setattr(updater, '_tag_is_newer', lambda *_: True)
        monkeypatch.setattr(update_cli, '_tag_is_newer', updater._tag_is_newer)
        monkeypatch.setattr(updater, '_git', lambda *_, **__: pytest.fail('fetch forbidden'))
        monkeypatch.setattr(update_apply, '_git', updater._git)
        monkeypatch.setattr(update_cli, '_git', updater._git)
        monkeypatch.setattr(update_planning, '_git', updater._git)
        monkeypatch.setattr(update_preflight, '_git', updater._git)
        monkeypatch.setattr(update_recovery, '_git', updater._git)
        monkeypatch.setattr(update_repository, '_git', updater._git)
        monkeypatch.setattr(update_restore, '_git', updater._git)
        monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
        result = updater.cmd_check()
    else:
        result = updater.recover_interrupted()
    assert result == 2
    assert (Path(updater.STATE_PATH).read_bytes() if Path(updater.STATE_PATH).exists() else None) == before


def test_release_reference_and_unsafe_fixed_control_root_are_legacy_holds(updater):
    import release_apply_owner
    assert updater._release_legacy_blocked({'applying': {'release_ref': {}}})
    assert not updater._release_legacy_blocked({})
    with release_apply_owner._control_store(updater.HOME) as store:
        root = store.root
    gate = root / 'release-entry.lock'
    gate.chmod(0o644)
    assert updater._release_legacy_blocked({})
    assert gate.stat().st_mode & 0o777 == 0o644  # No repair by the reader.
    gate.chmod(0o600)
    (root / 'release-owner-activation.json').write_bytes(b'invalid-json')
    assert updater._release_legacy_blocked({})


def test_fixed_release_fence_survives_lost_sdc_config(updater, monkeypatch):
    import release_apply_owner
    with release_apply_owner._control_store(updater.HOME) as store:
        store.write('release-owner-activation.json', {'synthetic': True})
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(updater, '_apply_pipeline', lambda *_a, **_k: pytest.fail('legacy pipeline after config loss'))
    monkeypatch.setattr(update_apply, '_apply_pipeline', updater._apply_pipeline)
    assert updater.apply('v1.1.0', 'a' * 40, None) == 2


@pytest.mark.parametrize('entry', [False, True, 'saved-receipt', {}, None])
def test_post_merge_native_issuer_refuses_unregistered_entry_without_launch(updater, monkeypatch, entry):
    if entry is None:
        entry = release_apply_owner._EffectCapability()
    monkeypatch.setattr(updater.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('native refusal spawned a child'))
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='CHILD_HOLD'):
        updater._run_post_merge('a' * 40, run_lock_fd=10, _release_entry=entry)


def test_static_release_target_checks_keep_tree_and_runtime_refusals_without_running_candidate(updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    work = tmp_path / 'remote-work'
    (work / 'mcs/core').mkdir(parents=True)
    (work / 'mcs/core/ledger.py').write_text('SCHEMA_VERSION = 8\n')
    marker = tmp_path / 'candidate-was-executed'
    (work / 'mcs/mcs_setup.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n')
    (work / 'data').mkdir()
    (work / 'data/synthetic').write_text('synthetic')
    (work / 'link.sh').symlink_to('f.txt')
    (work / 'install.sh').write_text('# synthetic installer\n')
    (work / 'deployment').mkdir()
    (work / 'deployment/sdc-release-lock.json').write_text('{}')
    (work / 'deployment/requirements-standalone.txt').write_text('synthetic')
    _git(work, 'add', '.')
    _git(work, 'commit', '-qm', 'synthetic target')
    target = _git(work, 'rev-parse', 'HEAD').stdout.strip()
    _git(work, 'push', '-q', str(tmp_path / 'remote.git'), 'main')
    _git(repo, 'fetch', '-q', 'origin')
    _mk_schema(tmp_path / 'ledger.db', 8)
    monkeypatch.setattr(updater, 'REPO', str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    monkeypatch.setattr(updater, 'LEDGER', str(tmp_path / 'ledger.db'))
    monkeypatch.setattr(update_apply, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', updater.LEDGER)
    run = updater._run_with_lifetime
    def only_git(argv, **kwargs):
        assert argv[0] == 'git', 'static target checks ran candidate code'
        return run(argv, **kwargs)
    monkeypatch.setattr(updater, '_run_with_lifetime', only_git)
    monkeypatch.setattr(update_lifecycle_context, '_run_with_lifetime', updater._run_with_lifetime)
    monkeypatch.setattr(update_preflight, '_run_with_lifetime', updater._run_with_lifetime)
    monkeypatch.setattr(update_repository, '_run_with_lifetime', updater._run_with_lifetime)
    monkeypatch.setattr(update_runtime_migration, '_run_with_lifetime', updater._run_with_lifetime)
    monkeypatch.setattr(update_services, '_run_with_lifetime', updater._run_with_lifetime)
    errors, entries = updater._precheck_target_tree(target, False)
    assert entries and any(e.startswith('protected_path:') for e in errors)
    assert any(e.startswith('bad_entry_type:') for e in errors)
    assert updater._precheck_target_runtime_files(target, True) == [
        'install_sh_changed', 'release_dependency_lock_changed', 'standalone_requirements_changed']
    assert not marker.exists()


def test_release_static_schema_must_match_the_authenticated_core(updater, tmp_path, monkeypatch):
    monkeypatch.setattr(updater, '_ls_tree_paths', lambda *_a, **_k: [])
    monkeypatch.setattr(update_preflight, '_ls_tree_paths', updater._ls_tree_paths)
    monkeypatch.setattr(update_repository, '_ls_tree_paths', updater._ls_tree_paths)
    monkeypatch.setattr(updater, '_git', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, '', ''))
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    monkeypatch.setattr(updater, '_git_out', lambda argv, **kwargs: 'SCHEMA_VERSION = 9' if argv[0] == 'show' else '')
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    errors, _ = updater._precheck_target_tree('a' * 40, False, deadline=time.monotonic() + 3,
        installed_schema=8, expected_target_schema=8)
    assert 'release_schema_target_mismatch' in errors


def test_release_git_scope_uses_fixed_binary_and_environment_after_quiesce(tmp_path, monkeypatch):
    import os
    import shlex
    import subprocess
    import mcs_update as updater
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'init', '-q'], check=True)
    (repo / 'tracked').write_text('synthetic\n')
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['/usr/bin/git', '-C', str(repo), '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
        'commit', '-qm', 'synthetic'], check=True)
    expected = subprocess.check_output(['/usr/bin/git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'config', 'core.worktree', str(tmp_path / 'foreign')], check=True)
    marker = tmp_path / 'untrusted-git-ran'
    fake = tmp_path / 'bin'
    fake.mkdir()
    executable = fake / 'git'
    executable.write_text('#!/bin/sh\nprintf invoked > ' + shlex.quote(str(marker)) + '\nexit 77\n')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(fake))
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'foreign.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(tmp_path / 'foreign'))
    monkeypatch.setattr(updater, 'REPO', str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    with updater._release_git_scope():
        assert updater._head_sha() == expected
        assert updater._tree_clean()
    assert not marker.exists()
    assert os.environ['PATH'] == str(fake)
    assert updater._RELEASE_GIT_SCOPE.get() is False


@pytest.mark.parametrize(('config', 'legacy', 'expected'), [
    ('{}', None, 'none'),
    ('{}', '{"v": 1, "install_args": []}', 'required'),
    ('{"install_args": []}', '{"v": 1, "install_args": []}', 'none'),
    ('{"install_args": []}', '{"v": 1, "install_args": ["--no-llm"]}', 'unverified'),
    ('{}', '{broken', 'unverified'),
])
def test_release_migration_probe_is_read_only_and_returns_only_fixed_tokens(updater, tmp_path, config, legacy, expected):
    path = Path(mcs_util.CONF_PATH)
    path.write_text(config)
    record = Path(updater.DATA) / 'install_options.json'
    if legacy is not None:
        record.write_text(legacy)
    before = path.read_bytes(), record.read_bytes() if record.exists() else None
    assert updater._release_migration_probe(time.monotonic() + 10) == expected
    assert (path.read_bytes(), record.read_bytes() if record.exists() else None) == before


def test_release_migration_probe_refuses_timeout(updater, monkeypatch):
    def expired(*_a, **_k):
        raise subprocess.TimeoutExpired('synthetic', 1)
    monkeypatch.setattr(owned_process, 'run_owned', expired)
    assert updater._release_migration_probe(time.monotonic() + 10) == 'unverified'
