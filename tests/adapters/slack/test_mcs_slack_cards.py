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
    # compact mobile layout: 確認/担当 stay buttons, every other action
    # is an option of one select — each still carries its durable token
    button, menu = actions[0]["elements"]
    assert (button["action_id"], button["value"]) == ("mcs:a:" + "c" * 32,
                                                      "c" * 32)
    assert menu["type"] == "static_select"
    assert menu["action_id"] == "mcs:menu"
    assert [o["value"] for o in menu["options"]] == ["b" * 32]
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


def test_select_click_resolves_the_same_token_as_a_button():
    """The compact 操作 select carries the durable token as the chosen
    option's value; it passes the same origin checks as a button and a
    forged action_id or an outsider is rejected."""
    from hermes_plugin.mcs_slack import actions as slack_actions
    body = {"team": {"id": "T"}, "api_app_id": "A",
            "channel": {"id": "C"}, "user": {"id": "U1"},
            "message": {"ts": "1790000000.000001"}}
    kw = dict(team_id="T", application_id="A", channel_id="C",
              profile="p", allowed_user_ids={"U1"})
    select = {"type": "static_select", "action_id": "mcs:menu",
              "selected_option": {"value": "d" * 32}}
    got = slack_actions.origin(body, select, **kw)
    assert got and got["token"] == "d" * 32 and got["actor"] == "slack:T:U1"
    assert slack_actions.origin(
        body, {**select, "action_id": "mcs:a:" + "d" * 32}, **kw) is None
    assert slack_actions.origin(
        body, {**select, "selected_option": {"value": "x"}}, **kw) is None
    outsider = {**body, "user": {"id": "U9"}}
    assert slack_actions.origin(outsider, select, **kw) is None


def test_full_card_is_one_compact_row():
    """Every action of a busy card fits one actions block: the two
    toggles as buttons and the rest as options of a single select."""
    spec = _spec()
    ids = ("ack", "assign", "request", "tasks_done", "summary", "report",
           "mytasks", "unacked", "search", "prev", "next")
    buttons = [{"id": i, "ui": "button", "style": "secondary",
                "label": f"L{n}", "token": f"{n:032x}"}
               for n, i in enumerate(ids)]
    spec["parts"]["action_rows"] = [buttons[k:k + 5]
                                    for k in range(0, len(buttons), 5)]
    _, blocks = render(spec)
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    kinds = [e["type"] for e in actions[0]["elements"]]
    assert kinds == ["button", "button", "static_select"]
    assert len(actions[0]["elements"][2]["options"]) == len(ids) - 2


def test_link_label_and_query_escape_slack_markup_without_changing_link():
    from html import unescape
    spec = _spec()
    url = "https://example.invalid/?a=1&b=2"
    label = "開く & <@U0OP>"
    spec["parts"]["action_rows"] = [[{"ui": "link", "id": "link", "url": url,
                                         "label": label}]]
    _, blocks = render(spec)
    wire = blocks[-1]["elements"][0]["text"]
    assert wire.count("<") == wire.count(">") == 1
    wire_url, wire_label = wire[1:-1].split("|")
    assert unescape(wire_url) == url and unescape(wire_label) == label


@pytest.mark.parametrize("url", ["https://", "https://example.invalid/>|<!channel>",
                                  "https://example.invalid/\n<@U0OP>"])
def test_link_url_with_missing_host_or_slack_delimiter_is_rejected(url):
    spec = _spec()
    spec["parts"]["action_rows"] = [[{"ui": "link", "id": "link", "url": url,
                                         "label": "開く"}]]
    with pytest.raises(ValueError, match="slack_link_url"):
        render(spec)


def test_link_url_escape_expansion_obeys_context_budget():
    spec = _spec()
    spec["parts"]["action_rows"] = [[{
        "ui": "link", "id": "link", "label": "開く",
        "url": "https://example.invalid/?" + "&" * 480}]]
    with pytest.raises(ValueError, match="slack_link_budget"):
        render(spec)
