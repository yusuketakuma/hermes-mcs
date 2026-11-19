"""Independent, frozen gateway restart transport and bounded local file primitives."""
import errno
import inspect
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path



def _fdopen_owned(fd, mode, **stream_kwargs):
    """Adopt an owned FD, closing it if stream construction fails."""
    try:
        return os.fdopen(fd, mode, closefd=True, **stream_kwargs)
    except BaseException:
        try:
            os.close(fd)
        except OSError as cleanup_error:
            if cleanup_error.errno != errno.EBADF:
                raise
        raise


def _read_regular_bytes(path, maximum=16 * 1024 * 1024, private=False):
    """Read a bounded regular leaf without project imports, links or FIFO waits."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with _fdopen_owned(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode)
                or private and metadata.st_mode & 0o077 or metadata.st_size > maximum):
            raise ValueError("local_state_file_invalid")
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("local_state_file_too_large")
        return raw


def _read_local_json(path, maximum=None, private=False):
    """Read a regular, non-symlink file; preserve each caller's size policy."""
    return json.loads(_read_regular_bytes(
        path, 16 * 1024 * 1024 if maximum is None else maximum, private).decode("utf-8"))


def _atomic_json(path, value, prefix, suffix=".tmp", **dump_kw):
    """Publish JSON durably: private mkstemp, fsync, replace, dir fsync.
    Raises after removing the temporary file; callers keep their own error
    policy. Also frozen into _GATEWAY_RESTART_PROGRAM."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    try:
        with _fdopen_owned(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, **dump_kw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if tmp is not None:
            with suppress(OSError):
                os.unlink(tmp)


def _gateway_restart_report(data, status, request_id=None, **facts):
    """Keep restart requests separate from verified process replacement."""
    os.makedirs(data, exist_ok=True)
    value = {"status": status, "at": time.time(), **facts}
    if request_id is not None:
        value["request_id"] = request_id
    _atomic_json(os.path.join(data, "gateway_restart.json"), value,
                 ".gateway-restart.", suffix="")


def _gateway_self_started_at():
    """This process's start time via `ps -o lstart=`, as the supervisor probes it."""
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())],
                                capture_output=True, text=True, timeout=10,
                                env={**os.environ, "LC_ALL": "C"})
        stamp = re.sub(r"\s+", " ", (result.stdout or "").strip())
        return (time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y"))
                if result.returncode == 0 else None)
    except Exception:
        # Best effort: a missing probe only means the supervisor cannot prove
        # this helper ended, which holds the receipt — never a wrong retry.
        return None


def _gateway_helper_lock(data):
    """fd holding the helper flock, or None while another helper is alive."""
    import fcntl
    try:
        fd = os.open(os.path.join(data, "gateway_restart_helper.lock"),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _gateway_attempt_journal(data, request_id, fields):
    """Best-effort durable record of one restart attempt for the supervisor."""
    if not (type(request_id) is str
            and re.fullmatch(r"[0-9a-f]{32}", request_id) is not None):
        return
    try:
        directory = os.path.join(data, "gateway_restart_attempts")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, request_id + ".json")
        try:
            value = _read_local_json(path, 64 * 1024)
            if not isinstance(value, dict) \
                    or value.get("schema") != "gateway-restart-attempt/1" \
                    or value.get("request_id") != request_id:
                value = {}
        except Exception:
            value = {}
        value.update(schema="gateway-restart-attempt/1",
                     request_id=request_id, **fields)
        _atomic_json(path, value, ".gateway-restart-attempt.", suffix="")
    except Exception:
        pass


def _gateway_restart_run(data, agents, uid, request_id=None, expected_pid=None,
                         update_lock_fd=None):
    """Resolve the owned gateway, then verify a changed PID in a detached child."""
    # Local imports: _GATEWAY_RESTART_PROGRAM runs this source without the module header.
    import plistlib
    import shlex
    import shutil

    auto = expected_pid is not None or update_lock_fd is not None
    if not (request_id is None or type(request_id) is str
            and re.fullmatch(r"[0-9a-f]{32}", request_id) is not None):
        _gateway_restart_report(
            data, "failed", request_id=None,
            error="gateway_restart_auto_args_invalid")
        return 1
    if auto:
        try:
            if not (type(expected_pid) in (int, str)
                    and type(update_lock_fd) in (int, str)
                    and type(request_id) is str):
                raise ValueError("gateway_restart_auto_args_invalid")
            expected_pid = int(expected_pid)
            update_lock_fd = int(update_lock_fd)
            valid = (expected_pid > 0 and update_lock_fd >= 0
                     and re.fullmatch(r"[0-9a-f]{32}", request_id) is not None)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            safe_request_id = (request_id if type(request_id) is str
                               and re.fullmatch(r"[0-9a-f]{32}", request_id)
                               is not None else None)
            _gateway_restart_report(
                data, "failed", request_id=safe_request_id,
                error="gateway_restart_auto_args_invalid")
            return 1
        try:
            fd_stat = os.fstat(update_lock_fd)
            lock_stat = os.lstat(os.path.join(data, "update.lock"))
            held = (stat.S_ISREG(fd_stat.st_mode)
                    and stat.S_ISREG(lock_stat.st_mode)
                    and (fd_stat.st_dev, fd_stat.st_ino)
                    == (lock_stat.st_dev, lock_stat.st_ino))
        except OSError:
            held = False
        if not held:
            _gateway_restart_report(
                data, "failed", request_id=request_id,
                error="gateway_restart_lock_fd_mismatch")
            return 1
    helper_lock = _gateway_helper_lock(data)
    if helper_lock is None:
        # Another helper owns the restart path; this attempt never started a
        # restart child, which the journal alone can prove (no receipt here).
        _gateway_attempt_journal(data, request_id, {
            "state": "ended", "outcome": "blocked_existing_helper",
            "helper_pid": os.getpid(),
            "helper_started_at": _gateway_self_started_at(),
            "deadline_at": time.time(), "rounds": [], "pid_changed": False,
            "exited_at": time.time()})
        return 1
    journal = {"pid_changed": False, "rounds": []}
    _gateway_attempt_journal(data, request_id, {
        "state": "reserved", "helper_pid": os.getpid(),
        "helper_started_at": _gateway_self_started_at(),
        "deadline_at": time.time() + 660,
        "expected_pid": expected_pid if auto else None,
        "rounds": [], "pid_changed": False})

    def report(status, **facts):
        try:
            # End the journal before the receipt so the receipt's stamp is never
            # earlier than exited_at; the supervisor requires that ordering.
            _gateway_attempt_journal(data, request_id, {
                "state": "ended", "outcome": facts.get("error") or status,
                "rounds": journal["rounds"],
                "pid_changed": journal["pid_changed"], "exited_at": time.time()})
            _gateway_restart_report(data, status, request_id=request_id, **facts)
        finally:
            # Every exit after the lock goes through report; release the
            # helper lock here so a retained frame cannot wedge a later helper.
            os.close(helper_lock)

    # One normal collection run (480s), lock wait (150s), PID observation (30s).
    deadline = time.monotonic() + 660
    label = "ai.hermes.gateway"
    expected = os.path.realpath(os.path.join(agents, label + ".plist"))

    def snapshot(target):
        result = subprocess.run(["launchctl", "print", target], capture_output=True,
                                text=True, timeout=max(0.001, min(10, deadline - time.monotonic())))
        if result.returncode:
            return None
        text = result.stdout or ""
        path = re.search(r"^\s*path = (.+)$", text, re.M)
        if path is None or os.path.realpath(path.group(1).strip()) != expected:
            raise ValueError("gateway_service_not_owned")
        pid = re.search(r"^\s*pid = (\d+)$", text, re.M)
        return int(pid.group(1)) if pid else 0

    home = Path(agents).resolve().parent.parent
    install = home / ".hermes/hermes-agent"
    installs = {install}
    launchers = {str(home / ".local/bin/hermes"), str(install / "venv/bin/hermes"),
                 str(install / ".hermes/bin/hermes")}
    logs = home / ".hermes/logs"

    launcher = None

    def owned_command(argv, plist):
        nonlocal launcher
        if not argv:
            return False
        program = argv[0]
        if program == "hermes":
            environment = plist.get("EnvironmentVariables") or {}
            if not isinstance(environment, dict) or not isinstance(environment.get("PATH", os.defpath), str):
                return False
            program = shutil.which("hermes", path=environment.get("PATH", os.defpath)) or ""
        if (program in launchers and os.path.realpath(program) in launchers
                and os.path.isfile(program) and os.access(program, os.X_OK)):
            tail = argv[1:]
        elif (os.path.isabs(program) and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(program).name)
              and (any(Path(program).parent == root / "venv/bin" for root in installs)
                   or os.path.realpath(program) == os.path.realpath(sys.executable))):
            if argv[1:3] == ["-m", "hermes_cli.main"]:
                tail = argv[3:]
            elif argv[1:3] == ["-I", "-c"] and len(argv) > 3:
                # Exact isolated bootstrap emitted by hermes_cli._launchers.
                modules = {}
                for root in installs:
                    common = ("import os, sys, runpy; "
                              "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
                              "os.environ.pop('VIRTUAL_ENV', None); "
                              "sys.path.insert(0, " + repr(str(root)) + "); ")
                    prefixes = (
                        common + "os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME') or "
                        "str(__import__('hermes_constants').get_default_hermes_root()); "
                        "import hermes_bootstrap; ",
                        common + "sys._hermes_pin_default_home = True; import hermes_bootstrap; "
                        "os.environ.get('HERMES_HOME') or os.environ.__setitem__('HERMES_HOME', "
                        "str(__import__('hermes_constants').get_default_hermes_root())); ",
                    )
                    for prefix in prefixes:
                        modules.update({prefix + "runpy.run_module(" + repr(module)
                                        + ", run_name='__main__', alter_sys=True)": module
                                        for module in ("hermes_cli.main", "hermes_cli.stderr_timestamp")})
                module = modules.get(argv[3])
                if module is None:
                    return False
                tail = argv[4:]
                if module == "hermes_cli.stderr_timestamp":
                    tail = ["--run-module", module, *tail]
            else:
                return False
        else:
            return False
        if tail[:2] == ["--run-module", "hermes_cli.stderr_timestamp"]:
            if (len(tail) < 6 or tail[2] != "--error-log" or tail[4] != "--"
                    or Path(tail[3]) != logs / "gateway.error.log"):
                return False
            return owned_command(tail[5:], plist)
        if tail[:2] != ["gateway", "run"] or not all(
                arg in ("--external-supervisor", "--replace") for arg in tail[2:]):
            return False
        if program in launchers:
            launcher = program
        else:
            selected_install = Path(hermes_home) / "hermes-agent"
            candidates = [str(selected_install / "venv/bin/hermes"),
                          str(selected_install / ".hermes/bin/hermes")]
            if selected_install == install:
                candidates.append(str(home / ".local/bin/hermes"))
            launcher = next((path for path in candidates if path in launchers
                             and os.path.realpath(path) in launchers
                             and os.path.isfile(path) and os.access(path, os.X_OK)), None)
        return True

    def owned_jxa(argv, plist):
        if argv[:3] != ["/usr/bin/osascript", "-l", "JavaScript"]:
            return False
        script = argv[4] if len(argv) == 5 and argv[3] == "-e" else argv[3] if len(argv) == 4 else ""
        quoted = r'("(?:[^"\\]|\\.)*")'
        terminal = re.fullmatch(r'Application\("Terminal"\)\.doScript\(' + quoted + r'\);?', script)
        system = re.fullmatch(
            r'ObjC\.import\("stdlib"\); const status=\$\.system\(' + quoted + r'\); '
            r'const signal=status & 127; '
            r'\$\.exit\(status === -1 \? 1 : signal === 0 \? \(status >> 8\) & 255 : 128 \+ signal\);',
            script)
        match = terminal or system
        if match is None:
            return False
        shell = shlex.split(json.loads(match[1]))
        if system:
            if (not shell or shell[0] != "exec" or len(shell) < 6
                    or shell[-4:] != [">>", str(logs / "gateway.log"),
                                      "2>>", str(logs / "gateway.error.log")]):
                return False
            shell = shell[1:-4]
        return owned_command(shell, plist)

    issued = False
    try:
        definition = _read_regular_bytes(expected, 1024 * 1024)
        plist = plistlib.loads(definition)
        args = plist.get("ProgramArguments")
        if plist.get("Label") != label or not isinstance(args, list) or not args \
                or not all(isinstance(arg, str) for arg in args) \
                or plist.get("Program", args[0]) != args[0]:
            raise ValueError("gateway_service_not_owned")
        environment = plist.get("EnvironmentVariables") or {}
        if not isinstance(environment, dict):
            raise ValueError("gateway_service_not_owned")
        hermes_home = environment.get("HERMES_HOME", str(home / ".hermes"))
        if not isinstance(hermes_home, str) or not os.path.isabs(hermes_home):
            raise ValueError("gateway_service_not_owned")
        if (Path(hermes_home).parent.name == "profiles"
                or Path(hermes_home).resolve().parent.name == "profiles"):
            raise ValueError("gateway_service_not_owned")
        logs = Path(hermes_home) / "logs"
        cwd = plist.get("WorkingDirectory")
        if "HERMES_HOME" in environment and cwd in (hermes_home, str(Path(hermes_home) / "hermes-agent")):
            custom = Path(hermes_home) / "hermes-agent"
            installs.add(custom)
            launchers.update({str(custom / "venv/bin/hermes"), str(custom / ".hermes/bin/hermes")})
        if cwd is not None and cwd not in ({str(root) for root in installs} | {hermes_home}):
            raise ValueError("gateway_service_not_owned")
        if not (owned_command(args, plist) or owned_jxa(args, plist)):
            raise ValueError("gateway_service_not_owned")
        if launcher is None:
            raise ValueError("gateway_restart_launcher_unavailable")
        def current():
            if (_read_regular_bytes(expected, 1024 * 1024) != definition
                    or os.path.realpath(launcher) not in launchers
                    or not os.path.isfile(launcher) or not os.access(launcher, os.X_OK)):
                raise ValueError("gateway_service_not_owned")
            marker = Path(data, "restore_pending.json")
            if os.path.lexists(marker):
                restore = _read_local_json(marker)
                if not isinstance(restore, dict) or not (restore.get("phase") == "restored"
                        or ("phase" not in restore and "restored_at" in restore)):
                    raise ValueError("gateway_restart_restore_hold")
            found = []
            for domain in ("gui", "user"):
                target = f"{domain}/{int(uid)}/{label}"
                pid = snapshot(target)
                if pid is not None:
                    found.append((target, pid))
            if not found:
                raise ValueError("gateway_service_unavailable")
            if len({pid for _, pid in found}) > 1:
                raise ValueError("gateway_service_ambiguous")
            return found[0]

        def busy_refusal(output):
            # Match the official noninteractive guard, never arbitrary errors.
            # Session descriptions are ephemeral and never enter the receipt.
            if len(output) > 65536:
                return False
            lines = output.decode("utf-8", errors="replace").strip().splitlines()
            if len(lines) < 4:
                return False
            match = re.fullmatch(r"✗ Refusing to restart the gateway: ([1-9][0-9]*) live session\(s\) would be interrupted\.", lines[0])
            return bool(match and all(line.startswith("  • ") for line in lines[1:-2])
                        and lines[-2] == "  Let them finish, or interrupt them deliberately:"
                        and lines[-1] == "    hermes gateway restart --yes   # interrupts "
                        + match.group(1) + " live session(s)")

        target, old_pid = current()
        if not auto:
            _gateway_attempt_journal(data, request_id, {"expected_pid": old_pid})
        if auto and old_pid != expected_pid:
            report("failed", error="gateway_restart_target_changed",
                   expected_pid=expected_pid, observed_pid=old_pid)
            return 1
        base_pid = expected_pid if auto else old_pid
        env = dict(os.environ, HOME=str(home), HERMES_HOME=hermes_home)
        while time.monotonic() < deadline:
            target, pid = current()
            if pid and pid != base_pid:
                journal["pid_changed"] = True
            if auto:
                if pid != expected_pid:
                    report("failed", error="gateway_restart_target_changed",
                           expected_pid=expected_pid, observed_pid=pid)
                    return 1
            elif pid and pid != old_pid:
                report("supervisor_restart_verified", service=target,
                       previous_pid=old_pid or None, pid=pid)
                return 0
            if time.monotonic() >= deadline:
                raise ValueError("gateway_restart_busy_timeout")
            issued = True
            if len(journal["rounds"]) == 0:
                _gateway_attempt_journal(data, request_id, {"state": "in_flight"})
            # Unlinked private output avoids persisting live-session descriptions.
            with tempfile.TemporaryFile() as output:
                try:
                    result = subprocess.run(
                        [launcher, "-p", "default", "gateway", "restart"],
                        stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                        env=env, cwd="/",
                        timeout=max(0.001, min(300, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    journal["rounds"].append({"at": time.time(), "outcome": "timeout",
                                              "observed_pid": pid})
                    raise
                output.seek(0)
                refused = result.returncode == 1 and busy_refusal(output.read(65537))
            journal["rounds"].append({"at": time.time(),
                "outcome": "success" if not result.returncode
                    else "busy_refusal" if refused else "nonzero",
                "observed_pid": pid})
            _gateway_attempt_journal(data, request_id, {"rounds": journal["rounds"]})
            if not result.returncode:
                break
            if not refused:
                report("unknown", error="gateway_restart_nonzero",
                       service=target, previous_pid=old_pid or None)
                return 1
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(5, remaining))
        else:
            report("unknown", error="gateway_restart_busy_timeout",
                   service=target, previous_pid=old_pid or None)
            return 1
        until = min(deadline, time.monotonic() + 30)
        while time.monotonic() < until:
            target, pid = current()
            if pid and pid != old_pid:
                report("supervisor_restart_verified", service=target,
                       previous_pid=old_pid or None, pid=pid)
                return 0
            time.sleep(min(0.2, max(0, until - time.monotonic())))
        report("unknown", error="gateway_pid_not_replaced",
               service=target, previous_pid=old_pid or None)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError,
            RecursionError, plistlib.InvalidFileException) as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("gateway_") \
            else "gateway_restart_unverifiable"
        status = "unknown" if issued else "failed"
        report(status, error=code)
    return 1


_GATEWAY_RESTART_PROGRAM = (
    "import errno,os,sys,json,re,stat,tempfile,time,subprocess\nfrom pathlib import Path\n"
    "from contextlib import suppress\n" + inspect.getsource(_fdopen_owned)
    + "\n" + inspect.getsource(_read_regular_bytes)
    + "\n" + inspect.getsource(_read_local_json)
    + "\n" + inspect.getsource(_atomic_json)
    + "\n" + inspect.getsource(_gateway_restart_report)
    + "\n" + inspect.getsource(_gateway_self_started_at)
    + "\n" + inspect.getsource(_gateway_helper_lock)
    + "\n" + inspect.getsource(_gateway_attempt_journal)
    + "\n" + inspect.getsource(_gateway_restart_run)
    + "\nsys.exit(_gateway_restart_run(*sys.argv[1:]))\n")


def request_gateway_restart(data, agents, uid, *, expected_pid=None,
                            request_id=None, update_lock_fd=None):
    """Queue a detached verified restart; callers have already saved bookkeeping."""
    if request_id is not None and not (type(request_id) is str
            and re.fullmatch(r"[0-9a-f]{32}", request_id) is not None):
        return False
    request_id = request_id or uuid.uuid4().hex
    auto = expected_pid is not None or update_lock_fd is not None
    if auto:
        valid = (type(expected_pid) is int and expected_pid > 0
                 and type(update_lock_fd) is int and update_lock_fd >= 0)
        if not valid:
            return False
        try:
            fd_stat = os.fstat(update_lock_fd)
            lock_stat = os.lstat(os.path.join(data, "update.lock"))
            held = (stat.S_ISREG(fd_stat.st_mode)
                    and stat.S_ISREG(lock_stat.st_mode)
                    and (fd_stat.st_dev, fd_stat.st_ino)
                    == (lock_stat.st_dev, lock_stat.st_ino))
        except OSError:
            held = False
        if not held:
            return False
    try:
        _gateway_restart_report(data, "requested", request_id=request_id)
        env = {key: value for key, value in os.environ.items()
               if key not in ("XPC_SERVICE_NAME", "MCS_JOB_PID")}
        argv = [sys.executable, "-I", "-c", _GATEWAY_RESTART_PROGRAM,
                data, agents, str(uid), request_id]
        pass_fds = ()
        if auto:
            argv += [str(expected_pid), str(update_lock_fd)]
            pass_fds = (update_lock_fd,)
        # -I: no cwd/PYTHON* on sys.path — the child needs only stdlib.
        subprocess.Popen(argv, env=env, cwd="/",
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True,
                         pass_fds=pass_fds,
                         start_new_session=True)
        return True
    except OSError:
        with suppress(OSError):
            _gateway_restart_report(data, "failed", request_id=request_id,
                                    error="gateway_restart_spawn_failed")
        return False
