"""Slack face admission uses a conservative recursive block budget; overflow stays whole."""
import json

import pytest

from adapters.slack import cards
import present_slack
from notify_testkit import _dispatch, _intent, _latest_render, _seed_thread, led, pinned_clock
from slack_card_testkit import _spec
from slack_testkit import SLACK

__all__ = ["led", "pinned_clock"]


def _block_count(blocks):
    return sum(1 + _block_count(b.get("child_blocks", [])) for b in blocks)


@pytest.mark.parametrize("length", [3000, 3001, 30000])
def test_eight_posts_overflow_to_thread_without_losing_originals(led, pinned_clock, length):
    mids = tuple(range(100, 108))
    _seed_thread(led, mids=mids)
    originals = {mid: (f"合成{mid}:" + "検" * length)[:length] for mid in mids}
    with led.db:
        led.db.executemany("UPDATE messages SET body_text=? WHERE message_id=?",
                           [(text, mid) for mid, text in originals.items()])
    assert _dispatch(led, _intent(led, payload={"message_ids": list(mids)}), SLACK)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    _text, blocks = cards.render(spec)
    face = spec["parts"].get("slack", {}).get("face_posts", [])
    assert 0 < len(face) < len(mids)
    assert _block_count(blocks) <= present_slack.face_block_count(spec["parts"], face) <= 50
    on_face = {p["original"] for p in face}
    body_parts = [(p["name"], text) for p, text in zip(
        [p for p in spec["parts"]["manifest"] if p["kind"] == "body_part"],
        spec["parts"]["thread_body_parts"], strict=True)]
    for mid, original in originals.items():
        in_thread = "".join(text for name, text in body_parts if name.startswith(f"m:{mid}#"))
        assert (original in on_face and not in_thread) or (original not in on_face and in_thread == original)


def test_recursive_budget_accepts_50_but_counts_the_51st_block():
    spec = _spec()
    parts = spec["parts"]
    # Twenty one-line post boundaries cost 39 top-level blocks. The
    # original's container + ten children fill the remaining 11 slots.
    parts["containers"] = [{"type": "text", "text": f"合成{i}", "rule": True} for i in range(20)]
    parts["footer"] = []
    parts["action_rows"] = []
    post = {"at": 0, "stamps": [], "original": "検" * 30000}
    parts["slack"] = {"face_posts": [post]}
    assert _block_count(cards.render(spec)[1]) == present_slack.face_block_count(parts, [post]) == 50
    # One stamp context pushes the same candidate beyond the application's
    # conservative budget; the runner's existing admission gate rejects it.
    post["stamps"] = ["スタンプ 未取得"]
    assert _block_count(cards.render(spec)[1]) == present_slack.face_block_count(parts, [post]) == 51
