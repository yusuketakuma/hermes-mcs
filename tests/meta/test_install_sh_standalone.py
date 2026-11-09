"""install.sh runtime selection: --mode standalone installs no Hermes,
an existing standalone config keeps its mode, fresh non-interactive installs select standalone."""
import json
import shutil

import pytest

from test_install_sh import ROOT, _calls, _run, _world


def _hide_tools(stub, env, *names):
    """Hide selected tools even when the machine has /usr/bin/git."""
    system = stub / 'system-bin'
    system.mkdir(exist_ok=True)
    for name in ('sh', 'dirname', 'stat', 'readlink', 'mktemp', 'cp', 'chmod',
                 'rm', 'awk', 'shasum', 'sha256sum', 'tr', 'sed', 'grep', 'cat',
                 'cmp', 'mkdir', 'basename', 'lockf', 'flock', 'wc', 'mv', 'touch'):
        executable = shutil.which(name)
        if executable:
            (system / name).symlink_to(executable)
    for name in names:
        (stub / 'bin' / name).rename(stub / ('hidden-' + name))
    env['PATH'] = str(stub / 'bin') + ':' + str(system)


@pytest.mark.parametrize('missing', ['uv', 'git'])
@pytest.mark.parametrize('mode', ['--preflight', '--dry-run', 'install'])
def test_standalone_ocr_missing_tools_refuse_before_fresh_state(tmp_path, missing, mode):
    home, _, stub, env = _world(tmp_path)
    _hide_tools(stub, env, missing)
    flags = [] if mode == 'install' else [mode]
    result = _run(env, None, '--mode', 'standalone', '--no-brew', '--no-llm',
                  '--no-recovery', '--with-yomitoku', *flags)
    assert result.returncode != 0, result.stdout + result.stderr
    assert missing in result.stdout + result.stderr
    assert list(home.iterdir()) == []
    assert not any('mcs_setup.py services' in call or call.startswith(('uv ', 'venv-python '))
                   for call in _calls(stub))


@pytest.mark.parametrize('missing', [('git',), ('uv', 'git')])
def test_standalone_without_ocr_does_not_require_uv_or_git(tmp_path, missing):
    home, _, stub, env = _world(tmp_path)
    _hide_tools(stub, env, *missing)
    result = _run(env, None, '--preflight', '--mode', 'standalone', '--no-brew',
                  '--no-llm', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(home.iterdir()) == []


@pytest.mark.parametrize('with_uv', [True, False])
def test_empty_home_standalone_installs_and_checks_sdk_with_uv_or_pip(tmp_path, with_uv):
    home, _, stub, env = _world(tmp_path)
    if not with_uv:
        _hide_tools(stub, env, 'uv')
    flags = ('--mode', 'standalone', '--no-brew', '--no-llm', '--no-recovery')
    before = _run(env, None, '--preflight', *flags)
    assert before.returncode == 0, before.stdout + before.stderr
    assert list(home.iterdir()) == []
    result = _run(env, None, *flags)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(stub)
    install = next(i for i, call in enumerate(calls)
                   if ('uv pip install' if with_uv else '-m pip install -r') in call)
    checked = calls.index('sdk-metadata pinned')
    services = next(i for i, call in enumerate(calls) if 'mcs_setup.py services' in call)
    assert install < checked < services
    assert not (home / '.hermes').exists()
    assert (home / '.mcs/venv/.mcs-connector-requirements').read_bytes() == (
        ROOT / 'deployment/requirements-standalone.txt').read_bytes()
    assert not (home / '.mcs/venv/.mcs-pip-incomplete').exists()


@pytest.mark.parametrize('missing', ['uv', 'git'])
def test_standalone_ocr_tools_may_be_supplied_by_brew_before_use(tmp_path, missing):
    _, _, stub, env = _world(tmp_path)
    _hide_tools(stub, env, missing)
    env['STUB_MISSING_OCR_TOOL'] = missing
    (stub / 'bin/brew').write_text('''#!/bin/sh
echo "brew $*" >> "$STUB_LOG"
if [ "$1" = list ] && [ "$3" = "$STUB_MISSING_OCR_TOOL" ]; then exit 1; fi
if [ "$1" = install ] && [ "$2" = "$STUB_MISSING_OCR_TOOL" ]; then
    cp "$STUB_ROOT/hidden-$2" "$STUB_ROOT/bin/$2"
fi
exit 0
''')
    result = _run(env, None, '--preflight', '--mode', 'standalone', '--no-llm',
                  '--no-recovery', '--with-yomitoku')
    assert result.returncode == 0, result.stdout + result.stderr
    result = _run(env, None, '--mode', 'standalone', '--no-llm', '--no-recovery', '--with-yomitoku')
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(stub)
    installed = calls.index('brew install ' + missing)
    ocr = next(i for i, call in enumerate(calls) if 'scripts/setup_yomitoku.py' in call)
    assert installed < ocr


@pytest.mark.parametrize('fault', ['', 'missing:slack-bolt', 'missing:yarl',
                                  'mismatch:slack-sdk', 'mismatch:yarl'])
def test_matching_sdk_marker_is_checked_and_only_drift_is_reinstalled(tmp_path, fault):
    home, _, stub, env = _world(tmp_path)
    flags = ('--mode', 'standalone', '--no-brew', '--no-llm', '--no-recovery')
    first = _run(env, None, *flags)
    assert first.returncode == 0, first.stdout + first.stderr
    marker = home / '.mcs/venv/.mcs-connector-requirements'
    pinned = (ROOT / 'deployment/requirements-standalone.txt').read_bytes()
    assert marker.read_bytes() == pinned
    (stub / 'state/sdk-fault').write_text(fault)
    start = len(_calls(stub))
    result = _run(env, None, *flags)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(stub)[start:]
    assert any(call.startswith('uv pip install') for call in calls) == bool(fault)
    probes = [call for call in calls if call.startswith('sdk-metadata ')]
    assert probes == (['sdk-metadata ' + fault, 'sdk-metadata pinned'] if fault else ['sdk-metadata pinned'])
    assert marker.read_bytes() == pinned
    assert not (marker.parent / '.mcs-pip-incomplete').exists()


@pytest.mark.parametrize('fault', ['missing:slack-bolt', 'mismatch:yarl'])
def test_sdk_install_without_verified_pins_keeps_fresh_hold_and_incomplete_marker(tmp_path, fault):
    home, _, stub, env = _world(tmp_path)
    result = _run({**env, 'STUB_SDK_POST_FAULT': fault}, None, '--mode', 'standalone',
                  '--no-brew', '--no-llm', '--no-recovery')
    assert result.returncode != 0
    assert 'SDK の固定版を確認できません' in result.stderr
    venv = home / '.mcs/venv'
    assert (venv / '.mcs-pip-incomplete').is_file()
    assert not (venv / '.mcs-connector-requirements').exists()
    assert (home / '.mcs/data/.provider-bootstrap-fresh.json').is_file()
    assert not (home / '.mcs/config.json').exists()
    assert not any('mcs_setup.py services' in call for call in _calls(stub))


def test_failed_sdk_repair_retains_marker_and_retries_without_services(tmp_path):
    home, _, stub, env = _world(tmp_path)
    flags = ('--mode', 'standalone', '--no-brew', '--no-llm', '--no-recovery')
    first = _run(env, None, *flags)
    assert first.returncode == 0, first.stdout + first.stderr
    marker = home / '.mcs/venv/.mcs-connector-requirements'
    before = marker.read_bytes()
    (stub / 'state/sdk-fault').write_text('missing:yarl')
    start = len(_calls(stub))
    failed = _run({**env, 'STUB_FAIL_PIP': '1'}, None, *flags)
    assert failed.returncode != 0
    assert marker.read_bytes() == before
    assert (marker.parent / '.mcs-pip-incomplete').is_file()
    assert not any('mcs_setup.py services' in call for call in _calls(stub)[start:])
    retried = _run(env, None, *flags)
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert marker.read_bytes() == before
    assert not (marker.parent / '.mcs-pip-incomplete').exists()


def test_fresh_standalone_without_hermes_bootstraps_before_services(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    result = _run(env, None, '--mode', 'standalone', '--no-llm', '--no-brew', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not hermes_home.exists()
    root = home / '.mcs'
    assert json.loads((root / 'config.json').read_text())['runtime_mode'] == 'standalone'
    for provider in ('anthropic', 'openai', 'jev'):
        assert json.loads((root / 'data' / 'provider_settings' / (provider + '.json')).read_text()) == {}
    assert (root / 'data' / '.provider-settings-authority.json').is_file()
    calls = _calls(stub_root)
    bootstrap = next(i for i, call in enumerate(calls) if call == 'fresh-runner bootstrap-fresh')
    services = next(i for i, call in enumerate(calls) if 'mcs_setup.py services' in call)
    assert bootstrap < services
    assert not any('venv-hermes' in call for call in calls)


def test_fresh_standalone_holds_if_existing_hermes_cron_is_unknown(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    executable = hermes_home / 'hermes-agent' / 'venv' / 'bin' / 'hermes'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "synthetic-hermes %s\\n" "$*" >> "$STUB_LOG"\nexit 7\n')
    executable.chmod(0o755)
    result = _run(env, None, '--mode', 'standalone', '--no-llm', '--no-brew', '--no-recovery')
    assert result.returncode != 0
    calls = _calls(stub_root)
    assert 'synthetic-hermes cron list --all' in calls
    assert not any('mcs_setup.py services' in call for call in calls)
    assert not any(call.startswith('launchctl bootstrap') for call in calls)
    root = home / '.mcs'
    assert (root / 'data' / '.provider-bootstrap-fresh.json').is_file()
    assert not (root / 'data' / '.provider-settings-authority.json').exists()


def test_standalone_preflight_and_plan_skip_hermes(tmp_path):
    home, _, stub_root, env = _world(tmp_path)
    r = _run(env, None, "--preflight", "--mode", "standalone")
    assert r.returncode == 0, r.stdout
    assert "接続方法: standalone" in r.stdout
    assert "Hermes 用 Python" not in r.stdout and "Hermes コマンド" not in r.stdout
    r = _run(env, None, "--dry-run", "--no-llm", "--mode", "standalone")
    assert r.returncode == 0, r.stdout
    assert "2/6 独立接続" in r.stdout and "2/6 Hermes" not in r.stdout
    assert "MCS 単独接続では使用しません" in r.stdout.split("3/6 プラグイン")[1].splitlines()[0]
    assert "--no-plugin" not in r.stdout
    assert list(home.iterdir()) == []


def test_mode_defaults_follow_existing_config(tmp_path):
    home, _, _, env = _world(tmp_path)
    r = _run(env, None, "--preflight")              # no tty, no config -> standalone
    assert "接続方法: standalone" in r.stdout
    (home / ".mcs").mkdir()
    (home / ".mcs" / "config.json").write_text(json.dumps({"runtime_mode": "standalone"}))
    r = _run(env, None, "--preflight")
    assert "接続方法: standalone" in r.stdout
    assert _run(env, None, "--preflight", "--mode", "bogus").returncode == 2


def test_standalone_install_never_clones_hermes(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, None, "--mode", "standalone", "--no-llm", "--no-brew")
    calls = _calls(stub_root)
    assert not any("hermes-agent" in c and c.startswith("git clone") for c in calls), calls
    assert not hermes_home.exists()
    assert r.returncode == 0, r.stdout + r.stderr
    venv = str(home / ".mcs" / "venv")
    assert any(c.startswith("uv pip install") and "requirements-standalone.txt" in c
               for c in calls), calls
    python = [c for c in calls if c.startswith("venv-python")]
    # The venv receives the selected mode, the empty recovery option, the
    # initial/explicit flags, and the verified recovery Python in that order.
    assert (f"venv-python - {ROOT} standalone  1 1 "
            f"{stub_root / 'bin' / 'python3.14'}") in python
    assert any("mcs_setup.py services" in c for c in python)
    assert (home / ".mcs" / "venv" / "bin" / "python3").exists() and venv


@pytest.mark.parametrize(("setup_answer", "setup_status"), [
    (b"1\n", 0), (b"2\n", 0), (b"0\n", 0), (b"1\n", 1), (b"1\n", 130)])
def test_interactive_install_asks_for_the_runtime(tmp_path, setup_answer, setup_status):
    import os
    import selectors
    import shutil
    import subprocess
    import sys
    import time
    import pytest
    if sys.platform != "darwin" or not shutil.which("script"):
        pytest.skip("BSD script(1) gives the installer a terminal")
    from test_install_sh import INSTALL
    home, hermes_home, stub_root, env = _world(tmp_path)
    env["STUB_SETUP_EXIT"] = str(setup_status)
    env["STUB_REAL_PYTHON"] = sys.executable
    # The pipe retains the exact prompt until subscribed; answer only on that signal.
    with subprocess.Popen(
        ["script", "-q", "/dev/null", "sh", str(INSTALL), "--no-llm", "--no-brew"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    ) as process, selectors.DefaultSelector() as ready:
        assert process.stdout is not None and process.stdin is not None
        ready.register(process.stdout, selectors.EVENT_READ)
        output = b""
        # One hang guard for the whole installer run, like the other installer
        # runs in this file: no prompt has a latency contract, and preflight
        # work before the first prompt slows down under suite load.
        deadline = time.monotonic() + 120
        try:
            for prompt, answer in [
                ("選択 [1・推奨 / 2 / 0・中止]: ", b"wrong\n"),
                ("1 または 2 を入力してください。中止は 0 です。", b"1\n"),
                ("この内容で導入する（推奨）", b"1\n"),
                ("続けて初期設定を始める（推奨）", setup_answer),
            ]:
                signal = prompt.encode()
                while signal not in output:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and ready.select(remaining), output
                    chunk = os.read(process.stdout.fileno(), 4096)
                    assert chunk and len(output) + len(chunk) <= 65536, output
                    output += chunk
                process.stdin.write(answer)
                process.stdin.flush()
            # Keep BSD script's input open until the last answer has been consumed.
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0 and ready.select(remaining), output
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output += chunk
            process.wait(timeout=max(0, deadline - time.monotonic()))
        finally:
            if process.poll() is None:
                process.kill()
        assert process.returncode == setup_status, output
    assert "【2/6】MCS 単独接続の実行環境".encode() in output, output
    assert not hermes_home.exists()
    if setup_status:
        assert "初期設定は完了していません".encode() in output
        assert "導入が途中で停止しました".encode() not in output
    assert "LINE WORKS を選んだ場合".encode() in output
    assert "自動実行の登録を省略した場合".encode() in output
    assert any("mcs_setup.py init" in call for call in _calls(stub_root)) == (setup_answer == b"1\n")
    config = home / ".mcs/config.json"
    saved = config.read_bytes()
    assert json.loads(saved)["runtime_mode"] == "standalone"
    assert config.stat().st_mode & 0o777 == 0o600
    rerun = _run(env, None, "--preflight", "--no-llm", "--no-brew")
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "接続方法: standalone" in rerun.stdout
    assert config.read_bytes() == saved


def test_rerun_on_an_existing_install_never_prompts_and_keeps_settings(tmp_path):
    import shutil
    import subprocess
    import sys
    import pytest
    if sys.platform != "darwin" or not shutil.which("script"):
        pytest.skip("BSD script(1) gives the installer a terminal")
    from test_install_sh import INSTALL
    home, hermes_home, stub_root, env = _world(tmp_path)
    (home / ".mcs").mkdir()
    cfg = home / ".mcs" / "config.json"
    cfg.write_text('{"mcs_login_id": "synthetic"}\n')
    r = subprocess.run(["script", "-q", "/dev/null", "sh", str(INSTALL), "--preflight"],
                       stdin=subprocess.DEVNULL, env=env, capture_output=True, text=True, timeout=120)
    assert "選択 [1・推奨" not in r.stdout and "接続方法: hermes" in r.stdout
    assert cfg.read_text() == '{"mcs_login_id": "synthetic"}\n'
    r = subprocess.run(["sh", str(INSTALL), "--no-llm", "--no-brew"], env=env,
                       capture_output=True, text=True, timeout=120)
    assert json.loads(cfg.read_text()) == {'mcs_login_id': 'synthetic',
                                          'install_args': ['--no-brew', '--no-llm']}
    assert not any(c.startswith("venv-python - ") for c in _calls(stub_root))


def test_mode_selection_refuses_a_symlinked_config(tmp_path):
    """The --mode writer reads config.json like the recovery check: a symlink is
    refused, not followed and then replaced by a regular file."""
    import sys
    home, _, _, env = _world(tmp_path)
    env["STUB_REAL_PYTHON"] = sys.executable
    (home / ".mcs").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"runtime_mode": "hermes"}))
    (home / ".mcs" / "config.json").symlink_to(elsewhere)
    r = _run(env, None, "--mode", "standalone", "--no-llm", "--no-brew", "--no-recovery")
    assert r.returncode == 1 and "config.json を読み取れない" in r.stderr, r.stdout + r.stderr
    assert (home / ".mcs" / "config.json").is_symlink()
    assert json.loads(elsewhere.read_text()) == {"runtime_mode": "hermes"}


@pytest.mark.parametrize('prior', ['absent', 'empty', 'legacy', 'standalone', 'saved_empty'])
def test_default_selection_preserves_existing_runtime_authority(tmp_path, prior):
    home, _, _, env = _world(tmp_path)
    root = home / '.mcs'
    if prior != 'absent':
        root.mkdir()
        config = {'legacy': {'mcs_login_id': 'synthetic'},
                  'standalone': {'runtime_mode': 'standalone'}}.get(prior, {})
        (root / 'config.json').write_text(json.dumps(config))
        if prior == 'saved_empty':
            (root / 'data').mkdir()
            (root / 'data/install_options.json').write_text('[]')
    result = _run(env, None, '--preflight', '--no-recovery')
    expected = 'hermes' if prior in ('legacy', 'saved_empty') else 'standalone'
    assert f'接続方法: {expected}' in result.stdout, result.stdout + result.stderr
    if prior != 'absent':
        assert json.loads((root / 'config.json').read_text()) == config


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('explicit', [None, 'hermes', 'standalone'])
def test_new_noninteractive_mode_is_saved_and_reread(tmp_path, empty, explicit):
    home, _, stub, env = _world(tmp_path)
    config = home / '.mcs/config.json'
    if empty:
        config.parent.mkdir()
        config.write_text('{}')
    flags = ['--no-brew', '--no-llm']
    if explicit:
        flags += ['--mode', explicit]
    result = _run(env, None, *flags)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = explicit or 'standalone'
    assert json.loads(config.read_text())['runtime_mode'] == expected
    before = config.read_bytes()
    again = _run(env, None, '--preflight', '--no-llm', '--no-brew')
    assert f'接続方法: {expected}' in again.stdout
    assert config.read_bytes() == before
    if expected == 'standalone':
        assert not any(c.startswith('git clone') and 'hermes-agent' in c for c in _calls(stub))


@pytest.mark.parametrize('mode', ['--preflight', '--dry-run', None])
def test_empty_bootstrap_keeps_legacy_runtime_in_plan_and_noninteractive_install(tmp_path, mode):
    import hashlib
    import provider_settings
    home, _, _, env = _world(tmp_path)
    root = home / '.mcs'
    data = root / 'data'
    providers = data / 'provider_settings'
    providers.mkdir(mode=0o700, parents=True)
    data.chmod(0o700)
    config = root / 'config.json'
    config.write_text('{}')
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
        'common_policy_sha256': provider_settings.common_policy_digest({})}))
    authority.chmod(0o600)
    before = {path: path.read_bytes() for path in [config, authority, *providers.iterdir()]}
    flags = ['--no-brew', '--no-llm', '--no-recovery', '--no-services']
    if mode:
        flags.append(mode)
    result = _run(env, None, *flags)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '接続方法: hermes' in result.stdout
    assert {path: path.read_bytes() for path in before} == before
