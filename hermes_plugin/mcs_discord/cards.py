"""Discord Components V2 rendering for validated card specs.

The transport-neutral contract — schema, budgets, and token context —
lives in ``mcs_delivery.spec`` (``validate``/``token_map``); this module
is only the LayoutView mapping. The discord import stays inside
functions: registration and the /mcs command surface must work in an
environment without the SDK.
"""
from __future__ import annotations

from typing import BinaryIO

from ..mcs_delivery.spec import MAX_TEXT

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
    lines.extend(f"-# {c['text']}"
                 for c in spec["parts"].get("footer") or []
                 if c.get("type") == "text")
    children = [discord.ui.TextDisplay(chunk)
                for chunk in _text_chunks(lines)]
    for row in spec["parts"].get("action_rows") or []:
        ar = discord.ui.ActionRow()
        for b in row:
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


async def send_attachment(target, path: str | BinaryIO, name: str):
    """File upload for a durable attachment part — kept here so
    delivery.py stays SDK-free (only cards/actions may import
    discord.py, and only inside functions)."""
    import discord  # SDK required only inside the handler boundary
    return await target.send(
        file=discord.File(path, filename=name))
