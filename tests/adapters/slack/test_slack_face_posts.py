"""End to end through the Slack worker: a thread card posts its folded
original on the face and nothing but attachments in the thread."""
import asyncio
import json

import pytest

from notify_testkit import _dispatch, _intent, _seed_thread
from slack_testkit import SLACK, _granted_card, _mkworld, isolated_slack_ledger as led

__all__ = ["led"]


@pytest.mark.face_posts
def test_card_face_carries_the_fold_and_the_thread_stays_empty(led):
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE messages SET body_text='合成の元投稿' WHERE message_id=100")
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    card = next(kw for kind, kw in w.client.calls if kind == "create" and "thread_ts" not in kw)
    folds = [b for b in card["blocks"] if b.get("type") == "container"]
    assert [b["title"]["text"] for b in folds] == ["原文（6字）", "原文（2字）"]
    assert folds[0]["child_blocks"][0]["text"]["text"] == "合成の元投稿"
    assert "原文" not in json.dumps(card.get("text", ""))
    assert w.client.thread_posts == []
