"""Factual transport receipts survive revoke races, successor crashes and
moved urgent threads. Synthetic temp ledger, no transport or patient data."""
import json
import os
from pathlib import Path

import pytest

import notify_cards
import notify_cmds
import notify_scope
import notify_spool
import notify_transport
from mcs_requests import canonical
from notify_testkit import (CFG, NOW, SCOPE, _begin, _dispatch, _intent, _latest_render,
                            _part_receipt, _receipt, _seed_thread, _uuid)
from notify_urgent_testkit import _alert, led, world

__all__ = ["led", "world"]


def _part(led, render, part_id):
    return led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                          (render["delivery_id"], part_id)).fetchone()


def _delivered_card(led):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    return render


@pytest.mark.usefixtures("v1_thread_bodies")
def test_part_delivered_after_revoke_keeps_its_remote_post(led):
    render = _delivered_card(led)
    assert _receipt(led, render, f"{1:016x}", message_id="synthetic-card")["applied"]
    assert _part_receipt(led, render, "thread", remote_id="synthetic-thread")["applied"]
    with led.db:
        notify_cards.revoke_card(led.db, render["card_id"], NOW)
    assert tuple(_part(led, render, "body:0001")[k] for k in ("state", "error_code")) == ("held", "card_revoked")
    # a foreign attempt never settles the revoked hold
    foreign = _part_receipt(led, render, "body:0001", remote_id="synthetic-body", aid="p:foreign:body:0001")
    assert foreign["error"] == "attempt_mismatch"
    # the worker's own in-flight send proves the post exists
    out = _part_receipt(led, render, "body:0001", remote_id="synthetic-body")
    assert out["applied"], out
    part = _part(led, render, "body:0001")
    assert (part["state"], part["remote_id"]) == ("delivered", "synthetic-body")
    assert _part_receipt(led, render, "body:0001", remote_id="synthetic-body")["applied"]  # idempotent replay
    assert notify_scope.card_row(led.db, render["card_id"])["delivery_state"] == "revoked"


def _receipt_cmd(led, render, n=9):
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(n),
           "attempt_id": f"{1:016x}", "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
           "route_epoch": 1, "correlation": render["correlation"], **SCOPE,
           "result": "delivered", "message_id": "synthetic-card"}
    root = notify_spool.data_root(led)
    notify_spool.ensure_dirs(root)
    dirs = notify_spool.notify_dirs(root)
    path = notify_spool.publish_file(dirs["cmd_int"], req["command_id"] + ".json", canonical(req))
    return root, path, os.path.join(dirs["cmd_results"], req["command_id"] + ".json")


def _attempt_state(led):
    return led.db.execute("SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                          (f"{1:016x}",)).fetchone()[0]


def test_successor_render_crash_never_undoes_the_settled_receipt(led, monkeypatch):
    render = _delivered_card(led)
    root, path, res = _receipt_cmd(led, render)

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(notify_cards, "issue_render", boom)
    result = {}
    assert notify_cmds.drain_int_commands(led, result, CFG, root) == 1
    assert _attempt_state(led) == "delivered"
    assert notify_scope.card_row(led.db, render["card_id"])["message_id"] == "synthetic-card"
    assert "transport_receipt_successor_crash:RuntimeError" in result["errors"]
    assert not os.path.exists(path) and json.loads(Path(res).read_text())["applied"] is True


def test_receipt_crash_keeps_the_command_for_the_next_drain(led, monkeypatch):
    render = _delivered_card(led)
    root, path, res = _receipt_cmd(led, render)

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(notify_transport, "settle_attempt_tx", boom)
    result = {}
    assert notify_cmds.drain_int_commands(led, result, CFG, root) == 0
    assert "transport_receipt_crash:RuntimeError" in result["errors"]
    assert os.path.exists(path) and not os.path.exists(res)
    assert _attempt_state(led) == "granted"
    monkeypatch.undo()
    assert notify_cmds.drain_int_commands(led, {}, CFG, root) == 1
    assert _attempt_state(led) == "delivered" and not os.path.exists(path)


def test_moved_urgent_thread_denial_reissues_to_the_current_thread(world):
    store, cfg, event, _ = _alert(world)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    render = notify_cards._notice_renders(store.db, event["event_id"])[0]
    spec = os.path.join(notify_spool.notify_dirs(notify_spool.data_root(store))["discord_render"],
                        render["delivery_id"] + ".json")
    assert os.path.exists(spec)
    with store.db:
        store.db.execute("UPDATE notification_cards SET thread_id='another-thread'")
    assert _begin(store, render, n=1100, cfg=cfg)["error"] == "denied_urgent_thread_changed"
    renders = notify_cards._notice_renders(store.db, event["event_id"])
    assert [r["state"] for r in renders] == ["cancelled"] and not os.path.exists(spec)
    assert notify_cards.dispatch_intent(store, dict(event), cfg)["dispatched"]
    fresh = notify_cards._notice_renders(store.db, event["event_id"])[-1]
    assert fresh["state"] == "queued" and fresh["delivery_id"] != render["delivery_id"]
    assert json.loads(fresh["spec_json"])["delivery"]["thread_id"] == "another-thread"
