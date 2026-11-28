"""Process boundaries for the adapter's synthetic tests.

The production modules intentionally contain network, Keychain, and Chrome
entry points.  Tests exercise those contracts with fakes; this guard makes an
accidental live call fail at the boundary while leaving Python subprocesses
used by the synthetic CLI tests available.
"""
from __future__ import annotations

import atexit
import contextlib
import errno
import ipaddress
import os
import shlex
import shutil
import signal
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
# The pytest process that owns this run. Process matching and signals stay
# inside its own tree (2026-11-28: an update test's stray sweep matched the
# live drainers of the same user by command line and stopped them).
_TEST_ROOT_PID = os.getpid()
_PGREP_SHIM = r"""#!/bin/sh
# Real pgrep, narrowed to the process tree of this test run. A match on any
# other process of the same user is dropped, so callers never learn its pid.
# Exit 1 means "no match in this run"; 3 means the answer could not be verified.
root="${MCS_TEST_ROOT_PID:-}"
case "$root" in ''|*[!0-9]*) echo 'pgrep: the MCS test root is unknown' >&2; exit 3 ;; esac
real=''
for candidate in /usr/bin/pgrep /bin/pgrep; do
    if [ -x "$candidate" ]; then real="$candidate"; break; fi
done
[ -n "$real" ] || { echo 'pgrep: not available in MCS tests' >&2; exit 3; }
# The run's root must be a live, listable process before any answer is trusted.
seen="$(ps -o pid= -p "$root" 2>/dev/null)" || { echo 'pgrep: the MCS test root cannot be listed' >&2; exit 3; }
[ "$(printf '%s' "$seen" | tr -d ' \n')" = "$root" ] || { echo 'pgrep: the MCS test root cannot be listed' >&2; exit 3; }
out="$("$real" "$@")"
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
# One listing taken after the search; its exit status is kept (no pipe).
table="$(ps -A -o pid=,ppid=)" || { echo 'pgrep: ps failed' >&2; exit 3; }
verdict="$(printf '%s\n' "$table" | MCS_PGREP_MATCHES="$out" awk -v root="$root" -v self="$$" '
    NF != 2 || $1 !~ /^[0-9]+$/ || $2 !~ /^[0-9]+$/ { bad = 1; next }
    { parent[$1] = $2 }
    END {
        if (bad || !(root in parent)) { print "UNVERIFIED"; exit }
        count = split(ENVIRON["MCS_PGREP_MATCHES"], line, "\n")
        for (i = 1; i <= count; i++) {
            if (line[i] == "") continue
            split(line[i], field, " ")
            pid = field[1]
            if (pid !~ /^[0-9]+$/ || pid == self) continue
            if (!(pid in parent)) { print "MISSING " pid; continue }
            # Three outcomes: inside this run, proven outside (the walk reached
            # init), or not placed (an ancestor is not listed, or the walk is too deep).
            walk = pid
            placed = 0
            for (depth = 0; depth < 64; depth++) {
                if (walk == root) { print "KEEP " line[i]; placed = 1; break }
                if (walk <= 1) { placed = 1; break }
                if (!(walk in parent)) break
                walk = parent[walk]
            }
            if (!placed) print "UNPLACED " pid
        }
    }')" || { echo 'pgrep: awk failed' >&2; exit 3; }
found=1
while IFS=' ' read -r kind rest; do
    case "$kind" in
        UNVERIFIED) echo 'pgrep: unreadable ps output or unknown test root' >&2; exit 3 ;;
        UNPLACED) echo 'pgrep: the ancestry of a match could not be followed' >&2; exit 3 ;;
        KEEP) printf '%s\n' "$rest"; found=0 ;;
        MISSING)
            # Drop a match only when it is proven gone; alive but not listed is unverifiable.
            if kill -0 "$rest" 2>/dev/null; then echo 'pgrep: a match is alive but not listed' >&2; exit 3; fi ;;
    esac
done <<MCS_PGREP_VERDICT
$verdict
MCS_PGREP_VERDICT
exit "$found"
"""


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
    for name in ("hermes", "launchctl", "security", "pkill", "killall"):
        script = os.path.join(guard, name)
        with open(script, "w") as stream:
            stream.write("#!/bin/sh\necho 'real %s is disabled in MCS tests' >&2\nexit 97\n" % name)
        os.chmod(script, 0o755)
    # pgrep stays usable, but a child process sees only this run's own tree.
    os.environ["MCS_TEST_ROOT_PID"] = str(_TEST_ROOT_PID)
    script = os.path.join(guard, "pgrep")
    with open(script, "w") as stream:
        stream.write(_PGREP_SHIM)
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
    if executable in {"hermes", "launchctl", "pgrep", "pkill", "killall"}:
        # A real Hermes CLI or launchd writes the user's LaunchAgents and
        # services even under a temp HOME (2026-10-04: a services test
        # replaced the live gateway plist). Real pgrep/pkill/killall match
        # every process of the user, the live services included. Only stubs
        # and shims under the test temp tree may run.
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

_ORIGINAL_KILL = os.kill
_ORIGINAL_KILLPG = os.killpg
_SIGNAL_REFUSED = "signalling a process outside this test run is disabled in MCS tests"


def _process_table():
    """Return {pid: (parent, group)} for every visible process, or None when ps fails."""
    # The saved Popen class, not subprocess.run: a test that replaces
    # subprocess.Popen to record its own calls must not see this lookup.
    try:
        process = _ORIGINAL_POPEN(["ps", "-A", "-o", "pid=,ppid=,pgid="], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None
    try:
        output = process.communicate(timeout=20)[0]
    except subprocess.TimeoutExpired:
        # Reap only the helper this call started; the saved kill avoids re-entering the guard.
        with contextlib.suppress(OSError):
            _ORIGINAL_KILL(process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            process.communicate(timeout=20)
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    if process.returncode != 0:
        return None
    table = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3 or not all(field.isdigit() for field in fields):
            return None
        table[int(fields[0])] = (int(fields[1]), int(fields[2]))
    return table or None


def _in_test_tree(pid: int, table=None) -> bool:
    """True when `pid` is this test run's root or one of its descendants."""
    table = _process_table() if table is None else table
    if table is None:
        return False
    for _ in range(64):
        if pid == _TEST_ROOT_PID:
            return True
        if pid not in table or pid <= 1:
            return False
        pid = table[pid][0]
    return False


def _refuse():
    raise PermissionError(errno.EPERM, _SIGNAL_REFUSED)


def _check_signal(pid: int) -> None:
    """Return when a real signal may be sent to `pid`; raise otherwise.

    A single process must belong to this run. A process group (pid 0, a
    negative pid, killpg) needs every live member to belong to this run: a
    descendant in the group does not vouch for the others, and a group
    outlives its leader. A target the process table does not show is probed
    with signal 0: the kernel's own error (no such process, not permitted) is
    raised as it is, and a target that exists without being listed is refused.
    Nothing is sent on an unproven target."""
    if pid == os.getpid():
        return
    if pid == -1:
        _refuse()
    table = _process_table()
    if table is None:
        _refuse()
    if pid > 0:
        if pid in table:
            if _in_test_tree(pid, table):
                return
            _refuse()
        _ORIGINAL_KILL(pid, 0)      # raises the kernel's answer for an absent target
        _refuse()                   # alive but not listed: not provably ours
    group = os.getpgrp() if pid == 0 else -pid
    members = [member for member, (_parent, member_group) in table.items() if member_group == group]
    if not members:
        _ORIGINAL_KILLPG(group, 0)  # raises the kernel's answer for an absent group
        _refuse()                   # the group exists but none of it is listed
    if not all(_in_test_tree(member, table) for member in members):
        _refuse()


def _signal_allowed(pid: int) -> bool:
    """The decision of `_check_signal` as a value, for tests that must not send."""
    try:
        _check_signal(pid)
    except OSError:
        return False
    return True


def _guarded_kill(pid, sig):
    if sig != 0:
        _check_signal(pid)
    return _ORIGINAL_KILL(pid, sig)


def _guarded_killpg(pgid, sig):
    if sig != 0:
        _check_signal(-pgid if pgid > 0 else 0 if pgid == 0 else -1)
    return _ORIGINAL_KILLPG(pgid, sig)


# Popen.terminate/kill/send_signal and multiprocessing all go through os.kill.
_install(os, "kill", _guarded_kill)
_install(os, "killpg", _guarded_killpg)

# The bounded-http transport spawns a fresh interpreter for every
# request, so the in-process socket guard cannot reach it — a call that
# slips past a test's fakes could hit a real model endpoint.
# Synthetic loopback servers stay allowed (real-transport tests exercise
# the worker against their own fixture endpoints); only the production
# endpoints are denied at the boundary.
import bounded_http  # noqa: E402
import drug_info  # noqa: E402
import local_llm  # noqa: E402
import openai_client  # noqa: E402
import semantic_jev as _jev  # noqa: E402

_LIVE_ENDPOINTS = frozenset(
    {local_llm.ENDPOINT, _jev.JEV_ENDPOINT, _jev.JEV_MODELS_URL,
     bounded_http.ANTHROPIC_ENDPOINT, openai_client.ENDPOINT,
     drug_info.GATEWAY_URL, *local_llm.probe_urls(local_llm.ENDPOINT)}
    | set(_jev.JEV_ALLOWED_ENDPOINTS))
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _live_authority(url):
    # A loopback spelling and port bind every path on that live service.
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
_LIVE_CLOUD_HOSTS = frozenset(
    _live_authority(url)[0] for url in (bounded_http.ANTHROPIC_ENDPOINT, openai_client.ENDPOINT))


def _guarded_http_request(endpoint, *args, **kwargs):
    # Only a real spawn can reach a live endpoint — tests that stub
    # Popen (wire-replay fakes, synthetic workers) never touch the
    # network, so they pass through and exercise the transport contract.
    authority = _live_authority(endpoint)
    if ((endpoint in _LIVE_ENDPOINTS or authority in _LIVE_AUTHORITIES
         or authority is not None and authority[0] in _LIVE_CLOUD_HOSTS)
            and subprocess.Popen is _guarded_popen):
        raise RuntimeError(
            "live LLM/Jev/cloud endpoints are disabled in MCS tests")
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


@pytest.fixture(autouse=True)
def _private_health_file():
    """health_report.HEALTH_FILE is bound at import to the session-wide temp
    HOME, so a health.json published by one test would otherwise be read back
    by later ones (e.g. as proof that a queued health_write_failed recovered).
    Each test gets its own empty location; tests needing a specific path
    still monkeypatch it."""
    import health_report
    directory = tempfile.mkdtemp(prefix="health-", dir=_TEST_HOME)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(health_report, "HEALTH_FILE",
                      os.path.join(directory, "health.json"))
        yield
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_leftover_watchdog_deadline():
    """run_check._WATCHDOG_EXIT_AT is process-wide: the real tick arms it once
    and exits, but a test that arms it and returns leaves a hard deadline that
    later ticks in the same pytest process obey (their snapshot publish and
    backup stop once it has passed), so the damage shows up in an unrelated
    test, only for some orderings. Fail the test that left it armed and clear
    it for the ones that follow."""
    yield
    run_check = sys.modules.get("run_check")
    if run_check is not None and run_check._WATCHDOG_EXIT_AT is not None:
        run_check._WATCHDOG_EXIT_AT = None
        pytest.fail("this test left run_check's watchdog deadline armed; "
                    "disarm it or set _WATCHDOG_EXIT_AT through monkeypatch")


@pytest.fixture
def legacy_chat_format():
    """Suites written before chat_format_version keep the legacy Discord/
    LINE WORKS face (body as durable parts); chat-format tests opt back
    in per test."""
    import present_discord
    import present_lineworks
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(present_discord, "CHAT_BODY_ON_FACE", False)
        patch.setattr(present_lineworks, "CHAT_BODY_ON_FACE", False)
        yield


@pytest.fixture
def v1_thread_bodies():
    """chat_format_version=1 specs whose originals all overflow to thread
    body parts — contract suites that need durable parts keep the
    production v1 format instead of opting into legacy."""
    import present_discord
    import present_lineworks
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(present_discord, "FACE_ORIGINAL_MAX", 1)
        patch.setattr(present_lineworks, "FACE_ORIGINAL_MAX", 1)
        yield
