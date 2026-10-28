"""Resolve migrated per-profile MCS settings without mixing legacy grants."""
from __future__ import annotations

import os
from pathlib import Path

from .bridge import _ensure_repo_root


class _View:
    def __init__(self, context, mode, profile=None, values=None):
        self._context = context
        self.mode = mode
        self.profile = profile
        self._values = values or {}

    def get_config(self, key, default=None):
        if self.mode == "legacy":
            return self._context.get_config(key, default)
        if self.mode == "central":
            return self._values.get(key, default)
        return default

    def __getattr__(self, name):
        return getattr(self._context, name)


def view(context):
    """Read one config generation; an explicit central marker never falls back."""
    getter = getattr(context, "get_config", None)
    if getter is None:
        return _View(context, "legacy")
    try:
        marker = getter("settings_source", None)
        data_root = getter("data_root", None)
    except Exception:
        return _View(context, "invalid")
    if marker not in (None, "central"):
        return _View(context, "invalid")
    if not isinstance(data_root, str) or not data_root or not os.path.isabs(data_root):
        return _View(context, "invalid" if marker else "legacy")
    expected_root = os.path.abspath(os.path.expanduser(os.environ.get("MCS_ROOT", "~/.mcs")))
    expected_data = os.path.join(expected_root, "data")
    if marker == "central" and os.path.normpath(data_root) != expected_data:
        return _View(context, "invalid")
    # Legacy installations can have an arbitrary root; retain their behavior
    # until a central block is actually found. The central format pins /data.
    root = Path(os.path.normpath(data_root)).parent
    path = root / "config.json"
    _ensure_repo_root()
    from adapters.common.mcs_api import bootstrap
    bootstrap()
    import mcs_util
    try:
        config = mcs_util.parse_config(mcs_util._read_local(str(path), mcs_util.CONFIG_MAX_BYTES))
    except FileNotFoundError:
        return _View(context, "invalid" if marker else "legacy")
    except (OSError, ValueError, UnicodeError, RecursionError):
        return _View(context, "invalid")
    if not isinstance(config, dict):
        return _View(context, "invalid")
    if "hermes_plugin_settings" not in config:
        return _View(context, "invalid" if marker else "legacy")
    block = config["hermes_plugin_settings"]
    if (not isinstance(block, dict) or type(block.get("schema_version")) is not int
            or block["schema_version"] != 1 or not isinstance(block.get("profiles"), dict)):
        return _View(context, "invalid")
    try:
        profile = context.profile_name
    except Exception:
        return _View(context, "invalid")
    if not isinstance(profile, str) or not profile or profile != profile.strip():
        return _View(context, "invalid")
    entry = block["profiles"].get(profile)
    if not isinstance(entry, dict):
        return _View(context, "invalid")
    mode = entry.get("mode")
    if mode == "legacy" and marker is None:
        return _View(context, "legacy", profile)
    if mode != "central" or marker != "central":
        return _View(context, "invalid")
    if (os.path.normpath(data_root) != expected_data
            or entry.get("data_root") != expected_data):
        return _View(context, "invalid")
    values = entry.get("settings")
    if not isinstance(values, dict):
        return _View(context, "invalid")
    values = dict(values)
    for name, expected in (
        ("data_root", expected_data),
        ("snapshot", os.path.join(expected_data, "snapshots", "ledger-snapshot.db")),
        ("inbox", os.path.join(expected_data, "cmd")),
    ):
        if name != "data_root" and name not in values:
            return _View(context, "invalid")
        if name in values and values[name] != expected:
            return _View(context, "invalid")
        values[name] = expected
    if any(values.get(key) not in (None, profile) for key in ("profile", "slack_profile")):
        return _View(context, "invalid")
    values["profile"] = profile
    values["slack_profile"] = profile
    return _View(context, "central", profile, values)
