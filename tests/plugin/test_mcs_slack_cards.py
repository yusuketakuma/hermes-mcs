"""Synthetic Slack card rendering against the MCS neutral render contract."""

import json

import pytest

from hermes_plugin.mcs_slack.cards import render
from slack_card_testkit import _spec


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
    with pytest.raises(ValueError, match="bad_slack_scope"):
        render(spec)


def test_slack_spec_with_unknown_feature_is_rejected_not_rendered():
    """An outdated Slack worker holds a spec carrying a feature key it
    does not know instead of rendering the card without it."""
    spec = _spec()
    spec["parts"]["future_poll"] = {"q": "?"}
    with pytest.raises(ValueError, match="unsupported_parts_key"):
        render(spec)
