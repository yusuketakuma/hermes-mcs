"""The CLI wire owner has no callback to outbox state or live connection."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import notify_sender


def test_sender_import_and_target_need_no_flush_owner(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    code = """
import importlib.abc
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'mcs'))
import _mcs_path
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'notify_flush', 'ledger', 'notify_cards'}:
            raise AssertionError('sender called back to state owner')
sys.meta_path.insert(0, Refuse())
import notify_sender
assert notify_sender.target({'notify_target': 'slack:SYNTHETIC'}, 'new_messages') == 'slack:SYNTHETIC'
assert notify_sender.target({'notify_system_target': 'slack:SYSTEM'}, 'new_messages') is None
assert notify_sender.target({'notify_system_target': 'slack:SYSTEM'}, 'run_failed') == 'slack:SYSTEM'
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(repo)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('direct,code,error', [
    (True, 75, notify_sender.SendFailed),
    (False, 75, notify_sender.SendUncertain),
    (True, 2, notify_sender.SendUsage),
    (False, 1, notify_sender.SendUncertain),
])
def test_sender_exit_classification_preserves_negative_proof(monkeypatch, direct, code, error):
    argv = ['synthetic-python', '/synthetic/mcs_standalone/__main__.py'] if direct else ['hermes', 'send']
    seen = []

    def fake(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=code, stdout='', stderr='synthetic_fixed_code')

    monkeypatch.setattr(notify_sender.subprocess, 'run', fake)
    with pytest.raises(error):
        notify_sender.send(argv, 'synthetic')
    assert len(seen) == 1 and seen[0][0] == argv
    assert seen[0][1]['timeout'] == 180
