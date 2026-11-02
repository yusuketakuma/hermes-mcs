"""Synthetic part boundaries retain content and post identity on every transport."""
import copy

import notify_render
import present_slack
from adapters.slack.presentation import render_parts
from adapters.discord.cards import _zones


def test_post_groups_are_separated_without_fragmenting_medical_content():
    parts = {"containers": [
        {"type": "heading", "text": "Synthetic heading"},
        {"type": "meta", "text": "hidden"},
        {"type": "text", "text": "Synthetic context"},
        {"type": "text", "text": "Synthetic post", "rule": True},
        {"type": "text", "text": "Synthetic summary"},
    ], "footer": []}
    before = copy.deepcopy(parts)
    blocks = render_parts(parts, card_kind="thread")
    assert [b["type"] for b in blocks] == [
        "header", "section", "divider", "context", "section"]
    assert present_slack.face_block_count(parts) == len(blocks)
    zones, footer = _zones({"kind": "thread", "parts": parts}, str)
    assert len(zones) == 2 and footer == []
    for dialect in ("slack", "discord", "lineworks"):
        text = notify_render.parts_text(parts, dialect)
        assert text.count(notify_render.SECTION_RULE) == 1
        assert "hidden" not in text and "Synthetic summary" in text
    assert parts == before
