"""Native Slack/LINE WORKS JSON commands using the shared MCS preview and confirmation gates."""
from __future__ import annotations

from pathlib import Path

from .dispatch import dispatch, parse
from .mcs_api import deny
from . import paths


def answer(settings, raw, *, user, channel):
    """Run an authenticated native command; callers deliver the answer privately."""
    transport = settings.get("transport")
    if transport not in ("slack", "lineworks"):
        return deny("native_context_rejected")
    if user not in settings.get("allowed_user_ids", ()):
        return deny("user_not_allowed")
    if channel != settings.get("channel_id"):
        return deny("chat_not_allowed")
    flags = paths.read_flags(settings["data_root"])
    if not paths.route_open(flags, transport, settings.get("route_epoch")):
        return deny("interactive_off")
    identity = {"transport": transport, "user_id": user, "chat_id": channel,
                "scope_id": settings["team_id"], "profile": settings["profile"],
                "application_id": settings["application_id"],
                "route_epoch": settings["route_epoch"]}
    scoped = {**settings, "inbox": settings.get("inbox") or str(Path(settings["data_root"]) / "cmd"),
              "allowed_chat_ids": {channel}}
    # only a parse error is the caller's; a handler bug is operation_failed,
    # as in the Hermes /mcs handler
    try:
        data = parse(raw)
    except (ValueError, UnicodeError, TypeError):
        return deny("bad_command")
    except Exception:
        return deny("operation_failed")
    if not isinstance(data, dict) or "op" not in data:
        return deny("bad_command")
    try:
        return dispatch(data, scoped, identity)
    except Exception:
        return deny("operation_failed")
