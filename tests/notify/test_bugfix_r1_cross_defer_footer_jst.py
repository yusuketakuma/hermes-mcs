"""The legacy 保留中 footer shows defer_until in JST, not host local time."""
import json
import time
from datetime import datetime

import notify_cards
from mcs_queries import JST
from notify_testkit import CFG, NOW, _deliver, _dispatch, _intent, \
    _latest_render, _seed_thread, led

__all__ = ["led"]


def test_deferred_footer_uses_jst_on_utc_host(led, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        _seed_thread(led)
        _dispatch(led, _intent(led))
        _deliver(led)
        until = NOW + 3600
        led.db.execute(
            "INSERT INTO notification_triage(card_id,owner,defer_until,"
            "state,revision,last_actor,updated_at) "
            "VALUES(1,NULL,?,'deferred',1,'discord:1',?)", (until, NOW))
        led.db.commit()
        notify_cards.sweep(led, CFG, now=NOW + 1)
        text = json.dumps(_latest_render(led)["spec_json"], ensure_ascii=False)
    finally:
        monkeypatch.undo()
        time.tzset()
    jst = datetime.fromtimestamp(until, JST).strftime("%m-%d %H:%M")
    assert f"保留中（〜{jst}）" in text
