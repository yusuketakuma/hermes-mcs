"""Discord Components V2 rendering for validated card specs.

The transport-neutral contract — schema, budgets, and token context —
lives in ``mcs_delivery.spec`` (``validate``/``token_map``); this module
is only the LayoutView mapping plus the SDK-edge send helpers (file
upload, the single-post retry guard). The discord import stays inside
functions: registration and the /mcs command surface must work in an
environment without the SDK.
"""
from __future__ import annotations

import contextvars
import functools
import re
from typing import BinaryIO

from hermes_plugin.mcs_delivery.spec import MAX_TEXT

# discord.py releases whose HTTPClient.request retry loop and private
# aiohttp session (``_HTTPClient__session``) the single-post guard was
# verified against; the version is read off the client's own
# user_agent (formatted from discord.__version__) — anything else fails
# closed rather than risk an unguarded re-POST
_VERIFIED_SDK = frozenset({"2.7.1"})
_SDK_VERSION = re.compile(r"discord\.py (\S+)\)")
_GUARD_MARK = "_mcs_single_post"
# HTTP status of each POST issued inside the current single_post() call
# (None until/unless a response arrives); None outside it, so Hermes'
# own sends (other tasks, default context) pass untouched
_POSTS: contextvars.ContextVar[list[int | None] | None] = contextvars.ContextVar(
    "mcs_discord_single_post", default=None)

# accent bar colour per card kind — the visible card edge; a missing
# kind (notice op) leaves the container unaccented
_ACCENTS = {"thread": 0x5865F2, "signal": 0xF0A233, "digest": 0xF0A233}


def _text_chunks(lines, limit=MAX_TEXT):
    """Pack rendered lines into TextDisplay-sized chunks — a Container
    caps at 10 children and each display at ``limit`` chars, so the
    merged face must be split rather than emitted per container."""
    chunks, cur = [], ""
    for ln in lines:
        while len(ln) > limit:                   # single oversized line
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(ln[:limit])
            ln = ln[limit:]
        if cur and len(cur) + 1 + len(ln) > limit:
            chunks.append(cur)
            cur = ln
        else:
            cur = ln if not cur else cur + "\n" + ln
    if cur:
        chunks.append(cur)
    return chunks


def build_view(spec: dict):
    """spec -> discord.ui.LayoutView. Called only after validate()."""
    import discord  # SDK required only inside the handler boundary

    # timeout=None: dispatch lives on the on_interaction listener keyed
    # by custom_id, not on view-local callbacks — the view itself never
    # expires and holds no business logic
    view = discord.ui.LayoutView(timeout=None)
    # The whole face lives inside one Container — bare TextDisplays on
    # a LayoutView render as flat message text with no card look.
    lines = []
    for c in spec["parts"]["containers"]:
        t = c["type"]
        if t == "heading":
            lines.append(f"## {c['text']}")
        elif t == "field":
            lines.append(f"**{c['name']}**: {c['value']}")
        elif t == "quote":
            # '>>>' swallows to the end of the TextDisplay — merged
            # lines must use the per-line '>' form
            lines.extend(f"> {ln}" for ln in c["text"].splitlines())
        elif t == "meta":
            continue                       # correlation — not displayed
        else:
            lines.append(c["text"])
    # every footer line gets the subtext prefix — one footer item may
    # hold several lines (the open-task list)
    lines.extend(f"-# {ln}"
                 for c in spec["parts"].get("footer") or []
                 if c.get("type") == "text"
                 for ln in c["text"].splitlines())
    children = [discord.ui.TextDisplay(chunk)
                for chunk in _text_chunks(lines)]
    for row in spec["parts"].get("action_rows") or []:
        ar = discord.ui.ActionRow()
        for b in row:
            if b.get("ui") == "link":
                ar.add_item(discord.ui.Button(
                    style=discord.ButtonStyle.link, label=b["label"],
                    url=b["url"]))
                continue
            btn = discord.ui.Button(
                style=getattr(discord.ButtonStyle,
                              b.get("style", "secondary")),
                label=b["label"],
                custom_id=f"mcs:a:{b['token']}")
            # no callback — the native on_interaction listener is the
            # single dispatch point (plan §5)
            ar.add_item(btn)
        children.append(ar)
    if not children:
        children.append(discord.ui.TextDisplay("—"))
    view.add_item(discord.ui.Container(
        *children, accent_color=_ACCENTS.get(spec.get("kind"))))
    return view


def no_pings():
    """allowed_mentions for every card-side send/edit: footer names are
    <@id> mentions that must render as names and never notify anyone
    (nor may message text ever reach @everyone/roles)."""
    import discord  # SDK required only inside the handler boundary
    return discord.AllowedMentions.none()


async def send_attachment(target, path: str | BinaryIO, name: str):
    """File upload for a durable attachment part — kept here so
    delivery.py stays SDK-free (only cards/actions may import
    discord.py, and only inside functions)."""
    import discord  # SDK required only inside the handler boundary
    return await target.send(
        file=discord.File(path, filename=name),
        allowed_mentions=discord.AllowedMentions.none())


class RetryPolicyUnknown(RuntimeError):
    """The bound client is not a verified discord.py — a create POST
    could be re-sent by an unguarded SDK retry, so it is never made."""


class DiscordRetrySuppressed(RuntimeError):
    """The SDK tried a second POST inside one single_post() call; the
    first may have committed, so the outcome is unknown, not a resend."""


class _StatusRecorder:
    """Response context of a guarded POST: stores its HTTP status in
    the call's POST log once the response arrives (None if it never
    did — reset, timeout), so only a definitive 429 lets a retry through."""

    def __init__(self, cm, posts, i):
        self._cm, self._posts, self._i = cm, posts, i

    async def __aenter__(self):
        resp = await self._cm.__aenter__()
        self._posts[self._i] = getattr(resp, "status", None)
        return resp

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


def _guarded(request):
    @functools.wraps(request)
    def guarded(method, *args, **kwargs):
        posts = _POSTS.get()
        if posts is None or str(method).upper() != "POST":
            return request(method, *args, **kwargs)
        # a 429 is Discord refusing before acting — its re-POST cannot
        # duplicate; after 5xx, a reset or anything else the previous
        # POST may have committed
        if posts and posts[-1] != 429:
            raise DiscordRetrySuppressed(
                "discord.py retry of a create POST suppressed")
        posts.append(None)
        return _StatusRecorder(request(method, *args, **kwargs), posts,
                               len(posts) - 1)
    setattr(guarded, _GUARD_MARK, True)
    return guarded


def single_post_ready(bot) -> bool:
    """Install the single-post guard on the bot's current aiohttp
    session (once per session object) if the SDK is a verified one."""
    http = getattr(bot, "http", None)
    m = _SDK_VERSION.search(str(getattr(http, "user_agent", "")))
    if m is None or m.group(1) not in _VERIFIED_SDK:
        return False
    session = getattr(http, "_HTTPClient__session", None)
    request = getattr(session, "request", None)
    if not session or not callable(request):
        return False                     # discord MISSING before login
    if not getattr(request, _GUARD_MARK, False):
        session.request = _guarded(request)
    return True


async def single_post(bot, factory):
    """Await ``factory()`` allowing exactly one committable POST onto the
    wire — the SDK's in-call re-POST after a 429 goes out, after 5xx or
    ECONNRESET it raises DiscordRetrySuppressed before the session."""
    if not single_post_ready(bot):
        raise RetryPolicyUnknown("discord.py retry policy unverified")
    token = _POSTS.set([])
    try:
        return await factory()
    finally:
        _POSTS.reset(token)
