"""Synthetic signal fallback routes retain their proven card and companion thread."""
import itertools
import json

import pytest

import notify_cards
import notify_transport
from adapters.common.spec import validate
from adapters.slack.cards import validate as validate_slack
from notify_testkit import CFG, NOW, _dispatch, _intent, _latest_render, _seed_thread, _signal_row, _uuid, led
from slack_testkit import SLACK
from test_signal_thread_hotfix import delivered_source

__all__ = ["led"]
LATE = NOW + notify_cards.SIGNAL_SOURCE_HOLD_MAX_S + 60
SERIAL = itertools.count(45000)


def _identity(render, cfg):
    return {"version": notify_cards.TRANSPORT_VERSIONS[notify_cards.active_transport(cfg)],
            **notify_cards.delivery_scope(cfg), "delivery_id": render["delivery_id"],
            "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
            "route_epoch": render["route_epoch"], "correlation": render["correlation"]}


def _begin(store, render, cfg, now=LATE):
    serial = next(SERIAL)
    attempt = f"{serial:016x}"
    result = notify_transport.apply_transport_begin(store, {
        **_identity(render, cfg), "op": "transport_begin", "command_id": _uuid(serial),
        "attempt_id": attempt, "worker_id": "bb" * 8}, cfg, now=now)
    assert result["granted"], result
    return attempt


def _part(store, render, cfg, part, remote, now=LATE):
    result = notify_transport.apply_part_receipt(store, {
        **_identity(render, cfg), "op": "part_receipt", "command_id": _uuid(next(SERIAL)),
        "attempt_id": "p:" + render["delivery_id"].replace("-", "") + ":" + part,
        "part_id": part, "result": "delivered", "remote_id": remote}, cfg, now=now)
    assert result["applied"], result


def _land(store, render, cfg, *, finish=True, result="delivered", now=LATE):
    spec = json.loads(render["spec_json"])
    attempt = _begin(store, render, cfg, now)
    mid = spec["delivery"].get("message_id") or (
        f"1790000000.{next(SERIAL):06d}" if cfg is SLACK else f"synthetic-card-{next(SERIAL)}")
    receipt = notify_transport.apply_transport_receipt(store, {
        **_identity(render, cfg), "op": "transport_receipt", "command_id": _uuid(next(SERIAL)),
        "attempt_id": attempt, "result": result, "message_id": mid}, cfg, now=now)
    assert receipt["applied"], receipt
    if result != "delivered":
        return mid, None
    thread = spec["delivery"].get("thread_id") or (mid if cfg is SLACK else f"synthetic-thread-{next(SERIAL)}")
    _part(store, render, cfg, "thread", thread, now)
    if finish:
        for part in spec["parts"]["manifest"]:
            if part["kind"] == "body_part":
                _part(store, render, cfg, part["part_id"], f"synthetic-body-{next(SERIAL)}", now)
    return mid, thread


def _fallback(store, cfg):
    _seed_thread(store)
    _signal_row(store, "synthetic-transition", stype="transition_reconciliation", mids=[101])
    event = _intent(store, "signal", payload={"signal_keys": ["synthetic-transition"], "project_id": 1})
    _dispatch(store, event, cfg)
    assert _latest_render(store) is None
    assert _dispatch(store, event, cfg, now=LATE)["dispatched"]
    return _latest_render(store)


def _card(store, card_id=1):
    return dict(store.db.execute("SELECT * FROM notification_cards WHERE card_id=?", (card_id,)).fetchone())


@pytest.mark.parametrize("cfg", [CFG, SLACK], ids=["discord", "slack"])
@pytest.mark.parametrize("gc", [False, True], ids=["full-spec", "after-gc"])
def test_delivered_fallback_is_stable_and_updates_its_own_thread(led, cfg, gc):
    created = _fallback(led, cfg)
    mid, thread = _land(led, created, cfg)
    expires = list(led.db.execute("SELECT token,expires_at FROM notification_action_tokens ORDER BY token"))
    if gc:
        notify_cards.gc(led, cfg, now=LATE + 1)
        witness = json.loads(_latest_render(led)["spec_json"])
        assert witness == {"signal_thread_route": "card", "thread_id": None}
        assert "synthetic-transition" not in json.dumps(witness)
    for tick in (LATE + 2, LATE + 62, LATE + 122):
        assert notify_cards.sweep(led, cfg, now=tick)["updated"] == 0
    assert (_card(led)["message_id"], _card(led)["thread_id"]) == (mid, thread)
    assert _latest_render(led)["render_rev"] == 1
    assert list(led.db.execute("SELECT token,expires_at FROM notification_action_tokens ORDER BY token")) == expires
    with led.db:
        specs = []
        notify_cards._issue_render(led.db, 1, cfg, LATE + 180, specs, force=True)
    spec = specs[0]
    assert spec["op"] == "update" and spec["delivery"]["message_id"] == mid
    assert spec["delivery"]["thread_id"] == thread and "source_thread" not in spec["parts"]
    assert all(part.get("prior_remote_id") for part in spec["parts"]["manifest"] if part["kind"] == "body_part")
    (validate_slack if cfg is SLACK else validate)(spec)
    _land(led, _latest_render(led), cfg, now=LATE + 180)
    assert notify_cards.sweep(led, cfg, now=LATE + 181)["updated"] == 0


@pytest.mark.parametrize("cfg", [CFG, SLACK], ids=["discord", "slack"])
@pytest.mark.parametrize("state", ["granted", "unknown", "unfinished-parts"])
def test_fallback_never_moves_while_attempt_or_parts_are_unsettled(led, cfg, state):
    created = _fallback(led, cfg)
    if state == "granted":
        _begin(led, created, cfg)
    elif state == "unknown":
        _land(led, created, cfg, result="unknown")
    else:
        _land(led, created, cfg, finish=False)
    before = _card(led)
    delivered_source(led, card_id=2, cfg=cfg)
    assert notify_cards.sweep(led, cfg, now=LATE + 60)["updated"] == 0
    after = _card(led)
    assert (after["message_id"], after["thread_id"], after["desired_render_rev"]) == (
        before["message_id"], before["thread_id"], before["desired_render_rev"])


@pytest.mark.parametrize("cfg", [CFG, SLACK], ids=["discord", "slack"])
def test_available_source_moves_fallback_once_and_lost_source_falls_back_once(led, cfg):
    created = _fallback(led, cfg)
    _land(led, created, cfg)
    notify_cards.gc(led, cfg, now=LATE + 1)
    delivered_source(led, card_id=2, cfg=cfg)
    source = _card(led, 2)
    assert notify_cards.sweep(led, cfg, now=LATE + 60)["updated"] == 1
    moved = _latest_render(led)
    spec = json.loads(moved["spec_json"])
    assert spec["op"] == "create" and spec["parts"]["source_thread"] is True
    assert spec["delivery"]["thread_id"] == source["thread_id"]
    _land(led, moved, cfg, now=LATE + 60)
    notify_cards.gc(led, cfg, now=LATE + 61)
    assert notify_cards.sweep(led, cfg, now=LATE + 62)["updated"] == 0
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_state='deleted' WHERE card_id=2")
    notify_cards.sweep(led, cfg, now=LATE + 120)
    prior_revision = moved["render_rev"]
    moved = _latest_render(led)
    assert moved["render_rev"] == prior_revision + 1
    spec = json.loads(moved["spec_json"])
    assert spec["op"] == "create" and "source_thread" not in spec["parts"]
    assert "thread_id" not in spec["delivery"]
    mid, thread = _land(led, moved, cfg, now=LATE + 120)
    assert notify_cards.sweep(led, cfg, now=LATE + 121)["updated"] == 0
    assert (_card(led)["message_id"], _card(led)["thread_id"]) == (mid, thread)


@pytest.mark.parametrize("cfg", [CFG, SLACK], ids=["discord", "slack"])
def test_legacy_null_specs_keep_slack_own_root_and_hold_unknown_discord_route(led, cfg):
    created = _fallback(led, cfg)
    mid, thread = _land(led, created, cfg)
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE card_id=1")
    assert notify_cards.sweep(led, cfg, now=LATE + 60)["updated"] == 0
    assert (_card(led)["message_id"], _card(led)["thread_id"]) == (mid, thread)
    with led.db:
        specs = []
        notify_cards._issue_render(led.db, 1, cfg, LATE + 120, specs, force=True)
    assert bool(specs) is (cfg is SLACK)


@pytest.mark.parametrize("cfg", [CFG, SLACK], ids=["discord", "slack"])
def test_failed_source_face_update_does_not_remove_proven_original_thread(led, cfg):
    created = _fallback(led, cfg)
    _land(led, created, cfg)
    delivered_source(led, card_id=2, cfg=cfg)
    source = _card(led, 2)
    with led.db:
        led.db.execute("UPDATE notification_cards SET delivery_state='update_failed' WHERE card_id=2")
    assert notify_cards.signal_thread_target(led.db, _card(led), cfg)["thread_id"] == source["thread_id"]
    assert notify_cards.sweep(led, cfg, now=LATE + 60)["updated"] == 1
    moved = _latest_render(led)
    _land(led, moved, cfg, now=LATE + 60)
    assert notify_cards.sweep(led, cfg, now=LATE + 61)["updated"] == 0
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE part_id='thread' "
                       "AND delivery_id IN (SELECT delivery_id FROM notification_renders WHERE card_id=2)")
    assert notify_cards.signal_thread_target(led.db, _card(led), cfg) is None


@pytest.mark.parametrize("fault", ["card-receipt", "thread-receipt", "scope", "partial", "foreign", "thread-deleted", "revoked", "creation-unknown"])
def test_failed_source_update_still_requires_current_owned_delivery_proof(led, fault):
    created = _fallback(led, CFG)
    _land(led, created, CFG)
    delivered_source(led, card_id=2)
    source_render = _latest_render(led, 2)
    with led.db:
        led.db.execute("UPDATE notification_cards SET delivery_state='update_failed' WHERE card_id=2")
        if fault in ("card-receipt", "thread-receipt"):
            led.db.execute("UPDATE notification_render_parts SET remote_id='unrelated' WHERE delivery_id=? AND part_id=?",
                           (source_render["delivery_id"], "card" if fault == "card-receipt" else "thread"))
        elif fault == "scope":
            led.db.execute("UPDATE notification_renders SET channel_id='unrelated' WHERE delivery_id=?", (source_render["delivery_id"],))
        elif fault == "partial":
            led.db.execute("UPDATE messages SET body_state='partial' WHERE message_id=101")
        elif fault == "foreign":
            led.db.execute("UPDATE messages SET project_id=2 WHERE message_id=101")
        elif fault == "thread-deleted":
            led.db.execute("UPDATE notification_cards SET thread_state='deleted' WHERE card_id=2")
        elif fault == "revoked":
            led.db.execute("UPDATE notification_cards SET delivery_state='revoked' WHERE card_id=2")
        else:
            led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND part_id='card'", (source_render["delivery_id"],))
    assert notify_cards.signal_thread_target(led.db, _card(led), CFG) is None


def test_gc_route_witness_contains_no_content_and_is_not_replayed(led):
    created = _fallback(led, CFG)
    _land(led, created, CFG)
    notify_cards.gc(led, CFG, now=LATE + 1)
    witness = json.loads(_latest_render(led)["spec_json"])
    with pytest.raises(ValueError, match="bad_schema"):
        validate(witness)
    assert notify_cards.recover(led, CFG, {"errors": []})["republished"] == 0
    assert notify_cards.gc(led, CFG, now=LATE + 2)["spec_json_cleared"] == 0
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=? WHERE card_id=1",
                       (json.dumps({**witness, "text": "SYNTHETIC-CONTENT-CANARY"}),))
    notify_cards.gc(led, CFG, now=LATE + 3)
    assert "SYNTHETIC-CONTENT-CANARY" not in (_latest_render(led)["spec_json"] or "")
