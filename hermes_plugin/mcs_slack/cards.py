"""Convert validated MCS display parts to Slack Block Kit."""
from __future__ import annotations

import re

from ..mcs_delivery.spec import validate as validate_v1

_SECTION_MAX = 3000
_HEADER_MAX = 150
_CONTEXT_MAX = 2000
_BLOCK_MAX = 50
_FALLBACK = "MCS 確認カード"
SCHEMA = "mcs-card-render/v2"
LINK_ACTION = "mcs:link"          # URL buttons still post an action — acked
_MENTION = re.compile(r"<@[UW][A-Z0-9]{1,30}>")


def _mrkdwn(text):
    """Footer text as mrkdwn: runner-made <@U…> mentions render as the
    member's name; everything else is escaped so no other text can form
    a link, mention or broadcast."""
    def esc(t):
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out, last = [], 0
    for m in _MENTION.finditer(text):
        out += [esc(text[last:m.start()]), m.group(0)]
        last = m.end()
    return "".join(out) + esc(text[last:])


def validate(spec):
    """Require Slack identity while reusing the neutral v1 display budget."""
    if not isinstance(spec, dict) or spec.get("schema") != SCHEMA:
        raise ValueError("bad_slack_schema")
    delivery = spec.get("delivery")
    if not isinstance(delivery, dict) \
            or delivery.get("transport") != "slack" \
            or not isinstance(delivery.get("team_id"), str) \
            or not 0 < len(delivery["team_id"]) <= 64 \
            or "\x00" in delivery["team_id"] \
            or delivery.get("guild_id") is not None:
        raise ValueError("bad_slack_scope")
    validate_v1({**spec, "schema": "mcs-card-render/v1"})
    return spec


def _sections(text):
    return [
        {"type": "section",
         "text": {"type": "plain_text", "text": text[i:i + _SECTION_MAX]}}
        for i in range(0, len(text), _SECTION_MAX)
    ]


def render(spec):
    """Render visible containers/footer without serializing private context."""
    validate(spec)
    blocks = []
    for item in spec["parts"]["containers"]:
        kind = item["type"]
        if kind == "meta":
            continue
        if kind == "heading" and len(item["text"]) <= _HEADER_MAX:
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": item["text"]},
            })
            continue
        text = (f"{item['name']}: {item['value']}"
                if kind == "field" else item["text"])
        if kind == "quote":
            text = f"引用: {text}"
        blocks.extend(_sections(text))

    names = spec["parts"].get("mentions") == "silent"
    for item in spec["parts"].get("footer") or []:
        if item["type"] != "text":
            continue
        text = item["text"]
        if names:
            text = _mrkdwn(text)
        blocks.extend(
            {"type": "context",
             "elements": [{"type": "mrkdwn" if names else "plain_text",
                           "text": text[i:i + _CONTEXT_MAX]}]}
            for i in range(0, len(text), _CONTEXT_MAX)
        )

    for row in spec["parts"].get("action_rows") or []:
        elements = []
        for button in row:
            if len(button["label"]) > 75:
                raise ValueError("slack_button_label")
            if button.get("ui") == "link":
                elements.append({
                    "type": "button", "action_id": LINK_ACTION,
                    "text": {"type": "plain_text", "text": button["label"]},
                    "url": button["url"]})
                continue
            entry = {
                "type": "button",
                "text": {"type": "plain_text", "text": button["label"]},
                "action_id": "mcs:a:" + button["token"],
                "value": button["token"],
            }
            if button.get("style") in ("primary", "success"):
                entry["style"] = "primary"
            elif button.get("style") == "danger":
                entry["style"] = "danger"
            elements.append(entry)
        blocks.append({"type": "actions", "elements": elements})
    if len(blocks) > _BLOCK_MAX:
        raise ValueError("slack_block_budget")
    return _FALLBACK, blocks
