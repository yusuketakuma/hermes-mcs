"""Render independent native services and request a coordinated runtime restart."""
from __future__ import annotations

import json
import safe_io
import math
import os
from pathlib import Path
import plistlib
import sys
import time
import uuid

from mcs_runtime import python_executable
import service_contract
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
    path = Path(root).expanduser().resolve() / "data" / STATUS_FILE
    try:
        value = json.loads(safe_io.read_private_bytes(path, 1024 * 1024))
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
    except (OSError, ValueError, TypeError, KeyError, RecursionError, UnicodeError, OverflowError):
        raise ValueError("standalone_status_unavailable") from None


def request_restart(root):
    live = status(root)
    value = {"generation": live["generation"], "request_id": uuid.uuid4().hex,
             "requested_at": time.time()}
    path = Path(root).expanduser().resolve() / "data" / RESTART_FILE
    atomic_write(str(path), lambda stream: json.dump(value, stream), mode=0o600)
    return value["request_id"]


def _systemd_quote(value):
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("standalone_service_path_invalid")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def render_health(root, platform=None):
    """The independent health watcher as [(file name, content)] for launchd or
    systemd. It runs outside the host, so a dead, wedged or crash-looping host
    still raises an alert (health_watch reads standalone-status.json)."""
    root = Path(root).expanduser().resolve()
    platform = platform or sys.platform
    repository = str(Path(__file__).resolve().parents[1])
    python = python_executable({"runtime_mode": "standalone"}, root=root)
    argv = [python, repository + "/mcs/ingest/health_watch.py", "--home", str(root)]
    logfile = str(root / "data/health_watch.log")
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
                + "\nTimeoutStartSec=300\nUMask=0077\n")
        timer = ("[Unit]\nDescription=Independent MCS health watch timer\n\n"
                 f"[Timer]\nOnBootSec={HEALTH_INTERVAL_S}s\nOnUnitActiveSec={HEALTH_INTERVAL_S}s\n"
                 f"Unit={service_contract.HEALTH_UNIT}\n\n[Install]\nWantedBy=timers.target\n")
        return [(service_contract.HEALTH_UNIT, unit), (service_contract.HEALTH_TIMER, timer)]
    raise ValueError("standalone_service_platform_unsupported")


def render(root, platform=None):
    root = Path(root).expanduser().resolve()
    platform = platform or sys.platform
    repository = str(Path(__file__).resolve().parents[1])
    python = python_executable({"runtime_mode": "standalone"}, root=root)
    argv = [python, "-m", "mcs_standalone", "run", "--root", str(root)]
    logfile = str(root / "data/standalone.log")
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
                  "KillMode=control-group\n\n[Install]\nWantedBy=default.target\n")
        return service_contract.STANDALONE_UNIT, body
    raise ValueError("standalone_service_platform_unsupported")
