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
