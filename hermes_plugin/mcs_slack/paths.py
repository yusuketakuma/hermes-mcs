"""Slack render/state directories beside the shared MCS command inbox."""

import os

from hermes_plugin.mcs_discord import paths


def notify_dirs(root: str) -> dict[str, str]:
    dirs = paths.notify_dirs(root)
    dirs["render"] = os.path.join(root, "slack_render")
    dirs["state"] = os.path.join(root, "slack_state")
    return dirs


def ensure_dirs(root: str) -> dict[str, str]:
    dirs = notify_dirs(root)
    os.makedirs(dirs["state"], mode=0o700, exist_ok=True)
    for key in ("render", "cmd_int", "cmd_results", "flags"):
        if not os.path.isdir(dirs[key]):
            raise FileNotFoundError(dirs[key])
    return dirs
