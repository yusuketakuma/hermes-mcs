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
    agents = tmp_path / 'agents'
    agents.mkdir()
    data = tmp_path / 'data'
    data.mkdir()
    plist = agents / 'ai.hermes.gateway.plist'
    plist.write_bytes(plistlib.dumps({
        'Label': 'ai.hermes.gateway',
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
