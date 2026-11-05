"""Synthetic mcs discord medication views contracts with isolated shared fixtures."""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.common import paths, text
from adapters.discord import actions as actions_mod
from discord_testkit import MISSING, SETTINGS, FakeFollowup, FakeInteraction, _deliver
from discord_testkit import discord_world as discord_world
from mcs_discord_testkit import _drug_thread as _drug_thread, _pin_wall_clock as _pin_wall_clock


def test_meds_thread_button_answers_only_clicker(world, monkeypatch):
    act, _, bot, spec, thread, body = _drug_thread(world, monkeypatch)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'meds')}",
                         channel_id=thread.id, channel=SimpleNamespace(parent_id=42),
                         message_id=body.id)
    assert actions_mod._origin(ix, "mcs")["thread_id"] == str(thread.id)
    shared_body = list(thread.sent)
    asyncio.run(world.interact(act, ix))
    out = "\n".join(m["content"] for m in ix.followup.sent)
    assert "キラナ" in out and all(m["ephemeral"] for m in ix.followup.sent)
    assert not any(m["pings"] for m in ix.followup.sent)
    assert len(bot.channels[42].sent) == 1
    assert thread.sent == shared_body


@pytest.mark.parametrize("delayed", [False, True])
def test_drug_search_thread_modal_and_followup_stay_private(world, monkeypatch, delayed):
    act, reg, bot, spec, thread, body = _drug_thread(world, monkeypatch)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'drugsearch')}",
                         channel_id=thread.id, channel=SimpleNamespace(parent_id=42),
                         message_id=body.id)
    shared_body = list(thread.sent)
    asyncio.run(world.interact(act, ix))
    assert ix.response.modal is not None
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    assert reg.modal(modal_id)["origin"]["thread_id"] == str(thread.id)
    submit = FakeInteraction(f"mcs:m:{modal_id}", channel_id=thread.id,
                             channel=SimpleNamespace(parent_id=42), message_id=body.id,
                             components=[{"components": [{
                                 "custom_id": "query", "value": "キラナ"}]}])
    if delayed:
        monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
        asyncio.run(act.on_interaction(submit))
        assert reg.followups()
        rec = next(iter(reg.followups().values()))
        assert rec["origin"]["thread_id"] == str(thread.id)
        world.drain()
        asyncio.run(act.sweep_followups())
        answers = sys.modules["discord"].Webhook.sent
    else:
        asyncio.run(world.interact(act, submit))
        answers = submit.followup.sent
    assert any("キラナ" in m["content"] for m in answers)
    assert all(m["ephemeral"] for m in answers)
    assert len(bot.channels[42].sent) == 1
    assert thread.sent == shared_body


@pytest.mark.parametrize("delayed", [False, True])
def test_private_medication_navigation_registers_tokens_before_first_chunk(world, monkeypatch,
                                                                         delayed):
    _, reg, bot = world.mkworker()
    act = world.mkactions(reg, bot)
    token = "a" * 32
    result = {"outcome": "applied", "action": "list", "projects": [1],
              "navigation": [{"id": "meds", "ui": "button", "style": "secondary",
                              "label": "次の5件", "token": token}],
              "token_ctx": {token: {"action": "meds", "project_id": 1}}}
    answer = [("synthetic first", None), ("synthetic second", None)]
    original = (sys.modules["discord"].Webhook.send if delayed else FakeFollowup.send)

    async def checked_send(self, *args, **kwargs):
        assert reg.token(token) is not None
        return await original(self, *args, **kwargs)

    if delayed:
        monkeypatch.setattr(sys.modules["discord"].Webhook, "send", checked_send)
        origin = {key: SETTINGS[key] for key in
                  ("application_id", "channel_id", "guild_id", "profile")}
        origin["thread_id"] = "7700"
        reg.put_followup("synthetic-cmd", {"application_id": "1", "token": "synthetic",
                                         "actor": "discord:1001", "origin": origin,
                                         "project_ids": [1]})
        monkeypatch.setattr(paths, "read_result", lambda *_: result)
        monkeypatch.setattr(text, "view_answer", lambda *_: answer)
        asyncio.run(act.sweep_followups())
        sent = sys.modules["discord"].Webhook.sent
    else:
        monkeypatch.setattr(FakeFollowup, "send", checked_send)
        interaction = FakeInteraction("synthetic", channel_id=7700,
                                      channel=SimpleNamespace(parent_id=42))
        asyncio.run(act._send_answer(interaction, answer, result))
        sent = interaction.followup.sent
    assert len(sent) == 2 and all(message["ephemeral"] for message in sent)
    assert sent[0]["view"].items[0].custom_id == f"mcs:a:{token}"
    assert sent[0]["view"].stopped
    assert sent[1]["view"] is MISSING


def test_lost_registry_thread_drug_token_refreshes_and_recovers_buttons(world, monkeypatch):
    act, reg, bot, spec, thread, body = _drug_thread(world, monkeypatch)
    old_token = world.token(spec, "meds")
    reg._data["tokens"].pop(old_token)
    missing = FakeInteraction(f"mcs:a:{old_token}", channel_id=thread.id,
                              channel=SimpleNamespace(parent_id=42), message_id=body.id)
    asyncio.run(world.interact(act, missing))
    world.drain()
    receipts = [json.loads(row[0]) for row in world.led.db.execute(
        "SELECT receipt_json FROM command_receipts")]
    refresh = [receipt for receipt in receipts if receipt.get("kind") == "refresh"]
    assert len(refresh) == 1 and refresh[0]["outcome"] == "applied"
    _, fresh = world.spec()
    assert fresh["render_rev"] > spec["render_rev"]
    worker, recovered_registry, _ = world.mkworker(bot=bot)
    asyncio.run(_deliver(world, worker))
    new_token = world.token(fresh, "meds")
    assert new_token != old_token
    assert recovered_registry.token(new_token) is not None
    assert f"mcs:a:{new_token}" in [item.custom_id for item in body.view.items]
    assert len(bot.channels[42].sent) == 1
    recovered = world.mkactions(recovered_registry, bot)
    click = FakeInteraction(f"mcs:a:{new_token}", channel_id=thread.id,
                            channel=SimpleNamespace(parent_id=42), message_id=body.id)
    asyncio.run(world.interact(recovered, click))
    assert any("キラナ" in item["content"] for item in click.followup.sent)
    assert all(item["ephemeral"] for item in click.followup.sent)


@pytest.mark.parametrize("delayed", [False, True])
def test_private_medication_navigation_pages_posts_and_rejects_other_origin(world, monkeypatch,
                                                                          delayed):
    act, reg, bot, spec, thread, body = _drug_thread(
        world, monkeypatch, medication_count=6, older_medication=True)
    initial = FakeInteraction(f"mcs:a:{world.token(spec, 'meds')}",
                              channel_id=thread.id, channel=SimpleNamespace(parent_id=42),
                              message_id=body.id)
    shared_body = list(thread.sent)
    if delayed:
        original_wait = actions_mod.RESULT_WAIT_S
        monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
        asyncio.run(act.on_interaction(initial))
        assert reg.followups()
        world.drain()
        asyncio.run(act.sweep_followups())
        first = sys.modules["discord"].Webhook.sent[0]
        monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", original_wait)
    else:
        asyncio.run(world.interact(act, initial))
        first = initial.followup.sent[0]
    assert first["ephemeral"]
    assert "キラナ" in first["content"] and "合成薬6" not in first["content"]
    controls = {button.label: button.custom_id for button in first["view"].items}
    assert {"次の5件", "古い投稿"} <= controls.keys()

    def click(custom_id, *, user_id=1001, thread_id=None):
        interaction = FakeInteraction(custom_id, user_id=user_id,
                                      channel_id=thread.id if thread_id is None else thread_id,
                                      channel=SimpleNamespace(parent_id=42), message_id=999999)
        asyncio.run(world.interact(act, interaction))
        return interaction

    # A native ephemeral response message has a new id, but remains in the same thread.
    page = click(controls["次の5件"])
    assert any("合成薬6" in message["content"] for message in page.followup.sent)
    previous = next(button for button in page.followup.sent[0]["view"].items
                    if button.label == "前の5件")
    back = click(previous.custom_id)
    assert any("キラナ" in message["content"] for message in back.followup.sent)
    older = click(controls["古い投稿"])
    assert any("旧投稿薬" in message["content"] for message in older.followup.sent)
    assert any(button.label == "新しい投稿" for button in older.followup.sent[0]["view"].items)
    # Adapter authorization alone is insufficient: the runner also pins actor and thread.
    act._settings = {**SETTINGS, "allowed_user_ids": {"1001", "2002"}}
    outsider = click(controls["次の5件"], user_id=2002)
    fresh_next = next(button.custom_id for button in back.followup.sent[0]["view"].items
                      if button.label == "次の5件")
    sibling = click(fresh_next, thread_id=thread.id + 1)
    for denied in (outsider, sibling):
        assert denied.followup.sent
        assert all("合成薬6" not in message["content"] for message in denied.followup.sent)
    errors = {json.loads(row[0]).get("error") for row in world.led.db.execute(
        "SELECT receipt_json FROM command_receipts WHERE outcome='rejected'")}
    assert "actor_mismatch" in errors and "origin_mismatch" in errors
    assert all(message["ephemeral"] for interaction in (initial, page, back, older)
               for message in interaction.followup.sent)
    assert thread.sent == shared_body and len(bot.channels[42].sent) == 1
    assert reg.token(controls["次の5件"][len("mcs:a:"):]) is not None


def test_thread_post_line_is_subtext_but_signal_patient_heading_is_not():
    from adapters.discord.cards import _zones, escape_md
    containers = [{"type": "heading", "text": "合成"},
                  {"type": "text", "text": "10-01 09:40 合成さん", "rule": True},
                  {"type": "text", "text": "📋 要約"}]
    thread = {"kind": "thread", "parts": {"containers": containers, "footer": []}}
    zones, _ = _zones(thread, escape_md)
    assert zones[1] == ["-# 10-01 09:40 合成さん", "📋 要約"]
    signal = {"kind": "signal", "parts": {"containers": containers, "footer": []}}
    zones, _ = _zones(signal, escape_md)
    assert zones[1][0] == "10-01 09:40 合成さん"
