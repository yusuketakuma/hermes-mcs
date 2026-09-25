"""Neutral render-spec contract shared by every card transport.

``validate`` fails closed on shape or budget drift so a malformed
render never reaches a transport, and ``token_map`` captures each
button's render-time context for the worker registry. Transports add
their own scope checks and rendering on top (Slack's ``v2`` wraps this
``v1`` validator after checking its workspace fields).
"""
from __future__ import annotations

import re

SCHEMA = "mcs-card-render/v1"
OPS = ("create", "update", "revoke", "notice")
KINDS = ("thread", "signal", "digest")

# v1 display budgets (pinned from Components V2 / discord.py 2.7
# enforced limits) — a spec over budget is rejected whole, never
# truncated into a misleading card.
MAX_COMPONENTS = 40           # LayoutView counts rows and their buttons
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
    budget = MAX_COMPONENTS
    text_budget = MAX_TOTAL_TEXT
    for c in containers:
        if not isinstance(c, dict) \
                or c.get("type") not in _CONTAINER_TYPES:
            _err("bad_container")
        if c["type"] in ("heading", "text", "quote"):
            if not _text(c.get("text"), MAX_TEXT):
                _err("container_too_long")
            text_budget -= len(c["text"]) + 4   # "## " / ">>> " wrappers
        elif c["type"] == "field":
            if not _text(c.get("name"), 256) \
                    or not _text(c.get("value"), MAX_TEXT):
                _err("field_too_long")
            text_budget -= len(c["name"]) + len(c["value"]) + 6
        if c["type"] != "meta":
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
        budget -= len(row)
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
