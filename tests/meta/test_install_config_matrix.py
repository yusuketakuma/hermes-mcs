"""Installer options and existing configuration over synthetic commands/HOME."""
import itertools
import json
import sys

import pytest

from test_install_sh import _world, _run, _calls


FLAGS = ('--no-brew', '--no-llm', '--no-plugin', '--no-services', '--no-recovery')


@pytest.mark.parametrize('runtime', ['hermes', 'standalone'])
@pytest.mark.parametrize('bits', list(itertools.product([False, True], repeat=len(FLAGS))))
def test_every_skip_flag_combination_converges(tmp_path, runtime, bits):
    home, _, stub, env = _world(tmp_path)
    flags = [flag for flag, skip in zip(FLAGS, bits) if skip]
    result = _run(env, None, '--mode', runtime, *flags)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(stub)
    if '--no-brew' in flags:
        assert not any(c.startswith('brew install') for c in calls)
    assert any('mcs_setup.py services' in c for c in calls) == ('--no-services' not in flags)
    if runtime == 'standalone':
        assert not (home / '.hermes').exists()
        assert not any(c.startswith(('git clone', 'venv-hermes')) for c in calls)
    if '--no-recovery' in flags:
        assert not (home / '.mcs-recovery').exists()


@pytest.mark.parametrize('runtime', ['hermes', 'standalone'])
@pytest.mark.parametrize('mode', ['install', 'preflight', 'dry-run'])
@pytest.mark.parametrize('raw', ['{bad', '[]', '{"nested":' + '[' * 1500 + '0' + ']' * 1500 + '}'],
                         ids=['malformed', 'array', 'deep'])
def test_explicit_mode_does_not_write_before_invalid_config_refusal(tmp_path, runtime, mode, raw):
    home, _, stub, env = _world(tmp_path)
    if raw.startswith('{"nested":'):
        try:
            json.loads(raw)
        except RecursionError:
            pass
        else:
            # Iterative decoders still exercise an older supported Python's refusal.
            inject = stub / 'legacy-json-depth.py'
            inject.write_text('import json, sys\n'
                              'def limited(*args, **kwargs): raise RecursionError("synthetic decoder limit")\n'
                              'json.load = limited\n'
                              'sys.argv = sys.argv[1:]\n'
                              'exec(compile(sys.stdin.read(), "<mode-selection>", "exec"))\n')
            py = stub / 'bin/python3'
            py.write_text(py.read_text().replace('exec "$STUB_PARSE_PYTHON" "$@"',
                          'exec "$STUB_PARSE_PYTHON" "$STUB_ROOT/legacy-json-depth.py" "$@"'))
    env['STUB_REAL_PYTHON'] = sys.executable
    cfg = home / '.mcs/config.json'
    cfg.parent.mkdir()
    cfg.write_text(raw)
    opts = [] if mode == 'install' else ['--' + mode]
    result = _run(env, None, '--mode', runtime, *FLAGS, *opts)
    assert result.returncode != 0
    assert cfg.read_text() == raw
    assert 'Traceback' not in result.stderr
    assert not any(c.startswith(('git clone', 'uv ', 'brew install', 'venv-hermes', 'launchctl'))
                   for c in _calls(stub))
    assert sorted(p.name for p in home.iterdir()) == ['.mcs']


@pytest.mark.parametrize('before,after', [('hermes', 'hermes'), ('standalone', 'standalone'),
                                         ('standalone', 'hermes'), ('hermes', 'standalone')])
def test_explicit_runtime_keeps_other_initial_settings(tmp_path, before, after):
    home, _, _, env = _world(tmp_path)
    env['STUB_REAL_PYTHON'] = sys.executable
    cfg = home / '.mcs/config.json'
    cfg.parent.mkdir()
    original = {'runtime_mode': before, 'mcs_login_id': 'fictional-login',
                'notify_target': 'local', 'semantic': {'job_budget_seconds': 45.0},
                'synthetic_unknown_setting': {'keep': [1, 2]}}
    cfg.write_text(json.dumps(original))
    result = _run(env, None, '--mode', after, *FLAGS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(cfg.read_text()) == {**original, 'runtime_mode': after}


@pytest.mark.parametrize('response,serving', [
    ('{"data":[{"id":"synthetic-model"}]}', True),
    ('{"data":[]}', False), ('<html>not a model server</html>', False),
    ('{"data":[{"id":null}]}', False), ('{"data":[{"id":" "}]}', False),
])
def test_only_a_nonempty_model_list_reuses_external_server(tmp_path, response, serving):
    _, _, stub, env = _world(tmp_path)
    result = _run({**env, 'STUB_LLM_SERVING': '1', 'STUB_MODEL_RESPONSE': response},
                  None, '--no-services', '--no-recovery')
    assert result.returncode == 0, result.stdout + result.stderr
    downloaded = any(c.startswith('curl ') and ' -o ' in c for c in _calls(stub))
    assert downloaded is not serving


@pytest.mark.parametrize('busy', [False, True])
def test_old_standalone_venv_is_preserved_and_busy_runtime_is_not_replaced(tmp_path, busy):
    home, _, _, env = _world(tmp_path)
    venv = home / '.mcs/venv'
    (venv / 'bin').mkdir(parents=True)
    py = venv / 'bin/python3'
    py.write_text('#!/bin/sh\nexit 1\n')
    py.chmod(0o700)
    (venv / 'prior-marker').write_text('synthetic prior runtime')
    if busy:
        env['STUB_VENV_PROCESS'] = str(py) + ' -m synthetic-runtime'
    result = _run(env, None, '--mode', 'standalone', *FLAGS)
    assert result.returncode == (1 if busy else 0), result.stdout + result.stderr
    preserved = venv if busy else home / '.mcs/venv.pre-python314'
    assert (preserved / 'prior-marker').read_text() == 'synthetic prior runtime'
    if busy:
        assert 'still in use' in result.stderr
        assert not (home / '.mcs/venv.pre-python314').exists()
    else:
        assert (venv / 'bin/python3').exists()


@pytest.mark.parametrize('marker', ['ledger.db', 'service_manifest.json'])
def test_existing_installation_cannot_race_a_scheduled_tick_during_runtime_rebuild(tmp_path, marker):
    home, _, _, env = _world(tmp_path)
    venv = home / '.mcs/venv'
    (venv / 'bin').mkdir(parents=True)
    py = venv / 'bin/python3'
    py.write_text('#!/bin/sh\nexit 1\n')
    py.chmod(0o700)
    data = home / '.mcs/data'
    data.mkdir()
    (data / marker).write_text('synthetic established installation')
    result = _run(env, None, '--mode', 'standalone', *FLAGS)
    assert result.returncode == 1
    assert 'coordinated Python runtime migration' in result.stderr
    assert py.read_text() == '#!/bin/sh\nexit 1\n'
    assert not (home / '.mcs/venv.pre-python314').exists()


@pytest.mark.parametrize('proof', ['valid', 'wrong_inode', 'closed', 'foreign_backup'])
def test_updater_migration_requires_inherited_matching_run_lock(tmp_path, proof):
    import fcntl
    import os
    home, _, _, env = _world(tmp_path)
    venv = home / '.mcs/venv'
    (venv / 'bin').mkdir(parents=True)
    py = venv / 'bin/python3'
    py.write_text('#!/bin/sh\nexit 1\n')
    py.chmod(0o700)
    data = home / '.mcs/data'
    data.mkdir()
    (data / 'ledger.db').write_text('synthetic established installation')
    lock = data / 'run.lock'
    lock.touch(mode=0o600)
    selected = data / 'wrong.lock' if proof == 'wrong_inode' else lock
    fd = os.open(selected, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = str(venv) + '.pre-python314.1'
        env['MCS_UPDATE_RUN_LOCK_FD'] = str(fd)
        env['MCS_UPDATE_RUNTIME_BACKUP'] = str(home / 'foreign') if proof == 'foreign_backup' else backup
        result = _run(env, None, '--mode', 'standalone', *FLAGS,
                      pass_fds=() if proof == 'closed' else (fd,))
        assert result.returncode == (0 if proof == 'valid' else 1), result.stdout + result.stderr
        if proof == 'valid':
            assert (home / '.mcs/venv.pre-python314.1/bin/python3').read_text() == '#!/bin/sh\nexit 1\n'
        else:
            assert py.read_text() == '#!/bin/sh\nexit 1\n'
            assert 'migration lock or backup is invalid' in result.stderr
        # The helper must not release the updater's shared lock description.
        other = os.open(selected, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
    finally:
        os.close(fd)
