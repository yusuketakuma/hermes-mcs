"""The standalone drain help entry must bootstrap without starting work."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


DRAIN = Path(__file__).resolve().parents[2] / "mcs" / "semantic" / "semantic_drain.py"


@pytest.mark.parametrize("isolated", [False, True])
def test_drain_help_bootstraps_without_work(tmp_path, isolated):
    home = tmp_path / "home"
    root = tmp_path / "mcs-root"
    cwd = tmp_path / "other-directory"
    for path in (home, root, cwd):
        path.mkdir()
    env = {"HOME": str(home), "MCS_ROOT": str(root), "PATH": os.defpath, "PYTHONPATH": ""}
    flags = ["-I"] if isolated else []
    command = [sys.executable, *flags, "-B", str(DRAIN), "--help"]
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--drain" in result.stdout and "--revive-failed" in result.stdout

    # A second invocation observes the same entry point with Python audit hooks,
    # so help cannot open a database, access a socket, or spawn a worker.
    guarded = '''\
import runpy, sys

def deny_work(event, args):
    if event.startswith("sqlite3.connect") or event.startswith("socket.") or event in ("subprocess.Popen", "os.system"):
        raise RuntimeError("unexpected work during help")

sys.addaudithook(deny_work)
sys.argv = [sys.argv[1], "--help"]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    result = subprocess.run([sys.executable, *flags, "-B", "-c", guarded, str(DRAIN)],
                            cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--drain" in result.stdout and "--revive-failed" in result.stdout
    assert not list(home.iterdir()) and not list(root.iterdir())
