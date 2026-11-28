"""Search and signal boundary for tests that run an updater as a real child process.

The in-process guard of tests/conftest.py stops at the pytest interpreter. A
child interpreter gets this boundary instead:

- every Python started from the guard interpreter (``ChildGuard.python``)
  imports ``mcs_harness_child_guard`` from a ``.pth`` file in its own
  site-packages. The ``.pth`` is read with ``-I`` as well (only ``-S`` skips
  it), so isolated children are covered, and no PYTHONPATH is needed. The
  guard lets ``os.kill``/``os.killpg`` reach only the pids the test listed in
  the allow-list, unreaped children the same process started, and itself;
  everything else is refused with EPERM. Every decision is recorded.
- a fake ``pgrep`` never reads the system process table by name: it answers
  only the synthetic pids the test listed in the report list, while they run.

The allow-list and the report list are separate files, so a test can make the
child find a process that it must not signal.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import site
import subprocess
import sys

GUARD_MODULE = "mcs_harness_child_guard"
# The real-parent harness runs an old updater as a process. It runs only when
# this is exactly "1": the dedicated CI job sets it on a disposable runner, and
# a plain test run on an operator machine never does, whatever tags it has.
HARNESS_OPT_IN = "MCS_RUN_UPDATE_PARENT_HARNESS"


def harness_requested(environ) -> bool:
    """Return True only when `environ` explicitly asks for the real-parent harness."""
    return environ.get(HARNESS_OPT_IN) == "1"

_GUARD_SOURCE = r'''
"""Harness boundary: signals reach only synthetic targets this test run owns."""
import errno
import json
import os
import subprocess
import sys
import weakref

_ALLOW_FILE = {allow!r}
_LOG_FILE = {log!r}
_REAL_KILL = os.kill
_REAL_KILLPG = os.killpg
_OWNED = weakref.WeakSet()


def _read_allowed():
    # read once, before any updater code runs; unreadable allows nothing
    try:
        with open(_ALLOW_FILE) as stream:
            return frozenset(int(value) for value in stream.read().split())
    except (OSError, ValueError):
        return frozenset()


_ALLOWED = _read_allowed()


def _short(value):
    return value if len(value) <= 120 else value[:40] + "..."


def _record(**row):
    row["process"] = os.getpid()
    row["argv"] = [_short(part) for part in sys.orig_argv[:6]]
    try:
        with open(_LOG_FILE, "a") as stream:
            stream.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _owned(pid):
    # an unreaped child keeps its pid, so it cannot belong to anyone else yet
    return any(child.pid == pid and child.returncode is None for child in list(_OWNED))


def _allowed(pid):
    if pid == os.getpid():
        return True
    if pid > 0:
        return pid in _ALLOWED or _owned(pid)
    if pid < -1:
        return _owned(-pid)       # a group led by an unreaped child of this process
    return False                  # 0 is the group shared with the test runner, -1 is everyone


def _kill(pid, sig):
    if sig == 0:
        return _REAL_KILL(pid, sig)
    allowed = _allowed(pid)
    _record(event="signal", target=pid, signal=int(sig), allowed=allowed)
    if not allowed:
        raise PermissionError(errno.EPERM, "harness: not a synthetic target of this run")
    return _REAL_KILL(pid, sig)


def _killpg(pgid, sig):
    if sig == 0:
        return _REAL_KILLPG(pgid, sig)
    target = -pgid if pgid > 0 else 0
    allowed = _allowed(target)
    _record(event="signal", target=target, signal=int(sig), allowed=allowed)
    if not allowed:
        raise PermissionError(errno.EPERM, "harness: not a synthetic target of this run")
    return _REAL_KILLPG(pgid, sig)


_POPEN_INIT = subprocess.Popen.__init__


def _tracked_init(self, *args, **kwargs):
    _POPEN_INIT(self, *args, **kwargs)
    _OWNED.add(self)


subprocess.Popen.__init__ = _tracked_init
os.kill = _kill
os.killpg = _killpg
_record(event="installed", isolated=bool(sys.flags.isolated), allowed=sorted(_ALLOWED))
'''

_PGREP_SOURCE = r"""#!/bin/sh
# Harness pgrep. It never searches the system by name: it prints only the
# synthetic pids listed in the report file that still run (a zombie has
# ended). Exit 1 is "no match"; 3 is "cannot answer", never read as none.
report={report}
[ -r "$report" ] || {{ echo 'harness pgrep: report list unreadable' >&2; exit 3; }}
found=1
for pid in $(cat "$report"); do
    case "$pid" in *[!0-9]*) echo 'harness pgrep: bad pid in report list' >&2; exit 3 ;; esac
    state="$(/bin/ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')"
    case "$state" in ''|Z*) continue ;; esac
    echo "$pid"
    found=0
done
exit "$found"
"""


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


class ChildGuard:
    """Paths of one guard interpreter, its lists, its record and its fake pgrep."""

    def __init__(self, root: Path, bin_dir: Path):
        self.root = root
        self.python = root / "venv" / "bin" / "python3"
        self.allow_file = root / "signal-allow"
        self.report_file = root / "pgrep-report"
        self.log_file = root / "signal-log.jsonl"
        self.pgrep = bin_dir / "pgrep"

    def allow(self, *pids: int) -> None:
        """Write the pids a guarded child may signal (read at interpreter start)."""
        self.allow_file.write_text("".join(f"{pid}\n" for pid in pids))

    def report(self, *pids: int) -> None:
        """Write the synthetic pids the fake pgrep answers while they run."""
        self.report_file.write_text("".join(f"{pid}\n" for pid in pids))

    def records(self, event: str) -> list[dict]:
        """Return the guard's recorded rows of one event kind."""
        try:
            lines = self.log_file.read_text().splitlines()
        except FileNotFoundError:
            return []
        return [row for row in map(json.loads, lines) if row["event"] == event]


def create(root: Path, bin_dir: Path) -> ChildGuard:
    """Build the guard interpreter and fake pgrep under `root`; nothing is allowed yet."""
    guard = ChildGuard(root, bin_dir)
    root.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(guard.python.parent.parent)],
                   check=True, capture_output=True, timeout=120)
    (packages,) = (guard.python.parent.parent / "lib").glob("python*/site-packages")
    (packages / f"{GUARD_MODULE}.py").write_text(
        _GUARD_SOURCE.format(allow=str(guard.allow_file), log=str(guard.log_file)))
    # the test interpreter's own packages stay importable, then the guard loads
    (packages / "zz_mcs_harness_child_guard.pth").write_text(
        "".join(p + "\n" for p in site.getsitepackages() if os.path.isdir(p))
        + f"import {GUARD_MODULE}\n")
    guard.pgrep.write_text(_PGREP_SOURCE.format(report=_quote(guard.report_file)))
    guard.pgrep.chmod(0o755)
    guard.allow()
    guard.report()
    # fail closed before any updater runs: an isolated child must load the guard
    probe = subprocess.run([str(guard.python), "-I", "-c",
                            f"import os, sys; print({GUARD_MODULE!r} in sys.modules, os.kill.__module__)"],
                           capture_output=True, text=True, timeout=60)
    if probe.stdout.split() != ["True", GUARD_MODULE]:
        raise AssertionError("the guard interpreter does not load the signal guard: "
                             + probe.stdout + probe.stderr)
    return guard
