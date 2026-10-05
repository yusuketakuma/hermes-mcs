"""Convert validated MCS display parts to Slack Block Kit."""
from __future__ import annotations

import re
from html import escape
from urllib.parse import urlsplit

from adapters.common.spec import PRIMARY_ACTIONS
from adapters.common.spec import validate as validate_v1

_SECTION_MAX = 3000
_HEADER_MAX = 150
_CONTEXT_MAX = 2000
_BLOCK_MAX = 50
_FALLBACK = "MCS 確認カード"
SCHEMA = "mcs-card-render/v2"
LINK_ACTION = "mcs:link"          # URL buttons still post an action — acked
MENU_ACTION = "mcs:menu"          # the compact 操作 select — value = token
_DIVIDER = {"type": "divider"}
_MENU_PLACEHOLDER = "操作を選ぶ…"
_MENTION = re.compile(r"<@[UW][A-Z0-9]{1,30}>")


MEMBER_LABEL = "メンバー"


def mention_ids(spec) -> set:
    """Slack user ids the runner named as <@U…> in the footer."""
    return {m.group(0)[2:-1]
            for item in (spec.get("parts") or {}).get("footer") or []
            if isinstance(item, dict) and isinstance(item.get("text"), str)
            for m in _MENTION.finditer(item["text"])}


def _names(text, names):
    """Slack has no allowed_mentions: a live <@U…> in a re-posted card
    notifies the member. Runner mentions become the member's display
    name (``names``, looked up by the worker) or a neutral label, and
    the footer is sent as plain_text so nothing can form a mention."""
    return _MENTION.sub(
        lambda m: (names or {}).get(m.group(0)[2:-1]) or MEMBER_LABEL, text)


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
    # expand: body text is never folded behind "see more"
    return [
        {"type": "section", "expand": True,
         "text": {"type": "plain_text", "text": text[i:i + _SECTION_MAX]}}
        for i in range(0, len(text), _SECTION_MAX)
    ]


def render_parts(parts, names=None, silent=False) -> list:
    """Visible containers/footer of the shared display model as Block
    Kit — used for cards and the clicker-only 📊 answer."""
    blocks = []
    for item in parts["containers"]:
        kind = item["type"]
        if kind == "meta":
            continue
        if item.get("rule") and blocks:
            blocks.append(dict(_DIVIDER))
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

    footer = [i for i in parts.get("footer") or [] if i["type"] == "text"]
    if footer and blocks:
        blocks.append(dict(_DIVIDER))
    for item in footer:
        text = _names(item["text"], names) if silent else item["text"]
        blocks.extend(
            {"type": "context",
             "elements": [{"type": "plain_text",
                           "text": text[i:i + _CONTEXT_MAX]}]}
            for i in range(0, len(text), _CONTEXT_MAX)
        )
    return blocks


def render(spec, names=None):
    """Render visible containers/footer without serializing private context."""
    validate(spec)
    blocks = render_parts(spec["parts"], names,
                          spec["parts"].get("mentions") == "silent")
    quick, menu, links = [], [], []
    for row in spec["parts"].get("action_rows") or []:
        for button in row:
            if len(button["label"]) > 75:
                raise ValueError("slack_button_label")
            if button.get("ui") == "link":
                links.append(button)
            elif button.get("id") in PRIMARY_ACTIONS:
                # buttons; every other action goes into one select so the
                # card stays one compact row on mobile
                quick.append(button)
            else:
                menu.append(button)
    elements = []
    for button in quick:
        entry = {
            "type": "button",
            "text": {"type": "plain_text", "text": button["label"]},
            "action_id": "mcs:a:" + button["token"],
            "value": button["token"],
        }
        if button.get("style") in ("primary", "success"):
            entry["style"] = "primary"
        elements.append(entry)
    if menu:
        elements.append({
            "type": "static_select", "action_id": MENU_ACTION,
            "placeholder": {"type": "plain_text", "text": _MENU_PLACEHOLDER},
            "options": [{"text": {"type": "plain_text", "text": b["label"]},
                         "value": b["token"]} for b in menu]})
    if elements:
        if blocks:
            blocks.append(dict(_DIVIDER))
        blocks.append({"type": "actions", "elements": elements})
    for button in links:
        # a text link, not a button — it costs no row on mobile
        url = button["url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname \
                or any(c.isspace() or ord(c) < 32 or c in "<>|" for c in url):
            raise ValueError("slack_link_url")
        label = escape(button["label"].replace("|", "｜"), quote=False)
        link = f"<{escape(url, quote=False)}|{label}>"
        if len(link) > _CONTEXT_MAX:
            raise ValueError("slack_link_budget")
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": link}]})
    if len(blocks) > _BLOCK_MAX:
        raise ValueError("slack_block_budget")
    return _FALLBACK, blocks
