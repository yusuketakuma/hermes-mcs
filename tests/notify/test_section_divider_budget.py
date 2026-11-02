"""Discord's versioned divider reserve keeps each synthetic summary reachable."""
import json

import notify_render
import notify_parts
import present_slack
from adapters.common import spec as spec_module
from notify_testkit import CFG, _card, _dispatch, _intent, _seed_thread, led
from slack_testkit import SLACK

__all__ = ["led"]


def test_near_limit_summaries_are_paged_with_divider_budget(led, monkeypatch):
    mids = list(range(100, 108))
    _seed_thread(led, mids=tuple(mids))
    summaries = {mid: f"合成要約{mid}:" + "症状" * 500 for mid in mids}
    monkeypatch.setattr(notify_render, "_summary_block",
                        lambda db, mid, **kwargs: {"type": "text", "text": summaries[mid]})
    assert _dispatch(led, _intent(led, payload={"message_ids": mids}), CFG)["dispatched"]
    card = _card(led)
    card["layout"] = 3
    first = notify_render.card_content(led.db, card, cfg=CFG)
    assert first["pages"] > 1
    reached = []
    for page in range(first["pages"]):
        card["ui_state"] = json.dumps({"page": page})
        content = notify_render.card_content(led.db, card, cfg=CFG)
        blocks = content["containers"]
        used = spec_module._containers_cost(blocks)[1] + spec_module._footer_cost(content["footer"])[1]
        used += 14 * sum(bool(c.get("rule")) and c.get("type") != "meta" for c in blocks)
        assert used <= spec_module.MAX_TOTAL_TEXT
        text = "\n".join(c.get("text", "") for c in blocks)
        for mid in content["shown"]:
            assert summaries[mid] in text
        reached.extend(content["shown"])
    assert reached == mids


def test_legacy_provider_generation_keeps_its_original_page_budget(led, monkeypatch):
    mids = list(range(100, 104))
    _seed_thread(led, mids=tuple(mids))
    monkeypatch.setattr(notify_render, "_summary_block",
                        lambda db, mid, **kwargs: {"type": "text", "text": f"{mid}:" + "a" * 761})
    assert _dispatch(led, _intent(led, payload={"message_ids": mids}), CFG)["dispatched"]
    card = _card(led)
    card["layout"] = 3
    old = notify_render.card_content(led.db, card, cfg=CFG, _provider_version=1)
    current = notify_render.card_content(led.db, card, cfg=CFG)
    assert old["pages"] == 1 and old["shown"] == mids
    assert current["pages"] == 2


def test_separate_divider_over_part_limit_holds_the_whole_render(led, monkeypatch):
    monkeypatch.setattr(present_slack, "FACE_ORIGINAL_MAX", 20)
    monkeypatch.setattr(notify_parts, "MAX_PARTS", 4)
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", ("合成本文" * 400,))
    header = "↳ " + "x" * 1897
    monkeypatch.setattr(notify_parts, "card_body_text", lambda *a, **kw: (
        "合成", header + "\n" + notify_render.SECTION_RULE + "\n合成原文"))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)
    render = led.db.execute("SELECT delivery_id,state FROM notification_renders ORDER BY render_rev DESC LIMIT 1").fetchone()
    assert render["state"] == "held"
    parts = led.db.execute("SELECT state,error_code FROM notification_render_parts WHERE delivery_id=?",
                           (render["delivery_id"],)).fetchall()
    assert parts and all(row["state"] == "held" and row["error_code"] == "part_budget_exceeded" for row in parts)
