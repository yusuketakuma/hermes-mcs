"""Synthetic install sh config contracts; shared helpers preserve isolated runtime state."""
import hashlib
import os
import json
import subprocess
import pytest
from install_sh_testkit import INSTALL as INSTALL, SAVED_FLAGS as SAVED_FLAGS, _calls as _calls, _inject_before_config_persistence as _inject_before_config_persistence, _race_config_writer as _race_config_writer, _real_config_writer as _real_config_writer, _run as _run, _world as _world


@pytest.mark.parametrize('no_services', [True, False])
def test_interactive_no_services_defers_setup_and_preserves_normal_install(tmp_path, no_services):
    import errno
    import pty
    import select
    import time

    _, _, stub, env = _world(tmp_path)
    flags = ['--mode', 'hermes', '--no-brew', '--no-llm', '--no-plugin', '--no-recovery']
    if no_services:
        flags.append('--no-services')
    master, slave = pty.openpty()
    process = subprocess.Popen(['sh', str(INSTALL), *flags],
                               stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    output = b''
    deadline = time.monotonic() + 60
    try:
        # Accept installation and, if offered, the recommended immediate setup.
        os.write(master, b'1\n1\n')
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([master], [], [], remaining)[0], output
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            output += chunk
            assert len(output) < 100000
        assert process.wait(timeout=max(0.1, deadline - time.monotonic())) == 0, output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    calls = _calls(stub)
    for action in ('init', 'services', 'slack-channels'):
        assert sum(f'mcs_setup.py {action}' in call for call in calls) == int(not no_services), calls
    assert not any('gateway' in call or 'conversations.' in call for call in calls)
    if no_services:
        assert '初期設定を始める'.encode() not in output
        assert '--no-services のため、サービスを起動する初期設定は後で行います。'.encode() in output
        assert f"'{env['HOME']}/.local/bin/mcs' setup".encode() in output
    else:
        assert '初期設定を始める'.encode() in output


@pytest.mark.parametrize('existing', [False, True])
def test_config_changed_before_first_lock_stops_before_software(tmp_path, existing):
    home, hermes_home, stub_root, env = _world(tmp_path)
    config = home / '.mcs/config.json'
    if existing:
        config.parent.mkdir(mode=0o700)
        config.write_text('{"runtime_mode":"hermes"}')
        config.chmod(0o600)
    winner = '{"runtime_mode":"standalone"}'
    probe = stub_root / 'runtime-check.py'
    marker = "    case = os.environ.get('STUB_RECOVERY_RUNTIME', 'safe')"
    injection = f'''    os.makedirs({str(config.parent)!r}, mode=0o700, exist_ok=True)
    with open({str(config)!r}, 'w') as stream:
        stream.write({winner!r})
    os.chmod({str(config)!r}, 0o600)
'''
    probe.write_text(probe.read_text().replace(marker, injection + marker))
    result = _run(env, hermes_home, '--no-brew', '--no-llm', '--no-plugin', '--no-services')
    assert result.returncode != 0
    assert '導入前の確認中に設定' in result.stderr
    assert config.read_text() == winner
    assert not any(line.startswith(('brew ', 'git ', 'uv ')) for line in _calls(stub_root))


@pytest.mark.parametrize('provider_trace', [False, True])
def test_existing_data_without_config_keeps_legacy_or_holds_provider_trace(tmp_path, provider_trace):
    home, _, _, env = _world(tmp_path)
    _real_config_writer(env)
    data = home / '.mcs/data'
    data.mkdir(mode=0o700, parents=True)
    preserved = data / 'synthetic-user-data'
    preserved.write_bytes(b'preserve')
    if provider_trace:
        marker = data / '.provider-settings-pending.json'
        marker.write_text('{}')
        marker.chmod(0o600)
    result = _run(env, None, '--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    assert preserved.read_bytes() == b'preserve'
    assert not (data / '.provider-bootstrap-fresh.json').exists()
    if provider_trace:
        assert result.returncode != 0
        assert marker.read_text() == '{}'
        assert not (home / '.mcs/config.json').exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads((home / '.mcs/config.json').read_text()) == {'install_args': SAVED_FLAGS}
        assert not (data / '.provider-settings-authority.json').exists()


def test_noninteractive_install_creates_private_unconfigured_config(tmp_path):
    home, _, _, env = _world(tmp_path)
    _real_config_writer(env)
    flags = ('--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    result = _run(env, None, *flags)
    assert result.returncode == 0, result.stdout + result.stderr
    config = home / '.mcs/config.json'
    assert json.loads(config.read_text()) == {'install_args': SAVED_FLAGS}
    assert config.stat().st_mode & 0o777 == 0o600
    assert 'まだ利用開始前の初期設定が必要' in result.stdout
    before = (config.read_bytes(), config.stat().st_ino, config.stat().st_mtime_ns)
    result = _run(env, None, *flags)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (config.read_bytes(), config.stat().st_ino, config.stat().st_mtime_ns) == before


@pytest.mark.parametrize('runtime', ['hermes', 'standalone'])
def test_explicit_runtime_is_written_into_new_private_config(tmp_path, runtime):
    home, _, _, env = _world(tmp_path)
    _real_config_writer(env)
    result = _run(env, None, '--mode', runtime, '--no-brew', '--no-llm',
                  '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    config = home / '.mcs/config.json'
    assert json.loads(config.read_text()) == {'runtime_mode': runtime, 'install_args': SAVED_FLAGS}
    assert config.stat().st_mode & 0o777 == 0o600


def test_existing_explicit_install_preserves_other_settings(tmp_path):
    home, _, _, env = _world(tmp_path)
    _real_config_writer(env)
    config = home / '.mcs/config.json'
    config.parent.mkdir(mode=0o700)
    initial = {'runtime_mode': 'hermes', 'unknown': {'keep': True}, 'notify_target': 'synthetic'}
    config.write_text(json.dumps(initial))
    result = _run(env, None, '--mode', 'standalone', '--no-brew', '--no-llm',
                  '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(config.read_text()) == {**initial, 'runtime_mode': 'standalone', 'install_args': SAVED_FLAGS}
    assert config.stat().st_mode & 0o777 == 0o600
    data = config.parent / 'data'
    assert data.is_dir()
    assert not (data / '.provider-bootstrap-fresh.json').exists()
    assert not (data / '.provider-bootstrap-complete.json').exists()
    assert not (data / '.provider-settings-authority.json').exists()
    assert not (data / 'provider_settings').exists()


@pytest.mark.parametrize('option', ['mode', 'recovery'])
def test_split_runtime_options_stop_before_software_without_changing_authority(tmp_path, option):
    import provider_settings
    import cloud_policy
    import semantic_jev

    home, _, stub, env = _world(tmp_path)
    root = home / '.mcs'
    data = root / 'data'
    settings = data / 'provider_settings'
    settings.mkdir(mode=0o700, parents=True)
    data.chmod(0o700)
    common = {'install_args': [], 'cloud_llm': {'mode': 'off'}, 'semantic': {'mode': 'off'}}
    config = root / 'config.json'
    config.write_text(json.dumps(common))
    config.chmod(0o600)
    providers = {name: {'model': model} for name, model in cloud_policy.PROVIDERS.items()}
    providers['jev'] = {'model': semantic_jev.JEV_MODEL, 'daily_request_budget': 0}
    hashes = {}
    for name in provider_settings.PROVIDERS:
        path = settings / (name + '.json')
        path.write_text(json.dumps(providers[name]))
        path.chmod(0o600)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    authority = data / '.provider-settings-authority.json'
    authority.write_text(json.dumps({'version': 2, 'state': 'committed', 'files': hashes,
                                     'common_policy_sha256': provider_settings.common_policy_digest(common)}))
    authority.chmod(0o600)
    protected = [config, authority, *(settings / (name + '.json') for name in provider_settings.PROVIDERS)]
    before = {path: path.read_bytes() for path in protected}
    flags = ('--mode', 'standalone') if option == 'mode' else ('--recovery-python', str(stub / 'bin/python3.14'))
    result = _run(env, None, *flags, '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert {path: path.read_bytes() for path in protected} == before
    assert 'fresh-runner runtime-edit-decision' in _calls(stub)
    assert '提供元設定のある環境では' in result.stderr
    assert not any(line.startswith(('brew ', 'git clone', 'uv ', 'launchctl bootstrap')) for line in _calls(stub))
    assert '【1/6】' not in result.stdout


@pytest.mark.parametrize('length', [65536, 65537])
def test_existing_explicit_install_rejects_oversized_update(tmp_path, length):
    home, _, _, env = _world(tmp_path)
    _real_config_writer(env)
    config = home / '.mcs/config.json'
    config.parent.mkdir(mode=0o700)
    prefix, suffix = b'{"padding":"', b'"}'
    raw = prefix + b'x' * (length - len(prefix) - len(suffix)) + suffix
    config.write_bytes(raw)
    result = _run(env, None, '--mode', 'standalone', '--no-brew', '--no-llm',
                  '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert '保存せず中止' in result.stderr
    assert config.read_bytes() == raw


def test_existing_explicit_install_respects_shared_edit_lock(tmp_path):
    import config_edit

    home, _, _, env = _world(tmp_path)
    _real_config_writer(env)
    config = home / '.mcs/config.json'
    config.parent.mkdir(mode=0o700)
    raw = '{"runtime_mode":"hermes","unknown":"kept"}'
    config.write_text(raw)
    with config_edit._edit_lock(config):
        result = _run(env, None, '--mode', 'standalone', '--no-brew', '--no-llm',
                      '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert '別の設定保存が進行中' in result.stderr
    assert config.read_text() == raw


@pytest.mark.parametrize('change', ['edited', 'replaced', 'deleted', 'corrupted', 'symlink'])
def test_existing_explicit_install_refuses_changes_after_initial_read(tmp_path, change):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    config = home / '.mcs/config.json'
    config.parent.mkdir(mode=0o700)
    raw = '{"runtime_mode":"hermes","unknown":{"keep":true}}'
    config.write_text(raw)
    winner = '{"runtime_mode":"hermes","unknown":{"other":true}}'
    action = {
        'edited': f'target.write_text({winner!r})',
        'replaced': "fresh = target.with_name('fresh.json'); fresh.write_text(target.read_text()); fresh.replace(target)",
        'deleted': 'target.unlink()',
        'corrupted': "target.write_text('{broken')",
        'symlink': f"other = target.with_name('other.json'); other.write_text({winner!r}); target.unlink(); target.symlink_to(other)",
    }[change]
    _inject_before_config_persistence(stub, "from pathlib import Path\ntarget = Path(os.environ['HOME']) / '.mcs/config.json'\n" + action)
    result = _run(env, None, '--mode', 'standalone', '--no-brew', '--no-llm',
                  '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert '保存せず中止' in result.stderr
    if change == 'deleted':
        assert not config.exists()
    elif change == 'symlink':
        assert config.is_symlink() and config.read_text() == winner
    else:
        assert config.read_text() == {'edited': winner, 'replaced': raw, 'corrupted': '{broken'}[change]


def test_existing_explicit_install_detects_change_during_publication(tmp_path):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    config = home / '.mcs/config.json'
    config.parent.mkdir(mode=0o700)
    config.write_text('{"runtime_mode":"hermes"}')
    winner = '{"runtime_mode":"hermes","unknown":"kept"}'
    _inject_before_config_persistence(stub, f"""import config_edit
real_snapshot = config_edit._snapshot
calls = []
def raced_snapshot():
    calls.append(1)
    if len(calls) == 2:
        from pathlib import Path
        (Path(os.environ['HOME']) / '.mcs/config.json').write_text({winner!r})
    return real_snapshot()
config_edit._snapshot = raced_snapshot""")
    result = _run(env, None, '--mode', 'standalone', '--no-brew', '--no-llm',
                  '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert '保存せず中止' in result.stderr
    assert config.read_text() == winner


def test_existing_explicit_install_reports_unknown_after_directory_fsync_failure(tmp_path):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    config = home / '.mcs/config.json'
    config.parent.mkdir(mode=0o700)
    config.write_text('{"runtime_mode":"hermes","unknown":"kept"}')
    _inject_before_config_persistence(stub, """import safe_io
def fail_sync(*args, **kwargs):
    raise OSError('synthetic sync failure')
safe_io.fsync_dir = fail_sync""")
    result = _run(env, None, '--mode', 'standalone', '--no-brew', '--no-llm',
                  '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert '保存結果を確認できません' in result.stderr
    assert '保存せず中止' not in result.stderr
    assert json.loads(config.read_text()) == {'runtime_mode': 'standalone', 'unknown': 'kept'}


@pytest.mark.parametrize('mode', ['preflight', 'dry-run'])
def test_config_is_not_created_during_readonly_install_modes(tmp_path, mode):
    home, _, _, env = _world(tmp_path)
    _real_config_writer(env)
    _run(env, None, '--' + mode, '--no-brew', '--no-llm', '--no-plugin',
         '--no-services', '--no-recovery')
    assert not (home / '.mcs/config.json').exists()


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('raw,accepted', [
    ('{"runtime_mode":"hermes","synthetic":{"keep":true}}', True),
    ('{"runtime_mode":"standalone"}', False),
    ('{broken', False),
    ('{"runtime_mode":"hermes","runtime_mode":"standalone"}', False),
    ('{"runtime_mode":"hermes","notify_target":123}', False),
])
def test_config_creation_race_preserves_and_checks_winner(tmp_path, explicit, raw, accepted):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    _race_config_writer(stub, raw)
    flags = (['--mode', 'hermes'] if explicit else []) + [
        '--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery']
    result = _run(env, None, *flags)
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    if accepted:
        assert json.loads((home / '.mcs/config.json').read_text()) == {**json.loads(raw), 'install_args': SAVED_FLAGS}
    else:
        assert (home / '.mcs/config.json').read_text() == raw
        assert '既存設定を保持' in result.stderr


def test_config_creation_race_rejects_hardlinked_winner(tmp_path):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    raw = '{"runtime_mode":"hermes","synthetic":{"keep":true}}'
    _race_config_writer(stub, raw, hardlink=True)
    result = _run(env, None, '--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert (home / '.mcs/config.json').read_text() == raw
    assert (home / 'synthetic-linked-config').read_text() == raw
    assert (home / '.mcs/data/.provider-bootstrap-fresh.json').is_file()
    assert not (home / '.mcs/data/.provider-settings-authority.json').exists()


def test_config_creation_race_never_follows_a_symlink(tmp_path):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    raw = '{"runtime_mode":"hermes"}'
    _race_config_writer(stub, raw, symlink=True)
    result = _run(env, None, '--no-brew', '--no-llm', '--no-plugin',
                  '--no-services', '--no-recovery')
    assert result.returncode != 0
    config = home / '.mcs/config.json'
    assert config.is_symlink()
    assert (home / 'synthetic-target.json').read_text() == raw


@pytest.mark.parametrize('mode', [0o644, 0o660, 0o666])
def test_config_creation_race_rejects_nonprivate_winner(tmp_path, mode):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    raw = '{"runtime_mode":"hermes"}'
    _race_config_writer(stub, raw, mode=mode)
    result = _run(env, None, '--no-brew', '--no-llm', '--no-plugin',
                  '--no-services', '--no-recovery')
    assert result.returncode != 0
    config = home / '.mcs/config.json'
    assert config.read_text() == raw and config.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize('directory', ['home', 'mcs'])
def test_config_creation_rejects_writable_parent_directory(tmp_path, directory):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    root = home / '.mcs'
    if directory == 'mcs':
        root.mkdir(mode=0o700)
    (home if directory == 'home' else root).chmod(0o777)
    result = _run(env, None, '--no-brew', '--no-llm', '--no-plugin',
                  '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert not (root / 'config.json').exists()
    assert not (root / 'data').exists()
    assert _calls(stub) == []
    assert '本人以外に書き込みを許可' in result.stderr
    assert '設定の改変を防ぐため' in result.stderr
    assert '所有者と権限を管理者に確認' in result.stderr


@pytest.mark.parametrize('mode', ['preflight', 'dry-run', 'install'])
def test_oversized_config_is_refused_before_installer_changes(tmp_path, mode):
    from mcs_util import CONFIG_MAX_BYTES

    home, _, stub, env = _world(tmp_path)
    root = home / '.mcs'
    root.mkdir(mode=0o700)
    path = root / 'config.json'
    body = b'{"padding":"' + b'x' * CONFIG_MAX_BYTES + b'"}'
    path.write_bytes(body)
    before = (path.stat().st_ino, path.stat().st_mtime_ns)
    args = ([f'--{mode}'] if mode != 'install' else []) + [
        '--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery']
    result = _run(env, None, *args)
    assert result.returncode != 0
    assert 'config.json を読み取れない' in result.stderr
    assert path.read_bytes() == body
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == before
    assert not any(c.startswith(('uv ', 'git clone', 'launchctl bootstrap')) for c in _calls(stub))


def test_mode_detection_accepts_config_at_read_limit(tmp_path):
    from mcs_util import CONFIG_MAX_BYTES

    home, _, _, env = _world(tmp_path)
    root = home / '.mcs'
    root.mkdir(mode=0o700)
    prefix, suffix = b'{"padding":"', b'"}'
    body = prefix + b'x' * (CONFIG_MAX_BYTES - len(prefix) - len(suffix)) + suffix
    path = root / 'config.json'
    path.write_bytes(body)
    result = _run(env, None, '--preflight', '--no-brew', '--no-llm', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    assert path.read_bytes() == body


def test_config_creation_pins_parent_directory_during_swap(tmp_path):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    _race_config_writer(stub, '{}', swap_parent=True)
    result = _run(env, None, '--no-brew', '--no-llm', '--no-plugin',
                  '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert not (home / 'synthetic-alternate/config.json').exists()
    assert (home / 'synthetic-original/config.json').read_bytes() == b'{}\n'


def test_config_creation_accepts_private_existing_parent_symlink(tmp_path):
    home, _, _, env = _world(tmp_path)
    _real_config_writer(env)
    target = tmp_path / 'synthetic-root'
    target.mkdir(mode=0o700)
    (home / '.mcs').symlink_to(target, target_is_directory=True)
    result = _run(env, None, '--no-brew', '--no-llm', '--no-plugin',
                  '--no-services', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((target / 'config.json').read_text()) == {'install_args': SAVED_FLAGS}


def test_fresh_private_root_link_swap_stops_without_writing_new_target(tmp_path):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    original = tmp_path / 'synthetic-original-root'
    alternate = tmp_path / 'synthetic-alternate-root'
    original.mkdir(mode=0o700)
    alternate.mkdir(mode=0o700)
    alias = home / '.mcs'
    alias.symlink_to(original, target_is_directory=True)
    brew = stub / 'bin' / 'brew'
    brew.write_text("#!/bin/sh\n"
                    "if [ ! -e \"$STUB_STATE/root-swapped\" ]; then\n"
                    "  rm \"$HOME/.mcs\"\n"
                    f"  ln -s '{alternate}' \"$HOME/.mcs\"\n"
                    "  touch \"$STUB_STATE/root-swapped\"\n"
                    "fi\nexit 0\n")
    brew.chmod(0o755)
    result = _run(env, None, '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert 'リンクが変更されました' in result.stderr
    assert (original / 'data' / '.provider-bootstrap-fresh.json').is_file()
    assert not (alternate / 'data').exists()
    assert not (alternate / 'config.json').exists()


def test_config_creation_race_rejects_different_recovery_selection(tmp_path):
    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    selected = str(stub / 'bin/python3.14')
    raw = json.dumps({'runtime_mode': 'hermes', 'recovery_python': '/synthetic/other-python'})
    _race_config_writer(stub, raw)
    result = _run(env, None, '--mode', 'hermes', '--recovery-python', selected,
                  '--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0
    assert (home / '.mcs/config.json').read_text() == raw
    assert '既存設定を保持' in result.stderr


def test_failed_later_install_stage_keeps_created_config_for_retry(tmp_path):
    home, _, _, env = _world(tmp_path)
    _real_config_writer(env)
    flags = ('--no-brew', '--no-llm', '--no-plugin', '--no-recovery')
    result = _run({**env, 'STUB_FAIL_SERVICES': '1'}, None, *flags)
    assert result.returncode != 0
    config = home / '.mcs/config.json'
    assert config.read_bytes() == b'{}\n'
    result = _run(env, None, *flags)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(config.read_text()) == {'install_args': SAVED_FLAGS}


@pytest.mark.parametrize('race', [False, True])
def test_interactive_resume_can_choose_mode_after_noninteractive_failure(tmp_path, race):
    import errno
    import pty
    import select
    import time

    home, hermes_home, stub, env = _world(tmp_path)
    _real_config_writer(env)
    result = _run({**env, 'STUB_FAIL_SERVICES': '1'}, None, '--no-brew', '--no-llm',
                  '--no-plugin', '--no-recovery')
    assert result.returncode != 0
    config = home / '.mcs/config.json'
    assert config.read_bytes() == b'{}\n'
    winner = '{"runtime_mode":"hermes","synthetic":"kept"}'
    baseline_calls = len(_calls(stub))
    master, slave = pty.openpty()
    process = subprocess.Popen(['sh', str(INSTALL), '--no-brew', '--no-llm',
                                '--no-plugin', '--no-services', '--no-recovery'],
                               stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    output = b''
    deadline = time.monotonic() + 60
    try:
        prompts = [('選択 [1・推奨 / 2 / 0'.encode(), b'1\n'),
                   ('この内容で導入する（推奨）'.encode(), b'1\n')]
        for prompt, answer in prompts:
            while prompt not in output:
                remaining = deadline - time.monotonic()
                assert remaining > 0 and select.select([master], [], [], remaining)[0], output
                output += os.read(master, 4096)
                assert len(output) < 100000
            if race and prompt == 'この内容で導入する（推奨）'.encode():
                # The bootstrap snapshot was captured before this confirmation.
                # Replace the real config before the checked CAS is attempted.
                config.write_text(winner)
            os.write(master, answer)
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            if select.select([master], [], [], remaining)[0]:
                try:
                    output += os.read(master, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
        status = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        assert (status != 0) is race, output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    if race:
        assert config.read_text() == winner
        assert not any('mcs_setup.py services' in call for call in _calls(stub)[baseline_calls:])
    else:
        assert json.loads(config.read_text()) == {'runtime_mode': 'standalone', 'install_args': SAVED_FLAGS}
    assert not hermes_home.joinpath('plugins/mcs-discord-commands').exists()


@pytest.mark.parametrize('outcome', ['save', 'cancel', 'race'])
def test_interactive_bootstrap_runtime_choice_uses_checked_snapshot(tmp_path, outcome):
    import errno
    import pty
    import select
    import time
    import provider_settings

    home, _, stub, env = _world(tmp_path)
    _real_config_writer(env)
    root = home / '.mcs'
    data = root / 'data'
    providers = data / 'provider_settings'
    providers.mkdir(mode=0o700, parents=True)
    data.chmod(0o700)
    common = {'install_args': SAVED_FLAGS, 'synthetic_unknown': {'keep': True}}
    config = root / 'config.json'
    config.write_text(json.dumps(common))
    config.chmod(0o600)
    hashes = {}
    for name in provider_settings.PROVIDERS:
        path = providers / (name + '.json')
        path.write_text('{}')
        path.chmod(0o600)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    authority = data / '.provider-settings-authority.json'
    authority.write_text(json.dumps({'version': 2, 'state': 'committed',
        'configuration_state': 'bootstrap', 'files': hashes,
        'common_policy_sha256': provider_settings.common_policy_digest(common)}))
    authority.chmod(0o600)
    before = config.read_bytes()
    master, slave = pty.openpty()
    process = subprocess.Popen(['sh', str(INSTALL), '--no-brew', '--no-llm', '--no-plugin',
                                '--no-services', '--no-recovery'], stdin=slave, stdout=slave,
                               stderr=slave, env=env)
    os.close(slave)
    output = b''
    deadline = time.monotonic() + 60
    def answer_when(prompt, answer):
        nonlocal output
        while prompt not in output:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([master], [], [], remaining)[0], output
            output += os.read(master, 4096)
            assert len(output) < 100000
        os.write(master, answer)
    try:
        answer_when('選択 [1・推奨 / 2 / 0'.encode(), b'1\n')
        if outcome == 'race':
            config.write_text(json.dumps({**common, 'synthetic_unknown': {'keep': False}}))
        answer_when('この内容で導入する（推奨）'.encode(), b'2\n' if outcome == 'cancel' else b'1\n')
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            if select.select([master], [], [], remaining)[0]:
                try:
                    output += os.read(master, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
        status = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        assert (status == 0) is (outcome == 'save'), output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    if outcome == 'save':
        assert json.loads(config.read_text()) == {**common, 'runtime_mode': 'standalone'}
    elif outcome == 'cancel':
        assert config.read_bytes() == before
    else:
        assert json.loads(config.read_text()) == {**common, 'synthetic_unknown': {'keep': False}}
    assert not any('mcs_setup.py services' in call for call in _calls(stub))
