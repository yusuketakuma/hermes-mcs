"""Fair sweep rotation cannot make old unacknowledged content recent again."""
import pytest

import notify_cards
import notify_views
from notify_testkit import CFG, NOW, _delivered_card, led, pinned_clock

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def _age_content(led, *, manifest_time=None):
    old = NOW - 40 * 86400
    with led.db:
        led.db.execute("UPDATE notification_cards SET created_at=?,updated_at=?", (old, old))
        led.db.execute("UPDATE notification_view_manifests SET created_at=?",
                       (old if manifest_time is None else manifest_time,))


def test_unchanged_sweep_rotates_the_card_without_reviving_old_content(led):
    _delivered_card(led)
    _age_content(led)
    before = led.db.execute("SELECT desired_render_rev,source_generation,presentation_generation "
                            "FROM notification_cards").fetchone()[:]
    assert notify_views.unacked_view(led.db, "discord", NOW, [1])["items"] == []
    notify_cards.sweep(led, CFG, now=NOW)
    after = led.db.execute("SELECT desired_render_rev,source_generation,presentation_generation "
                           "FROM notification_cards").fetchone()[:]
    assert after == before
    assert led.db.execute("SELECT updated_at FROM notification_cards").fetchone()[0] == NOW
    assert notify_views.unacked_view(led.db, "discord", NOW, [1])["items"] == []


def test_a_recent_content_manifest_keeps_an_older_card_in_the_window(led):
    _delivered_card(led)
    _age_content(led, manifest_time=NOW)
    assert len(notify_views.unacked_view(led.db, "discord", NOW, [1])["items"]) == 1


def test_recent_creation_without_a_recent_manifest_remains_visible(led):
    _delivered_card(led)
    _age_content(led)
    with led.db:
        led.db.execute("UPDATE notification_cards SET created_at=?", (NOW,))
    assert len(notify_views.unacked_view(led.db, "discord", NOW, [1])["items"]) == 1


# LINE WORKS 未確認一覧 never shows the raw ``<@user>`` owner id. Synthetic only.
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


# The 🗂 button passes configured LINE WORKS user_names into unacked_view.
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
