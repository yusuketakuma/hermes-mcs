"""Digest keeps posts whose time is unknown (NULL posted_at_ts), matching
the notification age filter. Synthetic temp ledger only."""
from __future__ import annotations

import notify_digest
from notify_testkit import _patient, led
from test_notify_digest import ON, T, _seen, _text

__all__ = ["led"]


def test_null_posted_at_counted_with_max_age(led):
    _patient(led, 1)
    _seen(led, 100, T - 3600)
    _seen(led, 101, T - 1800)
    led.db.execute("UPDATE messages SET posted_at_ts=NULL WHERE message_id=101")
    led.db.commit()
    notify_digest.maybe_enqueue(led, {**ON, "notify_max_age_h": 48}, now=T)
    assert "新着 2件" in _text(led)
