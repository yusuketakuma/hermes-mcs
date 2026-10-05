"""The 🗂 button passes configured LINE WORKS user_names into unacked_view."""
import pytest

import notify_cards
from notify_testkit import NOW, _delivered_card, led, pinned_clock

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def test_unacked_button_uses_configured_lineworks_name(led):
    _delivered_card(led)
    led.db.execute("UPDATE notification_cards SET transport='lineworks'")
    led.db.execute("INSERT INTO notification_triage"
                   "(card_id,owner,state,last_actor,updated_at) VALUES"
                   "(1,'lineworks:team:someone@example.com','assigned','a',?)",
                   (NOW,))
    led.db.commit()
    cfg = {"notify": {"lineworks": {
        "user_names": {"someone@example.com": "合成 太郎"}}}}
    out = notify_cards._act_view(led.db, {}, {"transport": "lineworks"},
                                 "unacked", {"projects": [1]}, NOW, cfg)
    assert "（担当中: 合成 太郎）" in out["list"]["items"][0]["text"]
