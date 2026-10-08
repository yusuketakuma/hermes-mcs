"""Per-transport presentation keys: a 💊 button beside each shown
medication post (Slack/Discord) and the Discord urgent accent."""
from __future__ import annotations

import copy

import pytest

from adapters.common import spec as spec_mod
import json

from notify_testkit import (CFG, NOW, _dispatch, _intent, _llm_extract,  # noqa: F401
                            _seed_thread, _spec, _token_for, led, pinned_clock)
from test_card_drug_views import MED, _card, _click

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


NO_THREAD = {**CFG, "notify": {**CFG["notify"], "card_thread": False}}


def _thread_card(led, mids=(100, 101), meds_on=(101,), cfg=NO_THREAD):
    _seed_thread(led, mids=mids)
    for mid in meds_on:
        _llm_extract(led, mid, {"meds": [MED]})
    _dispatch(led, _intent(led, payload={"message_ids": list(mids)}), cfg)
    return _spec(led)


def test_discord_thread_body_keeps_drug_buttons_off_the_card_face(led):
    # card_thread on: the thread body carries 💊 (answers stay in the
    # thread); the card face gets no per-post buttons
    spec = _card(led)
    assert "post_actions" not in (spec["parts"].get("discord") or {})
    assert spec["parts"].get("thread_drug_actions") is True


def test_discord_without_thread_puts_meds_beside_the_post(led):
    spec = _thread_card(led)
    actions = spec["parts"]["discord"]["post_actions"]
    assert len(actions) == 1
    at, button = actions[0]["at"], actions[0]["button"]
    assert spec["parts"]["containers"][at].get("rule") is True
    assert button["id"] == "meds" and button["label"] == "💊 薬剤"
    assert button["token"] in spec_mod.token_map(spec)
    spec_mod.validate(spec)
    from notify_testkit import _deliver
    _deliver(led)
    got = _click(led, spec, "meds", token=button["token"], cfg=NO_THREAD)
    assert (got["outcome"], got["action"]) == ("applied", "list")
    assert got["list"]["title"].startswith("💊 ")


def test_each_button_belongs_to_its_own_post(led):
    spec = _thread_card(led, mids=(100, 101, 102), meds_on=(100, 102))
    actions = spec["parts"]["discord"]["post_actions"]
    got = []
    for a in actions:
        params = json.loads(led.db.execute(
            "SELECT params FROM notification_action_tokens WHERE token=?",
            (a["button"]["token"],)).fetchone()[0])
        posted = led.db.execute("SELECT posted_at FROM messages WHERE message_id=?",
                                (params["message_id"],)).fetchone()[0]
        line = spec["parts"]["containers"][a["at"]]["text"]
        assert line.startswith(f"{posted[5:10]} {posted[11:16]}")
        got.append(params["message_id"])
    assert got == [100, 102]


def test_many_medication_posts_stay_within_the_component_budget(led):
    mids = tuple(range(100, 109))
    spec = _thread_card(led, mids=mids, meds_on=mids)
    assert len(spec["parts"]["discord"].get("post_actions") or []) <= 5
    spec_mod.validate(spec)


def test_no_post_action_without_medication(led):
    spec = _thread_card(led, meds_on=())
    assert "post_actions" not in (spec["parts"].get("discord") or {})


@pytest.mark.parametrize("mutate,reason", [
    (lambda s: s["parts"].update(slack=s["parts"].pop("discord")), "bad_transport_parts"),
    (lambda s: s["parts"]["discord"]["post_actions"][0].update(at=0), "bad_post_action"),
    (lambda s: s["parts"]["discord"]["post_actions"][0]["button"].update(id="ack"),
     "bad_post_action_id"),
    (lambda s: s["parts"]["discord"].update(accent="red"), "bad_accent"),
    (lambda s: s["parts"]["discord"].update(extra=1), "unsupported_discord_parts_key"),
])
def test_transport_parts_are_validated(led, mutate, reason):
    spec = copy.deepcopy(_thread_card(led))
    mutate(spec)
    with pytest.raises(ValueError, match=reason):
        spec_mod.validate(spec)




def test_discord_layout_builds_a_section_with_the_post_button(led, monkeypatch):
    import sys
    from discord_testkit import _fake_discord
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    from adapters.discord.cards import build_view
    spec = _thread_card(led)
    container = build_view(spec).items[0]
    sections = [c for c in container.children if getattr(c, "accessory", None)]
    assert len(sections) == 1
    assert sections[0].accessory.custom_id.startswith("mcs:a:")
    assert container.accent_color != 0xED4245          # not urgent


def test_discord_urgent_card_gets_red_accent(led, monkeypatch):
    import sys
    from discord_testkit import _fake_discord
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    from adapters.discord.cards import build_view
    spec = copy.deepcopy(_card(led))
    spec["parts"].setdefault("discord", {})["accent"] = "urgent"
    spec_mod.validate(spec)
    assert build_view(spec).items[0].accent_color == 0xED4245


def test_runner_marks_an_urgent_discord_card(led):
    from notify_testkit import _dispatch, _intent, _llm_extract, _seed_thread, _spec
    _seed_thread(led)
    body = "本人が急変につき至急ご連絡ください。"
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", (body,))
    _llm_extract(led, 100, {"urgency": "high", "summary": "至急", "urgency_evidence": [body]})
    # a staff-typed 🚨 in another post is text, not an urgency verdict
    led.db.execute("UPDATE messages SET body_text='🚨 合成メモ' WHERE message_id=101")
    led.db.commit()
    _dispatch(led, _intent(led))
    spec = _spec(led)
    assert spec["parts"]["discord"]["accent"] == "urgent"
    spec_mod.validate(spec)


def test_quoted_alarm_emoji_does_not_turn_a_routine_card_red(led):
    from notify_testkit import _dispatch, _intent, _llm_extract, _seed_thread, _spec
    _seed_thread(led)
    led.db.execute("UPDATE messages SET body_text='🚨 と書かれた合成の通常連絡' WHERE message_id=100")
    _llm_extract(led, 100, {"urgency": "routine", "summary": "🚨 と書かれた通常連絡"})
    led.db.commit()
    _dispatch(led, _intent(led))
    assert "accent" not in (_spec(led)["parts"].get("discord") or {})
