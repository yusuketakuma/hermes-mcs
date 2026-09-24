"""Synthetic Slack card rendering against the MCS neutral render contract."""

import json

import pytest

from hermes_plugin.mcs_slack.cards import render


def _spec(text="合成の確認項目", label="本文表示"):
    return {
        "schema": "mcs-card-render/v2",
        "delivery_id": "00000000-0000-4000-8000-000000000001",
        "card_key": "synthetic-thread",
        "kind": "thread",
        "op": "create",
        "render_rev": 1,
        "source_generation": 1,
        "presentation_generation": 1,
        "ui_revision": 1,
        "delivery": {
            "profile": "cco",
            "transport": "slack",
            "application_id": "A_SYNTHETIC",
            "team_id": "T_SYNTHETIC",
            "guild_id": None,
            "channel_id": "C_SYNTHETIC",
            "route_epoch": 1,
            "correlation": "a" * 32,
            "intent_event_ids": [1],
        },
        "parts": {
            "containers": [
                {"type": "heading", "text": "合成カード"},
                {"type": "text", "text": text},
                {"type": "meta", "correlation": "a" * 32},
            ],
            "footer": [{"type": "text", "text": "合成フッター"}],
            "action_rows": [[
                {"id": "body", "ui": "button", "style": "secondary",
                 "label": label, "token": "b" * 32},
                {"id": "ack", "ui": "button", "style": "success",
                 "label": "確認", "token": "c" * 32},
            ]],
            "context": {"body": "非公開の合成本体"},
        },
    }


def test_render_keeps_body_private_and_preserves_buttons():
    # Given a public card spec whose context contains a private body.
    spec = _spec()

    # When the same neutral spec is rendered for Slack.
    text, blocks = render(spec)

    # Then the public fallback and blocks omit the private body, and
    # native actions carry the original durable tokens.
    assert "非公開の合成本体" not in text + json.dumps(blocks, ensure_ascii=False)
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    assert [(e["action_id"], e["value"]) for e in actions[0]["elements"]] == [
        ("mcs:a:" + "b" * 32, "b" * 32),
        ("mcs:a:" + "c" * 32, "c" * 32),
    ]
    assert any(b["type"] == "header" for b in blocks)


def test_render_splits_long_text_without_losing_content():
    # Given a valid MCS text item longer than Slack's section limit.
    content = "合" * 3100

    # When the item is converted to Slack sections.
    _, blocks = render(_spec(content))

    # Then each section fits and their content reconstructs the item.
    sections = [b["text"]["text"] for b in blocks
                if b["type"] == "section"]
    assert "".join(sections) == content
    assert all(len(s) <= 3000 for s in sections)


def test_render_respects_slack_button_label_limit():
    # Discord permits 80 characters but Slack permits only 75.
    # Reject the whole card rather than silently dropping or changing
    # the meaning of its action.
    with pytest.raises(ValueError, match="slack_button_label"):
        render(_spec(label="合" * 80))


def test_render_rejects_invalid_delivery_before_building_blocks():
    # Given a spec that could not pass the existing delivery boundary.
    spec = {**_spec(), "delivery": {}}

    # When rendered, then it fails closed before any network consumer.
    with pytest.raises(ValueError):
        render(spec)
