"""Native Slack/LINE WORKS JSON commands using the shared MCS preview and confirmation gates."""
from __future__ import annotations

import asyncio
from pathlib import Path

from .dispatch import dispatch, parse
from .mcs_api import deny
from . import paths


async def run_native(handler, *args, **kwargs):
    """Keep a native command owned until its synchronous work has actually ended."""
    work = asyncio.create_task(asyncio.to_thread(handler, *args, **kwargs))
    cancelled = False
    while not work.done():
        try:
            # Cancelling wait() leaves the command task alive. Its caller remains
            # in the scope's active listeners until this function finishes.
            await asyncio.wait({work})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Observe a worker failure without turning a possibly published command
        # into a not-sent answer; the durable queue/receipt owns its outcome.
        if not work.cancelled():
            work.exception()
        raise asyncio.CancelledError
    return work.result()


def answer(settings, raw, *, user, channel, authorized=None):
    """Run an authenticated native command; callers deliver the answer privately."""
    transport = settings.get("transport")
    if transport not in ("slack", "lineworks"):
        return deny("native_context_rejected")
    if user not in settings.get("allowed_user_ids", ()):
        return deny("user_not_allowed")
    if channel != settings.get("channel_id"):
        return deny("chat_not_allowed")
    def current():
        if authorized is not None and authorized() is not True:
            return False
        flags = paths.read_flags(settings["data_root"])
        return paths.route_open(flags, transport, settings.get("route_epoch"))

    if not current():
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
        return dispatch(data, scoped, identity, authorized=current)
    except Exception:
        return deny("operation_failed")
