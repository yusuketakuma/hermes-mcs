"""Section dividers remain visible when Discord compresses a card."""
import copy
import sys

import pytest

from adapters.common.spec import MAX_COMPONENTS, MAX_TOTAL_TEXT
from adapters.discord import cards
from discord_delivery_testkit import _spec
from discord_testkit import _fake_discord


@pytest.fixture(autouse=True)
def discord(monkeypatch):
    fake = _fake_discord()
    monkeypatch.setitem(sys.modules, "discord", fake)
    return fake


def _card(count, *, version=True, summary_size=12):
    spec = _spec([])
    spec["parts"].pop("manifest")
    spec["parts"]["containers"] = [
        item for i in range(count) for item in (
            {"type": "text", "rule": True, "text": f"合成職員{i}"},
            {"type": "text", "text": f"合成要約{i}" + "あ" * summary_size})]
    spec["parts"]["footer"] = [{"type": "text", "text": "合成の状態"}]
    if version:
        spec["parts"]["section_divider_version"] = 1
    return spec


def _children(view):
    (container,) = view.items
    return container.children


def _texts(children):
    return [item.content for child in children
            for item in (child, *getattr(child, "children", ()))
            if hasattr(item, "content")]


def test_short_card_keeps_native_dividers(discord):
    spec = _card(2)
    before = copy.deepcopy(spec)
    children = _children(cards.build_view(spec))
    assert sum(isinstance(item, discord.ui.Separator) for item in children) == 2
    assert cards._ZONE_RULE not in "\n".join(_texts(children))
    assert spec == before


def test_compressed_card_keeps_all_post_boundaries(discord):
    spec = _card(8)
    children = _children(cards.build_view(spec))
    text = "\n".join(_texts(children))
    assert text.count(cards._ZONE_RULE) == 7
    assert all(f"合成要約{i}" in text and f"合成職員{i}" in text for i in range(8))
    assert len(children) <= cards._CONTAINER_MAX
    assert 1 + sum(1 + len(getattr(child, "children", ())) for child in children) <= MAX_COMPONENTS
    assert sum(map(len, _texts(children))) <= MAX_TOTAL_TEXT


def test_near_limit_preserves_medical_text_and_stamp_rows(discord):
    spec = _card(8, summary_size=455)
    spec["parts"]["discord"] = {"face_posts": [
        {"at": 2 * i, "stamps": [f"合成スタンプ{i}"]} for i in range(8)]}
    children = _children(cards.build_view(spec))
    text = "\n".join(_texts(children))
    assert text.count(cards._ZONE_RULE) == 7
    assert all(f"合成要約{i}" in text and f"合成スタンプ{i}" in text for i in range(8))
    assert sum(map(len, _texts(children))) <= MAX_TOTAL_TEXT
    assert len(children) <= cards._CONTAINER_MAX


def test_compressed_post_actions_remain_reachable_via_menu(discord):
    spec = _card(8)
    def button(index):
        return {"id": "meds", "ui": "button", "style": "secondary",
                "label": "薬剤", "token": f"{index + 1:032x}"}
    spec["parts"]["discord"] = {"post_actions": [
        {"at": 2 * i, "button": button(i)} for i in range(5)]}
    spec["parts"]["action_rows"] = [[button(5)]]
    before = copy.deepcopy(spec)
    children = _children(cards.build_view(spec))
    text = "\n".join(_texts(children))
    assert text.count(cards._ZONE_RULE) == 7
    assert all(f"合成要約{i}" in text for i in range(8))
    selects = [control for child in children for control in getattr(child, "children", ())
               if isinstance(control, discord.ui.Select)]
    assert len(selects) == 1 and any(option.label == "薬剤" for option in selects[0].options)
    assert len(children) <= cards._CONTAINER_MAX
    assert sum(map(len, _texts(children))) <= MAX_TOTAL_TEXT
    assert spec == before


def test_legacy_sealed_spec_keeps_old_compressed_and_embed_output(discord):
    spec = _card(8, version=False)
    before = copy.deepcopy(spec)
    text = "\n".join(_texts(_children(cards.build_view(spec))))
    embed = cards.message_payload(spec)["embed"].description
    assert cards._ZONE_RULE not in text and cards._ZONE_RULE not in embed
    assert all(f"合成要約{i}" in text and f"合成要約{i}" in embed for i in range(8))
    assert spec == before


def test_new_embed_has_dividers_between_post_and_status(discord):
    embed = cards.message_payload(_card(3))["embed"].description
    assert embed.count(cards._ZONE_RULE) == 3
    assert embed.index("合成要約0") < embed.index(cards._ZONE_RULE) < embed.index("合成職員1")
    assert embed.rindex(cards._ZONE_RULE) < embed.index("合成の状態")


def test_oversized_unrepresentable_card_is_not_silently_truncated(discord):
    spec = _card(8, summary_size=550)
    before = copy.deepcopy(spec)
    with pytest.raises(ValueError, match="discord_section_budget"):
        cards.build_view(spec)
    with pytest.raises(ValueError, match="discord_embed_budget"):
        cards.message_payload(spec)
    assert spec == before
