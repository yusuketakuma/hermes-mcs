"""Synthetic install sh authority contracts; shared helpers preserve isolated runtime state."""
import os
import json
import subprocess
import pytest
from install_sh_testkit import INSTALL as INSTALL, _calls as _calls, _run as _run, _run_reinstall as _run_reinstall, _world as _world


@pytest.mark.parametrize('boundary', ['directory', 'lock'])
@pytest.mark.parametrize('invalid', [
    'extra-field', 'newline', 'tab', 'glob-owner', 'foreign-owner', 'unsafe-mode',
])
def test_install_rejects_invalid_stat_before_software(tmp_path, boundary, invalid):
    home, hermes_home, stub, env = _world(tmp_path)
    data = home / '.mcs/data'
    data.mkdir(parents=True, mode=0o700)
    (data / 'update.lock').touch(mode=0o600)
    uid = str(os.getuid())
    fields = [uid, '700'] if boundary == 'directory' else ['1', '2', uid, '600', '1']
    if invalid == 'extra-field':
        fields.append('unexpected')
    elif invalid == 'glob-owner':
        fields[0 if boundary == 'directory' else 2] = f'[{uid[0]}]{uid[1:]}'
    elif invalid == 'foreign-owner':
        fields[0 if boundary == 'directory' else 2] = str(os.getuid() + 1)
    elif invalid == 'unsafe-mode':
        fields[1 if boundary == 'directory' else 3] = '666'
    output = ('\n' if invalid == 'newline' else '\t' if invalid == 'tab' else ' ').join(fields)
    stat = stub / 'bin/stat'
    stat.write_text('''#!/bin/sh
case "$2" in
  *%i*) boundary=lock ;;
  *) boundary=directory ;;
esac
if [ "$boundary" = "$STUB_STAT_BOUNDARY" ]; then
    printf '%s\\n' "$STUB_STAT_OUTPUT"
else
    exec /usr/bin/stat "$@"
fi
''')
    stat.chmod(0o755)
    # The old unquoted field split expanded this pattern into a valid owner.
    cwd = tmp_path / 'stat-cwd'
    cwd.mkdir()
    (cwd / uid).touch()
    result = _run({**env, 'STUB_STAT_BOUNDARY': boundary, 'STUB_STAT_OUTPUT': output},
                  hermes_home, '--no-llm', cwd=cwd)
    assert result.returncode != 0, result.stdout + result.stderr
    expected = '所有者または書き込み権限' if boundary == 'directory' else '排他制御ファイルの所有者または権限'
    assert expected in result.stderr
    assert _calls(stub) == []
    assert not (home / '.mcs/config.json').exists()


def test_install_rejects_stat_lock_identity_mismatch_before_software(tmp_path):
    home, hermes_home, stub, env = _world(tmp_path)
    data = home / '.mcs/data'
    data.mkdir(parents=True, mode=0o700)
    (data / 'update.lock').touch(mode=0o600)
    stat = stub / 'bin/stat'
    stat.write_text('''#!/bin/sh
case "$2" in
  *%i*)
    case "$3" in /dev/fd/*) inode=3 ;; *) inode=2 ;; esac
    printf '1 %s %s 600 1\\n' "$inode" "$STUB_UID" ;;
  *) exec /usr/bin/stat "$@" ;;
esac
''')
    stat.chmod(0o755)
    result = _run(env, hermes_home, '--no-llm')
    assert result.returncode != 0
    assert '排他制御ファイルが変更されました' in result.stderr
    assert _calls(stub) == []
    assert not (home / '.mcs/config.json').exists()


def test_install_stops_before_software_when_update_lock_is_held(tmp_path):
    import fcntl
    home, hermes_home, stub_root, env = _world(tmp_path)
    data = home / '.mcs/data'
    data.mkdir(parents=True, mode=0o700)
    fd = os.open(data / 'update.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(env, hermes_home, '--no-llm')
        assert result.returncode != 0
        assert '別の導入または更新' in result.stderr
        assert _calls(stub_root) == []
        assert not (home / '.mcs/config.json').exists()
    finally:
        os.close(fd)


def test_install_rejects_symlinked_update_lock(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    data = home / '.mcs/data'
    data.mkdir(parents=True, mode=0o700)
    target = tmp_path / 'synthetic-lock-target'
    target.write_text('keep')
    (data / 'update.lock').symlink_to(target)
    result = _run(env, hermes_home, '--no-llm')
    assert result.returncode != 0
    assert target.read_text() == 'keep'
    assert _calls(stub_root) == []


def test_install_child_keeps_lock_after_parent_is_killed(tmp_path):
    import fcntl
    import time
    home, hermes_home, stub_root, env = _world(tmp_path)
    brew = stub_root / 'bin/brew'
    brew.write_text('''#!/bin/sh
touch "$STUB_STATE/brew-started"
while [ ! -f "$STUB_STATE/release-brew" ]; do /bin/sleep 0.05; done
exit 1
''')
    process = subprocess.Popen(['sh', str(INSTALL), '--no-llm', '--no-recovery', str(hermes_home)],
                               env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    lock_fd = None
    try:
        deadline = time.monotonic() + 20
        while not (stub_root / 'state/brew-started').exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        lock_fd = os.open(home / '.mcs/data/update.lock', os.O_WRONLY)
        process.kill()
        process.wait(timeout=5)
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (stub_root / 'state/release-brew').touch()
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
    finally:
        (stub_root / 'state/release-brew').touch()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if lock_fd is not None:
            os.close(lock_fd)


@pytest.mark.parametrize('record', ['legacy', 'pending'])
def test_recorded_install_requires_python_before_bootstrap(tmp_path, record):
    home, hermes_home, stub_root, env = _world(tmp_path)
    data = home / '.mcs/data'
    data.mkdir(mode=0o700, parents=True)
    path = (data / 'install_options.json' if record == 'legacy'
            else data.parent / '.install-options-migration.pending')
    body = '{"v":1,"install_args":["--no-llm","--no-recovery"]}'
    path.write_text(body)
    for name in ('python3', 'python3.14'):
        (stub_root / 'bin' / name).write_text('#!/bin/sh\nexit 1\n')
    result = _run(env, hermes_home, '--no-llm', '--no-recovery')
    assert result.returncode != 0
    assert path.read_text() == body
    assert not (home / '.mcs/config.json').exists()
    assert not any(line.startswith(('brew ', 'git ', 'uv ')) for line in _calls(stub_root))


def test_manual_reinstall_keeps_interrupted_update_record(tmp_path):
    home, hermes_home, _stub_root, env = _world(tmp_path)
    data = home / '.mcs/data'
    data.mkdir(parents=True, mode=0o700)
    journal = data / 'update_state.json'
    journal.write_text('{"v": 1, "applying": {"reinstall": true}, "stages": []}')
    before = journal.read_bytes()
    result = _run(env, hermes_home, '--no-llm', '--no-brew', '--no-services', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    assert journal.read_bytes() == before


@pytest.mark.parametrize('held', [False, True])
def test_updater_reinstall_requires_the_matching_held_update_lock(tmp_path, held):
    import fcntl
    home, hermes_home, stub_root, env = _world(tmp_path)
    data = home / '.mcs/data'
    data.mkdir(parents=True, mode=0o700)
    fd = os.open(data / 'update.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if held:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run({**env, 'MCS_UPDATE_REINSTALL': '1', 'MCS_UPDATE_LOCK_FD': str(fd)},
                      hermes_home, '--no-llm', '--no-brew', pass_fds=(fd,))
        if held:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0
            assert not any(line.startswith(('brew ', 'git ', 'uv ')) for line in _calls(stub_root))
            assert not (home / '.mcs/config.json').exists()
    finally:
        os.close(fd)


def test_updater_run_keeps_the_operator_opt_out_record(tmp_path):
    """The updater adds migration/repair opt-outs (--no-llm --no-plugin);
    they must not replace what the operator chose."""
    home, hermes_home, _stub_root, env = _world(tmp_path)
    record = home / ".mcs" / "config.json"
    assert _run(env, hermes_home).returncode == 0
    before = json.loads(record.read_text())["install_args"]
    r = _run_reinstall(env, hermes_home,
             "--no-llm", "--no-plugin", "--no-services")
    assert r.returncode == 0, r.stderr
    assert json.loads(record.read_text())["install_args"] == before


def test_updater_migrates_legacy_without_recording_its_temporary_flags(tmp_path):
    home, hermes_home, _stub_root, env = _world(tmp_path)
    config = home / '.mcs/config.json'
    data = home / '.mcs/data'
    data.mkdir(parents=True)
    config.write_text('{"synthetic": {"keep": true}}')
    legacy = data / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-brew"]}')
    original = legacy.read_bytes()
    result = _run_reinstall(env, hermes_home,
                  '--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(config.read_text()) == {'synthetic': {'keep': True}, 'install_args': ['--no-brew']}
    assert legacy.read_bytes() == original


def test_updater_reinstall_refuses_conflicting_records_before_side_effects(tmp_path):
    home, hermes_home, stub, env = _world(tmp_path)
    data = home / '.mcs/data'
    data.mkdir(parents=True)
    config = home / '.mcs/config.json'
    config.write_text('{"install_args": []}')
    legacy = data / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    result = _run_reinstall(env, hermes_home, '--no-llm', '--no-services')
    assert result.returncode != 0
    assert config.read_text() == '{"install_args": []}'
    assert legacy.read_text() == '{"v": 1, "install_args": ["--no-llm"]}'
    assert not any(c.startswith(('brew install', 'git clone', 'uv ', 'launchctl bootstrap')) for c in _calls(stub))


def test_updater_reinstall_checks_approved_base_before_side_effects(tmp_path):
    home, hermes_home, stub, env = _world(tmp_path)
    config = home / '.mcs/config.json'
    config.parent.mkdir()
    config.write_text('{"install_args": ["--no-llm"]}')
    result = _run_reinstall({**env, 'MCS_UPDATE_OPTIONS_BASE': 'empty'},
                  hermes_home, '--no-llm', '--no-services')
    assert result.returncode != 0 and '再導入を開始しません' in result.stderr
    assert config.read_text() == '{"install_args": ["--no-llm"]}'
    assert not any(c.startswith(('brew install', 'git clone', 'uv ', 'launchctl bootstrap')) for c in _calls(stub))


def test_updater_reinstall_reports_change_during_software_preparation(tmp_path):
    home, hermes_home, stub, env = _world(tmp_path)
    config = home / '.mcs/config.json'
    config.parent.mkdir()
    config.write_text('{"install_args": []}')
    git = stub / 'bin/git'
    git.write_text(git.read_text().replace('echo "git $*" >> "$STUB_LOG"',
        'echo "git $*" >> "$STUB_LOG"\n'
        'if [ "$1" = clone ]; then printf \'{"install_args": ["--no-llm"]}\' > "$HOME/.mcs/config.json"; fi'))
    result = _run_reinstall(env, hermes_home,
                  '--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    assert result.returncode != 0 and '途中まで反映' in result.stderr
    assert json.loads(config.read_text()) == {'install_args': ['--no-llm']}


def test_new_install_updates_opt_outs_without_a_legacy_record(tmp_path):
    import cloud_policy
    import config_edit
    import provider_settings
    import semantic_jev

    home, hermes_home, _stub, env = _world(tmp_path)
    first = _run(env, hermes_home, '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    assert first.returncode == 0, first.stderr
    config = home / '.mcs' / 'config.json'
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    # Complete synthetic setup through the checked writer, preserving split authority.
    with config_edit._edit_lock(config) as fd:
        state = provider_settings.read(config.parent, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
        common = {**state['common'], 'mcs_login_id': 'synthetic', 'notify_target': 'local',
                  'unknown_example': {'synthetic': 1}}
        provider_settings.commit_locked(config.parent, state['snapshot'], common, state['providers'],
            selected_provider='openai', model_owners=owners, lock_fd=fd, jev_model=semantic_jev.JEV_MODEL)
    second = _run(env, hermes_home, '--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')
    assert second.returncode == 0, second.stderr
    after = json.loads(config.read_text())
    assert after['install_args'] == ['--no-brew', '--no-llm', '--no-plugin', '--no-recovery']
    assert after['unknown_example'] == {'synthetic': 1}
    assert not (home / '.mcs' / 'data' / 'install_options.json').exists()


def test_conflicting_legacy_record_stops_before_install_side_effects(tmp_path):
    home, hermes_home, stub, env = _world(tmp_path)
    data = home / '.mcs' / 'data'
    data.mkdir(parents=True)
    config = home / '.mcs' / 'config.json'
    config.write_text('{}')
    legacy = data / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    original = legacy.read_bytes()
    result = _run(env, hermes_home, '--no-brew', '--no-llm')
    assert result.returncode != 0
    assert '旧記録' in result.stderr
    assert config.read_text() == '{}'
    assert legacy.read_bytes() == original
    assert not any(c.startswith(('brew install', 'git clone', 'uv ', 'launchctl bootstrap')) for c in _calls(stub))
