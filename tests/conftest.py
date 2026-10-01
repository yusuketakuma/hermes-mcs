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
import urllib.parse
import urllib.request
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS.parent / "mcs"))
import _mcs_path  # noqa: E402,F401  registers every subdir as import root

# Register module directories at every depth so area testkits remain
# importable when adapter tests are grouped by transport.
for _root, _dirs, _files in os.walk(_TESTS):
    _dirs[:] = sorted(d for d in _dirs if not d.startswith((".", "_")))
    if _root != str(_TESTS) and any(f.endswith(".py") for f in _files):
        sys.path.insert(0, _root)


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
    # Synthetic test bodies are placeholders, not clinical text — keep
    # the historical "every pending message is extracted" contract; the
    # prefilter path is exercised by tests that opt in explicitly.
    os.environ["MCS_EXTRACT_PREFILTER"] = "off"
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

# The bounded-http transport spawns a fresh interpreter for every
# request, so the in-process socket guard cannot reach it — a call that
# slips past a test's fakes would hit the real local LLM / Jev for real.
# Synthetic loopback servers stay allowed (real-transport tests exercise
# the worker against their own fixture endpoints); only the production
# endpoints are denied at the boundary.
import bounded_http  # noqa: E402
import local_llm  # noqa: E402
import semantic_jev as _jev  # noqa: E402

_LIVE_ENDPOINTS = frozenset(
    {local_llm.ENDPOINT, _jev.JEV_ENDPOINT, _jev.JEV_MODELS_URL,
     *local_llm.probe_urls(local_llm.ENDPOINT)}
    | set(_jev.JEV_ALLOWED_ENDPOINTS))
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _live_authority(url):
    # (loopback, port) of every production endpoint — any path on the
    # live llama-server/Jev authority (/slots, /v1/models, a localhost
    # spelling) is as live as the chat endpoint itself
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except (TypeError, ValueError):
        return None
    if parts.hostname in _LOOPBACK_NAMES:
        return ("loopback", port)
    return (parts.hostname, port)


_LIVE_AUTHORITIES = frozenset(
    a for a in map(_live_authority, _LIVE_ENDPOINTS) if a is not None)


def _guarded_http_request(endpoint, *args, **kwargs):
    # Only a real spawn can reach a live endpoint — tests that stub
    # Popen (wire-replay fakes, synthetic workers) never touch the
    # network, so they pass through and exercise the transport contract.
    if ((endpoint in _LIVE_ENDPOINTS
         or _live_authority(endpoint) in _LIVE_AUTHORITIES)
            and subprocess.Popen is _guarded_popen):
        raise RuntimeError(
            "live LLM/Jev endpoints are disabled in MCS tests")
    return _ORIG_HTTP_REQUEST(endpoint, *args, **kwargs)


# ``bounded_http`` is the single HTTP worker entry: JevClient and
# local_llm.bounded_request both call it by module attribute, so patching
# this one name covers every caller.
_ORIG_HTTP_REQUEST = bounded_http.bounded_http_request
_install(bounded_http, "bounded_http_request", _guarded_http_request)


# MCS and Chrome use a separate subprocess transport too. Only tests
# replacing the worker command with an explicitly synthetic interpreter
# may spawn it; replacing Popen alone could still wrap a real process.
import mcs_worker  # noqa: E402
import mcs_adapter  # noqa: E402

_ORIG_MCS_CALL = mcs_worker.bounded_call
_ORIG_MCS_COMMAND = mcs_worker._worker_command


def _guarded_mcs_call(*args, **kwargs):
    if mcs_worker._worker_command is _ORIG_MCS_COMMAND:
        raise RuntimeError("live MCS/CDP workers are disabled in MCS tests")
    return _ORIG_MCS_CALL(*args, **kwargs)


_install(mcs_worker, "bounded_call", _guarded_mcs_call)
_install(mcs_adapter, "bounded_call", _guarded_mcs_call)


def _restore() -> None:
    while _ORIGINALS:
        module, name, original = _ORIGINALS.pop()
        setattr(module, name, original)
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


atexit.register(_restore)


def pytest_unconfigure(config) -> None:
    _restore()
