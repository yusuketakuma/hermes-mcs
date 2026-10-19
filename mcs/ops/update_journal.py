"""Read and atomically publish the updater journal without loading its lifecycle CLI."""
from __future__ import annotations

import json
import os
import stat

from mcs_util import atomic_write

def default_state() -> dict:
    return {"v": 1, "stages": [], "applied": [], "attempts": {},
            "executed": {}, "applying": None}


def valid_state(state):
    """Check journal shapes before any recovery decision or mutation."""
    if not isinstance(state, dict) or type(state.get("v")) is not int \
            or state["v"] != 1:
        return False
    for key in ("stages", "applied"):
        if not isinstance(state.get(key, []), list) \
                or any(not isinstance(row, dict) for row in state.get(key, [])):
            return False
    if state.get("applying") is not None and not isinstance(state["applying"], dict):
        return False
    for key in ("attempts", "executed", "restore_consent"):
        if key in state and not isinstance(state[key], dict):
            return False
    records = [*state.get("stages", []), *state.get("applied", []),
               state.get("applying") or {}]
    for row in records:
        stamp = row.get("at", 0)
        if type(stamp) not in (int, float) or not 0 <= stamp < 1e12:
            return False
    return all(isinstance(row.get("stage"), str)
               for row in state.get("stages", []))


def read_control_json(path):
    """Read a regular local control file without following links or waiting on FIFOs."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        # fdopen raises (e.g. IsADirectoryError) without closing ``fd``
        handle = os.fdopen(fd, "r", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("control_file_not_regular")
        return json.load(handle)


def load_state(path) -> dict:
    """Lock-free read — writers publish atomically so a torn read never
    happens. Unparsable state is 'corrupt': callers must escalate and
    touch nothing (S17/S24)."""
    try:
        state = read_control_json(path)
    except FileNotFoundError:
        return {"_corrupt": True} if os.path.lexists(path) else default_state()
    except (OSError, ValueError, RecursionError):
        return {"_corrupt": True}
    if not valid_state(state):
        return {"_corrupt": True}
    return {**default_state(), **state}


def save_state(path, state: dict) -> None:
    """tmp -> fsync -> os.replace -> dir fsync (R13). Caller holds
    update.lock (or is the --locks-held post-merge child)."""
    atomic_write(path,
                 lambda f: json.dump(state, f, ensure_ascii=False,
                                     sort_keys=True),
                 tmp_prefix=".update_state.")
