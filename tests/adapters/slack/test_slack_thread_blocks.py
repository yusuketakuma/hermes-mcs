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
