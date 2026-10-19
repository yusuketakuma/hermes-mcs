"""選択されたPythonと復旧監視の実行環境を、設定変更なしで診断する。

Machine I/O lives here; version/safety policy remains in mcs_runtime.
Callers supply the selected executable, deployed plist and checkout explicitly.
No configuration, service globals, cached probe results or SDK clients are owned
by this module. Each probe observes the supplied executable at call time.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
from pathlib import Path

import mcs_runtime
from lifecycle_io import run_bounded
from mcs_config import recovery_python_problem


def executable_available(exe: str) -> bool:
    """Whether one explicit executable path is a runnable regular file."""
    return os.path.isfile(exe) and os.access(exe, os.X_OK)


def probe_python(exe: str) -> mcs_runtime.RuntimeFacts | None:
    """Inspect Python/SQLite/package metadata without importing SDK clients."""
    # Only stdlib and distribution metadata: never import SDK clients or MCS.
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        return None
    r = run_bounded([exe, "-I", "-c", """
import sys, sqlite3, json
from importlib.metadata import version, PackageNotFoundError
packages = {}
for name in ('discord.py', 'slack-bolt', 'slack-sdk'):
    try:
        packages[name] = version(name)
    except PackageNotFoundError:
        packages[name] = None
print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])),
                  'sqlite': sqlite3.sqlite_version, 'packages': packages}))
"""], timeout=20)
    if r.returncode:
        return None
    try:
        data = json.loads(getattr(r, "stdout", ""))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not all(
            isinstance(data.get(key), str) and
            re.fullmatch(r"\d+\.\d+\.\d+", data[key])
            for key in ("python", "sqlite")):
        return None
    packages = data.get("packages")
    names = ("discord.py", "slack-bolt", "slack-sdk")
    if not isinstance(packages, dict) or set(packages) != set(names):
        return None
    if any(value is not None and (
            not isinstance(value, str) or
            re.fullmatch(r"\d+(?:\.\d+)+(?:[a-z]+\d+)?", value) is None)
           for value in packages.values()):
        return None
    return {"python": data["python"], "sqlite": data["sqlite"],
            "packages": packages}


def python_problem(exe: str) -> str | None:
    """None when `exe` is an executable Python >= 3.14."""
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        return f"Python（{exe}）が見つからないか、実行できません"
    r = run_bounded([exe, "-c",
              "import sys; sys.exit(sys.version_info < (3, 14))"],
             timeout=20)
    if r.returncode != 0:
        return (f"Python（{exe}）を起動できないか、3.14未満です "
                f"(rc={r.returncode})")
    # Inspect the actual service interpreter's linked SQLite, not the
    # version of a sqlite3 executable or a different Python on PATH.
    r = run_bounded([exe, "-c",
              "import sqlite3,sys; v=sqlite3.sqlite_version_info; "
              "sys.exit(0 if v >= (3,51,3) or "
              "(v[:2] == (3,50) and v >= (3,50,7)) or "
              "(v[:2] == (3,44) and v >= (3,44,6)) else 1)"],
             timeout=20)
    if r.returncode:
        return "常駐処理のSQLiteに安全なデータ保存の修正がありません（3.51.3以上、3.50.7以上、または3.44.6以上の対応系列が必要です）"
    return None


def recovery_python(cfg) -> str:
    """Resolve the explicit watchdog selection or supported installation path."""
    if "recovery_python" in cfg:
        return cfg["recovery_python"]
    # Native supervision has a minimal PATH; keep Homebrew's stable path.
    for path in ("/opt/homebrew/bin/python3.14", "/usr/local/bin/python3.14",
                 "/usr/bin/python3.14"):
        if os.access(path, os.X_OK):
            return path
    return shutil.which("python3.14") or "/usr/bin/python3.14"


def recovery_executable(plist) -> str | None:
    """Read the absolute interpreter selection from one parsed launchd plist."""
    if not isinstance(plist, dict):
        return None
    if "Program" in plist:
        exe = plist["Program"]
    else:
        argv = plist.get("ProgramArguments")
        exe = argv[0] if isinstance(argv, list) and argv else None
    return exe if isinstance(exe, str) and os.path.isabs(exe) else None


def recovery_runtime(plist_path: str | Path, *, checkout: str | Path) -> mcs_runtime.RuntimeFacts | None:
    """Inspect the deployed command from the explicit plist and checkout."""
    # Inspect the deployed watchdog's command, not ambient /usr/bin/python3.
    try:
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        exe = recovery_executable(plist)
        if exe is None or recovery_python_problem(exe, checkout=checkout):
            return None
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
        return None
    return probe_python(exe)
