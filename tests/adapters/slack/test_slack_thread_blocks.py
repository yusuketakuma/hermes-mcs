"""Slack thread chunks of a header/body-split post carry Block Kit beside the plain text."""
import asyncio
import json

import pytest
from slack_card_testkit import _spec
from slack_testkit import isolated_slack_ledger as led, _rewrite_world, ROOT_TS

from adapters.slack.cards import thread_part_blocks

__all__ = ["led"]

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


def test_body_chunk_folds_behind_collapsed_container_only_with_a_header_part():
    spec = _split_spec("m:100:header#1", "m:100#1")
    (block,) = thread_part_blocks(spec, {"name": "m:100#1"}, BODY)
    assert block["type"] == "container" and block["is_collapsible"] and block["default_collapsed"]
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
