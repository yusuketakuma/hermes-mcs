"""Native Slack/LINE WORKS JSON commands using the shared MCS preview and confirmation gates."""
from __future__ import annotations

from pathlib import Path

from hermes_plugin import _dispatch, _parse, _deny
from . import paths


def answer(settings, raw, *, user, channel):
    """Run an authenticated native command; callers deliver the answer privately."""
    transport = settings.get("transport")
    if transport not in ("slack", "lineworks"):
        return _deny("native_context_rejected")
    if user not in settings.get("allowed_user_ids", ()):
        return _deny("user_not_allowed")
    if channel != settings.get("channel_id"):
        return _deny("chat_not_allowed")
    flags = paths.read_flags(settings["data_root"])
    if (flags.get("interactive") is not True or flags.get("restore_pending")
            or flags.get("transport") != transport
            or flags.get("route_epoch") != settings.get("route_epoch")):
        return _deny("interactive_off")
    identity = {"transport": transport, "user_id": user, "chat_id": channel,
                "scope_id": settings["team_id"], "profile": settings["profile"],
                "application_id": settings["application_id"],
                "route_epoch": settings["route_epoch"]}
    scoped = {**settings, "inbox": settings.get("inbox") or str(Path(settings["data_root"]) / "cmd"),
              "allowed_chat_ids": {channel}}
    try:
        data = _parse(raw)
        if not isinstance(data, dict) or "op" not in data:
            return _deny("bad_command")
        return _dispatch(data, scoped, identity)
    except (ValueError, UnicodeError, TypeError):
        return _deny("bad_command")
    except Exception:
        return _deny("operation_failed")
