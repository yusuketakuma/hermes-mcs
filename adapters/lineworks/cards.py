"""Render the shared card display using official LINE WORKS message limits."""
from __future__ import annotations

import hashlib

from adapters.common.spec import PRIMARY_ACTIONS, validate as validate_display
from notify_render import display_text, lineworks_card_split
from notify_cards import _split_body_chunks

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
    rest = lineworks_card_split(display_text(spec["parts"]))[1]
    if rest and "display#1" not in groups:
        raise ValueError("lineworks_display_overflow_missing")
    expected = _split_body_chunks(rest)
    actual = {part.get("name"): chunks[int(part["part_id"].rsplit(":", 1)[1]) - 1]
              for part in entries if part.get("kind") == "body_part"
              and str(part.get("name") or "").startswith("display#")}
    if actual != {f"display#{i}": chunk for i, chunk in enumerate(expected, 1)}:
        raise ValueError("lineworks_display_overflow_mismatch")
    return spec


def logical_message_id(spec):
    """A local card binding, never an invented provider message identifier."""
    return "lw:" + hashlib.sha256(spec["card_key"].encode()).hexdigest()[:32]


def buttons(text, actions):
    if not actions:
        return {"type": "text", "text": text or "MCS"}
    if len(text) > 1000 or not 1 <= len(actions) <= 10:
        raise ValueError("lineworks_button_budget")
    return {"type": "button_template", "contentText": text or "MCS",
            "actions": actions}


# Card buttons, in display order; every other action sits behind ``more``.
CARD_BUTTONS = (*PRIMARY_ACTIONS, "link", "more")


def _slot(button):
    return "link" if button.get("ui") == "link" else button["id"]


def secondary(spec):
    """The card's buttons offered in the 1:1 「その他の操作」 menu."""
    return [b for row in spec["parts"].get("action_rows") or [] for b in row
            if _slot(b) not in CARD_BUTTONS]


def heading(spec):
    """First line of the card heading (the patient line), or ""."""
    for item in (spec.get("parts") or {}).get("containers") or []:
        if item.get("type") == "heading":
            return str(item.get("text") or "").split("\n", 1)[0]
    return ""


def action_buttons(spec):
    """The card's own buttons: ack, assign, request, link, more."""
    actions = []
    shown = [b for row in spec["parts"].get("action_rows") or [] for b in row
             if _slot(b) in CARD_BUTTONS]
    for button in sorted(shown, key=lambda b: CARD_BUTTONS.index(_slot(b))):
        if len(button["label"]) > 20:
            raise ValueError("lineworks_button_label")
        if button.get("ui") == "link":
            actions.append({"type": "uri", "label": button["label"],
                            "uri": button["url"]})
        else:
            actions.append({"type": "message", "label": button["label"],
                            "postback": "mcs:a:" + button["token"]})
    return actions


def render(spec):
    validate(spec)
    # The runner seals the remaining display into durable display#k parts.
    return buttons(lineworks_card_split(display_text(spec["parts"]))[0],
                   action_buttons(spec)[:10])
