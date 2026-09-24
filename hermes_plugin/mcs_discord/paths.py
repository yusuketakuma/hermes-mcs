"""Shared filesystem plumbing for the card worker.

Directory layout under the configured MCS data root, the runner's
filename sanitize rule for cmd_results, and the DB-free view of
runner-published flags — never trusting a path that does not sit
under the configured data root.
"""
from __future__ import annotations

import os

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


def read_result(results_dir: str, command_id: str) -> dict | None:
    path = os.path.join(results_dir, safe_name(command_id) + ".json")
    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > 64 * 1024:
                return None
            import json
            return json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def read_flags(root: str) -> dict:
    """flags/notify.json — the runner-published effective state."""
    import json
    path = os.path.join(root, "flags", "notify.json")
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
