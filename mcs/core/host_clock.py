"""Read this host's boot time and boot session through short, fixed, read-only OS queries."""
from __future__ import annotations

import re
import subprocess
import sys

SYSCTL = "/usr/sbin/sysctl"
_QUERY_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
_BOOT = re.compile(r"\bsec = (\d+)(?:, usec = (\d+))?")
_SESSION = re.compile(r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}")


def read_only_query(argv, *, timeout: float = 3) -> str | None:
    """Run one fixed read-only OS query; stdout, or None on any failure or timeout."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                             env=dict(_QUERY_ENV))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return out.stdout if out.returncode == 0 else None


def parse_boot_time(text) -> float | None:
    """``kern.boottime`` text (``{ sec = N, usec = M } ...``) as epoch seconds, else None."""
    match = _BOOT.search(text) if isinstance(text, str) else None
    if match is None:
        return None
    return int(match[1]) + (int(match[2]) / 1e6 if match[2] else 0.0)


def boot_time(*, timeout: float = 2) -> float | None:
    """This macOS host's boot time in epoch seconds; None elsewhere or when unreadable."""
    if sys.platform != "darwin":
        return None
    return parse_boot_time(read_only_query([SYSCTL, "-n", "kern.boottime"], timeout=timeout))


def boot_session(*, timeout: float = 2) -> str | None:
    """This macOS host's per-boot session UUID (``kern.bootsessionuuid``); None elsewhere or when unreadable.

    Unlike ``kern.boottime`` it is not derived from the wall clock, so a
    clock step leaves it unchanged."""
    if sys.platform != "darwin":
        return None
    text = read_only_query([SYSCTL, "-n", "kern.bootsessionuuid"], timeout=timeout)
    value = text.strip() if isinstance(text, str) else ""
    return value.upper() if _SESSION.fullmatch(value) else None
