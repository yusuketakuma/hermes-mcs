"""Synthetic structured LINE WORKS faces preserve source text and closed assets."""
import copy
import json

import pytest

from adapters.common.provider_assets import PROVIDER_ASSETS
from adapters.common.text import notification_preview
from adapters.lineworks import cards
from adapters.lineworks.client import ClientError, _validate_content
import present_lineworks
import processing_route


def _parts():
    states = dict.fromkeys(processing_route.PROVIDERS, "done")
    header = {"type": "text", "provider_scope": "header", "provider_states": states,
              "visible_providers": list(processing_route.PROVIDERS),
              "text": "完了 · " + processing_route.provider_text(states, list(processing_route.PROVIDERS))}
    return {"containers": [{"type": "heading", "text": "合成患者"}, header,
                           {"type": "text", "rule": True, "text": "合成投稿A"},
                           {"type": "text", "text": "📋 合成要約A"},
                           {"type": "text", "rule": True, "text": "合成投稿B"},
                           {"type": "text", "text": "📋 合成要約B"}],
            "footer": [], "processing_display_version": 1}


def _wire(nodes):
    return {"type": "flex", "altText": "合成", "contents": {
        "type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": nodes}}}


def test_face_keeps_summary_original_per_post_and_catalog_icons():
    parts = _parts()
    posts = [{"at": 2, "stamps": ["スタンプ 合成"], "original": "合成原文A"},
             {"at": 4, "stamps": [], "original": "合成原文B"}]
    nodes = cards._face_nodes(parts, posts)
    _validate_content(_wire(nodes))
    labels = [node["text"] for node in nodes if node["type"] == "text"]
    assert labels.index("📋 合成要約A") < labels.index("合成原文A") < labels.index("合成投稿B")
    assert labels.index("📋 合成要約B") < labels.index("合成原文B")
    row = next(node for node in nodes if node["type"] == "box")
    assert row["layout"] == "baseline"
    assert {child["url"] for child in row["contents"] if child["type"] == "icon"} == {
        url for _key, _name, url in PROVIDER_ASSETS}
    assert any(child.get("text", "").startswith("💻") for child in row["contents"])


def test_face_budget_includes_original_and_stamps():
    parts = _parts()
    post = {"at": 2, "stamps": ["合成" * 100], "original": "原文" * 500}
    assert present_lineworks.face_block_count(parts, [post]) > present_lineworks.BLOCK_MAX
    assert present_lineworks.face_block_count(parts, []) <= present_lineworks.BLOCK_MAX


@pytest.mark.parametrize("mutation", ["external_url", "url_list", "icon_top", "nested_box", "overlong", "json_overflow"])
def test_closed_flex_rejects_unknown_assets_nesting_and_budgets(mutation):
    row = {"type": "box", "layout": "baseline", "spacing": "sm", "contents": [
        {"type": "icon", "url": PROVIDER_ASSETS[0][2], "size": "sm"},
        {"type": "text", "text": "✓", "wrap": True}]}
    content = _wire([copy.deepcopy(row)])
    nodes = content["contents"]["body"]["contents"]
    if mutation in ("external_url", "url_list"):
        nodes[0]["contents"][0]["url"] = "https://example.invalid/secret.png" if mutation == "external_url" else []
    elif mutation == "icon_top":
        nodes[:] = [row["contents"][0]]
    elif mutation == "nested_box":
        nodes[0]["contents"] = [row]
    elif mutation == "overlong":
        nodes[:] = [{"type": "text", "text": "x" * 1001, "wrap": True}]
    else:
        nodes[:] = [{"type": "text", "text": "x", "wrap": True}] * 500
        assert len(json.dumps(content["contents"]).encode()) > 15000
    with pytest.raises(ClientError, match="validation_invalid"):
        _validate_content(content)


def test_no_original_overflow_retains_exact_sealed_prefix():
    parts = {"containers": [{"type": "heading", "text": "合成"},
                            {"type": "text", "text": "長文" * 800}], "footer": []}
    nodes = cards._face_nodes(parts, [])
    from notify_render import display_text
    assert "\n".join(node["text"] for node in nodes) == present_lineworks.card_split(display_text(parts))[0]


def test_legacy_render_keeps_single_text_and_existing_controls():
    from lineworks_testkit import overflow_spec
    from notify_render import display_text
    spec = overflow_spec()
    result = cards.render(spec)
    assert result == cards.buttons(present_lineworks.card_split(display_text(spec["parts"]))[0],
                                   cards.action_buttons(spec)[:10],
                                   preview=notification_preview(spec["parts"], 400))
    assert "CHAT_BODY_ON_FACE" in vars(present_lineworks)
    assert not getattr(present_lineworks, "BODY_ON_FACE", False)
