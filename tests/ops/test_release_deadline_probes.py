"""Synthetic deadline propagation; never invokes services, credentials or network."""
import json
import os
import fcntl
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import environment_diagnostics
import lifecycle_io
import maintenance
import mcs_update
import owned_process
import runtime_inspection
import service_management


def test_lifecycle_deadline_interrupts_a_real_wait():
    start = time.monotonic()
    result = lifecycle_io.run_bounded([sys.executable, '-I', '-c', 'import time; time.sleep(10)'], timeout=20, deadline=start + .25)
    assert result.returncode == 124 and time.monotonic() - start < 3


@pytest.mark.parametrize('deadline', [True, False, '1', float('inf'), float('nan')])
def test_lifecycle_invalid_deadline_never_spawns(monkeypatch, deadline):
    monkeypatch.setattr(lifecycle_io.subprocess, 'run', lambda *_a, **_k: pytest.fail('invalid deadline spawned'))
    with pytest.raises(ValueError, match='lifecycle_deadline_invalid'):
        lifecycle_io.run_bounded(['synthetic'], deadline=deadline)


def test_expired_lifecycle_deadline_never_spawns(monkeypatch):
    monkeypatch.setattr(lifecycle_io.subprocess, 'run', lambda *_a, **_k: pytest.fail('expired deadline spawned'))
    assert lifecycle_io.run_bounded(['synthetic'], deadline=time.monotonic() - 1).returncode == 124


@pytest.mark.parametrize('text,value', [(True, 'synthetic'), (False, b'\x00synthetic')])
def test_updater_deadline_wrapper_preserves_text_input_and_binary_output(text, value):
    program = 'import sys; data=sys.stdin.read(); sys.stdout.write(data.upper())' if text else 'import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data.upper())'
    result = mcs_update._run_with_lifetime([sys.executable, '-I', '-c', program], input=value, capture_output=True,
        text=text, timeout=20, deadline=time.monotonic() + 3)
    assert result.returncode == 0 and result.stdout == value.upper()


def test_updater_deadline_stops_waiting_instead_of_accepting_a_late_child_result():
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        mcs_update._run_with_lifetime([sys.executable, '-I', '-c', 'import time; time.sleep(10)'],
            capture_output=True, timeout=20, deadline=start + .25)
    assert time.monotonic() - start < 3


def test_git_receives_the_remaining_budget_and_no_lazy_fetch_environment(monkeypatch):
    now, seen = [100.0], []
    monkeypatch.setattr(mcs_update.time, 'monotonic', lambda: now[0])
    def git(args, timeout, environment, **kwargs):
        seen.append((args, timeout, environment, kwargs))
        return subprocess.CompletedProcess(args, 0, 'synthetic', '')
    monkeypatch.setattr(mcs_update.owned_process, 'run_owned', git)
    mcs_update._git(['status'], timeout=30, deadline=100.5)
    assert seen[0][0] == ['/usr/bin/git', '-C', mcs_update.REPO, '--work-tree', mcs_update.REPO, 'status']
    assert seen[0][1] == .5 and seen[0][3]['deadline'] == 100.5
    env = seen[0][2]
    assert env['GIT_NO_LAZY_FETCH'] == env['GIT_NO_REPLACE_OBJECTS'] == '1'
    assert env['GIT_CONFIG_VALUE_3'] == 'never' and env['GIT_CONFIG_VALUE_4'] == '/dev/null'
    now[0] = 101
    with pytest.raises(mcs_update.UpdateError, match='release_prequiesce_timeout'):
        mcs_update._git(['status'], deadline=100.5)
    assert len(seen) == 1


def test_runtime_and_service_probes_forward_the_same_absolute_deadline(tmp_path, monkeypatch):
    calls = []
    facts = {'python': '3.14.7', 'sqlite': '3.51.3', 'packages': {'discord.py': None, 'slack-bolt': None, 'slack-sdk': None}}
    def run(argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps(facts))
    monkeypatch.setattr(runtime_inspection, 'run_bounded', run)
    deadline = time.monotonic() + 10
    assert runtime_inspection.probe_python(sys.executable, deadline=deadline) == facts
    assert calls[0]['deadline'] == deadline
    monkeypatch.setattr(service_management, 'service_substitutions', lambda *_a, **_k: {'PYTHON': sys.executable})
    paths = SimpleNamespace(agents_dir=str(tmp_path), checkout=str(tmp_path))
    service_management.runtime_gate_errors({'recovery_python': sys.executable}, paths=paths, deadline=deadline)
    assert len(calls) == 3 and all(v['deadline'] == deadline for v in calls)


def test_baseline_diagnostic_owner_receives_deadline_and_does_not_swallow_expiry(monkeypatch):
    seen, now = [], [100.0]
    monkeypatch.setattr(mcs_update.time, 'monotonic', lambda: now[0])
    def check(cfg, **kwargs):
        seen.append(kwargs['deadline'])
        now[0] = 102
        return [], []
    monkeypatch.setattr(environment_diagnostics, 'check_environment', check)
    with pytest.raises(mcs_update.UpdateError, match='release_prequiesce_timeout'):
        mcs_update._baseline_check({}, deadline=101)
    assert seen == [101]


def test_busy_backup_stops_within_budget_and_does_not_publish_a_partial_file(tmp_path):
    source, staging, destination = (tmp_path / name for name in ('source.db', 'staging.db', 'destination.db'))
    connection = sqlite3.connect(source)
    connection.execute('CREATE TABLE synthetic(value)')
    connection.commit()
    connection.execute('BEGIN EXCLUSIVE')
    start = time.monotonic()
    try:
        with pytest.raises((maintenance.MaintenanceError, sqlite3.OperationalError)):
            maintenance._publish_backup(str(source), str(staging), str(destination), deadline=start + .25)
        assert time.monotonic() - start < 3
        assert not staging.exists() and not destination.exists()
    finally:
        connection.rollback()
        connection.close()


def test_deadline_probe_inherits_only_duplicates_of_the_callers_lock(tmp_path):
    path = tmp_path / 'synthetic.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(descriptor)
        program = 'import os,sys; expected=(int(sys.argv[1]),int(sys.argv[2])); found=[]\nfor fd in range(3,256):\n try:\n  s=os.fstat(fd)\n  if (s.st_dev,s.st_ino)==expected: found.append(fd)\n except OSError: pass\nassert len(found)==1,found\nprint("inherited")'
        with owned_process.lifetime_fds((descriptor,)):
            result = lifecycle_io.run_bounded([sys.executable, '-I', '-c', program, str(info.st_dev), str(info.st_ino)], deadline=time.monotonic() + 5)
        assert result.returncode == 0 and result.stdout == 'inherited\n'
        assert os.fstat(descriptor).st_ino == info.st_ino
    finally:
        os.close(descriptor)


def test_owned_deadline_preserves_text_input_working_directory_and_capacity(tmp_path):
    result = owned_process.run_owned([sys.executable, '-I', '-c', 'import os,sys; print(os.getcwd()); print(sys.stdin.read().upper())'],
        5, dict(os.environ), deadline=time.monotonic() + 5, input_text='synthetic', cwd=tmp_path)
    assert result.returncode == 0 and result.stdout.splitlines() == [str(tmp_path), 'SYNTHETIC']
    for value in (b'synthetic', 'x' * 16385, True):
        with pytest.raises(ValueError, match='owned_input_invalid'):
            owned_process.run_owned(['synthetic-never-spawn'], 5, {}, deadline=time.monotonic() + 5, input_text=value)


def test_owned_expiry_during_fd_preparation_starts_no_child(monkeypatch):
    from contextlib import contextmanager
    now = [100.0]
    monkeypatch.setattr(owned_process.time, 'monotonic', lambda: now[0])
    @contextmanager
    def prepare(*_):
        now[0] = 102
        yield {}
    monkeypatch.setattr(owned_process, '_inherited_fds', prepare)
    monkeypatch.setattr(owned_process.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('expired child spawned'))
    with pytest.raises(subprocess.TimeoutExpired):
        owned_process.run_owned(['synthetic'], 5, {}, deadline=101)


def test_expired_probe_stops_its_actual_helper_group_and_reaps_the_leader(tmp_path, monkeypatch):
    from test_git_timeout_cleanup import _group_gone
    helper_pid = tmp_path / 'helper.pid'
    program = ('import pathlib,subprocess,sys,time; '
        'child=subprocess.Popen([sys.executable,"-I","-c","import time; time.sleep(30)"]); '
        'pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)')
    children = []
    original = owned_process.subprocess.Popen
    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(owned_process.subprocess, 'Popen', popen)
    result = lifecycle_io.run_bounded([sys.executable, '-I', '-c', program, str(helper_pid)], deadline=time.monotonic() + 1)
    monkeypatch.setattr(owned_process.subprocess, 'Popen', original)
    assert result.returncode == 124 and helper_pid.exists()
    assert len(children) == 1 and children[0].returncode is not None
    assert _group_gone(children[0].pid)
