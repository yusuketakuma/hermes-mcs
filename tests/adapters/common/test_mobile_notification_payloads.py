"""Push fields and full card/actions survive native payload conversion without services."""
import copy
import json

import pytest

from adapters.common.text import notification_preview
from adapters.slack import cards as slack
from adapters.discord import cards as discord
from adapters.lineworks import cards as lineworks
from adapters.lineworks.client import _validate_content, _silent_content
from discord_testkit import _fake_discord
from slack_card_testkit import _spec

PREVIEW = "合成患者 / 合成看護師（合成所属） / 10-06 08:30: 投稿の自動要約: 依頼候補（未確認）: 朝の薬の変更確認"


def test_slack_preview_has_five_fields_full_blocks_and_unchanged_actions():
    spec = _spec("完全な表示本文 <@SYNTHETIC> & 元の本文")
    spec["parts"]["preview_text"] = PREVIEW + " <@SYNTHETIC> &"
    text, blocks = slack.render(spec)
    assert PREVIEW in text and "&lt;@SYNTHETIC&gt; &amp;" in text
    wire = json.dumps(blocks, ensure_ascii=False)
    assert "完全な表示本文" in wire and "c" * 32 in wire and "b" * 32 in wire
    assert "非公開の合成本体" not in text + wire


def test_discord_classic_preview_preserves_complete_face_buttons_and_v2_compatibility(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    spec = _spec("完全な表示本文")
    spec["parts"]["preview_text"] = PREVIEW
    payload = discord.message_payload(spec)
    assert payload["content"] == PREVIEW
    assert "完全な表示本文" in payload["embed"].description
    assert "合成フッター" in payload["embed"].description
    assert "非公開の合成本体" not in payload["content"] + payload["embed"].description
    items = payload["view"].items
    assert any(getattr(item, "custom_id", "") == "mcs:a:" + "c" * 32 for item in items)
    assert discord.message_payload(spec, components_v2=True).keys() == {"view"}


def test_lineworks_flex_alttext_preserves_full_face_tokens_and_uri():
    spec = _spec("完全な表示本文")
    spec["schema"] = lineworks.SCHEMA
    spec["delivery"].update(transport="lineworks", team_id="SYNTHETIC")
    spec["delivery"].pop("guild_id")
    spec["parts"]["preview_text"] = PREVIEW
    payload = lineworks.render(spec)
    _validate_content(payload)
    assert payload["type"] == "flex" and payload["altText"] == PREVIEW
    assert "完全な表示本文" in payload["contents"]["body"]["contents"][0]["text"]
    assert payload["contents"]["body"]["contents"][0]["wrap"] is True
    actions = [item["action"] for item in payload["contents"]["footer"]["contents"]]
    assert any(action.get("postback") == "mcs:a:" + "c" * 32 for action in actions)
    long = lineworks.buttons("完全本文", [{"type": "uri", "label": "原本", "uri": "https://example.test/source"}], preview=PREVIEW * 10)
    _validate_content(long)
    assert len(long["altText"]) == 400 and long["altText"].endswith("…")
    assert long["contents"]["footer"]["contents"][0]["action"]["uri"] == "https://example.test/source"
    mentions = copy.deepcopy(payload)
    mentions["altText"] += " <m userId='SYNTHETIC'>合成</m>"
    mentions["contents"]["body"]["contents"][0]["text"] += " <m userId='SYNTHETIC'>合成</m>"
    assert "<m " not in json.dumps(_silent_content(mentions), ensure_ascii=False)


@pytest.mark.parametrize("transport", ["slack", "discord", "lineworks"])
def test_older_specs_fall_back_to_public_content_only(transport):
    parts = _spec("既存の公開要約")["parts"]
    assert "既存の公開要約" in notification_preview(parts)
    assert "非公開の合成本体" not in notification_preview(parts)
