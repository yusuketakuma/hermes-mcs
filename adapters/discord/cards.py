"""Discord Components V2 rendering for validated card specs.

The transport-neutral contract — schema, budgets, and token context —
lives in ``adapters.common.spec`` (``validate``/``token_map``); this module
is only the LayoutView mapping plus SDK-edge file upload. The native
request retry guard belongs to delivery. The discord import stays inside
functions: registration and the /mcs command surface must work in an
environment without the SDK.
"""
from __future__ import annotations

from typing import BinaryIO

from adapters.common.spec import MAX_BUTTONS, MAX_COMPONENTS, MAX_TEXT, MAX_TOTAL_TEXT
from adapters.common.card_controls import (MENU_LABEL, STAMP_PICKER_LABEL,
                                           plan_controls, stamp_label)
from adapters.common.text import escape_md, notification_preview

# accent bar colour per card kind — the visible card edge; a missing
# kind (notice op) leaves the container unaccented
_ACCENTS = {"thread": 0x5865F2, "signal": 0xF0A233, "digest": 0xF0A233}
_URGENT_ACCENT = 0xED4245         # parts["discord"]["accent"] == "urgent"


def _accent(spec):
    own = spec["parts"].get("discord") or {}
    if own.get("accent") == "urgent":
        return _URGENT_ACCENT
    return _ACCENTS.get(spec.get("kind"))

ACTION_PREFIX = "mcs:a:"          # card button custom_id = prefix + token
MENU_ID = "mcs:menu"              # the 操作 select — value = token
_POST_BUTTONS_MAX = 5                   # one action row of per-post 💊 buttons
_MENU_MAX = 25                    # options per select



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
_EMBED_MAX = 4096                 # embed description
_ZONE_RULE = "─" * 12


def _zones(spec: dict, esc, *, face=False) -> tuple:
    """Card face as text zones: a container with ``rule`` and the footer
    each start a new zone (a Separator is drawn between zones). With
    ``face``, a post's stamp rows (parts["discord"]["face_posts"]) close
    the zone its post line opened, as subtext under its summary."""
    zones, cur, rows = [], [], []
    own = spec["parts"].get("discord") or {}
    actions = {a["at"]: a["button"] for a in own.get("post_actions") or []}
    stamps = {p["at"]: p["stamps"] for p in own.get("face_posts") or []} if face else {}
    for index, c in enumerate(spec["parts"]["containers"]):
        t = c["type"]
        if t == "meta":
            continue                       # correlation — not displayed
        if spec["parts"].get("processing_display_version") in (1, 2) and c.get("provider_scope") == "header":
            from processing_route import compact_header
            c = {**c, "text": compact_header(c)}
        if c.get("rule") and cur:
            zones.append(cur + rows)
            cur = []
        if c.get("rule"):
            rows = [f"-# {esc(ln)}" for row in stamps.get(index, ())
                    for ln in row.splitlines()]
        if index in actions:
            # (subtext line, button): a Section with the post's 💊 accessory
            cur.append(("-# " + esc(c["text"]).replace("\n", " "), actions[index]))
            continue
        if spec.get("kind") == "thread" and c.get("rule") and t == "text":
            # a post's time/sender line is subtext above its summary
            cur.extend(f"-# {esc(ln)}" for ln in c["text"].splitlines())
            continue
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
        zones.append(cur + rows)
    # every footer line gets the subtext prefix — one footer item may
    # hold several lines (the open-task list)
    footer = [f"-# {esc(ln)}"
              for c in spec["parts"].get("footer") or []
              if c.get("type") == "text"
              for ln in c["text"].splitlines()]
    return zones, footer


def _zone_text(zone) -> list:
    """A zone's lines with each post-action (line, button) pair reduced to its line."""
    return [ln if isinstance(ln, str) else ln[0] for ln in zone]


def _button_items(spec, primary_actions=()):
    """Buttons for ``primary_actions`` and links, every other operation in the menu.

    Cards pass none, so 確認・担当・タスク作成 sit in the menu as on Slack;
    the companion-thread drug view passes its own button set."""
    import discord
    controls = plan_controls(spec["parts"])
    primary, links, menu = [], [], []
    # A full link row leaves the stamp form reachable in the menu.
    operations = controls.menu if len(controls.links) < MAX_BUTTONS else controls.operations
    for button in (*operations, *controls.links):
        if spec["parts"].get("thread_drug_actions") is True \
                and button.get("id") in {"meds", "drugsearch"}:
            continue
        if button.get("ui") == "link":
            links.append(discord.ui.Button(style=discord.ButtonStyle.link,
                                            label=button["label"], url=button["url"]))
        elif button.get("id") in primary_actions:
            primary.append(discord.ui.Button(
                style=getattr(discord.ButtonStyle, button.get("style", "secondary")),
                label=button["label"], custom_id=f"{ACTION_PREFIX}{button['token']}"))
        else:
            menu.append(discord.SelectOption(label=button["label"], value=button["token"]))
    return primary + links, menu


def _stamp_rows(spec):
    """(stamp row, 別の投稿 button or None): emoji only, the own stamp marked ✓ as on Slack."""
    import discord
    controls = plan_controls(spec["parts"])
    form = controls.stamp_picker if len(controls.links) < MAX_BUTTONS else None
    row = [discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label=stamp_label(b),
        custom_id=f"{ACTION_PREFIX}{b['token']}") for b in controls.stamps]
    other = discord.ui.Button(style=discord.ButtonStyle.secondary, label=STAMP_PICKER_LABEL,
                              custom_id=f"{ACTION_PREFIX}{form['token']}") if form else None
    return row, other


def thread_drug_view(spec, *, private=False):
    """Runner-issued medication actions on the first companion-thread body."""
    import discord
    drug_actions = {"meds", "drugsearch"}
    if private:
        drug_actions |= {"summary", "search", "report"}
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
    def describe(with_stamps):
        zones, footer = _zones(spec, escape_md, face=with_stamps)
        posts = [ln for zone in zones for ln in zone if not isinstance(ln, str)]
        zones = [_zone_text(zone) for zone in zones]
        separator = ("\n" + _ZONE_RULE + "\n"
                     if spec["parts"].get("section_divider_version") == 1 else "\n\n")
        groups = ["\n".join(zone) for zone in zones]
        if footer:
            groups.append("\n".join(footer))
        return posts, separator.join(groups).strip() or "—"

    # the legacy embed has no sections: a post action's line stays text
    # and its 💊 becomes a button in a row of its own, labelled with the
    # post's time when more than one post has one. Face stamp rows are
    # the first thing to go over budget (the thread still has them).
    posts, face = describe(True)
    if len(face) > _EMBED_MAX:
        posts, face = describe(False)
    if len(face) > _EMBED_MAX:
        raise ValueError("discord_embed_budget")
    view = discord.ui.View(timeout=None)
    # the Slack order: 操作を選ぶ…, the stamp row, 別の投稿 + MCSで開く, then per-post 💊
    links, menu = _button_items(spec)
    if menu:
        view.add_item(discord.ui.Select(
            custom_id=MENU_ID, placeholder=MENU_LABEL,
            min_values=1, max_values=1, options=menu[:_MENU_MAX], row=0))
    stamps, other = _stamp_rows(spec)
    for button in stamps:
        button.row = 1
        view.add_item(button)
    for button in ([other] if other else []) + links:
        button.row = 2
        view.add_item(button)
    for line, button in posts[:_POST_BUTTONS_MAX]:
        # "-# MM-DD HH:MM sender…" -> "💊 MM-DD HH:MM"
        stamp = " ".join(line.removeprefix("-# ").split(" ")[:2])
        view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=button["label"] if len(posts) == 1 else f"💊 {stamp}"[:80],
            custom_id=f"{ACTION_PREFIX}{button['token']}", row=3))
    payload = {"content": escape_md(notification_preview(spec["parts"])),
               "embed": discord.Embed(description=face, colour=_accent(spec))}
    if links or menu or posts or stamps:
        view.stop()
        payload["view"] = view
    return payload


def build_view(spec: dict):
    """spec -> discord.ui.LayoutView. Called only after validate().
    Face: text zones split by Separators, a Separator, then the 操作を選ぶ…
    select, the stamp row and one row with 別の投稿 + MCSで開く."""
    import discord  # SDK required only inside the handler boundary

    zones, footer = _zones(spec, escape_md)
    face_zones, _ = _zones(spec, escape_md, face=True)

    def separator():
        return discord.ui.Separator(
            visible=True, spacing=discord.SeparatorSpacing.small)

    links, menu = _button_items(spec)
    stamps, other = _stamp_rows(spec)
    rows = []
    # the Slack order: 操作を選ぶ…, the stamp row, then 別の投稿 + MCSで開く
    if menu:
        select_row = discord.ui.ActionRow()
        select_row.add_item(discord.ui.Select(
            custom_id=MENU_ID, placeholder=MENU_LABEL,
            min_values=1, max_values=1, options=menu[:_MENU_MAX]))
        rows.append(select_row)
    for group in (stamps, ([other] if other else []) + links):
        row = discord.ui.ActionRow()
        for button in group:
            row.add_item(button)
        if row.children:
            rows.append(row)

    def face(groups):
        out = []
        for lines in groups:
            if out:
                out.append(separator())
            run = []
            for line in lines + [None]:
                if isinstance(line, str):
                    run.append(line)
                    continue
                out.extend(discord.ui.TextDisplay(chunk)
                           for chunk in _text_chunks(run))
                run = []
                if line is not None:
                    text, button = line
                    out.append(discord.ui.Section(
                        discord.ui.TextDisplay(text),
                        accessory=discord.ui.Button(
                            style=discord.ButtonStyle.secondary,
                            label=button["label"],
                            custom_id=f"{ACTION_PREFIX}{button['token']}")))
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

    def too_big(children):
        # the Container, its children (a Section also counts its text
        # and accessory) and every row's items count
        nested = sum(2 for c in children if getattr(c, "accessory", None) is not None)
        return len(children) > _CONTAINER_MAX or 1 + len(children) + nested + sum(
            len(r.children) for r in rows) > MAX_COMPONENTS

    def text_len(children):
        return sum(len(getattr(t, "content", None) or "")
                   for c in children for t in (c, *getattr(c, "children", ())))

    # first fit wins: face stamp rows outlast the rule zones but go before
    # the post buttons (the thread still has the rows); the last level is
    # used as is. The new divider format also text-checks the merged zones.
    def one(groups):
        merged = []
        for zone in groups:
            if merged and spec["parts"].get("section_divider_version") == 1:
                merged.append(_ZONE_RULE)
            merged.extend(zone)
        return [merged]
    levels = [(face_zones, True), (one(face_zones), True)] if face_zones != zones else []
    # Too many rule zones: merge them, retaining text dividers in the new
    # format. If needed, post buttons fall back to the 💊 menu entry.
    levels += [(zones, False), (one(zones), False), ([_zone_text(one(zones)[0])], False)]
    for groups, checked in levels:
        children = layout(groups)
        if not (too_big(children) or (checked or spec["parts"].get("section_divider_version") == 1)
                and text_len(children) > MAX_TOTAL_TEXT):
            break
    else:
        if spec["parts"].get("section_divider_version") == 1:
            raise ValueError("discord_section_budget")
    if not children:
        children.append(discord.ui.TextDisplay("—"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*children, accent_color=_accent(spec)))
    return view


def no_pings():
    """allowed_mentions for every card-side send/edit: footer names are
    <@id> mentions that must render as names and never notify anyone
    (nor may message text ever reach @everyone/roles)."""
    import discord  # SDK required only inside the handler boundary
    return discord.AllowedMentions.none()


_IMAGE_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp")


def spoiler_attachment(name: str) -> bool:
    """Images (care photos, documents shot by phone) upload as spoilers:
    the thread shows the caption and the file name, the picture only on
    tap. Discord stores them as ``SPOILER_<name>``."""
    return str(name or "").lower().endswith(_IMAGE_SUFFIX)


async def send_attachment(target, path: str | BinaryIO, name: str,
                          content: str | None = None):
    """File upload for a durable attachment part — kept here so
    delivery.py stays SDK-free (only cards/actions may import
    discord.py, and only inside functions). ``content`` is the file's
    visible caption line, sent in the same message."""
    import discord  # SDK required only inside the handler boundary
    kwargs = {"content": content} if content else {}
    return await target.send(
        file=discord.File(path, filename=name,
                          **({"spoiler": True} if spoiler_attachment(name) else {})),
        allowed_mentions=discord.AllowedMentions.none(), **kwargs)
