"""Synthetic resource holds stay private across recovery and direct publication."""
import notify_parts
import json
from pathlib import Path

import pytest

import notify_cards as cards
from notify_testkit import (CFG, NOW, _begin, _dispatch, _intent, _latest_render,
                            _seed_thread, led as led)


@pytest.mark.parametrize("error_code", ["part_budget_exceeded", "thread_cleanup_budget_exceeded"])
def test_resource_hold_is_never_published_or_granted(led, monkeypatch, error_code):
    _seed_thread(led, mids=(100,))
    monkeypatch.setattr(notify_parts, "MAX_PARTS", 3)
    monkeypatch.setattr(notify_parts, "card_body_text", lambda *a, **k: ("合成", "x" * 4000))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    held = _latest_render(led)
    assert held["state"] == "held" and held["parts_state"] == "held"
    rows = led.db.execute("SELECT state,error_code FROM notification_render_parts WHERE delivery_id=?",
                          (held["delivery_id"],)).fetchall()
    assert rows and all(row["state"] == "held" and row["error_code"] == "part_budget_exceeded" for row in rows)
    if error_code != "part_budget_exceeded":
        with led.db:
            led.db.execute("UPDATE notification_render_parts SET error_code=? WHERE delivery_id=?",
                           (error_code, held["delivery_id"]))
    dirs = cards.notify_dirs(cards.data_root(led))
    path = Path(dirs["discord_render"]) / (held["delivery_id"] + ".json")
    assert not path.exists()
    assert cards.recover(led, CFG, {})["republished"] == 0
    assert cards.publish_specs(led.db, dirs, [json.loads(held["spec_json"])], NOW + 1) == []
    assert not path.exists()
    assert _latest_render(led)["spec_published"] == 0
    assert not _begin(led, held, n=1600)["granted"]
    assert _latest_render(led)["state"] == "held"
