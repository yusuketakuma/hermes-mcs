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


def _gateway_restart_run(data, agents, uid, expected_pid=None, request_id=None,
                         update_lock_fd=None):
    """Resolve the owned gateway, then verify a changed PID in a detached child."""
    # Local imports: _GATEWAY_RESTART_PROGRAM runs this source without the module header.
    import plistlib
    import shlex
    import shutil

    auto = expected_pid is not None or request_id is not None \
        or update_lock_fd is not None
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
            _gateway_restart_report(
                data, "failed", request_id=request_id,
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
        if auto and old_pid != expected_pid:
            _gateway_restart_report(
                data, "failed", request_id=request_id,
                error="gateway_restart_target_changed",
                expected_pid=expected_pid, observed_pid=old_pid)
            return 1
        env = dict(os.environ, HOME=str(home), HERMES_HOME=hermes_home)
        while time.monotonic() < deadline:
            target, pid = current()
            if auto:
                if pid != expected_pid:
                    _gateway_restart_report(
                        data, "failed", request_id=request_id,
                        error="gateway_restart_target_changed",
                        expected_pid=expected_pid, observed_pid=pid)
                    return 1
            elif pid and pid != old_pid:
                _gateway_restart_report(data, "supervisor_restart_verified", service=target,
                                        previous_pid=old_pid or None, pid=pid,
                                        request_id=request_id)
                return 0
            if time.monotonic() >= deadline:
                raise ValueError("gateway_restart_busy_timeout")
            issued = True
            # Unlinked private output avoids persisting live-session descriptions.
            with tempfile.TemporaryFile() as output:
                result = subprocess.run([launcher, "-p", "default", "gateway", "restart"],
                                        stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                        env=env, cwd="/",
                                        timeout=max(0.001, min(300, deadline - time.monotonic())))
                output.seek(0)
                refused = result.returncode == 1 and busy_refusal(output.read(65537))
            if not result.returncode:
                break
            if not refused:
                _gateway_restart_report(data, "unknown", error="gateway_restart_nonzero",
                                        service=target, previous_pid=old_pid or None,
                                        request_id=request_id)
                return 1
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(5, remaining))
        else:
            _gateway_restart_report(data, "unknown", error="gateway_restart_busy_timeout",
                                    service=target, previous_pid=old_pid or None,
                                    request_id=request_id)
            return 1
        until = min(deadline, time.monotonic() + 30)
        while time.monotonic() < until:
            target, pid = current()
            if pid and pid != old_pid:
                _gateway_restart_report(data, "supervisor_restart_verified", service=target,
                                        previous_pid=old_pid or None, pid=pid,
                                        request_id=request_id)
                return 0
            time.sleep(min(0.2, max(0, until - time.monotonic())))
        _gateway_restart_report(data, "unknown", error="gateway_pid_not_replaced",
                                service=target, previous_pid=old_pid or None,
                                request_id=request_id)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError,
            RecursionError, plistlib.InvalidFileException) as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("gateway_") \
            else "gateway_restart_unverifiable"
        status = "unknown" if issued else "failed"
        _gateway_restart_report(data, status, error=code, request_id=request_id)
    return 1


_GATEWAY_RESTART_PROGRAM = (
    "import errno,os,sys,json,re,stat,tempfile,time,subprocess\nfrom pathlib import Path\n"
    "from contextlib import suppress\n" + inspect.getsource(_fdopen_owned)
    + "\n" + inspect.getsource(_read_regular_bytes)
    + "\n" + inspect.getsource(_read_local_json)
    + "\n" + inspect.getsource(_atomic_json)
    + "\n" + inspect.getsource(_gateway_restart_report)
    + "\n" + inspect.getsource(_gateway_restart_run)
    + "\nsys.exit(_gateway_restart_run(*sys.argv[1:]))\n")


def request_gateway_restart(data, agents, uid, *, expected_pid=None,
                            request_id=None, update_lock_fd=None):
    """Queue a detached verified restart; callers have already saved bookkeeping."""
    auto = expected_pid is not None or request_id is not None \
        or update_lock_fd is not None
    if auto:
        valid = (type(expected_pid) is int and expected_pid > 0
                 and type(request_id) is str
                 and re.fullmatch(r"[0-9a-f]{32}", request_id) is not None
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
                data, agents, str(uid)]
        pass_fds = ()
        if auto:
            argv += [str(expected_pid), request_id, str(update_lock_fd)]
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
