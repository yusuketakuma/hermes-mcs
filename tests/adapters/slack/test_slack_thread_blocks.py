"""Slack thread chunks of a header/body-split post carry Block Kit beside the plain text."""
import asyncio
import copy
import json

import pytest
from slack_card_testkit import _spec
from slack_testkit import isolated_slack_ledger as led, _rewrite_world, ROOT_TS

from adapters.slack.presentation import render_parts, thread_part_blocks
from adapters.slack.cards import validate

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
def test_v3_visibility_filters_face_and_split_header_without_losing_states(cloud):
    import present_slack

    visible = cloud + ["jev", "local"]
    states = {"anthropic": "done", "openai": "done", "jev": "partial", "local": "unknown"}
    names = {"anthropic": "Anthropic", "openai": "OpenAI", "jev": "Jev", "local": "💻"}
    labels = {"anthropic": "実行済み", "openai": "実行済み", "jev": "一部実行・一部省略", "local": "不明"}
    fallback = " · ".join(f"{names[key]} {labels[key]}" for key in visible)
    parts = {"provider_states_version": 3, "containers": [
        {"type": "heading", "text": "合成カード"},
        {"type": "text", "provider_scope": "header", "provider_states": states.copy(),
         "visible_providers": visible, "text": "合成情報 · " + fallback},
        {"type": "text", "rule": True, "text": "合成投稿", "provider_states": states.copy()},
        {"type": "text", "text": "合成要約"}], "footer": [], "action_rows": []}
    blocks = render_parts(parts, card_kind="thread")
    elements = blocks[1]["elements"]
    assert [item["alt_text"] for item in elements if item["type"] == "image"] == [
        f"{names[key]} {labels[key]}" for key in visible if key != "local"]
    assert elements[-1]["text"] == "💻 ? 不明"
    assert parts["containers"][1]["provider_states"] == states
    assert parts["containers"][2]["provider_states"] == states
    assert present_slack.face_block_count(parts) == len(blocks)
    for key in {"anthropic", "openai"} - set(cloud):
        assert names[key] not in json.dumps(blocks, ensure_ascii=False)

    spec = _split_spec("m:100:header#1", "m:100#1")
    spec["parts"].update(provider_states_version=3, containers=parts["containers"])
    split = thread_part_blocks(spec, {"name": "m:100:header#1"},
                               "合成投稿\n" + fallback + "\n要約: 合成")
    assert [item["alt_text"] for item in split[1]["elements"] if item["type"] == "image"] == [
        f"{names[key]} {labels[key]}" for key in visible if key != "local"]
    assert split[2]["text"]["text"] == "要約: 合成"
    for key in {"anthropic", "openai"} - set(cloud):
        assert names[key] not in json.dumps(split, ensure_ascii=False)


@pytest.mark.parametrize("prefix", ["", "合成" * 2000 + "x"])
@pytest.mark.parametrize("visible", [["jev", "local"], ["openai", "jev", "local"]])
def test_v3_header_count_matches_blocks_without_anthropic(prefix, visible):
    import present_slack
    import processing_route

    states = dict.fromkeys(("anthropic", "openai", "jev", "local"), "unknown")
    fallback = processing_route.provider_text(states, visible)
    text = (prefix + " · " if prefix else "") + fallback
    parts = {"provider_states_version": 3, "containers": [
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
def test_v3_provider_visibility_rejects_invalid_metadata(visible):
    parts = {"provider_states_version": 3, "containers": [
        {"type": "text", "provider_scope": "header", "visible_providers": visible,
         "provider_states": dict.fromkeys(("anthropic", "openai", "jev", "local"), "unknown"),
         "text": "Jev 不明 · 💻 不明"}], "footer": [], "action_rows": []}
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
    out = _run(w, spec, BODY, {"part_id": "body:0002", "kind": "body_part", "name": "m:100#1"})
    assert out["result"] == "delivered"
    assert len(attempts) == 2 and "blocks" in attempts[0] and "blocks" not in attempts[1]
    assert "blocks" not in w.client.thread_posts[-1]


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
    assert spec["parts"]["provider_states_version"] == 3
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
