"""Urgency escalation resolves 'our' organizations from signals.self_organizations."""
import notify_urgent
from notify_testkit import _msg, led
from test_notify_urgent import _base, _fact, world

__all__ = ["led", "world"]


def test_documented_self_organizations_count_as_own_post(world):
    store, cfg, _, _ = world
    cfg = {**cfg, "signals": {**cfg["signals"], "self_organizations": ["SYNTH薬局"]}}
    _base(store)
    _fact(store)
    with store.db:
        store.db.execute("DELETE FROM artifacts WHERE kind='station_staff_v1'")
        _msg(store, 101, ts=int(store.db.execute(
            "SELECT posted_at_ts FROM messages WHERE message_id=100").fetchone()[0]) + 60,
            org="SYNTH薬局")
        store.db.execute("UPDATE messages SET sender_id=99 WHERE message_id=101")
    result = notify_urgent.maybe_enqueue(store, cfg)
    assert result["queued"] == 0
    assert result["deferred"].get("own_post_observed") == 1, result
