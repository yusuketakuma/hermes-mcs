"""Render independent native services and request a coordinated runtime restart."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import plistlib
import sys
import time
import uuid

from mcs_runtime import python_executable
from mcs_util import atomic_write

LABEL = "ai.mcs.standalone"
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
        from .config import _private_bytes
        value = json.loads(_private_bytes(path, "status_missing", "status_invalid", 1024 * 1024))
        if not isinstance(value, dict) or type(value.get("updated_at")) not in (int, float) \
                or not math.isfinite(value["updated_at"]):
            raise ValueError
        age = (time.time() if now is None else now) - value["updated_at"]
        if (not isinstance(value, dict) or not -2 <= age <= STATUS_TTL
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


def render(root, platform=None):
    root = Path(root).expanduser().resolve()
    platform = platform or sys.platform
    repository = str(Path(__file__).resolve().parents[1])
    python = python_executable({"runtime_mode": "standalone"}, root=root)
    argv = [python, "-m", "mcs_standalone", "run", "--root", str(root)]
    logfile = str(root / "data/standalone.log")
    if platform == "darwin":
        value = {"Label": LABEL, "ProgramArguments": argv,
                 "WorkingDirectory": repository, "RunAtLoad": True, "KeepAlive": True,
                 "ExitTimeOut": 3660, "AbandonProcessGroup": True,
                 "ThrottleInterval": 30, "StandardOutPath": logfile,
                 "StandardErrorPath": logfile}
        return LABEL + ".plist", plistlib.dumps(value).decode("utf-8")
    if platform.startswith("linux"):
        def quote(value):
            if any(c in value for c in "\n\r\x00"):
                raise ValueError("standalone_service_path_invalid")
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
        body = ("[Unit]\nDescription=Independent MCS runtime\nAfter=network-online.target\n\n"
                "[Service]\nType=simple\nWorkingDirectory=" + quote(repository)
                + "\nExecStart=" + " ".join(quote(v) for v in argv)
                + "\nRestart=always\nRestartSec=30\nTimeoutStopSec=3660\n"
                  "KillMode=control-group\n\n[Install]\nWantedBy=default.target\n")
        return "mcs-standalone.service", body
    raise ValueError("standalone_service_platform_unsupported")
