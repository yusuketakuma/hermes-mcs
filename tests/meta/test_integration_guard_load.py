"""An integration-only pytest run must load the tests/conftest.py guards."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_integration_only_run_installs_guards():
    # a fresh interpreter collects only integration/, so tests/conftest.py
    # is not a conftest of this run — integration/conftest.py must load it.
    # Drop this run's guard-bin from PATH so the child's bare-launchctl shim
    # check proves its own guards; the socket check fails first otherwise,
    # so a regression never reaches the real launchctl.
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                           if os.path.basename(d) != "guard-bin")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=",
         "-p", "no:cacheprovider", "integration/test_integration_guards.py"],
        cwd=ROOT, env=dict(os.environ, PATH=path), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
