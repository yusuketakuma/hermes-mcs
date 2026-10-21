"""Every new Discord card is a legacy embed (V2 cannot carry the push
preview), so the per-post 💊 must be a button there, not only a V2
Section accessory."""
import sys

import pytest

from adapters.discord import cards
from discord_delivery_testkit import _spec
from discord_testkit import _fake_discord


@pytest.fixture(autouse=True)
def _stub_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())


def _card(stamps):
    spec = _spec([])
    containers, actions = [], []
    for index, stamp in enumerate(stamps):
        containers.append({"type": "text", "rule": True,
                           "text": f"{stamp} 合成職員（合成訪問看護）"})
        containers.append({"type": "text", "text": "・薬剤: 合成薬A 5mg"})
        actions.append({"at": len(containers) - 2, "button": {
            "id": "meds", "ui": "button", "style": "secondary",
            "label": "💊 薬剤", "token": f"{index:032x}"}})
    spec["parts"]["containers"] = containers
    spec["parts"]["discord"] = {"post_actions": actions}
    return spec


def _post_buttons(payload):
    return [item for item in payload["view"].items
            if getattr(item, "custom_id", "").startswith("mcs:a:")]


def test_embed_turns_each_post_action_into_a_button_of_its_own_row():
    payload = cards.message_payload(_card(["09-24 08:40", "09-25 09:10"]))
    buttons = _post_buttons(payload)
    assert [b.label for b in buttons] == ["💊 09-24 08:40", "💊 09-25 09:10"]
    assert [b.custom_id for b in buttons] == ["mcs:a:" + "0" * 32, "mcs:a:" + "0" * 31 + "1"]
    assert all(b.row == 3 for b in buttons)
    assert "09-24 08:40 合成職員" in payload["embed"].description   # the line stays text


def test_single_post_keeps_the_plain_label():
    assert [b.label for b in _post_buttons(cards.message_payload(_card(["09-24 08:40"])))] == ["💊 薬剤"]


def test_post_buttons_never_exceed_one_row():
    payload = cards.message_payload(_card([f"09-{day:02d} 08:00" for day in range(1, 8)]))
    assert len(_post_buttons(payload)) == cards._POST_BUTTONS_MAX


@pytest.mark.parametrize("components_v2", [False, True])
def test_full_link_row_keeps_stamp_form_in_menu_without_overflow(components_v2):
    from adapters.common.spec import validate
    spec = _spec([])
    spec["parts"].pop("manifest")  # Supported legacy pre-plan card.
    stamps = [{"id": "stamp_press", "ui": "button", "label": label, "token": f"{i:032x}"}
              for i, label in enumerate(("👀", "🙆", "🙏", "👍", "✅"), 1)]
    links = [{"id": "link", "ui": "link", "label": f"合成リンク{i}",
              "url": f"https://example.test/{i}"} for i in range(5)]
    picker = {"id": "stamp", "ui": "button", "label": "MCSスタンプ", "token": "a" * 32}
    spec["parts"]["action_rows"] = [stamps, links, [picker]]
    validate(spec)
    view = cards.message_payload(spec, components_v2=components_v2)["view"]
    if components_v2:
        rows = [item.children for item in view.items[0].children if hasattr(item, "children")]
    else:
        rows = [[item for item in view.items if item.row == row] for row in range(5)]
    assert all(len(row) <= 5 for row in rows)
    items = [item for row in rows for item in row]
    menu = [option.value for item in items for option in getattr(item, "options", [])]
    buttons = [item.custom_id.removeprefix("mcs:a:") for item in items
               if str(getattr(item, "custom_id", "")).startswith("mcs:a:")]
    assert menu == [picker["token"]]
    assert buttons == [button["token"] for button in stamps]
    assert [item.url for item in items if getattr(item, "url", None)] == [b["url"] for b in links]
