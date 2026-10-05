"""LINE WORKS 未確認一覧 never shows the raw ``<@user>`` owner id. Synthetic only."""
from __future__ import annotations

import pytest

import notify_views
from notify_testkit import NOW, _delivered_card, led, pinned_clock

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


@pytest.mark.parametrize("names, label", [
    (None, "メンバー"), ({"someone@example.com": "合成 太郎"}, "合成 太郎")])
def test_lineworks_unacked_owner_uses_member_name(led, names, label):
    _delivered_card(led)
    led.db.execute("UPDATE notification_cards SET transport='lineworks'")
    led.db.execute("INSERT INTO notification_triage"
                   "(card_id,owner,state,last_actor,updated_at) VALUES"
                   "(1,'lineworks:team:someone@example.com','assigned','a',?)",
                   (NOW,))
    led.db.commit()
    text = notify_views.unacked_view(led.db, "lineworks", NOW, [1],
                                     member_names=names)["items"][0]["text"]
    assert f"（担当中: {label}）" in text and "<@" not in text
