"""Synthetic test runs must not launch the native Keychain owner child."""
from pathlib import Path
import shlex
import sys

import pytest


@pytest.mark.parametrize('entry', ['absolute', 'relative', 'shell', 'bash-c', 'sh-lc',
                                   'bash-c-relative', 'direct', 'module', 'cd-chain'])
@pytest.mark.parametrize('operation', ['run', 'popen'])
def test_native_keychain_child_is_rejected_before_process_creation(monkeypatch, entry, operation):
    guard = sys.modules['mcs_test_guard']
    child = Path(__file__).resolve().parents[2] / 'mcs/ops/sdc_keychain.py'
    command = [sys.executable, '-I', str(child), '3', '4']
    monkeypatch.chdir(child.parents[2])
    if entry == 'relative':
        command[2] = 'mcs/ops/sdc_keychain.py'
    elif entry == 'shell':
        command = shlex.join(command)
    elif entry in {'bash-c', 'sh-lc'}:
        command = ['/bin/bash' if entry == 'bash-c' else 'sh', '-c' if entry == 'bash-c' else '-lc',
                   shlex.join(command)]
    elif entry == 'bash-c-relative':
        command = ['bash', '-lc', shlex.join([sys.executable, '-I', 'mcs/ops/sdc_keychain.py', '3', '4'])]
    elif entry == 'direct':
        command = [str(child), '3', '4']
    elif entry == 'module':
        command = [sys.executable, '-I', '-m', 'mcs.ops.sdc_keychain', '3', '4']
    elif entry == 'cd-chain':
        command = 'cd mcs/ops && exec python3 -I sdc_keychain.py 3 4'

    def forbidden(*args, **kwargs):
        pytest.fail('the native child must never start')

    monkeypatch.setattr(guard, '_ORIGINAL_' + operation.upper(), forbidden)
    with pytest.raises(RuntimeError, match='disabled in MCS tests'):
        getattr(guard, '_guarded_' + operation)(command, shell=isinstance(command, str))


def test_explicit_synthetic_worker_stays_available(tmp_path, monkeypatch):
    guard = sys.modules['mcs_test_guard']
    seen = []
    command = [sys.executable, '-I', str(tmp_path / 'synthetic-worker.py')]
    monkeypatch.setattr(guard, '_ORIGINAL_POPEN', lambda argv: seen.append(argv) or 'synthetic')
    assert guard._guarded_popen(command) == 'synthetic'
    assert seen == [command]
