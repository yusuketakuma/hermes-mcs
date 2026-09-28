"""Shared filesystem plumbing for the card worker.

Directory layout under the configured MCS data root, the runner's
filename sanitize rule for cmd_results, and the DB-free view of
runner-published flags — never trusting a path that does not sit
under the configured data root.
"""
from __future__ import annotations

import os
import hashlib
import tempfile
from contextlib import suppress

# short key -> on-disk dir under the MCS data root
SUBDIRS = {"render": "discord_render", "state": "discord_state",
           "flags": "flags", "cmd_int": "cmd_int",
           "cmd_results": "cmd_results"}


def data_root(settings: dict) -> str:
    """The configured MCS data dir holding the five worker dirs."""
    root = settings.get("data_root")
    if not isinstance(root, str) or not root.strip() or "\x00" in root:
        raise ValueError("data_root")
    return root


def notify_dirs(root: str) -> dict[str, str]:
    return {key: os.path.join(root, name)
            for key, name in SUBDIRS.items()}


def ensure_dirs(root: str) -> dict[str, str]:
    out = notify_dirs(root)
    os.makedirs(out["state"], mode=0o700, exist_ok=True)
    for key in ("render", "cmd_int", "cmd_results", "flags"):
        # runner-owned dirs must already exist; a missing dir means the
        # deployment is not provisioned — surface it, never create a
        # host-like path by accident
        if not os.path.isdir(out[key]):
            raise FileNotFoundError(out[key])
    return out


def safe_name(command_id) -> str:
    """cmd_results filenames follow the runner's sanitize rule."""
    return ("".join(c if c.isalnum() or c in "._-" else "_"
                    for c in str(command_id))[:120] or "unknown")


def atomic_write(path: str, raw: bytes, tmp_prefix: str = ".atomic-",
                 mode: int | None = None, dir_fsync: bool = True) -> str:
    """tmp -> fsync -> [chmod] -> os.replace -> [dir fsync]: readers see
    the whole old file or the whole new one, and a mid-write crash
    leaves no torn file (the tmp file is unlinked on failure).
    Returns ``path``."""
    fd, tmp = tempfile.mkstemp(prefix=tmp_prefix, suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        if dir_fsync:
            dfd = os.open(os.path.dirname(path),
                          os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except OSError:
        with suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def read_result(results_dir: str, command_id: str) -> dict | None:
    path = os.path.join(results_dir, safe_name(command_id) + ".json")
    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > 64 * 1024:
                return None
            import json
            data = json.loads(handle.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_flags(root: str) -> dict:
    """Read effective flags and hold immediately when a restore marker appears."""
    import json
    path = os.path.join(root, "flags", "notify.json")
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    # A restore writes its durable marker before it can publish flags.
    # Previously granted cards and dependent parts must see that hold too.
    try:
        os.lstat(os.path.join(root, "restore_pending.json"))
    except FileNotFoundError:
        pass
    except OSError:
        data["restore_pending"] = True
    else:
        data["restore_pending"] = True
    return data


def read_verified_attachment(path: str | None, part: dict) -> bytes | None:
    """Return the sealed bytes once, bounded by the collector's 64 MiB limit."""
    size = part.get("bytes")
    if (not path or not part.get("sha256") or type(size) is not int
            or not 0 <= size <= 64 * 1024 * 1024):
        return None
    try:
        with open(path, "rb") as stream:
            if os.fstat(stream.fileno()).st_size != size:
                return None
            blob = stream.read(size + 1)
    except (OSError, ValueError):
        return None
    if len(blob) != size or hashlib.sha256(blob).hexdigest() != part["sha256"]:
        return None
    return blob
