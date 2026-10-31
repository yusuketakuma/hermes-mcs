"""Slack thread chunks of a header/body-split post carry Block Kit beside the plain text."""
import asyncio
import copy
import json

import pytest
from slack_card_testkit import _spec
from slack_testkit import isolated_slack_ledger as led, _rewrite_world, ROOT_TS

from adapters.common import eventlog
from adapters.slack.presentation import render_parts, thread_part_blocks
from adapters.slack.cards import validate
from adapters.slack import delivery

__all__ = ["led"]
pytestmark = pytest.mark.thread_bodies

HEADER = "↳ 10-17 09:12 合成看護師\n要約: 合成の発熱と食欲低下\nスタンプ 👍2 ✅1"
BODY = "（1/2）合成の原文です。" * 20


def _split_spec(*names):
    spec = _spec()
    spec["parts"]["manifest"] = [{"part_id": f"body:{i + 1:04d}", "kind": "body_part", "name": n}
                                 for i, n in enumerate(names)]
    return spec


def test_header_chunk_reads_post_line_summary_then_stamps():
    blocks = thread_part_blocks(_split_spec("m:100:header#1", "m:100#1"),
                                {"name": "m:100:header#1"}, HEADER)
    assert [b["type"] for b in blocks] == ["context", "section", "context"]
    assert blocks[0]["elements"][0]["text"] == "↳ 10-17 09:12 合成看護師"
    assert blocks[1]["text"]["text"] == "要約: 合成の発熱と食欲低下"
    assert blocks[2]["elements"][0]["text"].startswith("スタンプ ")


def test_provider_context_uses_fixed_images_and_accessible_states():
    states = {"anthropic": "done", "openai": "unknown", "jev": "partial", "local": "skipped"}
    line = "Anthropic 実行済み · OpenAI 不明 · Jev 一部実行・一部省略 · 💻 省略"
    parts = {"containers": [{"type": "text", "rule": True,
                             "text": "合成の投稿\n" + line, "provider_states": states}], "footer": []}
    blocks = render_parts(parts, card_kind="thread")
    assert [b["type"] for b in blocks] == ["context", "context"]
    elements = blocks[1]["elements"]
    assert len(elements) == 7
    assert [e["alt_text"] for e in elements if e["type"] == "image"] == [
        "Anthropic 実行済み", "OpenAI 不明", "Jev 一部実行・一部省略"]
    assert [e["text"] for e in elements if e["type"] == "plain_text"] == ["✓", "?", "✓↷", "💻 ↷ 省略"]
    assert [e["image_url"] for e in elements if e["type"] == "image"] == [
        "https://www.anthropic.com/images/icons/apple-touch-icon.png",
        "https://avatars.githubusercontent.com/u/14957082?v=4",
        "https://framerusercontent.com/images/aNFzSFxM4fjICmnibw7npfZjcQ.png"]
    spec = _split_spec("m:100:header#1", "m:100#1")
    spec["parts"]["provider_states_version"] = 1
    header = "合成の投稿\n" + line + "\n要約: 合成の観察"
    thread = thread_part_blocks(spec, {"name": "m:100:header#1"}, header)
    assert thread[1] == blocks[1]
    assert thread[2]["text"]["text"] == "要約: 合成の観察"


def test_v2_provider_context_is_in_card_header_once_with_metadata_and_accessible_used():
    import present_slack

    aggregate = {"anthropic": "used", "openai": "unknown", "jev": "partial", "local": "done"}
    post_states = {"anthropic": "done", "openai": "unknown", "jev": "partial", "local": "unknown"}
    fallback = "Anthropic 使用あり · OpenAI 不明 · Jev 一部実行・一部省略 · 💻 実行済み"
    face_posts = [{"at": 2, "original": "合成本文一", "stamps": ["スタンプ 👍1"]},
                  {"at": 4, "original": "合成本文二", "stamps": ["スタンプ ✅1"]}]
    parts = {"provider_states_version": 2, "containers": [
        {"type": "heading", "text": "💬 合成カード"},
        {"type": "text", "provider_scope": "header", "provider_states": aggregate,
         "text": "起点 06-01 · 返信2件 · " + fallback},
        {"type": "text", "rule": True, "text": "09:00 合成職員一", "provider_states": post_states},
        {"type": "text", "text": "合成要約一"},
        {"type": "text", "rule": True, "text": "09:01 合成職員二", "provider_states": post_states},
        {"type": "text", "text": "合成要約二"}],
        "footer": [], "action_rows": [], "slack": {"face_posts": face_posts}}
    blocks = render_parts(parts, card_kind="thread")
    assert blocks[0]["type"] == "header"
    context = blocks[1]
    assert context["type"] == "context" and len(context["elements"]) == 8
    assert context["elements"][0] == {"type": "plain_text", "text": "起点 06-01 · 返信2件"}
    assert [item["alt_text"] for item in context["elements"] if item["type"] == "image"] == [
        "Anthropic 使用あり", "OpenAI 不明", "Jev 一部実行・一部省略"]
    assert [item["text"] for item in context["elements"] if item["type"] == "plain_text"] == [
        "起点 06-01 · 返信2件", "✓", "?", "✓↷", "💻 ✓ 実行済み"]
    provider_blocks = [block for block in blocks if block["type"] == "context"
                       and any(element["type"] == "image" for element in block["elements"])]
    assert provider_blocks == [context]
    assert fallback not in json.dumps(blocks, ensure_ascii=False)
    assert any(element.get("text") == "09:00 合成職員一" for block in blocks
               if block["type"] == "context" for element in block["elements"])
    assert any(element.get("text") == "09:01 合成職員二" for block in blocks
               if block["type"] == "context" for element in block["elements"])
    stamp_lines = [element["text"] for block in blocks if block["type"] == "context"
                   for element in block["elements"] if "スタンプ" in element.get("text", "")]
    assert stamp_lines == ["09:00 合成職員一\nスタンプ 👍1", "09:01 合成職員二\nスタンプ ✅1"]
    nested = sum(len(block.get("child_blocks", [])) for block in blocks)
    assert present_slack.face_block_count(parts, face_posts) == len(blocks) + nested


@pytest.mark.parametrize("prefix", ["", "合成の状態", "a" * 4001])
def test_v2_provider_header_count_includes_long_metadata_prefix(prefix):
    import present_slack

    states = {"anthropic": "unknown", "openai": "used", "jev": "skipped", "local": "unknown"}
    fallback = "Anthropic 不明 · OpenAI 使用あり · Jev 省略 · 💻 不明"
    text = (prefix + " · " if prefix else "") + fallback
    parts = {"provider_states_version": 2, "containers": [
        {"type": "text", "provider_scope": "header", "provider_states": states, "text": text}],
        "footer": [], "action_rows": []}
    blocks = render_parts(parts, card_kind="thread")
    assert present_slack.face_block_count(parts) == len(blocks)
    assert all(block["type"] == "context" for block in blocks)
    assert all(len(element.get("text", "")) <= 2000 for block in blocks for element in block["elements"])
    assert len(blocks[-1]["elements"]) == (8 if prefix else 7)
    preceding = "".join(element["text"] for block in blocks[:-1] for element in block["elements"])
    last_prefix = blocks[-1]["elements"][0]["text"] if prefix else ""
    assert preceding + last_prefix == prefix
    assert [item["alt_text"] for item in blocks[-1]["elements"] if item["type"] == "image"][1] == "OpenAI 使用あり"


def test_v2_thread_header_keeps_legacy_per_post_provider_details():
    spec = _split_spec("m:100:header#1", "m:100#1")
    spec["parts"]["provider_states_version"] = 2
    line = "Anthropic 実行済み · OpenAI 不明 · Jev 省略 · 💻 不明"
    blocks = thread_part_blocks(spec, {"name": "m:100:header#1"},
                                "合成投稿\n" + line + "\n要約: 合成\nスタンプ 👍1")
    assert [block["type"] for block in blocks] == ["context", "context", "section", "context"]
    assert [item["alt_text"] for item in blocks[1]["elements"] if item["type"] == "image"] == [
        "Anthropic 実行済み", "OpenAI 不明", "Jev 省略"]
    assert blocks[2]["text"]["text"] == "要約: 合成"
    assert blocks[3]["elements"][0]["text"] == "スタンプ 👍1"


@pytest.mark.parametrize("cloud", [[], ["anthropic"], ["openai"], ["anthropic", "openai"]])
@pytest.mark.parametrize("version", [3, 4])
def test_v3_v4_visibility_filters_face_and_split_header_without_losing_states(cloud, version):
    import present_slack

    visible = cloud + ["jev", "local"]
    states = {"anthropic": "done", "openai": "done", "jev": "partial", "local": "unknown"}
    names = {"anthropic": "Anthropic", "openai": "OpenAI", "jev": "Jev", "local": "💻"}
    labels = {"anthropic": "実行済み", "openai": "実行済み", "jev": "一部実行・一部省略", "local": "不明"}
    fallback = " · ".join(f"{names[key]} {labels[key]}" for key in visible)
    prefix = "合成情報 · 解析中" if version == 4 else "合成情報"
    parts = {"provider_states_version": version, "containers": [
        {"type": "heading", "text": "合成カード"},
        {"type": "text", "provider_scope": "header", "provider_states": states.copy(),
         "visible_providers": visible, "text": prefix + " · " + fallback},
        {"type": "text", "rule": True, "text": "合成投稿", "provider_states": states.copy()},
        {"type": "text", "text": "合成要約"}], "footer": [], "action_rows": []}
    blocks = render_parts(parts, card_kind="thread")
    elements = blocks[1]["elements"]
    assert elements[0]["text"] == prefix
    assert [item["alt_text"] for item in elements if item["type"] == "image"] == [
        f"{names[key]} {labels[key]}" for key in visible if key != "local"]
    assert elements[-1]["text"] == "💻 ? 不明"
    assert parts["containers"][1]["provider_states"] == states
    assert parts["containers"][2]["provider_states"] == states
    assert present_slack.face_block_count(parts) == len(blocks)
    for key in {"anthropic", "openai"} - set(cloud):
        assert names[key] not in json.dumps(blocks, ensure_ascii=False)

    spec = _split_spec("m:100:header#1", "m:100#1")
    spec["parts"].update(provider_states_version=version, containers=parts["containers"])
    split = thread_part_blocks(spec, {"name": "m:100:header#1"},
                               "合成投稿\n" + fallback + "\n要約: 合成")
    assert [item["alt_text"] for item in split[1]["elements"] if item["type"] == "image"] == [
        f"{names[key]} {labels[key]}" for key in visible if key != "local"]
    assert split[2]["text"]["text"] == "要約: 合成"
    for key in {"anthropic", "openai"} - set(cloud):
        assert names[key] not in json.dumps(split, ensure_ascii=False)


@pytest.mark.parametrize("prefix", ["", "合成" * 2000 + "x"])
@pytest.mark.parametrize("visible", [["jev", "local"], ["openai", "jev", "local"]])
@pytest.mark.parametrize("version", [3, 4])
def test_v3_v4_header_count_matches_blocks_without_anthropic(prefix, visible, version):
    import present_slack
    import processing_route

    states = dict.fromkeys(("anthropic", "openai", "jev", "local"), "unknown")
    fallback = processing_route.provider_text(states, visible)
    text = (prefix + " · " if prefix else "") + fallback
    parts = {"provider_states_version": version, "containers": [
        {"type": "text", "provider_scope": "header", "visible_providers": visible,
         "provider_states": states, "text": text}], "footer": [], "action_rows": []}
    blocks = render_parts(parts, card_kind="thread")
    assert present_slack.face_block_count(parts) == len(blocks)
    assert all(block["type"] == "context" for block in blocks)
    assert all(len(element.get("text", "")) <= 2000 for block in blocks for element in block["elements"])
    prior = "".join(element["text"] for block in blocks[:-1] for element in block["elements"])
    last = blocks[-1]["elements"][0]["text"] if prefix else ""
    assert prior + last == prefix
    assert "Anthropic" not in json.dumps(blocks, ensure_ascii=False)


@pytest.mark.parametrize("visible", [None, [], ["local", "jev"], ["jev", "jev", "local"],
                                      ["other", "jev", "local"], "jev,local"])
@pytest.mark.parametrize("version", [3, 4])
def test_v3_v4_provider_visibility_rejects_invalid_metadata(visible, version):
    parts = {"provider_states_version": version, "containers": [
        {"type": "text", "provider_scope": "header", "visible_providers": visible,
         "provider_states": dict.fromkeys(("anthropic", "openai", "jev", "local"), "unknown"),
         "text": "Jev 不明 · 💻 不明"}], "footer": [], "action_rows": []}
    with pytest.raises(ValueError, match="bad_provider_states"):
        render_parts(parts, card_kind="thread")


@pytest.mark.parametrize("prefix", ["解析完了", "解析完了 · 初回順 Jev → OpenAI"])
def test_v4_status_and_proven_order_share_provider_context(prefix):
    import present_slack
    import processing_route

    visible = ["openai", "jev", "local"]
    states = {"anthropic": "unknown", "openai": "done", "jev": "done", "local": "unknown"}
    fallback = processing_route.provider_text(states, visible)
    parts = {"provider_states_version": 4, "containers": [
        {"type": "text", "provider_scope": "header", "visible_providers": visible,
         "provider_states": states, "text": prefix + " · " + fallback}],
        "footer": [], "action_rows": []}
    blocks = render_parts(parts, card_kind="thread")
    assert len(blocks) == present_slack.face_block_count(parts) == 1
    assert blocks[0]["elements"][0] == {"type": "plain_text", "text": prefix}
    assert "Anthropic" not in json.dumps(blocks, ensure_ascii=False)


@pytest.mark.parametrize("visible,sequence", [
    (["jev", "local"], ["local", "jev"]),
    (["jev", "local"], ["jev", "local"]),
    (["anthropic", "jev", "local"], ["local", "jev"]),
    (["openai", "jev", "local"], ["jev", "openai"]),
    (["openai", "jev", "local"], ["openai", "jev"]),
    (["anthropic", "openai", "jev", "local"], ["jev", "local"]),
    (["anthropic", "openai", "jev", "local"], ["local", "jev"]),
])
@pytest.mark.parametrize("long_prefix", [False, True])
def test_v4_proven_order_changes_native_icons_and_separates_other_visible_providers(
        visible, sequence, long_prefix):
    import present_slack

    states = {"anthropic": "unknown", "openai": "done", "jev": "partial", "local": "unknown"}
    prefix = ("合成" * 2001 if long_prefix else "合成") + " · 採用結果の処理順"
    labels = {"anthropic": "Anthropic 不明", "openai": "OpenAI 実行済み",
              "jev": "Jev 一部実行・一部省略", "local": "💻 不明"}
    rest = [key for key in visible if key not in sequence]
    fallback = " · ".join([*(labels[key] for key in sequence),
                           *(["その他（順序不明）"] if rest else []), *(labels[key] for key in rest)])
    parts = {"provider_states_version": 4, "containers": [
        {"type": "text", "provider_scope": "header", "visible_providers": visible,
         "provider_states": states, "provider_sequence": sequence,
         "text": prefix + " · " + fallback}], "footer": [], "action_rows": []}
    blocks = render_parts(parts, card_kind="thread")
    assert len(blocks) == present_slack.face_block_count(parts)
    elements = blocks[-1]["elements"]
    assert len(elements) <= 10
    assert all(len(element.get("text", "")) <= 2000 for block in blocks for element in block["elements"])
    assert "".join(e["text"] for b in blocks[:-1] for e in b["elements"]) + elements[0]["text"] == prefix
    names = {"Anthropic": "anthropic", "OpenAI": "openai", "Jev": "jev"}
    ordered = [names[e["alt_text"].split()[0]] if e["type"] == "image" else "local"
               for e in elements if e["type"] == "image" or e.get("text", "").startswith("💻 ")]
    assert ordered == sequence + [key for key in visible if key not in sequence]
    labels = [e.get("text") for e in elements]
    assert ("その他（順序不明）" in labels) == (len(visible) > len(sequence))
    if len(visible) > len(sequence):
        separator = labels.index("その他（順序不明）")
        provider_positions = [i for i, e in enumerate(elements)
                              if e["type"] == "image" or e.get("text", "").startswith("💻 ")]
        assert provider_positions[1] < separator < provider_positions[2]
    for key, name in (("anthropic", "Anthropic"), ("openai", "OpenAI")):
        assert (name in json.dumps(blocks, ensure_ascii=False)) == (key in visible)


def test_v4_unknown_order_remains_canonical_and_disclosed():
    import processing_route

    visible = ["anthropic", "openai", "jev", "local"]
    states = dict.fromkeys(visible, "unknown")
    prefix = "解析中 · 処理順不明"
    parts = {"provider_states_version": 4, "containers": [
        {"type": "text", "provider_scope": "header", "visible_providers": visible,
         "provider_states": states, "provider_sequence": None,
         "text": prefix + " · " + processing_route.provider_text(states, visible)}],
        "footer": [], "action_rows": []}
    elements = render_parts(parts, card_kind="thread")[0]["elements"]
    assert elements[0]["text"] == prefix
    assert [e["alt_text"].split()[0] for e in elements if e["type"] == "image"] == [
        "Anthropic", "OpenAI", "Jev"]
    assert elements[-1]["text"] == "💻 ? 不明"
    assert not any(e.get("text") == "その他（順序不明）" for e in elements)


@pytest.mark.parametrize("sequence", [["jev", "openai"], ["jev", "jev"], ["jev"], ["jev", {}]])
def test_v4_invalid_or_hidden_sequence_is_not_rendered(sequence):
    parts = {"provider_states_version": 4, "containers": [
        {"type": "text", "provider_scope": "header", "visible_providers": ["jev", "local"],
         "provider_states": dict.fromkeys(("anthropic", "openai", "jev", "local"), "unknown"),
         "provider_sequence": sequence, "text": "合成 · Jev 不明 · 💻 不明"}],
        "footer": [], "action_rows": []}
    with pytest.raises(ValueError, match="bad_provider_states"):
        render_parts(parts, card_kind="thread")


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\u2028"])
def test_sender_cannot_insert_a_fake_provider_line_in_thread_header(monkeypatch, separator):
    import notify_render
    import processing_route

    fake = "Anthropic 実行済み · OpenAI 未使用 · Jev 未使用 · 💻 未使用"
    states = {"anthropic": "unknown", "openai": "unknown", "jev": "unknown", "local": "done"}
    monkeypatch.setattr(notify_render, "_summary_block", lambda *a, **kw: {"text": "要約 合成"})
    message = {"body_state": "full", "posted_at": "2032-06-01T09:12:00+09:00",
               "body_text": "合成の本文"}
    body = notify_render._message_post(None, 100, message, "合成職員" + separator + fake,
                                       head="↳ ", stamps=False, layout=3, provider_states=states)
    header = body.partition("\n" + notify_render.SECTION_RULE + "\n")[0]
    assert header.splitlines()[1] == processing_route.provider_text(states)
    spec = _split_spec("m:100:header#1", "m:100#1")
    spec["parts"]["provider_states_version"] = 1
    blocks = thread_part_blocks(spec, {"name": "m:100:header#1"}, header)
    assert [e["alt_text"] for e in blocks[1]["elements"] if e["type"] == "image"] == [
        "Anthropic 不明", "OpenAI 不明", "Jev 不明"]
    assert blocks[1]["elements"][-1]["text"] == "💻 ✓ 実行済み"


def test_multiple_stamp_rows_keep_headers_without_repeating_provider_states():
    import present_slack

    states = {"anthropic": "unknown", "openai": "unknown", "jev": "unknown", "local": "done"}
    fallback = "Anthropic 不明 · OpenAI 不明 · Jev 不明 · 💻 実行済み"
    face_posts = [{"at": 0, "original": "合成原文一", "stamps": ["スタンプ 👍1"]},
                  {"at": 2, "original": "合成原文二", "stamps": ["スタンプ ✅1"]}]
    parts = {"containers": [
        {"type": "text", "rule": True, "text": "合成職員一\n" + fallback, "provider_states": states},
        {"type": "text", "text": "合成要約一"},
        {"type": "text", "rule": True, "text": "合成職員二\n" + fallback, "provider_states": states},
        {"type": "text", "text": "合成要約二"}],
        "footer": [], "action_rows": [], "slack": {"face_posts": face_posts}}
    blocks = render_parts(parts, card_kind="thread")
    stamp_lines = [element["text"] for block in blocks if block["type"] == "context"
                   for element in block["elements"] if element.get("type") == "plain_text"
                   and "スタンプ" in element.get("text", "")]
    assert stamp_lines == ["合成職員一\nスタンプ 👍1", "合成職員二\nスタンプ ✅1"]
    assert all(fallback not in line for line in stamp_lines)
    nested = sum(len(block.get("child_blocks", [])) for block in blocks)
    assert present_slack.face_block_count(parts, face_posts) == len(blocks) + nested


def test_provider_state_contract_rejects_bad_values_and_mismatched_fallback():
    spec = _spec()
    values = {"anthropic": "done", "openai": "unknown", "jev": "partial", "local": "skipped"}
    fallback = "Anthropic 実行済み · OpenAI 不明 · Jev 一部実行・一部省略 · 💻 省略"
    spec["parts"]["containers"][1] = {"type": "text", "rule": True, "text": "合成\n" + fallback,
                                      "provider_states": values}
    spec["parts"]["provider_states_version"] = 1
    validate(spec)
    for mutation in ("bad_state", "bad_text", "bad_version", "extra_provider"):
        broken = copy.deepcopy(spec)
        item = broken["parts"]["containers"][1]
        if mutation == "bad_state":
            item["provider_states"]["jev"] = "invented"
        elif mutation == "bad_text":
            item["text"] += " extra"
        elif mutation == "bad_version":
            broken["parts"]["provider_states_version"] = True
        else:
            item["provider_states"]["other"] = "done"
        with pytest.raises(ValueError, match="bad_provider_states"):
            validate(broken)


def _v2_provider_spec():
    spec = _spec()
    states = {"anthropic": "done", "openai": "unknown", "jev": "partial", "local": "skipped"}
    fallback = "Anthropic 実行済み · OpenAI 不明 · Jev 一部実行・一部省略 · 💻 省略"
    spec["parts"]["provider_states_version"] = 2
    spec["parts"]["containers"] = [
        {"type": "heading", "text": "合成カード"},
        {"type": "text", "provider_scope": "header", "provider_states": states.copy(),
         "text": "合成情報 · " + fallback},
        {"type": "text", "rule": True, "text": "合成の投稿", "provider_states": states.copy()},
        {"type": "text", "text": "合成の要約"},
    ]
    return spec


@pytest.mark.parametrize("mutation", [
    "forged_aggregate", "duplicate_scope", "late_header", "post_scope", "footer_scope",
    "missing_version", "invalid_version", "unknown_provider", "post_used", "invalid_state",
    "header_rule", "header_newline", "forged_fallback", "missing_header", "states_not_post",
    "header_at_start", "header_after_unrelated_text", "no_heading",
])
def test_v2_provider_contract_rejects_tampering(mutation):
    spec = _v2_provider_spec()
    validate(spec)
    parts = spec["parts"]
    containers = parts["containers"]
    header, post = containers[1], containers[2]
    if mutation == "forged_aggregate":
        header["provider_states"]["openai"] = "done"
        header["text"] = header["text"].replace("OpenAI 不明", "OpenAI 実行済み")
    elif mutation == "duplicate_scope":
        containers.insert(2, copy.deepcopy(header))
    elif mutation == "late_header":
        containers[1], containers[2] = post, header
    elif mutation == "post_scope":
        post["provider_scope"] = "header"
    elif mutation == "footer_scope":
        parts["footer"][0]["provider_scope"] = "header"
    elif mutation == "missing_version":
        del parts["provider_states_version"]
    elif mutation == "invalid_version":
        parts["provider_states_version"] = True
    elif mutation == "unknown_provider":
        post["provider_states"]["other"] = "done"
    elif mutation == "post_used":
        post["provider_states"]["anthropic"] = "used"
    elif mutation == "invalid_state":
        post["provider_states"]["local"] = "invented"
    elif mutation == "header_rule":
        header["rule"] = False
    elif mutation == "header_newline":
        header["text"] = "合成情報\n" + header["text"]
    elif mutation == "forged_fallback":
        header["text"] = header["text"].replace("OpenAI 不明", "OpenAI 実行済み")
    elif mutation == "missing_header":
        del containers[1]
    elif mutation == "states_not_post":
        containers[3]["provider_states"] = post["provider_states"].copy()
    elif mutation == "header_at_start":
        containers[0], containers[1] = header, containers[0]
    elif mutation == "header_after_unrelated_text":
        containers.insert(1, {"type": "text", "text": "合成の追加情報"})
    elif mutation == "no_heading":
        containers[0] = {"type": "text", "text": "合成タイトル"}
    with pytest.raises(ValueError, match="bad_provider_states"):
        validate(spec)


def test_v2_missing_post_state_counts_as_unknown_in_aggregate():
    spec = _v2_provider_spec()
    parts = spec["parts"]
    header = parts["containers"][1]
    parts["containers"].append({"type": "text", "rule": True, "text": "合成の別投稿"})
    # A missing state is still a second post: do not present the one known
    # post's status as an exact aggregate for all posts.
    with pytest.raises(ValueError, match="bad_provider_states"):
        validate(spec)
    header["provider_states"] = {"anthropic": "used", "openai": "unknown",
                                 "jev": "used", "local": "unknown"}
    header["text"] = "Anthropic 使用あり · OpenAI 不明 · Jev 使用あり · 💻 不明"
    validate(spec)
    # One missing-state post, or no posts at all, both produce all unknown.
    unknown = dict.fromkeys(("anthropic", "openai", "jev", "local"), "unknown")
    header["provider_states"] = unknown
    header["text"] = "Anthropic 不明 · OpenAI 不明 · Jev 不明 · 💻 不明"
    del parts["containers"][2]
    validate(spec)
    parts["containers"].pop()
    validate(spec)


def test_body_chunk_folds_behind_collapsed_container_only_with_a_header_part():
    spec = _split_spec("m:100:header#1", "m:100#1")
    (block,) = thread_part_blocks(spec, {"name": "m:100#1"}, BODY)
    assert block["type"] == "container" and block["is_collapsible"] and block["default_collapsed"]
    assert block["width"] == "full"   # fills the message pane instead of the fixed standard width
    assert block["title"]["text"] == f"原文（{len(BODY)}字）"
    assert "".join(c["text"]["text"] for c in block["child_blocks"]) == BODY
    # legacy chunks (summary and body in one text) and unnamed parts stay plain text
    assert thread_part_blocks(_split_spec("m:100#1"), {"name": "m:100#1"}, BODY) is None
    assert thread_part_blocks(spec, {"name": "attach:0001"}, BODY) is None
    assert thread_part_blocks(spec, None, BODY) is None


def _run(w, spec, text, part):
    ctx = {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    return asyncio.run(w.worker._body_part(spec, text, ctx, part))


def _run_with_gate(w, spec, part, gate):
    token = delivery.RETRY_ALLOWED.set(gate)
    try:
        return _run(w, spec, BODY, part)
    finally:
        delivery.RETRY_ALLOWED.reset(token)


class _MetricResponse(dict):
    def __init__(self, status=200, **fields):
        super().__init__(**fields)
        self.status_code = status


class _MetricRejection(Exception):
    def __init__(self, status=200, **fields):
        super().__init__("SYNTHETIC_PRIVATE_EXCEPTION")
        self.response = _MetricResponse(status, **fields)


@pytest.mark.parametrize("variant", ["plain", "blocks", "fallback"])
@pytest.mark.parametrize("method", ["post", "update"])
def test_length_rejection_metrics_are_shape_only_and_preserve_the_exception(variant, method):
    worker = object.__new__(delivery.DeliveryWorker)
    lines, calls = [], []
    worker._log = lambda event, **fields: lines.append(eventlog.render(event, fields))
    text = "SYNTHETIC_PRIVATE_BODY &amp; あ"
    block_text = "SYNTHETIC_PRIVATE_BLOCK"
    blocks = [{"type": "container", "title": {"type": "plain_text", "text": "hidden"},
               "child_blocks": [{"type": "section", "text": {"type": "plain_text", "text": block_text}}]}]
    kwargs = {"text": text, "channel": "SYNTHETIC_PRIVATE_ID",
              "thread_ts" if method == "post" else "ts": "SYNTHETIC_PRIVATE_TS"}
    rejection = _MetricRejection(ok=False, error="msg_too_long")

    async def operation(**sent):
        calls.append(sent)
        if variant == "fallback" and len(calls) == 1:
            raise _MetricRejection(ok=False, error="invalid_blocks")
        raise rejection

    async def allowed():
        return True

    token = delivery.RETRY_ALLOWED.set(allowed)
    try:
        with pytest.raises(_MetricRejection) as caught:
            asyncio.run(worker._blocks_call(operation, None if variant == "plain" else blocks,
                                            _metric_method=method, **kwargs))
    finally:
        delivery.RETRY_ALLOWED.reset(token)
    assert caught.value is rejection
    events = [json.loads(line) for line in lines]
    (metric,) = [event for event in events if event["event"] == "slack_msg_too_long"]
    selected = blocks if variant == "blocks" else None
    payload = {**kwargs, **({"blocks": selected} if selected else {})}
    assert metric == {"event": "slack_msg_too_long", "slack_method": method, "slack_variant": variant,
                      "text_chars": len(text), "text_bytes": len(text.encode("utf-8")),
                      "blocks_present": int(selected is not None), "block_count": 2 if selected else 0,
                      "max_block_text_chars": len(block_text) if selected else 0,
                      "compact_json_bytes": len(json.dumps(payload, ensure_ascii=False,
                                                           separators=(",", ":")).encode("utf-8"))}
    assert len(calls) == (2 if variant == "fallback" else 1)
    assert all("SYNTHETIC_PRIVATE" not in line for line in lines)


@pytest.mark.parametrize("status,ok,error", [
    (500, False, "msg_too_long"),
    (None, False, "msg_too_long"),
    (200, True, "msg_too_long"),
    (200, False, "fatal_error"),
])
def test_length_metrics_ignore_unconfirmed_and_other_failures(status, ok, error):
    worker = object.__new__(delivery.DeliveryWorker)
    events = []
    worker._log = lambda event, **fields: events.append((event, fields))
    rejection = _MetricRejection(status, ok=ok, error=error)

    async def operation(**_kwargs):
        raise rejection

    with pytest.raises(_MetricRejection) as caught:
        asyncio.run(worker._blocks_call(operation, None, _metric_method="post", text="synthetic"))
    assert caught.value is rejection and events == []


@pytest.mark.parametrize("failure", ["metric", "logger"])
def test_length_diagnostic_failure_never_changes_delivery_exception(monkeypatch, failure):
    worker = object.__new__(delivery.DeliveryWorker)
    rejection = _MetricRejection(ok=False, error="msg_too_long")

    def fail(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_FAILURE")

    worker._log = fail if failure == "logger" else lambda *_args, **_kwargs: None
    if failure == "metric":
        monkeypatch.setattr(delivery, "_rejection_metrics", fail)

    async def operation(**_kwargs):
        raise rejection

    with pytest.raises(_MetricRejection) as caught:
        asyncio.run(worker._blocks_call(operation, None, text="synthetic"))
    assert caught.value is rejection


def test_returned_length_rejection_is_observed_without_changing_the_response():
    worker = object.__new__(delivery.DeliveryWorker)
    events = []
    worker._log = lambda event, **fields: events.append((event, fields))
    response = _MetricResponse(ok=False, error="msg_too_long")

    async def operation(**_kwargs):
        return response

    result = asyncio.run(worker._blocks_call(operation, None, _metric_method="post", text="synthetic"))
    assert result is response
    assert len(events) == 1 and events[0][0] == "slack_msg_too_long"


def test_length_diagnostic_failure_does_not_change_returned_response():
    worker = object.__new__(delivery.DeliveryWorker)

    def fail(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_FAILURE")

    worker._log = fail
    response = _MetricResponse(ok=False, error="msg_too_long")

    async def operation(**_kwargs):
        return response

    result = asyncio.run(worker._blocks_call(operation, None, text="synthetic"))
    assert result is response


def test_cancelled_sdk_attempt_is_not_observed_or_retried():
    worker = object.__new__(delivery.DeliveryWorker)
    events, calls = [], []
    worker._log = lambda event, **fields: events.append((event, fields))

    async def operation(**kwargs):
        calls.append(kwargs)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._blocks_call(operation, None, text="synthetic"))
    assert len(calls) == 1 and events == []


def test_length_rejection_is_recorded_before_existing_429_retry(monkeypatch):
    worker = object.__new__(delivery.DeliveryWorker)
    events, calls = [], []
    worker._log = lambda event, **fields: events.append((event, fields))
    rejection = _MetricRejection(429, ok=False, error="msg_too_long")
    response = _MetricResponse(ok=True)

    async def operation(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise rejection
        return response

    async def sleep(_seconds):
        return None

    async def allowed():
        return True

    monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
    token = delivery.RETRY_ALLOWED.set(allowed)
    try:
        result = asyncio.run(worker._blocks_call(operation, None, _metric_method="post", text="synthetic"))
    finally:
        delivery.RETRY_ALLOWED.reset(token)
    assert result is response and len(calls) == 2
    assert len(events) == 1 and events[0][0] == "slack_msg_too_long"


@pytest.mark.parametrize("shape", ["oversized", "deep", "cyclic", "wide"])
def test_unbounded_metric_input_is_skipped_without_changing_rejection(shape):
    worker = object.__new__(delivery.DeliveryWorker)
    events = []
    worker._log = lambda event, **fields: events.append((event, fields))
    rejection = _MetricRejection(ok=False, error="msg_too_long")
    text, blocks = "synthetic", None
    if shape == "oversized":
        text = "x" * (delivery._METRIC_MAX_BYTES + 1)
    elif shape == "wide":
        blocks = [{"type": "section"}] * (delivery._METRIC_MAX_NODES + 1)
    elif shape == "cyclic":
        blocks = [{"type": "container"}]
        blocks[0]["child_blocks"] = blocks
    else:
        block = {"type": "section"}
        for _ in range(delivery._METRIC_MAX_DEPTH + 1):
            block = {"type": "container", "child_blocks": [block]}
        blocks = [block]

    async def operation(**_kwargs):
        raise rejection

    with pytest.raises(_MetricRejection) as caught:
        asyncio.run(worker._blocks_call(operation, blocks, text=text))
    assert caught.value is rejection and events == []


def test_body_part_posts_blocks_and_keeps_plain_text_identity(led):
    w, spec, _replies = _rewrite_world(led)
    spec["delivery"]["thread_id"] = ROOT_TS
    spec["parts"]["manifest"] = [{"name": "m:100:header#1"}, {"name": "m:100#1"}]
    out = _run(w, spec, BODY, {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"})
    assert out["result"] == "delivered"
    posted = w.client.thread_posts[-1]
    assert posted["text"] == BODY and posted["blocks"][0]["type"] == "container"
    # the same chunk again binds to the reply by its plain text, no second post
    n = len(w.client.thread_posts)
    again = _run(w, spec, BODY, {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"})
    assert again == {"result": "delivered", "remote_id": out["remote_id"]}
    assert len(w.client.thread_posts) == n


def test_invalid_blocks_resends_plain_text_once_without_losing_the_part(led):
    w, spec, _replies = _rewrite_world(led)
    spec["delivery"]["thread_id"] = ROOT_TS
    spec["parts"]["manifest"] = [{"name": "m:100:header#1"}, {"name": "m:100#1"}]

    class Response(dict):
        status_code = 200

    class Rejected(Exception):
        response = Response(ok=False, error="invalid_blocks")

    original, attempts = w.client.chat_postMessage, []

    async def post(**kwargs):
        attempts.append(kwargs)
        if "blocks" in kwargs:
            raise Rejected()
        return await original(**kwargs)

    w.client.chat_postMessage = post

    async def allowed():
        return True

    out = _run_with_gate(w, spec, {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"}, allowed)
    assert out["result"] == "delivered"
    assert len(attempts) == 2 and "blocks" in attempts[0] and "blocks" not in attempts[1]
    assert {k: v for k, v in attempts[0].items() if k != "blocks"} == attempts[1]
    assert "blocks" not in w.client.thread_posts[-1]


@pytest.mark.parametrize("gate_result", ["missing", "denied", "raises", "timeout", "non_boolean"])
def test_invalid_blocks_never_falls_back_without_confirmed_authority(led, gate_result):
    w, spec, _replies = _rewrite_world(led)
    spec["delivery"]["thread_id"] = ROOT_TS
    spec["parts"]["manifest"] = [{"name": "m:100:header#1"}, {"name": "m:100#1"}]

    class Response(dict):
        status_code = 200

    class Rejected(Exception):
        response = Response(ok=False, error="invalid_blocks")

    attempts = []

    async def post(**kwargs):
        attempts.append(kwargs)
        raise Rejected()

    async def gate():
        if gate_result == "raises":
            raise RuntimeError("synthetic gate failure")
        if gate_result == "timeout":
            raise TimeoutError("synthetic gate timeout")
        return 1 if gate_result == "non_boolean" else False

    w.client.chat_postMessage = post
    part = {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"}
    out = _run_with_gate(w, spec, part, None if gate_result == "missing" else gate)
    assert out == {"result": "not_sent", "error_code": "invalid_blocks"}
    assert len(attempts) == 1 and "blocks" in attempts[0]


def test_invalid_blocks_cancellation_does_not_fall_back(led):
    w, spec, _replies = _rewrite_world(led)
    spec["delivery"]["thread_id"] = ROOT_TS
    spec["parts"]["manifest"] = [{"name": "m:100:header#1"}, {"name": "m:100#1"}]

    class Response(dict):
        status_code = 200

    class Rejected(Exception):
        response = Response(ok=False, error="invalid_blocks")

    attempts = []

    async def post(**kwargs):
        attempts.append(kwargs)
        raise Rejected()

    async def gate():
        raise asyncio.CancelledError()

    w.client.chat_postMessage = post
    part = {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"}
    with pytest.raises(asyncio.CancelledError):
        _run_with_gate(w, spec, part, gate)
    assert len(attempts) == 1


@pytest.mark.parametrize("failure", ["revoked", "raises"])
def test_invalid_blocks_rechecks_current_worker_authority(led, failure):
    w, spec, _replies = _rewrite_world(led)
    spec["delivery"]["thread_id"] = ROOT_TS
    spec["parts"]["manifest"] = [{"name": "m:100:header#1"}, {"name": "m:100#1"}]

    class Response(dict):
        status_code = 200

    class Rejected(Exception):
        response = Response(ok=False, error="invalid_blocks")

    attempts, checks = [], []

    async def post(**kwargs):
        attempts.append(kwargs)
        raise Rejected()

    async def authority(current_spec):
        checks.append(current_spec is spec)
        if failure == "raises":
            raise RuntimeError("synthetic authority failure")
        return False

    w.client.chat_postMessage = post
    w.worker._send_allowed = authority
    part = {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"}
    out = _run_with_gate(w, spec, part, w.worker._retry_gate(spec))
    assert out == {"result": "not_sent", "error_code": "invalid_blocks"}
    assert len(attempts) == 1 and checks == [True]


@pytest.mark.parametrize("status,ok,expected", [
    (500, False, "unknown"),
    (None, False, "unknown"),
    (200, True, "unknown"),
    (302, False, "not_sent"),
    (400, None, "not_sent"),
    (True, False, "not_sent"),
])
def test_unproven_invalid_blocks_response_preserves_original_classification(led, status, ok, expected):
    w, spec, _replies = _rewrite_world(led)
    spec["delivery"]["thread_id"] = ROOT_TS
    spec["parts"]["manifest"] = [{"name": "m:100:header#1"}, {"name": "m:100#1"}]

    class Response(dict):
        status_code = status

    class Rejected(Exception):
        response = Response(ok=ok, error="invalid_blocks")

    attempts, checks = [], []

    async def post(**kwargs):
        attempts.append(kwargs)
        raise Rejected()

    async def gate():
        checks.append(True)
        return True

    w.client.chat_postMessage = post
    part = {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"}
    assert _run_with_gate(w, spec, part, gate) == {"result": expected, "error_code": "invalid_blocks"}
    assert len(attempts) == 1 and checks == []


@pytest.mark.parametrize("failure", ["returned_rejection", "timeout", "fatal_error"])
def test_unverified_first_attempt_is_never_sent_again(led, failure):
    w, spec, _replies = _rewrite_world(led)
    spec["delivery"]["thread_id"] = ROOT_TS
    spec["parts"]["manifest"] = [{"name": "m:100:header#1"}, {"name": "m:100#1"}]

    class Response(dict):
        status_code = 200

    class Rejected(Exception):
        response = Response(ok=False, error="fatal_error")

    attempts = []

    async def post(**kwargs):
        attempts.append(kwargs)
        if failure == "returned_rejection":
            return Response(ok=False, error="invalid_blocks")
        if failure == "timeout":
            raise TimeoutError("synthetic transport timeout")
        raise Rejected()

    async def allowed():
        return True

    w.client.chat_postMessage = post
    part = {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"}
    out = _run_with_gate(w, spec, part, allowed)
    assert out["result"] == "unknown"
    assert out["error_code"] == {"returned_rejection": "bad_slack_response",
                                 "timeout": "timeouterror", "fatal_error": "fatal_error"}[failure]
    assert len(attempts) == 1


@pytest.mark.parametrize("status", [200, 400, 429])
@pytest.mark.parametrize("method", ["post", "update"])
def test_confirmed_invalid_blocks_retries_once_with_same_method_and_scope(led, status, method):
    w, spec, _replies = _rewrite_world(led)

    class Response(dict):
        status_code = status

    class Rejected(Exception):
        response = Response(ok=False, error="invalid_blocks")

    calls, checks = [], []

    async def operation(**kwargs):
        calls.append((method, kwargs))
        if len(calls) == 1:
            raise Rejected()
        return {"ok": True}

    async def allowed(_spec):
        checks.append(True)
        return True

    w.worker._send_allowed = allowed
    kwargs = {"channel": "SYNTHETIC_CHANNEL", "text": "合成本文",
              "thread_ts" if method == "post" else "ts": ROOT_TS}
    gate = w.worker._retry_gate(spec)
    token = delivery.RETRY_ALLOWED.set(gate)
    try:
        result = asyncio.run(w.worker._blocks_call(operation, [{"type": "divider"}], **kwargs))
    finally:
        delivery.RETRY_ALLOWED.reset(token)
    assert result == {"ok": True} and checks == [True]
    assert len(calls) == 2 and calls[0][0] == calls[1][0] == method
    assert calls[0][1]["blocks"] == [{"type": "divider"}]
    assert {k: v for k, v in calls[0][1].items() if k != "blocks"} == calls[1][1] == kwargs


@pytest.mark.parametrize("error", ["cant_update_message", "ratelimited"])
def test_other_rejections_are_not_retried_without_blocks(led, error, monkeypatch):
    w, spec, _replies = _rewrite_world(led)
    spec["delivery"]["thread_id"] = ROOT_TS
    spec["parts"]["manifest"] = [{"name": "m:100:header#1"}, {"name": "m:100#1"}]

    class Response(dict):
        status_code = 429 if error == "ratelimited" else 400

    class Rejected(Exception):
        response = Response(ok=False, error=error)

    attempts = []

    async def post(**kwargs):
        attempts.append(kwargs)
        raise Rejected()

    w.client.chat_postMessage = post
    from adapters.slack import delivery
    monkeypatch.setattr(delivery, "_rate_wait", lambda exc: None)
    out = _run(w, spec, BODY, {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"})
    assert out["result"] == "not_sent" and len(attempts) == 1
    assert json.dumps(attempts[0]["blocks"])  # the blocks were sent, once


@pytest.mark.parametrize("cloud", [(), ("anthropic",), ("openai",), ("anthropic", "openai")])
def test_writer_split_header_uses_registered_provider_visibility(led, monkeypatch, cloud):
    import mcs_util
    import present_slack
    import processing_route
    from notify_testkit import _dispatch, _intent, _seed_thread, _spec as written_spec
    from slack_testkit import SLACK

    requested = []
    keys = {"MCS_ANTHROPIC_API_KEY": "anthropic", "MCS_OPENAI_API_KEY": "openai"}

    def key_problem(name, root=None):
        requested.append(name)
        return None if keys[name] in cloud else "missing"

    monkeypatch.setattr(mcs_util, "private_env_problem", key_problem)
    monkeypatch.setattr(present_slack, "BODY_ON_FACE", False)
    monkeypatch.setattr(processing_route, "provider_states",
                        lambda *args, **kwargs: dict.fromkeys(processing_route.PROVIDERS, "done"))
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)
    spec = written_spec(led)
    validate(spec)
    assert requested == list(keys)
    assert spec["parts"]["provider_states_version"] == 4
    assert spec["parts"]["containers"][1]["visible_providers"] == [*cloud, "jev", "local"]
    part = next(p for p in spec["parts"]["manifest"]
                if p.get("kind") == "body_part" and p.get("name") == "m:100:header#1")
    text = spec["parts"]["thread_body_parts"][int(part["part_id"].split(":")[1]) - 1]
    blocks = thread_part_blocks(spec, part, text)
    assert blocks is not None
    images = [element["alt_text"].split()[0].lower() for block in blocks
              for element in block.get("elements", []) if element.get("type") == "image"]
    assert images == [*cloud, "jev"]
    rendered = json.dumps(blocks, ensure_ascii=False)
    for provider, name in (("anthropic", "Anthropic"), ("openai", "OpenAI")):
        assert (name in text) == (provider in cloud)
        assert (name in rendered) == (provider in cloud)


@pytest.mark.parametrize("version,sequence", [(2, None), (3, None), (4, None), (4, ["jev", "local"])])
def test_silent_footer_keeps_member_names_after_provider_header(version, sequence):
    from adapters.slack.cards import render

    spec = _spec()
    states = dict.fromkeys(("anthropic", "openai", "jev", "local"), "unknown")
    visible = ["anthropic", "openai", "jev", "local"] if version == 2 else ["jev", "local"]
    fallback = " · ".join(f"{name} 不明" for key, name in (
        ("anthropic", "Anthropic"), ("openai", "OpenAI"), ("jev", "Jev"), ("local", "💻"))
        if key in visible)
    header = {"type": "text", "provider_scope": "header", "provider_states": states.copy(),
              "text": fallback}
    if version >= 3:
        header["visible_providers"] = visible
    if version == 4:
        order = "Jev → 💻" if sequence else "不明"
        header.update(processing_status="processing", provider_sequence=sequence,
                      text=f"解析中 · 採用結果の処理順: {order} · {fallback}")
    spec["parts"].update(provider_states_version=version, mentions="silent", containers=[
        {"type": "heading", "text": "合成カード"}, header,
        {"type": "text", "rule": True, "text": "合成投稿", "provider_states": states.copy()}],
        footer=[{"type": "text", "text": "確認: <@U0A>・<@U0B>・<@U0X>"}])
    _, blocks = render(spec, names={"U0A": "合成担当A", "U0B": "合成担当B"})
    text = json.dumps(blocks, ensure_ascii=False)
    assert "合成担当A" in text and "合成担当B" in text and "メンバー" in text
    assert "<@U0A>" not in text and "<@U0B>" not in text and "<@U0X>" not in text
