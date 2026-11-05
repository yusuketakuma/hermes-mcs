"""Discord thread cards carry each post's stamp rows on the face while the
thread keeps every body (synthetic fixtures only)."""
import json

import pytest

import notify_render
from adapters.common.spec import validate
from notify_testkit import CFG, _dispatch, _intent, _latest_render, _seed_thread, led, pinned_clock

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock", "legacy_chat_format")
STAMP = "スタンプ 👀2 合成 太郎・合成 花子 · 観測 10-01 10:00"


def test_discord_face_posts_are_stamps_only_and_the_thread_keeps_the_bodies(led, monkeypatch):
    monkeypatch.setattr(notify_render, "thread_stamp_row",
                        lambda _db, mid, *_a, **_k: STAMP if mid == 100 else "")
    _seed_thread(led, mids=(100, 101))
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}), CFG)
    spec = validate(json.loads(_latest_render(led)["spec_json"]))
    posts = spec["parts"]["discord"]["face_posts"]
    assert [p["stamps"] for p in posts] == [[STAMP]]        # empty rows are not sent
    names = {p["name"] for p in spec["parts"]["manifest"] if p["kind"] == "body_part"}
    assert {"m:100#1", "m:101#1"} <= names                   # no body leaves the thread
