"""Render the shared card display using official LINE WORKS message limits."""
from __future__ import annotations

import hashlib

from hermes_plugin.mcs_delivery.spec import validate as validate_display
from notify_render import display_text
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
    chunks = spec["parts"].get("thread_body_parts") or []
    if any(len(chunk) > 1900 for chunk in chunks):
        raise ValueError("lineworks_text_budget")
    count = sum(len(row) for row in spec["parts"].get("action_rows") or [])
    required = {f"actions#{i}" for i in range(1, (count + 9) // 10)}
    groups = {part.get("name") for part in spec["parts"].get("manifest") or []
              if part.get("kind") == "body_part"}
    if not required <= groups:
        raise ValueError("lineworks_action_overflow_missing")
    if len(display_text(spec["parts"])) > 1000 and "display#1" not in groups:
        raise ValueError("lineworks_display_overflow_missing")
    expected = _split_body_chunks(display_text(spec["parts"])[1000:])
    entries = spec["parts"].get("manifest") or []
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


def action_buttons(spec):
    actions = []
    for row in spec["parts"].get("action_rows") or []:
        for button in row:
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
    # The runner seals the remaining display and action groups into durable parts.
    return buttons(display_text(spec["parts"])[:1000], action_buttons(spec)[:10])
