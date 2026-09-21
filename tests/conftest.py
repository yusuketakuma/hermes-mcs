"""Process boundaries for the adapter's synthetic tests.

The production modules intentionally contain network, Keychain, and Chrome
entry points.  Tests exercise those contracts with fakes; this guard makes an
accidental live call fail at the boundary while leaving Python subprocesses
used by the synthetic CLI tests available.
"""
from __future__ import annotations

import atexit
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcs"))


_SENSITIVE_ENV = (
    "TOKEN", "API_KEY", "APIKEY", "SECRET", "PASSWORD", "PASSWD",
    "COOKIE", "CREDENTIAL", "PRIVATE_KEY", "ACCESS_KEY",
)
_ORIGINALS: list[tuple[object, str, object]] = []
_TEST_HOME = tempfile.mkdtemp(prefix="mcs-test-home-")


def _reset_environment() -> None:
    """Give direct pytest invocations the same isolated HOME as the runner."""
    for key in tuple(os.environ):
        upper = key.upper()
        if any(part in upper for part in _SENSITIVE_ENV):
            os.environ.pop(key, None)
    os.environ["HOME"] = _TEST_HOME
    os.environ["XDG_CONFIG_HOME"] = os.path.join(_TEST_HOME, "config")
    os.environ["XDG_CACHE_HOME"] = os.path.join(_TEST_HOME, "cache")
    os.environ["XDG_DATA_HOME"] = os.path.join(_TEST_HOME, "data")
    os.environ["TMPDIR"] = os.path.join(_TEST_HOME, "tmp")
    for name in ("config", "cache", "data", "tmp"):
        os.makedirs(os.path.join(_TEST_HOME, name), exist_ok=True)


def _blocked(*args, **kwargs):
    raise RuntimeError("external network access is disabled in MCS tests")


def _command_parts(command) -> list[str]:
    if command is None:
        return []
    if isinstance(command, bytes):
        command = command.decode(errors="replace")
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return [command]
    return [os.fspath(part) if isinstance(part, os.PathLike) else str(part)
            for part in command]


def _blocked_process(command) -> bool:
    parts = _command_parts(command)
    if not parts:
        return False
    executable = os.path.basename(parts[0]).casefold()
    if executable == "security":
        return True
    return executable in {
        "chrome", "google chrome", "chromium", "chromium-browser",
        "google-chrome", "google-chrome-stable", "msedge",
    }


def _guarded_run(*args, **kwargs):
    command = kwargs.get("args", args[0] if args else None)
    if _blocked_process(command):
        raise RuntimeError("Keychain and Chrome processes are disabled in MCS tests")
    return _ORIGINAL_RUN(*args, **kwargs)


def _guarded_popen(*args, **kwargs):
    command = kwargs.get("args", args[0] if args else None)
    if _blocked_process(command):
        raise RuntimeError("Keychain and Chrome processes are disabled in MCS tests")
    return _ORIGINAL_POPEN(*args, **kwargs)


def _install(module, name: str, replacement) -> None:
    _ORIGINALS.append((module, name, getattr(module, name)))
    setattr(module, name, replacement)


_reset_environment()
_ORIGINAL_RUN = subprocess.run
_ORIGINAL_POPEN = subprocess.Popen
_install(urllib.request, "urlopen", _blocked)
_install(urllib.request.OpenerDirector, "open", _blocked)
_install(socket, "create_connection", _blocked)
_install(socket.socket, "connect", _blocked)
_install(socket.socket, "connect_ex", _blocked)
_install(subprocess, "run", _guarded_run)
_install(subprocess, "Popen", _guarded_popen)


def _restore() -> None:
    while _ORIGINALS:
        module, name, original = _ORIGINALS.pop()
        setattr(module, name, original)
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


atexit.register(_restore)


def pytest_unconfigure(config) -> None:
    _restore()
