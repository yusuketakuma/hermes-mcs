"""Install tests/conftest.py's process guards for integration-only runs.

pytest loads tests/conftest.py only when a path under tests/ is collected,
so ``scripts/run_tests.sh integration/<file>`` would otherwise run with no
socket, urlopen, launchctl/hermes/security, live LLM/Jev or MCS worker
guard. The guards mark ``socket.socket.connect`` as ``_blocked``; skip when
they are already installed (``-p integration.sdk_safety`` or tests/ first).
"""
from pathlib import Path
import runpy
import socket

if socket.socket.connect.__name__ != "_blocked":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/conftest.py"))
