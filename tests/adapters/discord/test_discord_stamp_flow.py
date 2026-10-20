"""Discord MCS stamp: click, runner form, confirm and queued receipt. Synthetic only, no network."""
from __future__ import annotations

import asyncio
import json

import discord_testkit
from discord_testkit import CFG, NOW, FakeInteraction, _deliver
from discord_testkit import discord_world  # noqa: F401  (registers fixture ``world``)

STAMP_CFG = {**CFG, "stamps": {"enabled": True, "account_user_id": 501,
                               "operator": "discord:1001"}}


def _observed(world, mid, reactions):
    world.led.db.execute(
        "INSERT OR REPLACE INTO message_metadata(message_id,source,content,checked_at,last_error)"
        " VALUES(?,?,?,?,?)", (mid, "capture", json.dumps(
            {"reactions": {"value": reactions, "observed_at": NOW}}), NOW, None))
    world.led.db.commit()


def _stamp_card(world, monkeypatch):
    monkeypatch.setattr(discord_testkit, "CFG", STAMP_CFG)   # the runner drain reads it
    world.seed()
    _observed(world, 100, [])
    _observed(world, 101, [{"type": "accepted", "count": 1, "self_reacted": True}])
    world.dispatch(cfg=STAMP_CFG)
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    return reg, bot, spec


def test_operator_stamp_reaches_a_queued_operation_not_a_delivery_claim(world, monkeypatch):
    reg, bot, spec = _stamp_card(world, monkeypatch)
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'stamp')}", message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    post, op, reason = ix.response.modal.children
    shown = {o.value: o.label for o in post.component.options}
    assert "承知" in shown["p2"] and "なし" in shown["p1"]
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    submit = FakeInteraction(f"mcs:m:{modal_id}", message_id=msg.id, components=[
        {"component": {"custom_id": "post", "values": ["p2"]}},
        {"component": {"custom_id": "op", "values": ["cancel"]}},
        {"components": [{"custom_id": "reason", "value": "誤って押した"}]}])
    asyncio.run(world.interact(act, submit))
    preview = submit.followup.sent[-1]
    assert "取消（現在: 承知）" in preview["content"] and "収集アカウント" in preview["content"]
    cid = next(b.custom_id for b in preview["view"].items if not b.custom_id.endswith(":cancel"))
    payload = reg.confirm(cid[len("mcs:c:"):])["payload"]
    assert (payload["cmd"], payload["op"], payload["message_id"]) == ("ops.mcs_reaction", "cancel", 101)
    confirm = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(world.interact(act, confirm))
    row = world.led.db.execute(
        "SELECT content FROM artifacts WHERE kind='mcs_stamp_operation_v1'").fetchone()
    assert row is not None and json.loads(row[0])["state"] == "queued"
    answer = "".join(m.get("content") or "" for m in confirm.followup.sent)
    assert "反映しました" not in answer
    # the queued admission keeps the followup until the terminal readback result
    cid = payload["command_id"]
    results = world.data / "cmd_results" / f"{cid}.json"
    results.write_text(json.dumps({"outcome": "applied", "stamp_state": "queued"}))
    asyncio.run(act.sweep_followups())
    assert reg.followup(cid) is not None
    results.write_text(json.dumps({"outcome": "rejected", "error": "stamp_rejected",
                                   "stamp_state": "rejected"}))
    asyncio.run(act.sweep_followups())
    assert reg.followup(cid) is None


def test_non_operator_never_gets_a_stamp_form(world, monkeypatch):
    reg, bot, spec = _stamp_card(world, monkeypatch)
    act = world.mkactions(reg, bot)
    act._settings = {**act._settings, "allowed_user_ids": {"1001", "2002"}}
    msg = bot.channels[42].sent[0]
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'stamp')}", user_id=2002, message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    assert ix.response.modal is None
    assert world.led.db.execute(
        "SELECT 1 FROM artifacts WHERE kind='mcs_stamp_operation_v1'").fetchone() is None
