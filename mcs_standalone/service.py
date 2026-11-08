"""Render independent native services and request a coordinated runtime restart."""
from __future__ import annotations

import json
import safe_io
import math
import os
from pathlib import Path
import plistlib
import re
import sys
import time
import uuid

from mcs_runtime import python_executable
import service_contract
from run_log_install import helper_path
from safe_io import atomic_write

# Watches the host from outside it: the same label as Hermes mode's agent.
HEALTH_INTERVAL_S = 300
STATUS_FILE = "standalone-status.json"
RESTART_FILE = "standalone-restart.request"
STATUS_TTL = 15


def pid_exists(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def status(root, *, now=None):
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        root_fd = os.open(Path(root).expanduser().absolute(), directory_flags)
        try:
            data_fd = os.open("data", directory_flags, dir_fd=root_fd)
            try:
                raw = safe_io.read_private_bytes(STATUS_FILE, 1024 * 1024, dir_fd=data_fd)
            finally:
                os.close(data_fd)
        finally:
            os.close(root_fd)
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("updated_at")) not in (int, float) \
                or not math.isfinite(value["updated_at"]):
            raise ValueError
        age = (time.time() if now is None else now) - value["updated_at"]
        if (not -2 <= age <= STATUS_TTL
                or not pid_exists(value.get("pid"))
                or not isinstance(value.get("generation"), str)
                or len(value["generation"]) != 32
                or not isinstance(value.get("children"), dict)):
            raise ValueError
        return value
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError, UnicodeError, OverflowError):
        raise ValueError("standalone_status_unavailable") from None


def restart_request_path(root, generation=None):
    """Keep automatic intents separate from the existing manual request file."""
    name = RESTART_FILE
    if generation is not None:
        if type(generation) is not str or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
            raise ValueError("standalone_generation_changed")
        # shortcut: Keep old generation files; add bounded retention if their count becomes costly.
        name += "." + generation
    return Path(root).expanduser().resolve() / "data" / name


def read_restart_request(path):
    """Read a bounded native restart intent without accepting ambiguous records."""
    from mcs_util import strict_json

    value = strict_json(safe_io.read_private_bytes(path, 2048))
    if (type(value) is not dict or set(value) != {"generation", "request_id", "requested_at"}
            or any(type(value[key]) is not str or re.fullmatch(r"[0-9a-f]{32}", value[key]) is None
                   for key in ("generation", "request_id"))
            or type(value["requested_at"]) not in (int, float)
            or not 0 <= value["requested_at"] < 1e12
            or not math.isfinite(value["requested_at"])):
        raise ValueError("standalone_restart_request_unverified")
    return value


def automatic_restart_blocked(root, generation):
    """Preserve current or unknown manual intent; ignore only a proved old generation."""
    if os.path.lexists(restart_request_path(root, generation)):
        return True
    manual = restart_request_path(root)
    try:
        value = read_restart_request(manual)
    except FileNotFoundError:
        return os.path.lexists(manual)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return True
    return value["generation"] == generation


def request_restart(root, *, expected_generation=None):
    live = status(root)
    if expected_generation is not None:
        if (type(expected_generation) is not str
                or re.fullmatch(r"[0-9a-f]{32}", expected_generation) is None
                or live["generation"] != expected_generation):
            raise ValueError("standalone_generation_changed")
        if (live.get("restarting") is not False or live.get("update_in_progress") is not False
                or "restart_reservation" not in live or live["restart_reservation"] is not None
                or "update_marker" not in live or live["update_marker"] is not None):
            raise ValueError("standalone_restart_busy")
        if automatic_restart_blocked(root, expected_generation):
            raise ValueError("standalone_restart_busy")
    value = {"generation": live["generation"], "request_id": uuid.uuid4().hex,
             "requested_at": time.time()}
    path = restart_request_path(root, expected_generation)
    if expected_generation is None:
        atomic_write(str(path), lambda stream: json.dump(value, stream), mode=0o600)
    else:
        # Late old writers cannot occupy the next generation's automatic path.
        safe_io.publish_new_bytes(path, json.dumps(value).encode("utf-8"))
    return value["request_id"]


def _systemd_quote(value):
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("standalone_service_path_invalid")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def render_health(root, platform=None, *, recovery_dir=None):
    """The independent health watcher as [(file name, content)] for launchd or
    systemd. It runs outside the host, so a dead, wedged or crash-looping host
    still raises an alert (health_watch reads standalone-status.json)."""
    root = Path(root).expanduser().resolve()
    platform = platform or sys.platform
    repository = str(Path(__file__).resolve().parents[1])
    python = python_executable({"runtime_mode": "standalone"}, root=root)
    argv = [python, repository + "/mcs/ingest/health_watch.py", "--home", str(root), "--send"]
    argv = [python, "-I", helper_path(root, recovery_dir, checkout=repository), "exec",
            "--data-dir", str(root / "data"), "--", *argv]
    logfile = os.devnull
    if platform == "darwin":
        value = {"Label": service_contract.HEALTH_LABEL, "ProgramArguments": argv,
                 "EnvironmentVariables": {"MCS_ROOT": str(root)},
                 "WorkingDirectory": repository, "StartInterval": HEALTH_INTERVAL_S,
                 "RunAtLoad": False, "StandardOutPath": logfile,
                 "StandardErrorPath": logfile, "Umask": 0o077}
        return [(service_contract.HEALTH_LABEL + ".plist", plistlib.dumps(value).decode("utf-8"))]
    if platform.startswith("linux"):
        _systemd_quote(repository)
        unit = ("[Unit]\nDescription=Independent MCS health watch\n\n"
                "[Service]\nType=oneshot\nWorkingDirectory=" + repository.replace("%", "%%") + "/."
                + "\nEnvironment=" + _systemd_quote("MCS_ROOT=" + str(root))
                + "\nExecStart=" + " ".join(_systemd_quote(v) for v in argv)
                + "\nTimeoutStartSec=300\nUMask=0077\nStandardOutput=null\nStandardError=null\n")
        timer = ("[Unit]\nDescription=Independent MCS health watch timer\n\n"
                 f"[Timer]\nOnBootSec={HEALTH_INTERVAL_S}s\nOnUnitActiveSec={HEALTH_INTERVAL_S}s\n"
                 f"Unit={service_contract.HEALTH_UNIT}\n\n[Install]\nWantedBy=timers.target\n")
        return [(service_contract.HEALTH_UNIT, unit), (service_contract.HEALTH_TIMER, timer)]
    raise ValueError("standalone_service_platform_unsupported")


def render(root, platform=None, *, recovery_dir=None):
    root = Path(root).expanduser().resolve()
    platform = platform or sys.platform
    repository = str(Path(__file__).resolve().parents[1])
    python = python_executable({"runtime_mode": "standalone"}, root=root)
    argv = [python, "-m", "mcs_standalone", "run", "--root", str(root)]
    argv = [python, "-I", helper_path(root, recovery_dir, checkout=repository), "exec",
            "--data-dir", str(root / "data"), "--", *argv]
    logfile = os.devnull
    if platform == "darwin":
        value = {"Label": service_contract.STANDALONE_LABEL, "ProgramArguments": argv,
                 "WorkingDirectory": repository, "RunAtLoad": True, "KeepAlive": True,
                 "ExitTimeOut": 3660, "AbandonProcessGroup": True,
                 "ThrottleInterval": 30, "StandardOutPath": logfile,
                 "StandardErrorPath": logfile, "Umask": 0o077}
        return service_contract.STANDALONE_LABEL + ".plist", plistlib.dumps(value).decode("utf-8")
    if platform.startswith("linux"):
        quote = _systemd_quote
        quote(repository)  # reject newline/NUL in the repository path too
        # Path directives keep literal spaces/$; only %-specifiers need escaping.
        body = ("[Unit]\nDescription=Independent MCS runtime\nAfter=network-online.target\n\n"
                "[Service]\nType=simple\nWorkingDirectory=" + repository.replace("%", "%%") + "/."
                + "\nExecStart=" + " ".join(quote(v) for v in argv)
                + "\nRestart=always\nRestartSec=30\nTimeoutStopSec=3660\nUMask=0077\n"
                  "StandardOutput=null\nStandardError=null\nKillMode=control-group\n\n[Install]\nWantedBy=default.target\n")
        return service_contract.STANDALONE_UNIT, body
    raise ValueError("standalone_service_platform_unsupported")
