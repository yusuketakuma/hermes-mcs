"""Discord Components V2 rendering for validated card specs.

The transport-neutral contract — schema, budgets, and token context —
lives in ``adapters.common.spec`` (``validate``/``token_map``); this module
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

from adapters.common.spec import MAX_COMPONENTS, MAX_TEXT, PRIMARY_ACTIONS
from adapters.common.text import notification_preview

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

MENU_ID = "mcs:menu"              # the 他の操作 select — value = token
_MENU_PLACEHOLDER = "他の操作…"
_MENU_MAX = 25                    # options per select
# kept verbatim: runner footer mentions (pings stay off via
# allowed_mentions) and URLs (a substitution would break the link)
_MD_KEEP = re.compile(r"(<@[!&]?\w+>|https?://\S+)")
# same-length look-alikes, so escaping never moves a text budget
_MD_INLINE = str.maketrans("*_~|`[]<\\", "＊＿～｜｀［］＜＼")
_MD_LINE = re.compile(r"^([ \t]*)(?:(\d+)\.|([#>+-]))", re.M)
_MD_LEAD = str.maketrans("#>+-", "＃＞＋－")


def escape_md(text: str) -> str:
    """User-authored text as literal Discord text with the same length:
    inline markup (``* _ ~ | ` [ ] < \\`` — no masked link, no tag)
    and the block markers at a line start (``#``/``-#`` headings, ``>``
    quote, ``-``/``+``/``1.`` lists) become fullwidth look-alikes."""
    text = "".join(part if i % 2 else part.translate(_MD_INLINE)
                   for i, part in enumerate(_MD_KEEP.split(text)))
    return _MD_LINE.sub(
        lambda m: m.group(1) + (f"{m.group(2)}．" if m.group(2)
                                else m.group(3).translate(_MD_LEAD)), text)


def _text_chunks(lines, limit=MAX_TEXT):
    """Pack rendered lines into TextDisplay-sized chunks — a Container
    caps at 10 children and each display at ``limit`` chars, so a zone
    is merged and split rather than emitted per container."""
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


_CONTAINER_MAX = 10               # children of one Container


def _zones(spec: dict, esc) -> tuple:
    """Card face as text zones: a container with ``rule`` and the footer
    each start a new zone (a Separator is drawn between zones)."""
    zones, cur = [], []
    for c in spec["parts"]["containers"]:
        t = c["type"]
        if t == "meta":
            continue                       # correlation — not displayed
        if c.get("rule") and cur:
            zones.append(cur)
            cur = []
        if t == "heading":
            cur.append(f"## {esc(c['text'])}")
        elif t == "field":
            cur.append(f"**{esc(c['name'])}**: {esc(c['value'])}")
        elif t == "quote":
            # '>>>' swallows to the end of the TextDisplay — merged
            # lines must use the per-line '>' form
            cur.extend(f"> {esc(ln)}" for ln in c["text"].splitlines())
        else:
            cur.append(esc(c["text"]))
    if cur:
        zones.append(cur)
    # every footer line gets the subtext prefix — one footer item may
    # hold several lines (the open-task list)
    footer = [f"-# {esc(ln)}"
              for c in spec["parts"].get("footer") or []
              if c.get("type") == "text"
              for ln in c["text"].splitlines()]
    return zones, footer


def _button_items(spec, primary_actions=PRIMARY_ACTIONS):
    """The same durable primary/link buttons and secondary menu in both formats."""
    import discord
    primary, menu, links = [], [], []
    for row in spec["parts"].get("action_rows") or []:
        for button in row:
            if spec["parts"].get("thread_drug_actions") is True \
                    and button.get("id") in {"meds", "drugsearch"}:
                continue
            if button.get("ui") == "link":
                links.append(discord.ui.Button(style=discord.ButtonStyle.link,
                                               label=button["label"], url=button["url"]))
            elif button.get("id") in primary_actions:
                primary.append(discord.ui.Button(
                    style=getattr(discord.ButtonStyle, button.get("style", "secondary")),
                    label=button["label"], custom_id=f"mcs:a:{button['token']}"))
            else:
                menu.append(discord.SelectOption(label=button["label"], value=button["token"]))
    return primary + links, menu


def thread_drug_view(spec):
    """Runner-issued medication actions on the first companion-thread body."""
    import discord
    drug_actions = {"meds", "drugsearch"}
    rows = [[button for row in spec["parts"].get("action_rows") or []
             for button in row if button.get("id") in drug_actions]]
    buttons, _ = _button_items({"parts": {"action_rows": rows}}, drug_actions)
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for button in buttons:
        view.add_item(button)
    view.stop()
    return view


def message_payload(spec, *, components_v2=False):
    """New cards carry preview content; existing V2 messages retain their valid format."""
    import discord
    if components_v2:
        view = build_view(spec)
        # MCS dispatches via the bot-wide on_interaction listener, not
        # native View callbacks. Keep the wire components but prevent
        # discord.py from retaining one persistent View for every card.
        view.stop()
        return {"view": view}
    zones, footer = _zones(spec, escape_md)
    face = "\n\n".join(["\n".join(zone) for zone in zones] + ["\n".join(footer)]).strip() or "—"
    if len(face) > 4096:
        raise ValueError("discord_embed_budget")
    view = discord.ui.View(timeout=None)
    primary, menu = _button_items(spec)
    for button in primary:
        button.row = 0
        view.add_item(button)
    if menu:
        view.add_item(discord.ui.Select(
            custom_id=MENU_ID, placeholder=_MENU_PLACEHOLDER,
            min_values=1, max_values=1, options=menu[:_MENU_MAX], row=1))
    payload = {"content": escape_md(notification_preview(spec["parts"])),
               "embed": discord.Embed(description=face, colour=_ACCENTS.get(spec.get("kind")))}
    if primary or menu:
        view.stop()
        payload["view"] = view
    return payload


def build_view(spec: dict):
    """spec -> discord.ui.LayoutView. Called only after validate().
    Face: text zones split by Separators, a Separator, then one row of
    primary buttons (+ link) and one row holding the 他の操作 select."""
    import discord  # SDK required only inside the handler boundary

    zones, footer = _zones(spec, escape_md)

    def separator():
        return discord.ui.Separator(
            visible=True, spacing=discord.SeparatorSpacing.small)

    primary, menu = discord.ui.ActionRow(), []
    buttons, menu = _button_items(spec)
    for button in buttons:
        primary.add_item(button)
    rows = []
    if primary.children:
        rows.append(primary)
    if menu:
        select_row = discord.ui.ActionRow()
        select_row.add_item(discord.ui.Select(
            custom_id=MENU_ID, placeholder=_MENU_PLACEHOLDER,
            min_values=1, max_values=1, options=menu[:_MENU_MAX]))
        rows.append(select_row)

    def face(groups):
        out = []
        for lines in groups:
            if out:
                out.append(separator())
            out.extend(discord.ui.TextDisplay(chunk)
                       for chunk in _text_chunks(lines))
        return out

    def layout(groups):
        children = face(groups)
        if footer:
            if children:
                children.append(separator())
            children.extend(discord.ui.TextDisplay(chunk)
                            for chunk in _text_chunks(footer))
        if rows:
            if children:
                children.append(separator())
            children.extend(rows)
        return children

    children = layout(zones)
    # the Container, its children and every row's items count
    if len(children) > _CONTAINER_MAX or 1 + len(children) + sum(
            len(r.children) for r in rows) > MAX_COMPONENTS:
        # too many rule zones: one text zone (no rule lines — the text
        # must stay within the validator's budget)
        children = layout([[ln for z in zones for ln in z]])
    if not children:
        children.append(discord.ui.TextDisplay("—"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        *children, accent_color=_ACCENTS.get(spec.get("kind"))))
    return view


def no_pings():
    """allowed_mentions for every card-side send/edit: footer names are
    <@id> mentions that must render as names and never notify anyone
    (nor may message text ever reach @everyone/roles)."""
    import discord  # SDK required only inside the handler boundary
    return discord.AllowedMentions.none()


async def send_attachment(target, path: str | BinaryIO, name: str,
                          content: str | None = None):
    """File upload for a durable attachment part — kept here so
    delivery.py stays SDK-free (only cards/actions may import
    discord.py, and only inside functions). ``content`` is the file's
    visible caption line, sent in the same message."""
    import discord  # SDK required only inside the handler boundary
    kwargs = {"content": content} if content else {}
    return await target.send(
        file=discord.File(path, filename=name),
        allowed_mentions=discord.AllowedMentions.none(), **kwargs)


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
