"""Per-transport presentation keys: a 💊 button beside each shown
medication post (Slack/Discord) and the Discord urgent accent."""
from __future__ import annotations

import copy

import pytest

from adapters.common import spec as spec_mod
from notify_testkit import NOW, _token_for, led, pinned_clock  # noqa: F401
from test_card_drug_views import _card, _click

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def test_discord_thread_card_puts_meds_beside_the_post(led):
    spec = _card(led)
    actions = spec["parts"]["discord"]["post_actions"]
    assert len(actions) == 1
    at, button = actions[0]["at"], actions[0]["button"]
    assert spec["parts"]["containers"][at].get("rule") is True
    assert button["id"] == "meds" and button["label"] == "💊 薬剤"
    assert button["token"] in spec_mod.token_map(spec)
    spec_mod.validate(spec)
    got = _click(led, spec, "meds", token=button["token"])
    assert (got["outcome"], got["action"]) == ("applied", "list")
    assert got["list"]["title"].startswith("💊 ")


def test_no_post_action_without_medication(led):
    spec = _card(led, meds=None)
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
    spec = copy.deepcopy(_card(led))
    mutate(spec)
    with pytest.raises(ValueError, match=reason):
        spec_mod.validate(spec)




def test_discord_layout_builds_a_section_with_the_post_button(led, monkeypatch):
    import sys
    from discord_testkit import _fake_discord
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    from adapters.discord.cards import build_view
    spec = _card(led)
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
    led.db.commit()
    _dispatch(led, _intent(led))
    spec = _spec(led)
    assert spec["parts"]["discord"]["accent"] == "urgent"
    spec_mod.validate(spec)
