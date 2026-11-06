"""Render the shared card display using official LINE WORKS message limits."""
from __future__ import annotations

import hashlib
import re

from adapters.common.spec import PRIMARY_ACTIONS, STAMP_ROW_ACTION, validate as validate_display
from adapters.common.card_controls import (MENU_LABEL, STAMP_PICKER_LABEL,
                                           plan_controls, stamp_label)
from adapters.common.text import notification_preview
from notify_render import display_text
from present_lineworks import card_split, face_block_count
from adapters.common.provider_assets import PROVIDER_ASSETS
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
    first_stamp = next((a for a in actions if a.get("_stamp")), None)
    footer = []
    for a in actions:
        if not a.get("_stamp"):
            footer.append(button(a))
        elif a is first_stamp:
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


_LABELS = {"more": MENU_LABEL, "stamp": STAMP_PICKER_LABEL}


def _on_card(controls):
    """Buttons drawn on the card; the stamp form only beside a full stamp row."""
    more = [b for b in controls.operations if b.get("id") == "more"]
    if more:
        picker = controls.stamp_picker
        return [*more, *controls.stamps, *([picker] if picker else []), *controls.links]
    # Legacy cards without a menu must keep their primary operations reachable.
    primary = sorted((b for b in controls.operations if b.get("id") in PRIMARY_ACTIONS),
                     key=lambda b: PRIMARY_ACTIONS.index(b["id"]))
    return [*primary, *controls.links, *controls.stamps]


def secondary(spec):
    """The card's buttons offered in the 1:1 「操作を選ぶ…」 menu."""
    controls = plan_controls(spec["parts"])
    if any(b.get("id") == "more" for b in controls.operations):
        return [b for b in controls.menu if b.get("id") != "more"]
    return [b for b in controls.operations if b.get("id") not in PRIMARY_ACTIONS]


def heading(spec):
    """First line of the card heading (the patient line), or ""."""
    for item in (spec.get("parts") or {}).get("containers") or []:
        if item.get("type") == "heading":
            return str(item.get("text") or "").split("\n", 1)[0]
    return ""


def action_buttons(spec):
    """The card's own buttons: 操作を選ぶ…, the stamp row, 別の投稿, MCSで開く."""
    actions = []
    for button in _on_card(plan_controls(spec["parts"])):
        stamp = button.get("ui") != "link" and button.get("id") == STAMP_ROW_ACTION
        # emoji only and the own stamp marked ✓, as on Slack and Discord
        label = (button["label"] if button.get("ui") == "link" else
                 stamp_label(button) if stamp else _LABELS.get(button.get("id"), button["label"]))
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
    parts = spec["parts"]
    result = buttons(card_split(display_text(parts))[0], action_buttons(spec)[:10],
                     preview=notification_preview(parts, 400))
    if parts.get("chat_format_version") == 1:
        posts = (parts.get("lineworks") or {}).get("face_posts") or []
        if posts and face_block_count(parts, posts) > 1000:
            raise ValueError("lineworks_face_budget")
        nodes = _face_nodes(parts, posts)
        if result["type"] == "text":
            # New structured faces need no invented action/footer.
            alt = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ",
                                   notification_preview(parts, 400)).split()) or "MCS"
            result = {"type": "flex", "altText": alt[:400], "contents": {
                "type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": nodes}}}
        else:
            result["contents"]["body"]["contents"] = nodes
        import json
        if len(json.dumps(result["contents"], ensure_ascii=False).encode("utf-8")) > 15000:
            raise ValueError("lineworks_flex_budget")
    return result


def _text_node(value):
    return {"type": "text", "text": value or "MCS", "wrap": True}


def _provider_box(item):
    """Known public logo icons and shared state marks; no clinical inference."""
    import processing_route
    states = item["provider_states"]
    visible = item.get("visible_providers") or list(processing_route.PROVIDERS)
    sequence = item.get("provider_sequence") or []
    ordered = sequence + [key for key in visible if key not in sequence]
    assets = {key: url for key, _name, url in PROVIDER_ASSETS}
    children = []
    for key in ordered:
        if states[key] not in processing_route.COMPACT_MARKS:
            continue
        if key in assets:
            children.append({"type": "icon", "url": assets[key], "size": "sm"})
            label = processing_route.COMPACT_MARKS[states[key]]
        else:
            label = "💻 " + processing_route.COMPACT_MARKS[states[key]]
        children.append(_text_node(label))
    return {"type": "box", "layout": "baseline", "spacing": "sm", "contents": children} if children else None


def _face_nodes(parts, posts):
    """Keep the sealed display prefix and insert only budgeted originals."""
    import notify_render
    import processing_route
    text = card_split(display_text(parts))[0]
    # Splitting the exact sealed prefix keeps the overflow boundary unchanged.
    # Header logos replace only the provider-name suffix; the status stays text.
    headers = {processing_route.compact_header(item): item
               for item in parts["containers"]
               if item.get("provider_scope") == "header"
               and parts.get("processing_display_version") in (1, 2, 3)}
    if not posts:
        out, plain = [], []
        def flush():
            if plain:
                value = "\n".join(plain)
                if value:
                    out.append(_text_node(value))
                plain.clear()
        for line in text.split("\n"):
            item = headers.get(line)
            if item is None:
                plain.append(line)
                continue
            flush()
            fallback = processing_route.compact_header(item)
            box = _provider_box(item)
            prefix = fallback.rsplit(" · ", 1)[0] if box and " · " in fallback else fallback
            if prefix:
                out.append(_text_node(prefix))
            if box:
                out.append(box)
        flush()
        return out or [_text_node("MCS")]
    by_at = {post["at"]: post for post in posts}
    out = []
    pending = None
    for at, item in enumerate(parts["containers"]):
        if item.get("rule") and pending is not None:
            out.extend(_original_nodes(pending))
            pending = None
        if item["type"] == "meta":
            continue
        value = notify_render.render_text({**parts, "containers": [item], "footer": []}, head="{}")
        if item.get("rule") and out:
            out.append(_text_node(notify_render.SECTION_RULE))
        if item.get("provider_scope") == "header" and parts.get("processing_display_version") in (1, 2, 3):
            box = _provider_box(item)
            prefix = value.rsplit(" · ", 1)[0] if box and " · " in value else value
            if prefix:
                out.append(_text_node(prefix))
            if box:
                out.append(box)
        elif value:
            out.append(_text_node(value))
        if at in by_at:
            pending = by_at[at]
    if pending is not None:
        out.extend(_original_nodes(pending))
    for item in parts.get("footer") or []:
        if item["type"] == "text" and item["text"]:
            out.append(_text_node(notify_render.MENTION_TOKEN_PATTERN.sub("メンバー", item["text"])))
    return out or [_text_node("MCS")]


def _original_nodes(post):
    import notify_render
    return [_text_node(notify_render.MENTION_TOKEN_PATTERN.sub("メンバー", value))
            for value in [*post["stamps"], "📄 原文", post["original"]] if value]
