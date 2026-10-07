"""Detached gateway restarts use owned services and factual PID evidence only."""
import json
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from ops_testkit import _load


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    rec = _load()
    home = tmp_path / 'home'
    agents = home / 'Library/LaunchAgents'
    agents.mkdir(parents=True)
    data = home / '.mcs/data'
    data.mkdir(parents=True)
    command = home / '.local/bin/hermes'
    command.parent.mkdir(parents=True)
    command.write_text('#!/bin/sh\nexit 0\n')
    command.chmod(0o700)
    plist = agents / 'ai.hermes.gateway.plist'
    plist.write_bytes(plistlib.dumps({
        'Label': 'ai.hermes.gateway',
        'WorkingDirectory': str(home / '.hermes'),
        'EnvironmentVariables': {'PATH': str(command.parent)},
        'ProgramArguments': ['/usr/bin/osascript', '-l', 'JavaScript',
                             'Application("Terminal").doScript("hermes gateway run")']}))
    calls = []
    running = {'pid': 101}

    def run(argv, **kwargs):
        assert argv[0] == 'launchctl'
        calls.append(argv)
        if argv[1] == 'kickstart':
            running['pid'] = 202
            return subprocess.CompletedProcess(argv, 0)
        if argv[-1].startswith(running.get('domain', 'user') + '/'):
            return subprocess.CompletedProcess(argv, 0,
                                               f'path = {plist}\npid = {running["pid"]}\n')
        return subprocess.CompletedProcess(argv, 113, '')

    monkeypatch.setattr(rec.subprocess, 'run', run)
    return rec, data, agents, plist, calls, running


@pytest.mark.parametrize('domain', ['user', 'gui'])
def test_owned_domain_jxa_gateway_is_restarted_and_pid_verified(gateway, domain):
    rec, data, agents, _, calls, running = gateway
    running['domain'] = domain
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 0
    assert ['launchctl', 'kickstart', '-k', f'{domain}/501/ai.hermes.gateway'] in calls
    report = json.loads((data / 'gateway_restart.json').read_text())
    assert report['status'] == 'supervisor_restart_verified' and report['pid'] == 202
    assert report['previous_pid'] == 101
    assert (data / 'gateway_restart.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('fault', ['foreign_path', 'foreign_program', 'ambiguous', 'reject', 'hold'])
def test_unowned_ambiguous_or_held_gateway_is_never_reported_restarted(gateway, monkeypatch, fault):
    rec, data, agents, plist, calls, running = gateway
    real = rec.subprocess.run
    if fault == 'foreign_program':
        plist.write_bytes(plistlib.dumps({'Label': 'ai.hermes.gateway',
                                         'ProgramArguments': ['/bin/other']}))
    elif fault == 'hold':
        (data / 'restore_pending.json').write_text('{"phase":"awaiting_consent"}')
    else:
        def run(argv, **kwargs):
            if fault == 'foreign_path' and argv[1] == 'print':
                return subprocess.CompletedProcess(argv, 0, 'path = /foreign/service.plist\npid = 101\n')
            if fault == 'ambiguous' and argv[-1].startswith('gui/'):
                return subprocess.CompletedProcess(argv, 0, f'path = {plist}\npid = 303\n')
            if fault == 'reject' and argv[1] == 'kickstart':
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 1)
            return real(argv, **kwargs)
        monkeypatch.setattr(rec.subprocess, 'run', run)
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 1
    report = json.loads((data / 'gateway_restart.json').read_text())
    assert report['status'] == 'failed'
    assert 'Application' not in json.dumps(report)
    if fault != 'reject':
        assert not any(call[1] == 'kickstart' for call in calls)
    assert running['pid'] == 101


def test_unchanged_pid_remains_unknown(gateway, monkeypatch):
    rec, data, agents, _, _, running = gateway
    real = rec.subprocess.run
    def run(argv, **kwargs):
        if argv[1] == 'kickstart':
            return subprocess.CompletedProcess(argv, 0)
        return real(argv, **kwargs)
    monkeypatch.setattr(rec.subprocess, 'run', run)
    clock = [0]
    monkeypatch.setattr(rec, 'time', SimpleNamespace(time=lambda: 0,
        monotonic=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0] + n)))
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 1
    assert json.loads((data / 'gateway_restart.json').read_text())['status'] == 'unknown'
    assert running['pid'] == 101


def test_request_is_detached_and_does_not_claim_restart_success(gateway, monkeypatch):
    rec, data, agents, _, _, _ = gateway
    calls = []
    monkeypatch.setenv('XPC_SERVICE_NAME', 'synthetic')
    monkeypatch.setenv('MCS_JOB_PID', 'synthetic')
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda argv, **kw: calls.append((argv, kw)))
    assert rec.request_gateway_restart(str(data), str(agents), 501)
    argv, kw = calls[0]
    assert argv[1:3] == ['-I', '-c'] and kw['start_new_session'] is True
    assert kw['cwd'] == '/'
    assert argv[-3:] == [str(data), str(agents), '501']
    assert 'XPC_SERVICE_NAME' not in kw['env'] and 'MCS_JOB_PID' not in kw['env']
    assert json.loads((data / 'gateway_restart.json').read_text())['status'] == 'requested'
    compile(argv[3], '<restart>', 'exec')


def test_spawn_failure_is_recorded_without_raising(gateway, monkeypatch):
    rec, data, agents, _, _, _ = gateway
    def fail(*args, **kwargs):
        raise OSError('synthetic')
    monkeypatch.setattr(rec.subprocess, 'Popen', fail)
    assert not rec.request_gateway_restart(str(data), str(agents), 501)
    assert json.loads((data / 'gateway_restart.json').read_text())['status'] == 'failed'


@pytest.mark.parametrize('stage,expected', [('print', 'failed'), ('kickstart', 'unknown')])
def test_timeout_after_restart_request_is_honestly_unknown(gateway, monkeypatch, stage, expected):
    rec, data, agents, _, _, _ = gateway
    real = rec.subprocess.run
    def run(argv, **kwargs):
        if argv[1] == stage:
            raise subprocess.TimeoutExpired(argv, 10)
        return real(argv, **kwargs)
    monkeypatch.setattr(rec.subprocess, 'run', run)
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 1
    assert json.loads((data / 'gateway_restart.json').read_text())['status'] == expected


def _program(gateway, argv, **fields):
    _, _, _, plist, _, _ = gateway
    value = plistlib.loads(plist.read_bytes())
    value.update(ProgramArguments=argv, **fields)
    plist.write_bytes(plistlib.dumps(value))


def _owned_launcher(gateway):
    _, _, agents, _, _, _ = gateway
    path = agents.parent.parent / '.hermes/hermes-agent/.hermes/bin/hermes'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o700)
    return path


@pytest.mark.parametrize('mode', ['cli', 'python_module', 'timestamp', 'libc_jxa', 'custom_libc_jxa'])
def test_owned_native_and_current_libc_jxa_commands_are_accepted(gateway, mode):
    import shlex
    import sys
    rec, data, agents, _, calls, _ = gateway
    home = agents.parent.parent
    logs = home / '.hermes/logs'
    fields = {}
    if mode == 'custom_libc_jxa':
        custom = home / 'configured-hermes'
        logs = custom / 'logs'
        fields = {'WorkingDirectory': str(custom), 'EnvironmentVariables': {'HERMES_HOME': str(custom)}}
    cli = str(_owned_launcher(gateway))
    argv = [cli, 'gateway', 'run', '--external-supervisor']
    if mode == 'python_module':
        argv = [sys.executable, '-m', 'hermes_cli.main', 'gateway', 'run']
    if mode in ('timestamp', 'libc_jxa', 'custom_libc_jxa'):
        argv = [cli, '--run-module', 'hermes_cli.stderr_timestamp', '--error-log',
                str(logs / 'gateway.error.log'), '--', *argv]
    if mode in ('libc_jxa', 'custom_libc_jxa'):
        shell = ('exec ' + shlex.join(argv) + ' >> ' + shlex.quote(str(logs / 'gateway.log'))
                 + ' 2>> ' + shlex.quote(str(logs / 'gateway.error.log')))
        script = ('ObjC.import("stdlib"); const status=$.system(' + json.dumps(shell) + '); '
                  'const signal=status & 127; '
                  '$.exit(status === -1 ? 1 : signal === 0 ? (status >> 8) & 255 : 128 + signal);')
        argv = ['/usr/bin/osascript', '-l', 'JavaScript', '-e', script]
    _program(gateway, argv, **fields)
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 0
    assert any(call[1] == 'kickstart' for call in calls)


@pytest.mark.parametrize('argv', [
    ['/bin/echo', 'gateway'], ['/bin/echo', 'hermes', 'gateway', 'run'],
    ['/bin/sh', '-c', 'echo hermes gateway run'],
    ['hermes', 'send', 'gateway'], ['hermes', 'gateway', 'status'],
    ['hermes', '--help', 'gateway', 'run'], ['hermes', 'gateway', 'run', 'other'],
    ['/tmp/hermes', 'gateway', 'run'],
    ['/usr/bin/osascript', '-l', 'JavaScript', '"hermes gateway run"'],
    ['/usr/bin/osascript', '-l', 'JavaScript',
     'Application("Terminal").doScript("echo hermes gateway run")'],
    ['/usr/bin/osascript', '-l', 'JavaScript',
     'Application("Terminal").doScript("hermes gateway run; echo other")'],
    ['/usr/bin/osascript', '-l', 'JavaScript',
     'Application("Terminal").doScript("hermes gateway run"); other()'],
])
def test_gateway_words_in_foreign_command_never_authorize_restart(gateway, argv):
    rec, data, agents, _, calls, running = gateway
    _program(gateway, argv)
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 1
    assert not any(call[1] == 'kickstart' for call in calls)
    assert running['pid'] == 101
    report = json.loads((data / 'gateway_restart.json').read_text())
    assert report['error'] == 'gateway_service_not_owned' and report['status'] == 'failed'
    assert 'ProgramArguments' not in report


def test_owned_named_cli_symlink_to_foreign_executable_is_rejected(gateway):
    rec, data, agents, _, calls, _ = gateway
    cli = agents.parent.parent / '.local/bin/hermes'
    cli.unlink()
    cli.symlink_to('/bin/echo')
    _program(gateway, [str(cli), 'gateway', 'run'])
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 1
    assert not any(call[1] == 'kickstart' for call in calls)


def test_foreign_working_directory_is_not_owned(gateway):
    rec, data, agents, _, calls, _ = gateway
    _program(gateway, [str(_owned_launcher(gateway)), 'gateway', 'run'],
             WorkingDirectory='/foreign')
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 1
    assert not any(call[1] == 'kickstart' for call in calls)


@pytest.mark.parametrize('mode', ['symlink_console', 'venv_module', 'isolated_bootstrap'])
def test_known_console_symlink_and_python_entry_positions_are_accepted(gateway, mode):
    import sys
    rec, data, agents, _, calls, _ = gateway
    home = agents.parent.parent
    install = home / '.hermes/hermes-agent'
    cli = _owned_launcher(gateway)
    local = home / '.local/bin/hermes'
    local.unlink()
    local.symlink_to(cli)
    if mode == 'symlink_console':
        argv = [str(local), 'gateway', 'run']
    else:
        python = install / 'venv/bin/python3'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        argv = [str(python), '-m', 'hermes_cli.main', 'gateway', 'run']
        if mode == 'isolated_bootstrap':
            source = ("import os, sys, runpy; "
                      "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
                      "os.environ.pop('VIRTUAL_ENV', None); "
                      f"sys.path.insert(0, {str(install)!r}); "
                      "os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME') or "
                      "str(__import__('hermes_constants').get_default_hermes_root()); "
                      "import hermes_bootstrap; "
                      "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)")
            argv = [str(python), '-I', '-c', source, 'gateway', 'run']
    _program(gateway, argv)
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 0
    assert any(call[1] == 'kickstart' for call in calls)


@pytest.mark.parametrize('mode', ['foreign_module', 'python_data', 'foreign_bootstrap',
                                 'program_override', 'malformed_environment'])
def test_python_data_other_entrypoints_and_program_override_are_rejected(gateway, mode):
    import sys
    rec, data, agents, _, calls, _ = gateway
    argv = [sys.executable, '-m', 'other', 'hermes', 'gateway', 'run']
    fields = {}
    if mode == 'python_data':
        argv = [sys.executable, '-c', 'print("hermes gateway run")', 'gateway', 'run']
    if mode == 'foreign_bootstrap':
        argv = [sys.executable, '-I', '-c',
                "import os, sys, runpy; runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)",
                'gateway', 'run']
    if mode in ('program_override', 'malformed_environment'):
        argv = [str(_owned_launcher(gateway)), 'gateway', 'run']
        fields = {'Program': '/bin/echo'} if mode == 'program_override' else {'EnvironmentVariables': ['PATH']}
    _program(gateway, argv, **fields)
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 1
    assert not any(call[1] == 'kickstart' for call in calls)


def test_configured_custom_hermes_home_retains_owned_jxa_shape(gateway):
    rec, data, agents, _, calls, _ = gateway
    custom = agents.parent.parent / 'configured-hermes'
    _program(gateway, ['/usr/bin/osascript', '-l', 'JavaScript',
                       'Application("Terminal").doScript("hermes gateway run")'],
             WorkingDirectory=str(custom),
             EnvironmentVariables={'PATH': str(agents.parent.parent / '.local/bin'),
                                   'HERMES_HOME': str(custom)})
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 0
    assert any(call[1] == 'kickstart' for call in calls)


def test_frozen_shared_gateway_program_is_python39_compatible(gateway):
    import ast
    rec, data, agents, _, calls, _ = gateway
    source = rec._GATEWAY_RESTART_PROGRAM
    ast.parse(source, feature_version=(3, 9))
    namespace = {'__name__': 'synthetic_frozen'}
    with pytest.raises(SystemExit) as finished:
        exec(source.rsplit('sys.exit(', 1)[0]
             + f'sys.exit(_gateway_restart_run({str(data)!r}, {str(agents)!r}, "501"))', namespace)
    assert finished.value.code == 0 and any(call[1] == 'kickstart' for call in calls)


@pytest.mark.parametrize('mode', ['console', 'symlink', 'python_module'])
def test_explicit_custom_install_is_bound_to_environment_and_working_directory(gateway, mode):
    import sys
    rec, data, agents, _, calls, _ = gateway
    home = agents.parent.parent
    custom = home / 'configured-hermes'
    install = custom / 'hermes-agent'
    cli = install / 'venv/bin/hermes'
    cli.parent.mkdir(parents=True)
    cli.write_text('#!/bin/sh\nexit 0\n')
    cli.chmod(0o700)
    argv = [str(cli), 'gateway', 'run']
    if mode == 'symlink':
        local = home / '.local/bin/hermes'
        local.unlink()
        local.symlink_to(cli)
        argv[0] = str(local)
    if mode == 'python_module':
        python = install / 'venv/bin/python3'
        python.symlink_to(sys.executable)
        argv = [str(python), '-m', 'hermes_cli.main', 'gateway', 'run']
    _program(gateway, argv, WorkingDirectory=str(install),
             EnvironmentVariables={'HERMES_HOME': str(custom)})
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 0
    assert any(call[1] == 'kickstart' for call in calls)


def test_custom_console_without_matching_environment_and_directory_is_rejected(gateway):
    rec, data, agents, _, calls, _ = gateway
    custom = agents.parent.parent / 'configured-hermes/hermes-agent/venv/bin/hermes'
    custom.parent.mkdir(parents=True)
    custom.write_text('#!/bin/sh\nexit 0\n')
    custom.chmod(0o700)
    _program(gateway, [str(custom), 'gateway', 'run'])
    assert rec._gateway_restart_run(str(data), str(agents), 501) == 1
    assert not any(call[1] == 'kickstart' for call in calls)
