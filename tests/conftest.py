"""Process boundaries for the adapter's synthetic tests.

The production modules intentionally contain network, Keychain, and Chrome
entry points.  Tests exercise those contracts with fakes; this guard makes an
accidental live call fail at the boundary while leaving Python subprocesses
used by the synthetic CLI tests available.
"""
from __future__ import annotations

import atexit
import ipaddress
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

import pytest
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
    """Isolate a direct pytest run: temp HOME/XDG/TMPDIR, no secret-looking or
    exported MCS root variables, the sandbox flag and child-PATH shims. This is
    narrower than scripts/run_tests.sh, which starts from an empty env -i."""
    for key in tuple(os.environ):
        upper = key.upper()
        if any(part in upper for part in _SENSITIVE_ENV):
            os.environ.pop(key, None)
    # mcs_util derives HOME/REPO from these at import: an exported live
    # root must never reach a direct pytest run (the runner's env -i drops it)
    for key in ("MCS_ROOT", "MCS_UPDATE_REPO"):
        os.environ.pop(key, None)
    os.environ["MCS_TEST_SANDBOX"] = "1"
    os.environ["HOME"] = _TEST_HOME
    os.environ["XDG_CONFIG_HOME"] = os.path.join(_TEST_HOME, "config")
    os.environ["XDG_CACHE_HOME"] = os.path.join(_TEST_HOME, "cache")
    os.environ["XDG_DATA_HOME"] = os.path.join(_TEST_HOME, "data")
    os.environ["TMPDIR"] = os.path.join(_TEST_HOME, "tmp")
    # Synthetic test bodies are placeholders, not clinical text — keep
    # the historical "every pending message is extracted" contract; the
    # prefilter path is exercised by tests that opt in explicitly.
    os.environ["MCS_EXTRACT_PREFILTER"] = "off"
    # Child processes do not pass through the subprocess guard: drop every
    # PATH entry that resolves a real Hermes CLI, so `hermes` is unreachable.
    # launchctl lives in /bin and security in /usr/bin, so shadow them with
    # fail-closed guards first (a bare-name call only; an absolute path is not caught).
    guard = os.path.join(_TEST_HOME, "guard-bin")
    os.makedirs(guard, exist_ok=True)
    for name in ("hermes", "launchctl", "security"):
        script = os.path.join(guard, name)
        with open(script, "w") as stream:
            stream.write("#!/bin/sh\necho 'real %s is disabled in MCS tests' >&2\nexit 97\n" % name)
        os.chmod(script, 0o755)
    os.environ["PATH"] = os.pathsep.join([guard] + [
        d for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and d != guard and not os.access(os.path.join(d, "hermes"), os.X_OK)])
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


_KEYCHAIN_CHILDREN = ("sdc_keychain.py", "sdc_keychain_read.py")


def _shell_tokens(parts, depth=0):
    for part in parts:
        yield part
        if depth < 3 and any(c.isspace() for c in part):
            yield from _shell_tokens(_command_parts(part), depth + 1)


def _keychain_child(token: str) -> bool:
    token = token.rstrip(";&|)")
    return (os.path.basename(token) in _KEYCHAIN_CHILDREN
            or token in {"mcs.ops.sdc_keychain", "mcs.ops.sdc_keychain_read"})


def _blocked_process(command, env=None) -> bool:
    parts = _command_parts(command)
    if not parts:
        return False
    # The native Keychain owner child (and its retired name) must never start in synthetic
    # tests, including as the entry itself, via `-m`, or inside a `sh -c` script string.
    if any(_keychain_child(token) for token in _shell_tokens(parts)):
        return True
    executable = os.path.basename(parts[0]).casefold()
    if executable == "security":
        return True
    if executable in {"hermes", "launchctl"}:
        # A real Hermes CLI or launchd writes the user's LaunchAgents and
        # services even under a temp HOME (2026-10-04: a services test
        # replaced the live gateway plist). Only stubs under the test
        # temp tree may run.
        path = (env or os.environ).get("PATH") if isinstance(env, dict) else None
        found = parts[0] if os.sep in parts[0] else shutil.which(parts[0], path=path)
        if found is None:
            return False      # not runnable: the caller sees its own OSError
        real = os.path.realpath(found)
        return not any(real.startswith(os.path.realpath(root) + os.sep)
                       for root in (_TEST_HOME, tempfile.gettempdir()))
    return executable in {
        "chrome", "google chrome", "chromium", "chromium-browser",
        "google-chrome", "google-chrome-stable", "msedge",
    }


def _guarded_run(*args, **kwargs):
    command = kwargs.get("args", args[0] if args else None)
    if _blocked_process(command, kwargs.get("env")):
        raise RuntimeError("Keychain, Chrome, real Hermes and launchctl processes "
                           "are disabled in MCS tests")
    return _ORIGINAL_RUN(*args, **kwargs)


def _guarded_popen(*args, **kwargs):
    command = kwargs.get("args", args[0] if args else None)
    if _blocked_process(command, kwargs.get("env")):
        raise RuntimeError("Keychain, Chrome, real Hermes and launchctl processes "
                           "are disabled in MCS tests")
    return _ORIGINAL_POPEN(*args, **kwargs)


def _install(module, name: str, replacement) -> None:
    _ORIGINALS.append((module, name, getattr(module, name)))
    setattr(module, name, replacement)


_reset_environment()
# A stable name for tests that inspect the guards; several conftest.py
# files (root, tests/, integration/) may share the bare "conftest" name.
sys.modules.setdefault("mcs_test_guard", sys.modules[__name__])
_ORIGINAL_RUN = subprocess.run
_ORIGINAL_POPEN = subprocess.Popen
_install(urllib.request, "urlopen", _blocked)
_install(urllib.request.OpenerDirector, "open", _blocked)
_install(socket, "create_connection", _blocked)
_install(socket.socket, "connect", _blocked)
_install(socket.socket, "connect_ex", _blocked)
_install(socket.socket, "sendto", _blocked)
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def _guarded_getaddrinfo(host, *args, **kwargs):
    """Resolve only what needs no DNS (None, localhost, numeric) so loopback
    binds keep working while a real hostname never leaves the machine."""
    name = host.decode() if isinstance(host, bytes) else host
    if name is None or name == "localhost":
        return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)
    try:
        # the whole literal, scope included: "192.0.2.1%x" or "fe80::1%a%b" is
        # not numeric, and whitespace or control characters never are
        ipaddress.ip_address(name)
        literal = name.isprintable() and not any(c.isspace() for c in name)
    except (ValueError, TypeError):
        literal = False
    if not literal:
        return _blocked()
    return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)


_install(socket, "getaddrinfo", _guarded_getaddrinfo)
_install(subprocess, "run", _guarded_run)
_install(subprocess, "Popen", _guarded_popen)

# The bounded-http transport spawns a fresh interpreter for every
# request, so the in-process socket guard cannot reach it — a call that
# slips past a test's fakes would hit the real local LLM / Jev for real.
# Synthetic loopback servers stay allowed (real-transport tests exercise
# the worker against their own fixture endpoints); only the production
# endpoints are denied at the boundary.
import bounded_http  # noqa: E402
import drug_info  # noqa: E402
import local_llm  # noqa: E402
import semantic_jev as _jev  # noqa: E402

_LIVE_ENDPOINTS = frozenset(
    {local_llm.ENDPOINT, _jev.JEV_ENDPOINT, _jev.JEV_MODELS_URL,
     drug_info.GATEWAY_URL, *local_llm.probe_urls(local_llm.ENDPOINT)}
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
# the host's own OS accounts must not decide auto-login tests
_install(mcs_adapter, "_login_accounts", lambda: [])


def _restore() -> None:
    while _ORIGINALS:
        module, name, original = _ORIGINALS.pop()
        setattr(module, name, original)
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


atexit.register(_restore)


def pytest_unconfigure(config) -> None:
    _restore()


@pytest.fixture(autouse=True)
def _collecting_cards_unless_requested():
    """Suites written for the collecting card layout keep it; reply-card tests
    set notify.reply_cards explicitly (the shipped default is pinned separately)."""
    import notify_cards
    # Do not prolong the tests' filesystem patches past tmp_path cleanup.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(notify_cards, "REPLY_CARDS_DEFAULT", False)
        yield
