"""Service reconciliation keeps helper processes inside its owned session."""
import subprocess
import sys
import time

import pytest

import mcs_update
import owned_process
from ops_testkit import _load, patch_lifecycle_lookup


@pytest.mark.parametrize('kind', ['update', 'recovery'])
@pytest.mark.parametrize('failure', ['timeout', 'unavailable', 'nonzero'])
def test_services_failure_uses_owned_runner_and_fixed_diagnostics(
        tmp_path, monkeypatch, kind, failure):
    module = mcs_update if kind == 'update' else _load()
    script = tmp_path / 'mcs/ops/mcs_setup.py'
    script.parent.mkdir(parents=True)
    script.write_text('# fully synthetic service entry\n')
    patch_lifecycle_lookup(monkeypatch, module, 'REPO', str(tmp_path))
    if kind == 'update':
        # Run-log installation has separate coverage; exercise owned service cleanup.
        patch_lifecycle_lookup(monkeypatch, module, '_prepare_run_log', lambda: None)
    if kind == 'recovery':
        patch_lifecycle_lookup(monkeypatch, module, 'AGENTS_DIR', str(tmp_path / 'no-agents'))
        patch_lifecycle_lookup(monkeypatch, module, 'MANIFEST_PATH', str(tmp_path / 'no-manifest'))
        patch_lifecycle_lookup(monkeypatch, module, '_runtime_mode', lambda: 'standalone')
        patch_lifecycle_lookup(monkeypatch, module, '_runtime_config', lambda: {})
        patch_lifecycle_lookup(monkeypatch, module, '_setup_python', lambda: sys.executable)
    calls = []

    def owned(argv, timeout, env, stdin=None):
        calls.append(argv)
        assert argv == [sys.executable, str(script), 'services']
        assert timeout == 120 and stdin is subprocess.DEVNULL
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, timeout)
        if failure == 'unavailable':
            raise OSError('SYNTHETIC_SECRET_CANARY')
        return subprocess.CompletedProcess(argv, 1, 'SYNTHETIC_SECRET_CANARY', '')

    patch_lifecycle_lookup(monkeypatch, owned_process if kind == 'update' else module,
                        'run_owned' if kind == 'update' else '_run_owned', owned)
    # A direct subprocess.run would appear successful and bypass cleanup.
    monkeypatch.setattr(module.subprocess, 'run', lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 0, '', ''))
    if kind == 'update':
        with pytest.raises(module.UpdateError, match='services_failed') as error:
            module._services_reconcile()
        assert 'SYNTHETIC_SECRET_CANARY' not in str(error.value)
    else:
        assert module._reconcile_membership({}) == ['services_reconcile_failed']
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['update', 'recovery'])
def test_service_timeout_stops_its_synthetic_helper_tree(tmp_path, monkeypatch, kind):
    module = mcs_update if kind == 'update' else _load()
    script = tmp_path / 'mcs/ops/mcs_setup.py'
    script.parent.mkdir(parents=True)
    pidfile = tmp_path / 'helper.pid'
    helper = tmp_path / 'synthetic-helper.py'
    helper.write_text('import time; time.sleep(30)\n')
    script.write_text(
        'import subprocess, sys, time\n'
        'from pathlib import Path\n'
        f'child = subprocess.Popen([sys.executable, {str(helper)!r}])\n'
        f'Path({str(pidfile)!r}).write_text(str(child.pid))\n'
        'time.sleep(30)\n')
    patch_lifecycle_lookup(monkeypatch, module, 'REPO', str(tmp_path))
    if kind == 'update':
        # Run-log installation has separate coverage; exercise owned service cleanup.
        patch_lifecycle_lookup(monkeypatch, module, '_prepare_run_log', lambda: None)
    if kind == 'recovery':
        patch_lifecycle_lookup(monkeypatch, module, 'AGENTS_DIR', str(tmp_path / 'no-agents'))
        patch_lifecycle_lookup(monkeypatch, module, 'MANIFEST_PATH', str(tmp_path / 'no-manifest'))
        patch_lifecycle_lookup(monkeypatch, module, '_runtime_mode', lambda: 'standalone')
        patch_lifecycle_lookup(monkeypatch, module, '_runtime_config', lambda: {})
        patch_lifecycle_lookup(monkeypatch, module, '_setup_python', lambda: sys.executable)
    run = owned_process.run_owned if kind == 'update' else module._run_owned
    patch_lifecycle_lookup(monkeypatch, owned_process if kind == 'update' else module,
                        'run_owned' if kind == 'update' else '_run_owned', lambda argv, timeout, env, stdin=None:
                        run(argv, 2, env, stdin))
    try:
        if kind == 'update':
            with pytest.raises(module.UpdateError, match='services_failed'):
                module._services_reconcile()
        else:
            assert module._reconcile_membership({}) == ['services_reconcile_failed']
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        until = time.monotonic() + 2
        while True:
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            if not state or state.startswith('Z'):
                break
            assert time.monotonic() < until, 'owned helper survived service timeout'
            time.sleep(0.05)
    finally:
        if pidfile.exists():
            import os
            import signal
            from contextlib import suppress
            pid = int(pidfile.read_text())
            args = subprocess.run(['ps', '-o', 'args=', '-p', str(pid)],
                                  capture_output=True, text=True, timeout=2).stdout
            if str(helper) in args:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
