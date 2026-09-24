"""Render-spec validation and conversion to Discord Components V2.

The spec file is the contract — ``validate`` fails closed on shape or
budget drift so a malformed render never reaches Discord, and
``build_view`` maps the neutral container model onto LayoutView items.
The discord import stays inside functions: registration and the /mcs
command surface must work in an environment without the SDK.
"""
from __future__ import annotations

import re

SCHEMA = "mcs-card-render/v1"
OPS = ("create", "update", "revoke", "notice")
KINDS = ("thread", "signal", "digest")

# Components V2 budgets (discord.py 2.7 enforced limits) — a spec over
# budget is rejected whole, never truncated into a misleading card.
MAX_TOP_LEVEL = 40            # LayoutView item ceiling
MAX_TOTAL_TEXT = 4000         # summed across all TextDisplay items
MAX_TEXT = 4000               # single TextDisplay content limit
MAX_ROWS = 5                  # ActionRow per view
MAX_BUTTONS = 5               # buttons per ActionRow
MAX_LABEL = 80
MAX_THREAD_NAME = 100
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")

STYLES = {"primary": 1, "secondary": 2, "success": 3, "danger": 4}
_CONTAINER_TYPES = ("heading", "text", "field", "quote", "meta")


def _err(msg):
    raise ValueError(msg)


def _text(v, n):
    return isinstance(v, str) and 0 < len(v) <= n


def _opt_id(v):
    return v is None or _text(v, 64)


def validate(spec) -> dict:
    """Shape + budget check. Returns the spec or raises ValueError with
    a stable reason (surfaced to receipts/logs — no PHI in the reason)."""
    if not isinstance(spec, dict):
        _err("bad_spec")
    if spec.get("schema") != SCHEMA:
        _err("bad_schema")
    if not _UUID.match(str(spec.get("delivery_id") or "")):
        _err("bad_delivery_id")
    if spec.get("op") not in OPS:
        _err("bad_op")
    if type(spec.get("render_rev")) is not int or spec["render_rev"] < 1:
        _err("bad_render_rev")
    for k in ("source_generation", "presentation_generation",
              "ui_revision"):
        if type(spec.get(k)) is not int or spec[k] < 0:
            _err(f"bad_{k}")
    if spec["op"] != "notice":
        if not _text(spec.get("card_key"), 200):
            _err("bad_card_key")
        if spec.get("kind") not in KINDS:
            _err("bad_kind")
    delivery = spec.get("delivery")
    if not isinstance(delivery, dict):
        _err("bad_delivery")
    for k in ("application_id", "channel_id"):
        if not _text(delivery.get(k), 64):
            _err(f"bad_delivery_{k}")
    for k in ("profile", "guild_id", "message_id", "thread_id"):
        if not _opt_id(delivery.get(k)):
            _err(f"bad_delivery_{k}")
    if spec["op"] in ("update", "revoke") \
            and not delivery.get("message_id"):
        # an edit/delete with no target is a malformed spec — fail at
        # validation instead of burning an attempt as no_target
        _err("bad_delivery_message_id")
    if type(delivery.get("route_epoch")) is not int \
            or delivery["route_epoch"] < 1:
        _err("bad_route_epoch")
    if not _HEX32.match(str(delivery.get("correlation") or "")):
        _err("bad_correlation")
    ids = delivery.get("intent_event_ids")
    if not isinstance(ids, list) \
            or not all(type(i) is int and i > 0 for i in ids):
        _err("bad_intent_event_ids")
    parts = spec.get("parts")
    if not isinstance(parts, dict):
        _err("bad_parts")
    containers = parts.get("containers")
    if not isinstance(containers, list):
        _err("bad_containers")
    budget = MAX_TOP_LEVEL
    text_budget = MAX_TOTAL_TEXT
    for c in containers:
        if not isinstance(c, dict) \
                or c.get("type") not in _CONTAINER_TYPES:
            _err("bad_container")
        if c["type"] in ("heading", "text", "quote"):
            if not _text(c.get("text"), MAX_TEXT):
                _err("container_too_long")
            text_budget -= len(c["text"]) + 4   # "## " / "> " wrappers
        elif c["type"] == "field":
            if not _text(c.get("name"), 256) \
                    or not _text(c.get("value"), MAX_TEXT):
                _err("field_too_long")
            text_budget -= len(c["name"]) + len(c["value"]) + 5
        budget -= 1
    footer = parts.get("footer") or []
    if not isinstance(footer, list):
        _err("bad_footer")
    for c in footer:
        if not isinstance(c, dict) \
                or c.get("type") not in ("text", "meta"):
            _err("bad_footer_item")
        if c["type"] == "text":
            if not _text(c.get("text"), MAX_TEXT):
                _err("footer_too_long")
            text_budget -= len(c["text"]) + 3   # "-# " wrapper
            budget -= 1
    if text_budget < 0:
        _err("text_budget")
    rows = parts.get("action_rows") or []
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        _err("bad_action_rows")
    budget -= len(rows)
    for row in rows:
        if not isinstance(row, list) or not row \
                or len(row) > MAX_BUTTONS:
            _err("bad_action_row")
        for b in row:
            if not isinstance(b, dict) or b.get("ui") != "button":
                _err("bad_button")
            if not _TOKEN.match(str(b.get("token") or "")):
                _err("bad_button_token")
            if not _text(b.get("label"), MAX_LABEL):
                _err("bad_button_label")
            if b.get("style", "secondary") not in STYLES:
                _err("bad_button_style")
            if not _text(b.get("id"), 32):
                _err("bad_button_id")
    if budget < 0:
        _err("component_budget")
    name = parts.get("thread_name")
    if name is not None and not _text(name, MAX_THREAD_NAME):
        _err("bad_thread_name")
    return spec


def build_view(spec: dict):
    """spec -> discord.ui.LayoutView. Called only after validate()."""
    import discord  # SDK required only inside the handler boundary

    # timeout=None: dispatch lives on the on_interaction listener keyed
    # by custom_id, not on view-local callbacks — the view itself never
    # expires and holds no business logic
    view = discord.ui.LayoutView(timeout=None)
    for c in spec["parts"]["containers"]:
        t = c["type"]
        if t == "heading":
            view.add_item(discord.ui.TextDisplay(f"## {c['text']}"))
        elif t == "field":
            view.add_item(
                discord.ui.TextDisplay(f"**{c['name']}**: {c['value']}"))
        elif t == "quote":
            view.add_item(discord.ui.TextDisplay(f"> {c['text']}"))
        elif t == "meta":
            continue                       # correlation — not displayed
        else:
            view.add_item(discord.ui.TextDisplay(c["text"]))
    for c in spec["parts"].get("footer") or []:
        if c.get("type") == "text":
            view.add_item(discord.ui.TextDisplay(f"-# {c['text']}"))
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
        view.add_item(ar)
    return view


def token_map(spec: dict) -> dict:
    """token -> captured context for the registry, from the spec's own
    action rows — works before the snapshot has seen the new tokens."""
    out = {}
    ctx = spec["parts"].get("context") or {}
    for row in spec["parts"].get("action_rows") or []:
        for b in row:
            out[b["token"]] = {
                "action": b["id"],
                "card_key": spec.get("card_key"),
                "kind": spec.get("kind"),
                "project_id": ctx.get("project_id"),
                "context": ctx,
                "channel_id": spec["delivery"].get("channel_id"),
            }
    return out
