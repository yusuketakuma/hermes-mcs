"""Lossless whitespace handling and explicit holds for impossible empty posts."""
import json

import pytest

import notify_parts
from notify_testkit import CFG, _card, _dispatch, _intent, _latest_render, _seed_thread, led

__all__ = ["led"]


@pytest.mark.parametrize("text", [
    "a" + " " * 10 + "b",
    "a" + "\n" * 30 + "b",
    "a" + "　" * 30,
    " " * 30 + "b",
    " " * 30,
    "ab\n\n\nc",
])
def test_blank_runs_never_discard_source_characters(text):
    chunks = notify_parts.split_body_chunks(text, limit=4)
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 4 for chunk in chunks)


def test_impossible_blank_run_requires_hold_and_keeps_original_parts(led):
    _seed_thread(led)
    text = "合成本文" + " " * 6000 + "末尾"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", (text,))
    led.db.commit()
    _dispatch(led, _intent(led))
    spec = json.loads(_latest_render(led)["spec_json"])
    parts, hold = notify_parts.build_manifest(
        led.db, _card(led), spec, {"shown": [100, 101]}, True, cfg=CFG)
    assert hold == "blank_part_unrepresentable"
    assert text in "".join(parts["thread_body_parts"])
    assert any(not chunk.strip() for chunk in parts["thread_body_parts"])


def test_representable_blank_run_still_avoids_empty_posts():
    text = "ab" + " " * 5 + "cd"
    chunks = notify_parts.split_body_chunks(text, limit=4)
    assert "".join(chunks) == text
    assert all(chunk.strip() for chunk in chunks)
