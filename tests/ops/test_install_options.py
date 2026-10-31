"""Synthetic install option migration and legacy rollback safeguards."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys

import config_edit
import cloud_policy
import install_options
import mcs_util
import provider_settings
import pytest
import safe_io
import semantic_jev


def put(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def test_inspection_bootstraps_with_system_python(tmp_path):
    python = Path('/usr/bin/python3') if Path('/usr/bin/python3').exists() else Path(sys.executable)
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    script = Path(install_options.__file__)
    result = subprocess.run([str(python), str(script), 'inspect', str(config), str(record), '-', '--no-llm'],
                            text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'empty'


@pytest.mark.parametrize("pending", [False, True])
def test_system_python_can_inspect_and_probe_provider_settings(tmp_path, pending):
    python = Path('/usr/bin/python3') if Path('/usr/bin/python3').exists() else Path(sys.executable)
    root = tmp_path / 'synthetic-install'
    root.mkdir(mode=0o700)
    data = root / 'data'
    data.mkdir(mode=0o700)
    config, record = root / 'config.json', data / 'install_options.json'
    put(config, {'other': 'synthetic'})
    if pending:
        put(data / '.provider-settings-pending.json', {'state': 'synthetic'})
    config_before = config.read_bytes()
    code = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import install_options
import provider_settings
root = Path(sys.argv[2])
try:
    inspected = install_options.inspect(root / 'config.json', root / 'data' / 'install_options.json')
except ValueError:
    inspected = 'held'
print(json.dumps({'version': list(sys.version_info[:2]),
                  'inspect': inspected,
                  'probe': provider_settings.probe(root)}))
"""
    result = subprocess.run([str(python), '-B', '-c', code, str(Path(install_options.__file__).parent), str(root)],
                            text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['version'] >= [3, 9]
    assert observed['inspect'] == ('held' if pending else 'empty')
    assert observed['probe'] == {'state': 'pending' if pending else 'legacy_unverified', 'config_present': True}
    assert config.read_bytes() == config_before and not record.exists()
    if pending:
        assert json.loads((data / '.provider-settings-pending.json').read_text()) == {'state': 'synthetic'}


@pytest.mark.parametrize('marker', ['pending', 'authority', 'directory'])
@pytest.mark.parametrize('config_present', [False, True])
def test_provider_preflight_holds_incomplete_settings_without_changes(tmp_path, marker, config_present):
    root = tmp_path / 'synthetic-install'
    root.mkdir(mode=0o700)
    data = root / 'data'
    data.mkdir(mode=0o700)
    config, record = root / 'config.json', data / 'install_options.json'
    if config_present:
        put(config, {'install_args': []})
    if marker == 'directory':
        (data / 'provider_settings').mkdir(mode=0o700)
    else:
        put(data / ('.provider-settings-' + marker + '.json'), {'synthetic': True})
    before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    python = Path('/usr/bin/python3') if Path('/usr/bin/python3').exists() else Path(sys.executable)
    result = subprocess.run([str(python), '-B', str(Path(install_options.__file__)), 'provider-check',
                             str(config), str(record), '-'], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode != 0
    assert sorted(str(path.relative_to(root)) for path in root.rglob('*')) == before
    assert not record.exists()


def test_provider_preflight_accepts_valid_split_with_system_python(tmp_path):
    root = tmp_path / 'synthetic-install'
    root.mkdir(mode=0o700)
    data = root / 'data'
    data.mkdir(mode=0o700)
    settings = data / 'provider_settings'
    settings.mkdir(mode=0o700)
    config, record = root / 'config.json', data / 'install_options.json'
    common = {'install_args': [], 'cloud_llm': {'mode': 'off'}, 'semantic': {'mode': 'off'}}
    put(config, common)
    hashes = {}
    providers = {name: {'model': model} for name, model in cloud_policy.PROVIDERS.items()}
    providers['jev'] = {'model': semantic_jev.JEV_MODEL, 'daily_request_budget': 0}
    for name in provider_settings.PROVIDERS:
        path = settings / (name + '.json')
        put(path, providers[name])
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    put(data / '.provider-settings-authority.json', {'version': 2, 'state': 'committed', 'files': hashes,
        'common_policy_sha256': provider_settings.common_policy_digest(common)})
    python = Path('/usr/bin/python3') if Path('/usr/bin/python3').exists() else Path(sys.executable)
    result = subprocess.run([str(python), '-B', str(Path(install_options.__file__)), 'inspect',
                             str(config), str(record), '-', '--no-llm'], capture_output=True, text=True,
                            timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert not record.exists()


@pytest.mark.parametrize(('central', 'legacy', 'expected'), [
    ({}, None, None),
    ({'install_args': []}, None, []),
    ({}, {'install_args': ['--no-llm']}, ['--no-llm']),
    ({'install_args': []}, {'install_args': []}, []),
    ({'install_args': []}, {'install_args': ['--no-llm']}, None),
    ({'install_args': None}, {'install_args': ['--no-llm']}, None),
    ({'install_args': ['--no-services']}, None, None),
])
def test_recorded_distinguishes_missing_empty_invalid_and_conflict(tmp_path, central, legacy, expected):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, central)
    if legacy is not None:
        put(record, legacy)
    assert install_options.recorded(config, record) == expected


def test_recorded_does_not_fallback_from_unreadable_central(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    config.write_text('{broken')
    put(record, {'install_args': []})
    assert install_options.recorded(config, record) is None
    with pytest.raises(ValueError):
        install_options.inspect(config, record, [])


@pytest.mark.parametrize('body', [
    '{"v": 2, "install_args": []}',
    '{"v": true, "install_args": []}',
    '{"install_args": [], "install_args": ["--no-llm"]}',
    '{"install_args": [], "other": NaN}',
    '{"install_args": [], "other": 1e9999}',
])
def test_ambiguous_or_future_legacy_record_is_not_trusted(tmp_path, body):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'install_args': []})
    record.write_text(body)
    assert install_options.recorded(config, record) is None
    with pytest.raises(ValueError):
        install_options.inspect(config, record, [])
    assert record.read_text() == body


def test_save_updates_new_record_and_keeps_other_settings(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': {'new': 2}})
    base = install_options.inspect(config, record, ['--no-llm'])
    assert install_options.save(config, record, ['--no-llm'], base) == 0
    assert json.loads(config.read_text()) == {'other': {'new': 2}, 'install_args': ['--no-llm']}
    base = install_options.inspect(config, record, ['--no-llm'])
    before = (config.read_bytes(), config.stat().st_ino, config.stat().st_mtime_ns)
    assert install_options.save(config, record, ['--no-llm'], base) == 0
    assert (config.read_bytes(), config.stat().st_ino, config.stat().st_mtime_ns) == before
    base = install_options.inspect(config, record, [])
    assert install_options.save(config, record, [], base) == 0
    assert json.loads(config.read_text()) == {'other': {'new': 2}, 'install_args': []}
    assert not record.exists()


@pytest.mark.parametrize("config_present", [False, True])
def test_initial_noop_does_not_create_or_rewrite_unfinished_config(tmp_path, config_present):
    config, record = tmp_path / "config.json", tmp_path / "install_options.json"
    if config_present:
        put(config, {})
        before = (config.read_bytes(), config.stat().st_ino, config.stat().st_mtime_ns)
    assert install_options.inspect(config, record) == "empty"
    assert install_options.save(config, record, None, "empty") == 0
    if config_present:
        assert (config.read_bytes(), config.stat().st_ino, config.stat().st_mtime_ns) == before
    else:
        assert not config.exists()
    assert not record.exists()


def test_migrates_matching_legacy_without_changing_it(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    put(record, {'v': 1, 'install_args': ['--no-llm']})
    original = record.read_bytes()
    base = install_options.inspect(config, record, ['--no-llm'])
    assert install_options.save(config, record, ['--no-llm'], base) == 0
    assert record.read_bytes() == original
    assert json.loads(config.read_text()) == {'other': 1, 'install_args': ['--no-llm']}
    with pytest.raises(ValueError, match='legacy_rollback_conflict'):
        install_options.inspect(config, record, [])


def test_update_migrates_only_legacy_value_and_keeps_its_bytes(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    put(record, {'v': 1, 'install_args': ['--no-brew']})
    original = record.read_bytes()
    base = install_options.inspect(config, record)
    assert install_options.save(config, record, None, base) == 0
    assert json.loads(config.read_text()) == {'other': 1, 'install_args': ['--no-brew']}
    assert record.read_bytes() == original


def test_save_keeps_other_writer_value_but_rejects_changed_option(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    put(config, {'other': 2})
    assert install_options.save(config, record, [], base) == 0
    assert json.loads(config.read_text()) == {'other': 2, 'install_args': []}
    put(config, {'other': 3, 'install_args': ['--no-brew']})
    assert install_options.save(config, record, [], base) == 1
    assert json.loads(config.read_text()) == {'other': 3, 'install_args': ['--no-brew']}


def test_save_refuses_new_or_changed_legacy_after_inspection(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    put(record, {'install_args': []})
    assert install_options.save(config, record, [], base) == 1
    assert json.loads(config.read_text()) == {'other': 1}
    assert json.loads(record.read_text()) == {'install_args': []}


def test_save_refuses_config_change_before_replace(monkeypatch, tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    original = safe_io.atomic_write
    def raced(path, writer, *args, **kwargs):
        def stage(stream):
            put(config, {'other': 2})
            writer(stream)
        return original(path, stage, *args, **kwargs)
    monkeypatch.setattr(safe_io, 'atomic_write', raced)
    assert install_options.save(config, record, [], base) == 1
    assert json.loads(config.read_text()) == {'other': 2}


@pytest.mark.parametrize(('exception', 'status'), [(OSError, 1), (KeyboardInterrupt, 130)])
def test_save_reports_fsync_uncertainty(monkeypatch, tmp_path, capsys, exception, status):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    monkeypatch.setattr(safe_io, 'fsync_dir', lambda *_a, **_k: (_ for _ in ()).throw(exception('synthetic')))
    assert install_options.save(config, record, [], base) == status
    assert json.loads(config.read_text())['install_args'] == []
    assert '保存結果を確認できません' in capsys.readouterr().err


def test_lock_failure_does_not_claim_uncertain_save(tmp_path, capsys):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    with config_edit._edit_lock(config):
        assert install_options.save(config, record, [], base) == 1
    assert json.loads(config.read_text()) == {'other': 1}
    assert '保存結果を確認できません' not in capsys.readouterr().err


def test_config_size_limit_before_save(tmp_path, monkeypatch):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'data': 'x' * 100})
    original = config.read_bytes()
    monkeypatch.setattr(mcs_util, 'CONFIG_MAX_BYTES', len(original) + 1)
    base = install_options.inspect(config, record, [])
    assert install_options.save(config, record, [], base) == 1
    assert config.read_bytes() == original


def test_symlink_legacy_does_not_fallback_or_get_overwritten(tmp_path):
    config, record, target = (tmp_path / n for n in ('config.json', 'install_options.json', 'target'))
    put(config, {'install_args': []})
    put(target, {'install_args': []})
    record.symlink_to(target)
    assert install_options.recorded(config, record) is None
    with pytest.raises(OSError):
        install_options.inspect(config, record, [])
    assert target.exists() and record.is_symlink()


def _fresh_install_fixture(tmp_path, *, root_mode=0o700):
    import fcntl
    import os
    import stat

    root = tmp_path / 'synthetic-root'
    root.mkdir(mode=root_mode)
    root.chmod(root_mode)
    data = root / 'data'
    data.mkdir(mode=0o700)
    data_info = data.stat()
    token = f'{data_info.st_dev} {data_info.st_ino} {data_info.st_uid} {stat.S_IMODE(data_info.st_mode):o} {data_info.st_nlink}'
    lock = data / 'update.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    install_options.fresh_marker_init(root, token, fd)
    config = root / 'config.json'
    put(config, {'runtime_mode': 'standalone'})
    stat_result = config.stat()
    proof = json.dumps({'identity': provider_settings._identity(stat_result),
                        'sha256': hashlib.sha256(config.read_bytes()).hexdigest()})
    return root, data, fd, config, token, proof


def test_existing_data_legacy_guard_requires_actual_held_lock_and_no_provider_artifacts(tmp_path):
    import fcntl
    import os

    root = tmp_path / 'mcs'
    data = root / 'data'
    data.mkdir(mode=0o700, parents=True)
    lock = data / 'update.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    config = root / 'config.json'
    record = data / 'install_options.json'
    try:
        with pytest.raises(ValueError, match='fresh_lock_unheld'):
            install_options.require_uninitialized_legacy(config, record, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install_options.require_uninitialized_legacy(config, record, fd)
        (data / '.provider-settings-stage-synthetic').mkdir()
        with pytest.raises(ValueError, match='legacy_install_unverified'):
            install_options.require_uninitialized_legacy(config, record, fd)
    finally:
        os.close(fd)


def test_fresh_bootstrap_publishes_checked_split_before_services(tmp_path, monkeypatch):
    import os
    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path)
    monkeypatch.setattr(install_options, '_fresh_cron_absent', lambda: True)
    monkeypatch.setattr(install_options, '_fresh_workers_absent', lambda: True)
    try:
        assert install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd) == 0
        owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
        state = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
        assert state['mode'] == 'split' and state['common'] == {'runtime_mode': 'standalone'}
        assert state['providers'] == {name: {} for name in provider_settings.PROVIDERS}
        assert not (data / '.provider-settings-pending.json').exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize('damage', ['ledger', 'manifest', 'pending', 'staging', 'config', 'data', 'worker', 'lock'])
def test_fresh_bootstrap_refuses_ambiguous_or_existing_state(tmp_path, monkeypatch, damage):
    import os
    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path)
    monkeypatch.setattr(install_options, '_fresh_cron_absent', lambda: True)
    monkeypatch.setattr(install_options, '_fresh_workers_absent', lambda: damage != 'worker')
    try:
        if damage in ('ledger', 'manifest', 'pending', 'staging'):
            names = {'ledger': 'ledger.db', 'manifest': 'service_manifest.json',
                     'pending': '.provider-settings-pending.json',
                     'staging': '.provider-settings-stage-synthetic'}
            (data / names[damage]).write_text('synthetic')
        elif damage == 'config':
            put(config, {'runtime_mode': 'hermes'})
        elif damage == 'data':
            token = '0 0 0 700 1'
        elif damage == 'lock':
            os.close(fd)
            fd = os.open(data / 'update.lock', os.O_RDONLY | os.O_NOFOLLOW)
        snapshot = {path.name: path.read_bytes() for path in data.iterdir() if path.is_file()}
        config_before = config.read_bytes()
        with pytest.raises(ValueError, match='fresh_bootstrap_unverified'):
            install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd)
        assert config.read_bytes() == config_before
        assert {path.name: path.read_bytes() for path in data.iterdir() if path.is_file()} == snapshot
    finally:
        os.close(fd)


def test_fresh_bootstrap_rechecks_workers_before_publication(tmp_path, monkeypatch):
    import os
    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path)
    checks = iter([True, False])
    monkeypatch.setattr(install_options, '_fresh_cron_absent', lambda: True)
    monkeypatch.setattr(install_options, '_fresh_workers_absent', lambda: next(checks))
    try:
        with pytest.raises(ValueError, match='fresh_bootstrap_unverified'):
            install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd)
        assert set(path.name for path in data.iterdir()) == {'update.lock', install_options._FRESH_MARKER}
        assert not (data / '.provider-settings-pending.json').exists()
    finally:
        os.close(fd)


def test_fresh_bootstrap_preserves_pending_on_interrupted_publication(tmp_path, monkeypatch):
    import os
    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path)
    monkeypatch.setattr(install_options, '_fresh_cron_absent', lambda: True)
    monkeypatch.setattr(install_options, '_fresh_workers_absent', lambda: True)
    pending = data / '.provider-settings-pending.json'
    def interrupted(*args, **kwargs):
        put(pending, {'synthetic': 'pending'})
        raise ValueError('synthetic interruption')
    monkeypatch.setattr(provider_settings, 'bootstrap_locked', interrupted)
    try:
        with pytest.raises(ValueError, match='fresh_bootstrap_unverified'):
            install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd)
        assert json.loads(pending.read_text()) == {'synthetic': 'pending'}
    finally:
        os.close(fd)


def test_fresh_bootstrap_rejects_transient_data_entry(tmp_path, monkeypatch):
    import os
    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path)
    def concurrent_trace():
        marker = data / 'ledger.db'
        marker.write_text('synthetic')
        marker.unlink()
        return True
    monkeypatch.setattr(install_options, '_fresh_cron_absent', lambda: True)
    monkeypatch.setattr(install_options, '_fresh_workers_absent', concurrent_trace)
    try:
        with pytest.raises(ValueError, match='fresh_bootstrap_unverified'):
            install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd)
        assert set(path.name for path in data.iterdir()) == {'update.lock', install_options._FRESH_MARKER}
    finally:
        os.close(fd)


def test_fresh_bootstrap_rejects_same_bytes_with_new_config_inode(tmp_path, monkeypatch):
    import os
    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path)
    monkeypatch.setattr(install_options, '_fresh_cron_absent', lambda: True)
    monkeypatch.setattr(install_options, '_fresh_workers_absent', lambda: True)
    replacement = root / 'synthetic-config'
    replacement.write_bytes(config.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, config)
    try:
        with pytest.raises(ValueError, match='fresh_bootstrap_unverified'):
            install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd)
        assert set(path.name for path in data.iterdir()) == {'update.lock', install_options._FRESH_MARKER}
    finally:
        os.close(fd)


@pytest.mark.parametrize('kind', ['clear', 'owned_process', 'owned_service', 'inventory_error'])
def test_fresh_worker_inventory_requires_a_readable_empty_owned_set(tmp_path, monkeypatch, kind):
    import os
    from types import SimpleNamespace

    monkeypatch.setattr(install_options.sys, 'platform', 'darwin')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    def run(argv, **kwargs):
        if argv[0] == '/bin/ps':
            output = f'{os.getpid()} python synthetic\n'
            if kind == 'owned_process':
                output += '123456 python -m mcs_standalone\n'
            return SimpleNamespace(returncode=0, stdout=output)
        output = 'PID\tStatus\tLabel\n'
        if kind == 'owned_service':
            output += '-\t0\tai.mcs.standalone\n'
        return SimpleNamespace(returncode=1 if kind == 'inventory_error' else 0, stdout=output)
    monkeypatch.setattr(subprocess, 'run', run)
    assert install_options._fresh_workers_absent() is (kind == 'clear')


@pytest.mark.parametrize('entries,expected', [
    ([], True),
    ([{'name': 'MCS unread check', 'script': 'mcs_check.sh'}], False),
    ([{'name': 'synthetic', 'script': '/synthetic/mcs_deep.sh'}], False),
    ([{'name': 'synthetic', 'script': 'other.sh'}], True),
    ([{'name': 'synthetic'}], False),
    (None, False),
])
def test_fresh_hermes_cron_inventory_requires_proven_absence(tmp_path, monkeypatch, entries, expected):
    import service_management
    home = tmp_path / 'synthetic-home'
    home.mkdir()
    hermes_home = home / '.hermes'
    hermes_home.mkdir()
    executable = hermes_home / 'synthetic-hermes'
    executable.touch()
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setenv('MCS_FRESH_RUNTIME_MODE', 'hermes')
    monkeypatch.setenv('MCS_FRESH_HERMES_HOME', str(hermes_home))
    monkeypatch.setenv('MCS_FRESH_HERMES_EXE', str(executable))
    inspected = []
    def inspect(path):
        inspected.append(path)
        return entries
    monkeypatch.setattr(service_management, 'inspect_cron', inspect)
    assert install_options._fresh_cron_absent() is expected
    assert inspected == [str(executable)]


@pytest.mark.parametrize(('mode', 'home'), [('hermes', ''), ('invalid', '/synthetic')])
def test_fresh_cron_inventory_rejects_missing_home_or_invalid_mode(monkeypatch, mode, home):
    monkeypatch.setenv('MCS_FRESH_RUNTIME_MODE', mode)
    monkeypatch.setenv('MCS_FRESH_HERMES_HOME', home)
    assert not install_options._fresh_cron_absent()


@pytest.mark.parametrize('name', ['.env', '.env.backup', 'chrome-profile', 'token_cache.json', 'backups'])
def test_fresh_bootstrap_refuses_prior_root_state_without_reading_it(tmp_path, monkeypatch, name):
    import os
    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path)
    prior = root / name
    if name in ('chrome-profile', 'backups'):
        prior.mkdir()
    else:
        prior.write_text('synthetic prior state')
    monkeypatch.setattr(install_options, '_fresh_workers_absent', lambda: True)
    monkeypatch.setattr(install_options, '_fresh_cron_absent', lambda: True)
    try:
        with pytest.raises(ValueError, match='fresh_bootstrap_unverified'):
            install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd)
        assert prior.exists() and (data / install_options._FRESH_MARKER).exists()
    finally:
        os.close(fd)


def test_fresh_hold_binds_root_inode_and_rejects_other_root(tmp_path):
    import os
    import shutil
    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path)
    other = tmp_path / 'other-root'
    other.mkdir(mode=0o700)
    (other / 'data').mkdir(mode=0o700)
    shutil.copyfile(data / install_options._FRESH_MARKER, other / 'data' / install_options._FRESH_MARKER)
    (other / 'data' / install_options._FRESH_MARKER).chmod(0o600)
    try:
        with pytest.raises(ValueError, match='fresh_marker_invalid'):
            install_options._fresh_marker_read(other)
        assert (data / install_options._FRESH_MARKER).exists()
    finally:
        os.close(fd)


def test_fresh_hold_survives_before_config_creation_and_reinit_is_refused(tmp_path):
    import fcntl
    import os
    import stat
    root = tmp_path / 'synthetic-root'
    root.mkdir(mode=0o700)
    data = root / 'data'
    data.mkdir(mode=0o700)
    info = data.stat()
    token = f'{info.st_dev} {info.st_ino} {info.st_uid} {stat.S_IMODE(info.st_mode):o} {info.st_nlink}'
    fd = os.open(data / 'update.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        install_options.fresh_marker_init(root, token, fd)
        before = (data / install_options._FRESH_MARKER).read_bytes()
        with pytest.raises(ValueError):
            install_options.fresh_marker_init(root, token, fd)
        assert (data / install_options._FRESH_MARKER).read_bytes() == before
        assert not (root / 'config.json').exists()
    finally:
        os.close(fd)


def test_fresh_bootstrap_allows_existing_private_owner_clone(tmp_path, monkeypatch):
    import os
    # A normal owned Git checkout may be world-readable, but not group-writable.
    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path, root_mode=0o755)
    monkeypatch.setattr(install_options, '_fresh_workers_absent', lambda: True)
    monkeypatch.setattr(install_options, '_fresh_cron_absent', lambda: True)
    try:
        assert install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd) == 0
    finally:
        os.close(fd)


@pytest.mark.parametrize('fault', [
    'link', 'fsync_link', 'unlink_fresh', 'fsync_fresh', 'unlink_complete', 'fsync_complete',
])
def test_fresh_completion_io_failure_reports_unknown_and_never_rebootstraps(tmp_path, monkeypatch, fault):
    import os

    root, data, fd, config, token, proof = _fresh_install_fixture(tmp_path)
    monkeypatch.setattr(install_options, '_fresh_workers_absent', lambda: True)
    monkeypatch.setattr(install_options, '_fresh_cron_absent', lambda: True)
    original_link, original_unlink, original_fsync = os.link, os.unlink, os.fsync
    phase = {'value': 'before', 'injected': False}

    def fail_once(name):
        if fault == name and not phase['injected']:
            phase['injected'] = True
            raise OSError('synthetic completion I/O failure')

    def link(src, dst, *args, **kwargs):
        if dst == install_options._FRESH_COMPLETE:
            fail_once('link')
            result = original_link(src, dst, *args, **kwargs)
            phase['value'] = 'linked'
            return result
        return original_link(src, dst, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if path == install_options._FRESH_MARKER and phase['value'] == 'linked_synced':
            fail_once('unlink_fresh')
            result = original_unlink(path, *args, **kwargs)
            phase['value'] = 'fresh_unlinked'
            return result
        if path == install_options._FRESH_COMPLETE and phase['value'] == 'fresh_synced':
            fail_once('unlink_complete')
            result = original_unlink(path, *args, **kwargs)
            phase['value'] = 'complete_unlinked'
            return result
        return original_unlink(path, *args, **kwargs)

    def fsync(target_fd):
        info = os.fstat(target_fd)
        directory = data.stat()
        if (info.st_dev, info.st_ino) == (directory.st_dev, directory.st_ino):
            transitions = {'linked': ('fsync_link', 'linked_synced'),
                           'fresh_unlinked': ('fsync_fresh', 'fresh_synced'),
                           'complete_unlinked': ('fsync_complete', 'complete_synced')}
            if phase['value'] in transitions:
                name, next_phase = transitions[phase['value']]
                fail_once(name)
                result = original_fsync(target_fd)
                phase['value'] = next_phase
                return result
        return original_fsync(target_fd)

    monkeypatch.setattr(os, 'link', link)
    monkeypatch.setattr(os, 'unlink', unlink)
    monkeypatch.setattr(os, 'fsync', fsync)
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    try:
        with pytest.raises(ValueError, match='fresh_bootstrap_unverified'):
            install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd)
        assert phase['injected']
        state = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
        assert state['mode'] == 'split'
        fresh = data / install_options._FRESH_MARKER
        complete = data / install_options._FRESH_COMPLETE
        assert fresh.exists() is (fault in ('link', 'fsync_link', 'unlink_fresh'))
        assert complete.exists() is (fault not in ('link', 'fsync_complete'))
        with pytest.raises(ValueError, match='fresh_bootstrap_unverified'):
            install_options.bootstrap_fresh(config, data / 'install_options.json', proof, token, fd)
        assert provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)['snapshot'] == state['snapshot']
        if fresh.exists():
            if complete.exists():
                with pytest.raises(ValueError, match='fresh_pending'):
                    install_options.fresh_complete(root, fd)
            assert fresh.exists()
        elif complete.exists():
            install_options.fresh_complete(root, fd)
            assert not complete.exists()
            after = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
            assert after['mode'] == 'split'
            for key in ('files', 'authority', 'pending', 'provider_settings'):
                assert after['snapshot'][key] == state['snapshot'][key]
    finally:
        os.close(fd)


def _runtime_split_fixture(tmp_path, common=None):
    root = tmp_path / 'synthetic-runtime'
    root.mkdir(mode=0o700)
    data = root / 'data'
    data.mkdir(mode=0o700)
    directory = data / 'provider_settings'
    directory.mkdir(mode=0o700)
    config = root / 'config.json'
    common = common if common is not None else {
        'runtime_mode': 'standalone', 'recovery_python': '/synthetic/python',
        'cloud_llm': {'mode': 'off'}, 'semantic': {'mode': 'off'},
    }
    put(config, common)
    hashes = {}
    for name in provider_settings.PROVIDERS:
        path = directory / (name + '.json')
        put(path, {})
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    put(data / '.provider-settings-authority.json', {
        'version': 2, 'state': 'committed', 'files': hashes,
        'common_policy_sha256': provider_settings.common_policy_digest(common),
    })
    _, snapshot = config_edit._snapshot(config)
    base = ':'.join(str(value) for value in snapshot[0]) + ':' + snapshot[1].hex()
    return root, data, config, base


def _runtime_bootstrap_fixture(tmp_path):
    common = {'install_args': [], 'synthetic_unknown': {'preserve': True}}
    root, data, config, base = _runtime_split_fixture(tmp_path, common)
    authority = data / '.provider-settings-authority.json'
    value = json.loads(authority.read_text())
    value['configuration_state'] = 'bootstrap'
    put(authority, value)
    return root, data, config, base


def test_bootstrap_runtime_confirmed_edit_preserves_other_fields(tmp_path):
    root, data, config, base = _runtime_bootstrap_fixture(tmp_path)
    record = data / 'install_options.json'
    token = install_options.bootstrap_runtime_inspect(config, record, base)
    assert token.startswith('bootstrap:')
    result = install_options.bootstrap_runtime_apply(config, record, token, '1', 'standalone', '')
    new_base, new_token = result.split()
    assert json.loads(config.read_text()) == {
        'install_args': [], 'synthetic_unknown': {'preserve': True}, 'runtime_mode': 'standalone'}
    assert install_options.main(['runtime-noop-verify', str(config), str(record), new_token,
                                 new_base, '1', 'standalone', '']) == 0
    assert all(json.loads((data / 'provider_settings' / (name + '.json')).read_text()) == {}
               for name in provider_settings.PROVIDERS)
    assert json.loads((data / '.provider-settings-authority.json').read_text())['configuration_state'] == 'bootstrap'


@pytest.mark.parametrize('damage', ['config', 'provider', 'authority', 'pending', 'fresh'])
def test_bootstrap_runtime_refuses_changed_snapshot_or_hold(tmp_path, damage):
    root, data, config, base = _runtime_bootstrap_fixture(tmp_path)
    record = data / 'install_options.json'
    token = install_options.bootstrap_runtime_inspect(config, record, base)
    if damage == 'config':
        put(config, {'install_args': [], 'synthetic_unknown': {'preserve': False}})
    elif damage == 'provider':
        put(data / 'provider_settings/openai.json', {'model': cloud_policy.PROVIDERS['openai']})
    elif damage == 'authority':
        authority = data / '.provider-settings-authority.json'
        value = json.loads(authority.read_text())
        del value['configuration_state']
        put(authority, value)
    elif damage == 'pending':
        put(data / '.provider-settings-pending.json', {})
    else:
        put(data / '.provider-bootstrap-fresh.json', {})
    before = config.read_bytes()
    assert install_options.main(['bootstrap-runtime-apply', str(config), str(record), token,
                                 '1', 'standalone', '']) == 1
    assert config.read_bytes() == before


def test_completed_split_cannot_be_used_as_bootstrap_runtime(tmp_path):
    _, data, config, base = _runtime_split_fixture(tmp_path)
    assert install_options.bootstrap_runtime_inspect(config, data / 'install_options.json', base) == 'none'


def test_split_runtime_noop_accepts_only_explicit_matching_raw_values(tmp_path):
    root, data, config, base = _runtime_split_fixture(tmp_path)
    before = config.read_bytes()
    token = install_options.runtime_edit_decision(config, data / 'install_options.json', base,
                                                  '1', 'standalone', '/synthetic/python')
    assert token.startswith('split:') and len(token) == len('split:') + 64
    assert install_options.main(['runtime-noop-verify', str(config), str(data / 'install_options.json'), token,
                                 base, '1', 'standalone', '/synthetic/python']) == 0
    assert config.read_bytes() == before
    with pytest.raises(ValueError, match='runtime_edit_unavailable'):
        install_options.runtime_edit_decision(config, data / 'install_options.json', base,
                                              '1', 'hermes', '/synthetic/python')
    with pytest.raises(ValueError, match='runtime_edit_unavailable'):
        install_options.runtime_edit_decision(config, data / 'install_options.json', base,
                                              '1', 'standalone', '/synthetic/other')


@pytest.mark.parametrize('common,mode_flag,requested,recovery', [
    ({'cloud_llm': {'mode': 'off'}}, '1', 'hermes', ''),
    ({'runtime_mode': 'standalone'}, '0', 'standalone', '/synthetic/python'),
])
def test_split_noop_does_not_turn_omitted_raw_selection_into_explicit(tmp_path, common, mode_flag, requested, recovery):
    _, data, config, base = _runtime_split_fixture(tmp_path, common)
    with pytest.raises(ValueError, match='runtime_edit_unavailable'):
        install_options.runtime_edit_decision(config, data / 'install_options.json', base, mode_flag, requested, recovery)


@pytest.mark.parametrize('damage', ['provider', 'authority', 'pending', 'config_base'])
def test_split_runtime_noop_rejects_changed_or_unreadable_snapshot(tmp_path, damage):
    _, data, config, base = _runtime_split_fixture(tmp_path)
    token = install_options.runtime_edit_decision(config, data / 'install_options.json', base,
                                                  '1', 'standalone', '')
    if damage == 'provider':
        path = data / 'provider_settings' / 'openai.json'
        put(path, {'model': cloud_policy.PROVIDERS['openai']})
        authority = data / '.provider-settings-authority.json'
        value = json.loads(authority.read_text())
        value['files']['openai'] = hashlib.sha256(path.read_bytes()).hexdigest()
        put(authority, value)
    elif damage == 'authority':
        put(data / '.provider-settings-authority.json', {})
    elif damage == 'pending':
        put(data / '.provider-settings-pending.json', {})
    else:
        base = base + '0'
    assert install_options.main(['runtime-noop-verify', str(config), str(data / 'install_options.json'), token,
                                 base, '1', 'standalone', '']) == 1


def test_legacy_runtime_selection_keeps_existing_edit_route(tmp_path):
    config = tmp_path / 'config.json'
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    put(config, {'runtime_mode': 'hermes'})
    assert install_options.runtime_edit_decision(config, data / 'install_options.json', 'unused',
                                                 '1', 'standalone', '') == 'legacy'


def test_split_runtime_noop_refuses_replaced_root_link_even_for_same_target(tmp_path):
    import os

    physical, data, config, base = _runtime_split_fixture(tmp_path)
    alias = tmp_path / 'synthetic-root-link'
    alias.symlink_to(physical, target_is_directory=True)
    linked_config, linked_record = alias / 'config.json', alias / 'data' / 'install_options.json'
    before = config.read_bytes()
    token = install_options.runtime_edit_decision(linked_config, linked_record, base,
                                                  '1', 'standalone', '/synthetic/python')
    assert install_options.main(['runtime-noop-verify', str(linked_config), str(linked_record), token,
                                 base, '1', 'standalone', '/synthetic/python']) == 0
    replacement = tmp_path / 'synthetic-root-replacement'
    replacement.symlink_to(physical, target_is_directory=True)
    os.replace(replacement, alias)
    assert install_options.main(['runtime-noop-verify', str(linked_config), str(linked_record), token,
                                 base, '1', 'standalone', '/synthetic/python']) == 1
    assert alias.resolve() == physical and config.read_bytes() == before
    assert not (data / '.provider-settings-pending.json').exists()
