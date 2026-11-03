"""Each Slack post keeps its original and stamps on its own card (synthetic only)."""
import notify_parts
import json

import pytest

import notify_cards
import notify_render
import present_slack

from adapters.slack import presentation as slack_presentation
from adapters.slack.cards import validate
from notify_testkit import (NOW, _card, _dispatch, _intent, _latest_render, _seed_thread,
                            delivered_source, led, pinned_clock)
from slack_testkit import SLACK

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")
STAMP = "スタンプ 👀2 合成 太郎・合成 花子 · 観測 10-01 10:00"


def _parts(led, render):
    return [dict(r) for r in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? ORDER BY idx",
        (render["delivery_id"],)).fetchall()]


def _spec(led):
    return json.loads(_latest_render(led)["spec_json"])



def _post_specs(led, event):
    delivered_source(led, mids=(100,), cfg=SLACK)
    assert _dispatch(led, event, SLACK, now=NOW + 1)["dispatched"]
    specs = [json.loads(_latest_render(led, row[0])["spec_json"]) for row in
             led.db.execute("SELECT card_id FROM notification_cards WHERE kind='thread' ORDER BY card_id")]
    assert {spec["parts"]["post_source_id"] for spec in specs} == set(json.loads(event["payload"])["message_ids"])
    return specs


def test_face_groups_stamps_below_all_folded_originals(led, monkeypatch):
    monkeypatch.setattr(notify_render, "thread_stamp_row", lambda *_a, **_k: STAMP)
    _seed_thread(led, mids=(100, 101))
    with led.db:
        led.db.execute("UPDATE messages SET body_text='合成の元投稿' WHERE message_id=100")
        led.db.execute("UPDATE messages SET body_text='合成の返信本文' WHERE message_id=101")
    event = _intent(led, payload={"message_ids": [100, 101]})
    _dispatch(led, event, SLACK)
    specs = _post_specs(led, event)
    for spec, original in zip(specs, ("合成の元投稿", "合成の返信本文"), strict=True):
        validate(spec)
        posts = spec["parts"]["slack"]["face_posts"]
        assert [p["original"] for p in posts] == [original]
        assert posts[0]["stamps"] == [STAMP]
        assert spec["parts"]["thread_body_parts"] == []
        assert not [p for p in spec["parts"]["manifest"] if p["kind"] == "body_part"]
        blocks = slack_presentation.render_parts(spec["parts"], card_kind="thread")
        folds = [i for i, block in enumerate(blocks) if block.get("type") == "container"]
        assert len(folds) == 1 and blocks[folds[0]]["title"]["text"] == f"原文（{len(original)}字）"
        stamp_at = [i for i, block in enumerate(blocks) if block["type"] == "context"
                    and STAMP in json.dumps(block, ensure_ascii=False)]
        assert len(stamp_at) == 1 and stamp_at[0] > folds[0]
        assert spec["parts"]["containers"][posts[0]["at"]]["text"] in blocks[stamp_at[0]]["elements"][0]["text"]
        assert blocks[folds[0]]["child_blocks"][0]["text"]["text"] == original
        assert len(blocks) <= present_slack.face_block_count(spec["parts"], posts) <= present_slack.BLOCK_MAX


def test_update_rewrites_old_thread_chunks_and_moves_body_to_face(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    monkeypatch.setattr(present_slack, "BODY_ON_FACE", False)
    delivered_source(led, mids=(100,), cfg=SLACK)
    first = _latest_render(led)
    old = [p["part_id"] for p in _parts(led, first) if p["kind"] == "body_part"]
    assert old
    monkeypatch.setattr(present_slack, "BODY_ON_FACE", True)
    with led.db:
        led.db.execute("UPDATE messages SET body_text='更新後の合成本文',content_hash=? WHERE message_id=100", ("b" * 64,))
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, [], force=True)
    spec = _spec(led)
    validate(spec)
    assert spec["parts"]["slack"]["face_posts"][0]["original"] == "更新後の合成本文"
    cleanup = [p for p in spec["parts"]["manifest"] if p.get("edit_only")]
    assert cleanup and {p["prior_remote_id"] for p in cleanup} == {"r-" + p for p in old}
    assert all(spec["parts"]["thread_body_parts"][int(p["part_id"].split(":")[1]) - 1]
               == notify_parts._THREAD_BODY_COLLECTED for p in cleanup)


def test_oversized_post_falls_back_to_the_thread(led, monkeypatch):
    monkeypatch.setattr(present_slack, "FACE_ORIGINAL_MAX", 20)
    _seed_thread(led, mids=(100, 101))
    with led.db:
        led.db.execute("UPDATE messages SET body_text='短い合成本文' WHERE message_id=100")
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=101", ("長い合成本文" * 10,))
    event = _intent(led, payload={"message_ids": [100, 101]})
    _dispatch(led, event, SLACK)
    root, reply = _post_specs(led, event)
    for spec in (root, reply):
        validate(spec)
    assert [p["original"] for p in root["parts"]["slack"]["face_posts"]] == ["短い合成本文"]
    assert not root["parts"]["thread_body_parts"]
    assert not reply["parts"].get("slack", {}).get("face_posts")
    names = {p["name"] for p in reply["parts"]["manifest"] if p["kind"] == "body_part"}
    assert names == {"m:101:header#1", "m:101#1"}
    assert "長い合成本文" * 10 in "".join(reply["parts"]["thread_body_parts"])
    assert _card(led)["kind"] == "thread"


def test_observed_zero_stamps_gives_an_empty_row_and_the_fold_follows_the_summary(led, monkeypatch):
    monkeypatch.setattr(notify_render, "thread_stamp_row", lambda *_a, **_k: "")
    _seed_thread(led, mids=(100,))
    with led.db:
        led.db.execute("UPDATE messages SET body_text='合成の元投稿' WHERE message_id=100")
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)
    spec = _spec(led)
    validate(spec)
    assert spec["parts"]["slack"]["face_posts"][0]["stamps"] == []
    blocks = slack_presentation.render_parts(spec["parts"], card_kind="thread")
    fold = next(i for i, b in enumerate(blocks) if b.get("type") == "container")
    assert blocks[fold - 1]["type"] == "section"


def test_block_cap_sends_the_rest_of_the_page_to_the_thread(led, monkeypatch):
    monkeypatch.setattr(present_slack, "BLOCK_MAX", 40)
    monkeypatch.setattr(notify_render, "thread_stamp_row", lambda *_a, **_k: "a\nb\nc\nd")
    mids = tuple(range(100, 108))
    _seed_thread(led, mids=mids)
    event = _intent(led, payload={"message_ids": list(mids)})
    _dispatch(led, event, SLACK)
    specs = _post_specs(led, event)
    assert len(specs) == len(mids)
    for spec in specs:
        validate(spec)
        posts = spec["parts"]["slack"]["face_posts"]
        assert len(posts) == 1
        blocks = slack_presentation.render_parts(spec["parts"], card_kind="thread")
        assert len(blocks) <= present_slack.face_block_count(spec["parts"], posts) <= present_slack.BLOCK_MAX
        assert not [p for p in spec["parts"]["manifest"] if p["kind"] == "body_part"]
