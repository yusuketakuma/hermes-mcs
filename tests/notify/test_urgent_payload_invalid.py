"""A corrupted urgent re-check notice payload is a durable, final denial:
the runner records it once per begin, unlinks the dead spec and never
raises; a corrupted row never breaks other notices. Synthetic ledger,
stub transport only."""
import json
import os

import pytest

import notify_cards
import notify_urgent
from notify_testkit import NOW, _begin
from test_alert_thread_hotfix import _alert
from test_notify_urgent import led, world

__all__ = ["led", "world"]

BROKEN = ['{"message_id": 100}', "{not json", "[]", '"text"', "null"]


def _dispatched(world):
    store, cfg, event, _ = _alert(world)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    return store, cfg, event, notify_cards._notice_renders(store.db, event["event_id"])[0]


def _spec_path(store, render):
    dirs = notify_cards.notify_dirs(notify_cards.data_root(store))
    return os.path.join(dirs[render["transport"] + "_render"], f"{render['delivery_id']}.json")


def _corrupt(store, event, payload):
    with store.db:
        store.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?",
                         (payload, event["event_id"]))


@pytest.mark.parametrize("payload", BROKEN)
def test_corrupted_payload_is_a_final_durable_denial(world, payload):
    store, cfg, event, render = _dispatched(world)
    assert os.path.exists(_spec_path(store, render))
    _corrupt(store, event, payload)
    first = _begin(store, render, n=1100, cfg=cfg)
    assert first["granted"] is False and first["error"] == "denied_urgent_payload_invalid"
    assert store.db.execute("SELECT state,error_code FROM notification_delivery_attempts "
                            "WHERE attempt_id=?", (f"{1100:016x}",)).fetchone()[:] \
        == ("not_sent", "denied_urgent_payload_invalid")
    assert not os.path.exists(_spec_path(store, render))          # never re-claimed
    assert _begin(store, render, n=1100, cfg=cfg) == first          # same command: same answer
    again = _begin(store, render, n=1200, cfg=cfg)                   # a stale claim never raises
    assert again["granted"] is False


@pytest.mark.parametrize("payload", BROKEN)
def test_shared_check_answers_instead_of_raising(world, payload):
    store, cfg, event, _ = _alert(world)
    _corrupt(store, event, payload)
    row = store.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                           (event["event_id"],)).fetchone()
    assert notify_urgent.check_delivery(store, cfg, row) == {
        "ok": False, "reason": "urgent_payload_invalid"}
    # the notice dispatch uses the same answer and never sends it
    out = notify_cards.dispatch_intent(store, dict(row), cfg)
    assert not out.get("dispatched")


def test_source_change_keeps_its_transient_denial_and_spec(world):
    store, cfg, event, render = _dispatched(world)
    with store.db:
        store.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
    result = _begin(store, render, n=1100, cfg=cfg)
    assert result["error"] == "denied_urgent_source_changed"
    assert os.path.exists(_spec_path(store, render))


def test_a_granted_send_is_never_turned_into_not_sent(world):
    store, cfg, event, render = _dispatched(world)
    granted = _begin(store, render, n=1100, cfg=cfg)
    assert granted["granted"] is True
    _corrupt(store, event, "{not json")
    assert _begin(store, render, n=1100, cfg=cfg) == granted         # replay of the grant
    assert store.db.execute("SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                            (f"{1100:016x}",)).fetchone()[0] == "granted"


def _history_row(store, payload, *, project_id=1, at):
    with store.db:
        store.db.execute(
            "INSERT INTO notify_outbox(kind,project_id,payload,state,next_try,created_at,"
            "updated_at,route) VALUES('urgent_notice',?,?,'accepted',NULL,?,?,'interactive')",
            (project_id, payload, at, at))


UNREADABLE = ["[]", "{not json", '"text"', '{"message_id": 1' + "0" * 5000 + "}",
              '{"message_id": 100, "hash": "x", "stage": ["E1"], "shadow": false}',
              '{"message_id": 100, "stage": {"E": 1}, "shadow": "yes"}']


def test_a_corrupted_history_row_in_the_room_holds_another_notice_without_raising(world):
    store, cfg, event, render = _dispatched(world)
    _history_row(store, "[]", at=NOW)
    result = _begin(store, render, n=1100, cfg=cfg)
    assert result["granted"] is False and result["error"] == "denied_urgent_source_changed"
    assert os.path.exists(_spec_path(store, render))               # transient: kept


def _candidate(world, **policy):
    from test_notify_urgent import _base, _fact
    store, cfg, _, _ = world
    cfg["urgency_escalation"].update(policy)
    _base(store)
    _fact(store)
    return store, cfg


@pytest.mark.parametrize("payload", UNREADABLE)
def test_unreadable_history_still_counts_toward_the_daily_cap(world, payload):
    store, cfg = _candidate(world, max_per_day=1)
    _history_row(store, payload, project_id=None, at=NOW - 60)
    out = notify_urgent.maybe_enqueue(store, cfg, now=NOW)
    assert out["queued"] == 0 and out["deferred"].get("daily_cap") == 1


@pytest.mark.parametrize("payload", UNREADABLE)
def test_unreadable_history_still_holds_the_room_cooldown(world, payload):
    store, cfg = _candidate(world, room_cooldown_min=5)
    _history_row(store, payload, at=NOW - 60)
    out = notify_urgent.maybe_enqueue(store, cfg, now=NOW)
    assert out["queued"] == 0 and out["deferred"].get("room_cooldown") == 1


def test_a_known_live_row_is_not_counted_in_shadow_mode_but_an_unknown_one_is(world):
    store, cfg = _candidate(world, mode="shadow", room_cooldown_min=5)
    _history_row(store, '{"message_id": 999, "shadow": false}', at=NOW - 60)
    assert notify_urgent.maybe_enqueue(store, cfg, now=NOW)["suppressed"] == 1
    store.db.execute("DELETE FROM notify_outbox WHERE kind='urgent_notice'")
    store.db.commit()
    _history_row(store, "{not json", at=NOW - 60)
    out = notify_urgent.maybe_enqueue(store, cfg, now=NOW)
    assert out["suppressed"] == 0 and out["deferred"].get("room_cooldown") == 1


def test_without_unreadable_history_the_same_candidate_is_queued(world):
    store, cfg = _candidate(world, max_per_day=1, room_cooldown_min=5)
    assert notify_urgent.maybe_enqueue(store, cfg, now=NOW)["queued"] == 1


def _mutated(event, **change):
    payload = json.loads(event["payload"])
    for key, value in change.items():
        if value is _DROP:
            payload.pop(key)
        else:
            payload[key] = value
    return json.dumps(payload)


_DROP = object()


@pytest.mark.parametrize("change", [
    {"stage": _DROP}, {"stage": ["E1"]}, {"stage": 1}, {"stage": "E3"}, {"stage": "E2:0"},
    {"stage": "E2:x"}, {"stage": "e1"}, {"stage": "E2:1 "},
    {"base_event_id": _DROP}, {"base_event_id": "1"}, {"base_event_id": 0},
    {"base_event_id": -4}, {"base_event_id": True}, {"base_event_id": [1]},
])
def test_a_malformed_stage_or_base_is_a_final_denial(world, change):
    store, cfg, event, render = _dispatched(world)
    _corrupt(store, event, _mutated(event, **change))
    result = _begin(store, render, n=1100, cfg=cfg)
    assert result["error"] == "denied_urgent_payload_invalid"
    assert not os.path.exists(_spec_path(store, render))


def test_a_well_formed_but_different_stage_stays_a_transient_source_change(world):
    store, cfg, event, render = _dispatched(world)
    payload = json.loads(event["payload"])
    other = "E2:2" if payload["stage"] != "E2:2" else "E2:1"
    _corrupt(store, event, _mutated(event, stage=other, base_event_id=payload["base_event_id"] + 1000))
    result = _begin(store, render, n=1100, cfg=cfg)
    assert result["error"] == "denied_urgent_source_changed"
    assert os.path.exists(_spec_path(store, render))


def test_an_unused_decoration_field_is_not_required(world):
    store, cfg, event, render = _dispatched(world)
    _corrupt(store, event, _mutated(event, urgency_artifact_id=_DROP))
    assert _begin(store, render, n=1100, cfg=cfg)["granted"] is True
