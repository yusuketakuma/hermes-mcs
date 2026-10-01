"""Generate private LINE WORKS service candidates without installing or starting them."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import sys

from hermes_plugin.mcs_delivery.paths import atomic_write


def _systemd_arg(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def generate(root, *, python_executable=sys.executable, platform=sys.platform) -> Path:
    """Write an owner-only launchd or systemd user-service candidate."""
    if platform not in ("darwin", "linux"):
        raise ValueError("lineworks_service_platform_unsupported")
    root = Path(root).expanduser().resolve()
    # Preserve a venv's executable path: resolving its symlink can bypass the venv.
    python = Path(os.path.abspath(os.path.expanduser(str(python_executable))))
    entry = Path(__file__).resolve().parents[2] / "lineworks_adapter" / "__main__.py"
    if (not root.is_dir() or not entry.is_file() or not python.is_file()
            or not os.access(python, os.X_OK)
            or any(ord(c) < 32 or ord(c) == 127 for c in str(root) + str(python) + str(entry))):
        raise ValueError("lineworks_service_paths_invalid")
    data = root / "data"
    folder = data / "lineworks-service"
    state = data / "lineworks_state"
    for directory in (data, folder, state):
        if directory.is_symlink():
            raise ValueError("lineworks_service_paths_invalid")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    stdout, stderr = state / "service.stdout.log", state / "service.stderr.log"
    if stdout.is_symlink() or stderr.is_symlink():
        raise ValueError("lineworks_service_paths_invalid")
    argv = [str(python), str(entry), "run", "--root", str(root)]
    if platform == "darwin":
        target = folder / "ai.mcs.lineworks.plist"
        body = plistlib.dumps({
            "Label": "ai.mcs.lineworks", "ProgramArguments": argv,
            "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 10, "Umask": 0o077,
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONUTF8": "1"},
            "StandardOutPath": str(stdout), "StandardErrorPath": str(stderr),
        })
    else:
        target = folder / "hermes-mcs-lineworks.service"
        # Path directives keep literal spaces/$; only %-specifiers need escaping.
        # '/.' prevents trailing whitespace or backslash from changing parsing.
        working = str(root).replace("%", "%%") + "/."
        body = ("[Unit]\nDescription=MCS LINE WORKS adapter\n\n[Service]\nType=exec\n"
                f"WorkingDirectory={working}\n"
                f"ExecStart={' '.join(_systemd_arg(v) for v in argv)}\n"
                "Restart=always\nRestartSec=10\nUMask=0077\n"
                "Environment=PYTHONUTF8=1\n"
                f"StandardOutput=append:{str(stdout).replace('%', '%%')}\n"
                f"StandardError=append:{str(stderr).replace('%', '%%')}\n"
                "\n[Install]\nWantedBy=default.target\n").encode("utf-8")
    if target.is_symlink():
        raise ValueError("lineworks_service_paths_invalid")
    atomic_write(str(target), body, mode=0o600)
    return target
