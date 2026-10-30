"""Synthetic read-only probes stay bounded when another handler reaps their child."""
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

import environment_diagnostics
import lifecycle_io


def test_process_start_probe_does_not_enter_a_mutation_lease_hold_after_external_reap():
    source = Path(environment_diagnostics.__file__).resolve().parents[1]
    program = textwrap.dedent(f"""
        import json, os, signal, socket, subprocess, sys, time
        sys.path.insert(0, {str(source)!r})
        import _mcs_path
        import environment_diagnostics

        class NoNetwork(socket.socket):
            def connect(self, *args, **kwargs):
                raise AssertionError('network forbidden')
            def connect_ex(self, *args, **kwargs):
                raise AssertionError('network forbidden')
        socket.socket = NoNetwork
        reaped = []
        def reap(*args):
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    return
                if not pid:
                    return
                reaped.append(pid)
        signal.signal(signal.SIGCHLD, reap)
        original = subprocess.Popen
        def probe(argv, **kwargs):
            assert argv == ['ps', '-o', 'lstart=', '-p', '424242']
            helper = 'import os,time; pid=os.fork(); time.sleep(.8) if pid == 0 else None; os._exit(0)'
            child = original([sys.executable, '-I', '-c', helper], **kwargs)
            until = time.monotonic() + 1
            while child.pid not in reaped:
                assert time.monotonic() < until
                time.sleep(.005)
            return child
        subprocess.Popen = probe
        started = time.monotonic()
        result = environment_diagnostics.process_started_at(424242, deadline=started + .15)
        elapsed = time.monotonic() - started
        assert result is None and reaped
        assert elapsed < .7, elapsed
        time.sleep(1)
        print(json.dumps({{'result': result, 'reaped': len(reaped)}}))
    """)
    result = subprocess.run([sys.executable, '-I', '-c', program], capture_output=True,
                            text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['result'] is None


@pytest.mark.parametrize('deadline', [True, False, '1', float('inf'), float('nan')])
def test_invalid_process_probe_deadline_never_spawns(monkeypatch, deadline):
    monkeypatch.setattr(lifecycle_io, 'run_bounded',
                        lambda *args, **kwargs: pytest.fail('invalid deadline spawned'))
    with pytest.raises(ValueError, match='lifecycle_deadline_invalid'):
        environment_diagnostics.process_started_at(424242, deadline=deadline)


def test_expired_process_probe_deadline_never_spawns(monkeypatch):
    monkeypatch.setattr(environment_diagnostics.time, 'monotonic', lambda: 100)
    monkeypatch.setattr(lifecycle_io, 'run_bounded',
                        lambda *args, **kwargs: pytest.fail('expired deadline spawned'))
    assert environment_diagnostics.process_started_at(424242, deadline=100) is None


def test_late_process_metadata_is_not_accepted(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(environment_diagnostics.time, 'monotonic', lambda: now[0])
    def run(argv, **kwargs):
        now[0] = 102.0
        return subprocess.CompletedProcess(argv, 0, 'Mon Jan 1 00:00:00 2024', '')
    monkeypatch.setattr(lifecycle_io, 'run_bounded', run)
    assert environment_diagnostics.process_started_at(424242, deadline=101) is None
