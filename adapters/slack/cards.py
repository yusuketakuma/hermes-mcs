"""Validate Slack cards and compose their content with native interactive controls."""
from __future__ import annotations

from html import escape

from adapters.common.card_controls import MENU_LABEL, STAMP_PICKER_LABEL, plan_controls
from adapters.common.spec import validate as validate_v1
from adapters.common.text import notification_preview
from .presentation import BLOCK_MAX, CONTEXT_MAX, render_parts, safe_url

SCHEMA = "mcs-card-render/v2"
LINK_ACTION = "mcs:link"
MENU_ACTION = "mcs:menu"
STAMP_PAIR_PREFIX = "mcs:stamp-pair:"


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

def render(spec, names=None):
    """Render visible containers/footer without serializing private context."""
    validate(spec)
    blocks = render_parts(spec["parts"], names,
                          spec["parts"].get("mentions") == "silent",
                          card_kind=spec.get("kind"))
    controls = plan_controls(spec["parts"])
    for row in spec["parts"].get("action_rows") or []:
        for button in row:
            if len(button["label"]) > 75:
                raise ValueError("slack_button_label")
    stamp_form = controls.stamp_picker
    menu = controls.menu
    links, stamps = controls.links, controls.stamps
    elements = []
    if menu:
        elements.append({
            "type": "static_select", "action_id": MENU_ACTION,
            "placeholder": {"type": "plain_text", "text": MENU_LABEL},
            "options": [{"text": {"type": "plain_text", "text": b["label"]},
                         "value": b["token"]} for b in menu]})
    if elements:
        if blocks:
            blocks.append({"type": "divider"})
        blocks.append({"type": "actions", "elements": elements})
    if stamps:
        stamp_buttons = []
        for button in stamps + ((stamp_form,) if stamp_form else ()):
            own = button.get("style") == "primary" and button is not stamp_form
            label = "🗂 " + STAMP_PICKER_LABEL if button is stamp_form else button["label"]
            accessible = "投稿を選んでスタンプを操作" if button is stamp_form else (
                ("取消: " if own else "スタンプ: ") + button["label"])
            if len(accessible) > 75:
                raise ValueError("slack_button_label")
            stamp_buttons.append({"type": "button", "text": {"type": "plain_text", "text": label},
                                  "action_id": "mcs:a:" + button["token"],
                                  "value": button["token"], "accessibility_label": accessible,
                                  **({"style": "primary"} if own else {})})
        # Feedback buttons show fixed thumbs on mobile, regardless of their text.
        blocks.append({"type": "actions",
                       "block_id": f"mcs:stamps:{spec['delivery_id']}:{spec['render_rev']}",
                       "elements": stamp_buttons})
    for button in links:
        # a text link, not a button — it costs no row on mobile
        url = button["url"]
        if not safe_url(url):
            raise ValueError("slack_link_url")
        label = escape(button["label"].replace("|", "｜"), quote=False)
        link = f"<{escape(url, quote=False)}|{label}>"
        if len(link) > CONTEXT_MAX:
            raise ValueError("slack_link_budget")
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": link}]})
    if len(blocks) > BLOCK_MAX:
        raise ValueError("slack_block_budget")
    return escape(notification_preview(spec["parts"]), quote=False), blocks
