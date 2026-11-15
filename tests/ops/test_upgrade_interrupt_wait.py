"""The standalone bootstrap waits for synthetic children before releasing temporary assets."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from test_mcs_upgrade import _launcher


_CHILD = '''import json, os, pathlib, signal, sys, time
root = pathlib.Path(sys.argv[1])
def interrupted(signum, frame):
    (root / 'cleaning').write_text('yes')
    time.sleep(0.6)
signal.signal(signal.SIGINT, interrupted)
(root / 'ready').write_text(str(os.getpid()))
time.sleep(1.0)
(root / 'done').write_text(json.dumps({'asset_present': pathlib.Path(__file__).exists()}))
sys.exit(int(sys.argv[2]))
'''


def _wait_file(path):
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail('synthetic child did not reach its checkpoint')
        time.sleep(0.01)


@pytest.mark.parametrize('scope', ['group', 'parent', 'second_parent_interrupt'])
def test_bootstrap_interrupt_waits_for_cleanup_before_releasing_assets(tmp_path, scope):
    launcher = Path(__file__).resolve().parents[2] / 'scripts/mcs_upgrade.py'
    parent = tmp_path / 'parent.py'
    parent.write_text(
        'import json, pathlib, runpy, sys, tempfile\n'
        f'run = runpy.run_path({str(launcher)!r})["_run_updater"]\n'
        f'root = pathlib.Path({str(tmp_path)!r})\n'
        'with tempfile.TemporaryDirectory(dir=root) as stage:\n'
        '    child = pathlib.Path(stage) / "child.py"\n'
        f'    child.write_text({_CHILD!r})\n'
        '    try:\n'
        '        run([sys.executable, str(child), str(root), "0"])\n'
        '    except KeyboardInterrupt:\n'
        '        result = {"interrupted": True, "child_done": (root / "done").exists()}\n'
        '    else:\n'
        '        result = {"interrupted": False}\n'
        '(root / "result").write_text(json.dumps({**result, "asset_removed": not child.exists()}))\n'
    )
    process = subprocess.Popen([sys.executable, str(parent)], start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_file(tmp_path / 'ready')
        if scope == 'parent':
            os.kill(process.pid, signal.SIGINT)
        else:
            os.killpg(process.pid, signal.SIGINT)
        if scope == 'second_parent_interrupt':
            _wait_file(tmp_path / 'cleaning')
            time.sleep(0.3)  # pass Popen.wait's initial SIGINT grace period
            os.kill(process.pid, signal.SIGINT)
        _, errors = process.communicate(timeout=15)
        assert process.returncode == 0, errors
        assert json.loads((tmp_path / 'done').read_text()) == {'asset_present': True}
        assert json.loads((tmp_path / 'result').read_text()) == {
            'interrupted': True, 'child_done': True, 'asset_removed': True}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)


def test_bootstrap_preserves_normal_child_exit_code():
    result = _launcher()._run_updater([sys.executable, '-c', 'raise SystemExit(7)'])
    assert result.returncode == 7


def test_bootstrap_preserves_original_system_exit_through_repeated_interrupts(monkeypatch):
    launcher = _launcher()
    original = SystemExit('synthetic original exit')
    failures = [original, KeyboardInterrupt(), SystemExit('second exit'), 9]
    seen = []

    class Child:
        def wait(self):
            value = failures.pop(0)
            seen.append(value)
            if isinstance(value, BaseException):
                raise value
            return value

    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **kw: Child())
    with pytest.raises(SystemExit) as caught:
        launcher._run_updater(['synthetic-updater'])
    assert caught.value is original and seen[-1] == 9 and not failures
