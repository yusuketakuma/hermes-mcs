"""Render the shared card display using official LINE WORKS message limits."""
from __future__ import annotations

import hashlib
import re

from adapters.common.spec import PRIMARY_ACTIONS, STAMP_ROW_ACTION, validate as validate_display
from adapters.common.text import notification_preview, stamp_icon
from notify_render import display_text
from present_lineworks import card_split
from notify_parts import split_body_chunks

SCHEMA = "mcs-card-render/v3"


def validate(spec):
    delivery = spec.get("delivery") if isinstance(spec, dict) else None
    if (not isinstance(delivery, dict) or spec.get("schema") != SCHEMA
            or delivery.get("transport") != "lineworks"
            or not isinstance(delivery.get("team_id"), str)
            or not 0 < len(delivery["team_id"]) <= 64
            or "\x00" in delivery["team_id"] or "guild_id" in delivery):
        raise ValueError("bad_lineworks_scope")
    validate_display({**spec, "schema": "mcs-card-render/v1"})
    if not isinstance(spec.get("card_key"), str) or not 0 < len(spec["card_key"]) <= 200:
        raise ValueError("bad_card_key")
    if spec.get("op") == "revoke":
        return spec  # a revoke sends only the heading; the body is never rendered
    chunks = spec["parts"].get("thread_body_parts") or []
    if any(len(chunk) > 1900 for chunk in chunks):
        raise ValueError("lineworks_text_budget")
    entries = spec["parts"].get("manifest") or []
    if not any(part.get("kind") == "thread" for part in entries):
        # no thread to carry the tail: render() shows the truncated head
        return spec
    groups = {part.get("name") for part in entries
              if part.get("kind") == "body_part"}
    rest = card_split(display_text(spec["parts"]))[1]
    if rest and "display#1" not in groups:
        raise ValueError("lineworks_display_overflow_missing")
    expected = split_body_chunks(rest)
    actual = {part.get("name"): chunks[int(part["part_id"].rsplit(":", 1)[1]) - 1]
              for part in entries if part.get("kind") == "body_part"
              and str(part.get("name") or "").startswith("display#")}
    if actual != {f"display#{i}": chunk for i, chunk in enumerate(expected, 1)}:
        raise ValueError("lineworks_display_overflow_mismatch")
    return spec


def logical_message_id(spec):
    """A local card binding, never an invented provider message identifier."""
    return "lw:" + hashlib.sha256(spec["card_key"].encode()).hexdigest()[:32]


def buttons(text, actions, *, preview=None):
    if not actions:
        return {"type": "text", "text": text or "MCS"}
    if len(text) > 1000 or not 1 <= len(actions) <= 10:
        raise ValueError("lineworks_button_budget")
    def button(action, **extra):
        return {"type": "button", "style": "secondary", **extra, "action": {
            **action, **({"text": action["label"]} if action["type"] == "message" else {})}}
    # MCS's own stamp row is one horizontal row where the first stamp sits.
    stamps = [{k: v for k, v in a.items() if k != "_stamp"} for a in actions if a.get("_stamp")]
    footer = []
    for a in actions:
        if not a.get("_stamp"):
            footer.append(button(a))
        elif a is next(x for x in actions if x.get("_stamp")):
            footer.append({"type": "box", "layout": "horizontal", "spacing": "xs",
                           "contents": [button(x, height="sm") for x in stamps]})
    contents = {"type": "bubble", "body": {
        "type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": text or "MCS", "wrap": True}]},
        "footer": {"type": "box", "layout": "vertical", "contents": footer}}
    # client._validate_content rejects C0/DEL in altText; split() alone keeps them.
    alt = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", preview or text or "MCS").split()) or "MCS"
    return {"type": "flex", "altText": alt[:399] + "…" if len(alt) > 400 else alt,
            "contents": contents}


# Card buttons in display order, as on Slack: 操作を選ぶ…, the stamp row, 別の投稿,
# MCSで開く. Every other operation (確認・担当・タスク作成 too) sits behind ``more``.
CARD_BUTTONS = ("more", STAMP_ROW_ACTION, "stamp", "link")
# without a ``more`` button nothing could reach them, so they stay on the card
_FALLBACK_BUTTONS = (*PRIMARY_ACTIONS, "link", "more", STAMP_ROW_ACTION)
_LABELS = {"more": "操作を選ぶ…", "stamp": "別の投稿"}


def _slot(button):
    return "link" if button.get("ui") == "link" else button["id"]


def _on_card(spec):
    """Buttons drawn on the card; the stamp form only beside a full stamp row."""
    flat = [b for row in spec["parts"].get("action_rows") or [] for b in row]
    if not any(_slot(b) == "more" for b in flat):
        return [b for b in flat if _slot(b) in _FALLBACK_BUTTONS]
    full = sum(b.get("id") == STAMP_ROW_ACTION for b in flat) == 5
    return [b for b in flat if _slot(b) in CARD_BUTTONS and (full or b["id"] != "stamp")]


def secondary(spec):
    """The card's buttons offered in the 1:1 「操作を選ぶ…」 menu."""
    shown = _on_card(spec)
    return [b for row in spec["parts"].get("action_rows") or [] for b in row
            if not any(b is c for c in shown)]


def heading(spec):
    """First line of the card heading (the patient line), or ""."""
    for item in (spec.get("parts") or {}).get("containers") or []:
        if item.get("type") == "heading":
            return str(item.get("text") or "").split("\n", 1)[0]
    return ""


def action_buttons(spec):
    """The card's own buttons: 操作を選ぶ…, the stamp row, 別の投稿, MCSで開く."""
    actions = []
    order = CARD_BUTTONS if any(_slot(b) == "more" for row in spec["parts"].get("action_rows") or []
                                for b in row) else _FALLBACK_BUTTONS
    for button in sorted(_on_card(spec), key=lambda b: order.index(_slot(b))):
        stamp = button.get("id") == STAMP_ROW_ACTION
        # emoji only and the own stamp marked ✓, as on Slack and Discord
        label = (stamp_icon(button["label"]) + (" ✓" if button.get("style") == "primary" else "")
                 if stamp else _LABELS.get(_slot(button), button["label"]))
        if len(label) > 20:
            raise ValueError("lineworks_button_label")
        if button.get("ui") == "link":
            actions.append({"type": "uri", "label": label, "uri": button["url"]})
        else:
            actions.append({"type": "message", "label": label,
                            "postback": "mcs:a:" + button["token"],
                            **({"_stamp": True} if stamp else {})})
    return actions


def render(spec):
    validate(spec)
    # The runner seals the remaining display into durable display#k parts.
    return buttons(card_split(display_text(spec["parts"]))[0],
                   action_buttons(spec)[:10], preview=notification_preview(spec["parts"], 400))
