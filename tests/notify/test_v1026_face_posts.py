"""Slack thread cards carry each post's stamps and folded original on the card face;
attachments stay in the thread, old thread chunks are rewritten on update, and an
oversized post falls back to the thread (synthetic fixtures only)."""
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


def test_face_groups_stamps_below_all_folded_originals(led, monkeypatch):
    monkeypatch.setattr(notify_render, "thread_stamp_row", lambda *_a, **_k: STAMP)
    _seed_thread(led, mids=(100, 101))
    with led.db:
        led.db.execute("UPDATE messages SET body_text='合成の元投稿' WHERE message_id=100")
        led.db.execute("UPDATE messages SET body_text='合成の返信本文' WHERE message_id=101")
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}), SLACK)
    spec = _spec(led)
    validate(spec)
    posts = spec["parts"]["slack"]["face_posts"]
    assert [p["original"] for p in posts] == ["合成の元投稿", "合成の返信本文"]
    assert all(p["stamps"] == [STAMP] for p in posts)
    assert spec["parts"]["thread_body_parts"] == []
    assert not [p for p in spec["parts"]["manifest"] if p["kind"] == "body_part"]
    blocks = slack_presentation.render_parts(spec["parts"], card_kind="thread")
    folds = [i for i, b in enumerate(blocks) if b.get("type") == "container"]
    assert [b["title"]["text"] for b in (blocks[i] for i in folds)] == ["原文（6字）", "原文（7字）"]
    stamp_at = [i for i, b in enumerate(blocks) if b["type"] == "context"
                and STAMP in json.dumps(b, ensure_ascii=False)]
    assert len(stamp_at) == 2 and min(stamp_at) > max(folds)
    assert all(spec["parts"]["containers"][p["at"]]["text"]
               in blocks[i]["elements"][0]["text"] for p, i in zip(posts, stamp_at))
    assert blocks[folds[0]]["child_blocks"][0]["text"]["text"] == "合成の元投稿"
    assert len(blocks) <= present_slack.face_block_count(spec["parts"], posts) <= present_slack.BLOCK_MAX
    # the reply's own line and summary sit between the two folds
    between = blocks[folds[0] + 1:folds[1] - 1]
    assert [b["type"] for b in between][:2] == ["divider", "context"]


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
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}), SLACK)
    spec = _spec(led)
    validate(spec)
    assert [p["original"] for p in spec["parts"]["slack"]["face_posts"]] == ["短い合成本文"]
    names = {p["name"] for p in spec["parts"]["manifest"] if p["kind"] == "body_part"}
    assert names == {"m:101:header#1", "m:101#1"}
    assert "長い合成本文" in "".join(spec["parts"]["thread_body_parts"])
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
    _dispatch(led, _intent(led, payload={"message_ids": list(mids)}), SLACK)
    spec = _spec(led)
    validate(spec)
    posts = spec["parts"]["slack"]["face_posts"]
    assert 0 < len(posts) < len(mids)
    blocks = slack_presentation.render_parts(spec["parts"], card_kind="thread")
    assert len(blocks) <= present_slack.face_block_count(spec["parts"], posts) <= present_slack.BLOCK_MAX
    names = {p["name"] for p in spec["parts"]["manifest"] if p["kind"] == "body_part"}
    assert names == {f"m:{m}#1" for m in mids[len(posts):]} | {f"m:{m}:header#1" for m in mids[len(posts):]}
