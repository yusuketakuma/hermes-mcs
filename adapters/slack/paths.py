"""Slack render/state directories beside the shared MCS command inbox."""
from __future__ import annotations

import os

from hermes_plugin.mcs_delivery import paths


def notify_dirs(root: str) -> dict[str, str]:
    dirs = paths.notify_dirs(root)
    dirs["render"] = os.path.join(root, "slack_render")
    dirs["state"] = os.path.join(root, "slack_state")
    return dirs


def ensure_dirs(root: str) -> dict[str, str]:
    return paths.ensure_dirs(root, notify_dirs(root))
