"""A card_resolve against a restore hold whose render is gone is authorized
only for the projects the stored hold proves — its card (signal/digest
cards through their coverage) and its held events' outbox rows — never
for an empty or request-supplied project set. Synthetic ledger only."""
import json
from types import SimpleNamespace

import pytest

import hermes_plugin
from notify_testkit import _signal_row
from test_notify_reconcile import CFG, world
from test_rebind_revoked_card import _lost_render_hold

__all__ = ["world"]

SCOPE = CFG["notify"]["discord"]
IDENTITY = {"transport": "discord", "chat_id": SCOPE["channel_id"],
            "profile": SCOPE["profile"], "scope_id": SCOPE["guild_id"]}


def _check(world, render, *, allowed=(1,)):
    settings = {"project_ids": set(allowed), "application_id": SCOPE["application_id"]}
    payload = {"delivery_id": render["delivery_id"]}
    return hermes_plugin._card_resolution(SimpleNamespace(db=world.led.db), settings,
                                          dict(IDENTITY), payload)


def _hold(world, **change):
    sets = ",".join(f"{k}=?" for k in change)
    with world.led.db:
        world.led.db.execute(f"UPDATE notification_restore_holds SET {sets}", tuple(change.values()))


def _event(world, project_id):
    with world.led.db:
        return world.led.outbox_add_tx("new_messages", project_id, {"message_ids": [1]})


def test_the_held_card_project_in_scope_resolves(world):
    render, _ = _lost_render_hold(world)
    assert _check(world, render) is None


def test_a_cardless_hold_is_checked_against_its_events(world):
    render, _ = _lost_render_hold(world)
    other = _event(world, 2)
    _hold(world, card_id=None, events_json=json.dumps([other]))
    assert _check(world, render) == "project_not_allowed"
    assert _check(world, render, allowed=(1, 2)) is None


def test_a_hold_that_proves_no_project_is_refused(world):
    render, _ = _lost_render_hold(world)
    _hold(world, card_id=None, events_json="[]")
    assert _check(world, render) == "project_unknown"


@pytest.mark.parametrize("keys,allowed,expected", [
    (["s-in"], (1,), None),                       # in-scope signal coverage
    (["s-in", "s-out"], (1,), "project_not_allowed"),
])
def test_a_projectless_card_is_checked_through_its_coverage(world, keys, allowed, expected):
    render, _ = _lost_render_hold(world)
    card_id = render["card_id"]
    _signal_row(world.led, "s-in", pid=1)
    _signal_row(world.led, "s-out", pid=2)
    with world.led.db:
        world.led.db.execute("UPDATE notification_cards SET kind='digest',project_id=NULL "
                             "WHERE card_id=?", (card_id,))
        world.led.db.execute("UPDATE notification_intent_cards SET coverage=? WHERE card_id=?",
                             (json.dumps(keys), card_id))
    _hold(world, events_json="[]")
    assert _check(world, render, allowed=allowed) == expected


@pytest.mark.parametrize("scope_json", ["{not json", "[]"])
def test_a_corrupt_hold_scope_is_refused_not_raised(world, scope_json):
    render, _ = _lost_render_hold(world)
    _hold(world, scope_json=scope_json)
    assert _check(world, render) == "hold_scope_corrupt"


BAD_EVENTS = ['["{other}"]', '{{"{other}": 1}}', '[true]', '[0]', '[-1]', '[{other}.0]',
              '[18446744073709551616]', '"{other}"', '{{not json', '[{other}, "x"]']


@pytest.mark.parametrize("events", BAD_EVENTS)
def test_held_events_must_be_positive_ids_for_authorization(world, events):
    render, _ = _lost_render_hold(world)
    other = _event(world, 2)
    _hold(world, events_json=events.format(other=other))
    assert _check(world, render, allowed=(1,)) == "hold_events_corrupt"


@pytest.mark.parametrize("events", BAD_EVENTS)
def test_a_corrupt_hold_is_rejected_before_any_write(world, events):
    import uuid
    import notify_transport
    render, aid = _lost_render_hold(world)
    other = _event(world, 2)
    with world.led.db:
        world.led.db.execute("UPDATE notify_outbox SET state='failed',next_try=NULL WHERE event_id=?",
                             (other,))
    _hold(world, events_json=events.format(other=other))
    before = world.led.db.execute("SELECT event_id,state,next_try FROM notify_outbox "
                                  "ORDER BY event_id").fetchall()
    card = dict(world.card(render["card_id"]))
    out = notify_transport.apply_card_resolve(world.led, {
        "version": 1, "cmd": "ops.card_resolve", "command_id": str(uuid.uuid4()),
        "actor": "op-user", "human_confirmed": True, "reason": "合成の配送結果確認",
        "delivery_id": render["delivery_id"], "attempt_id": aid, "result": "mark_not_sent",
        **SCOPE, "message_id": "synthetic-rebound",
        "evidence": {"method": "synthetic", "ref": "synthetic-card", "worker_stopped": True,
                     "proof": "remote_absent"}}, CFG)
    assert out["outcome"] == "rejected" and out["error"] == "hold_events_corrupt"
    assert world.led.db.execute("SELECT event_id,state,next_try FROM notify_outbox "
                                "ORDER BY event_id").fetchall() == before
    assert dict(world.card(render["card_id"])) == card
    assert world.led.db.execute("SELECT count(*) FROM notification_restore_holds "
                                "WHERE released_at IS NULL").fetchone()[0] == 1


def test_an_empty_event_list_with_a_card_still_resolves(world):
    render, _ = _lost_render_hold(world)
    _hold(world, events_json="[]")
    assert _check(world, render) is None
