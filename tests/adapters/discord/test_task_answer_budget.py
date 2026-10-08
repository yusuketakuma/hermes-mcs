"""Fully synthetic long task lists retain their private controls within Discord limits."""
import asyncio
import sys

import pytest

from adapters.common import envelopes, text
from discord_testkit import MISSING
from test_mcs_discord import (
    FakeFollowup, FakeInteraction, _deliver, _pin_wall_clock, _request, world,
)

__all__ = ["world", "_pin_wall_clock"]


def _prepare(world, monkeypatch):
    world.seed()
    titles = ["x" * 1000, "y" * 1000]
    ids = [_request(world, title=title) for title in titles]
    world.dispatch()
    sender, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, sender))
    _, spec = world.spec()
    actions = world.mkactions(reg, bot)
    mid = bot.channels[42].sent[0].id
    token = world.token(spec, "tasks")
    original = FakeFollowup.send

    async def bounded(self, content, **kwargs):
        assert len(content) <= 2000, "synthetic Discord content limit"
        await original(self, content, **kwargs)

    monkeypatch.setattr(FakeFollowup, "send", bounded)
    sdk = sys.modules["discord"]
    hook_send = sdk.Webhook.send

    async def bounded_hook(self, content, **kwargs):
        assert len(content) <= 2000, "synthetic Discord webhook content limit"
        await hook_send(self, content, **kwargs)

    monkeypatch.setattr(sdk.Webhook, "send", bounded_hook)
    return actions, reg, spec, mid, token, titles, ids, sdk


@pytest.mark.parametrize("delayed", [False, True])
def test_long_tasks_keep_full_text_and_usable_owner_scoped_controls(world, monkeypatch, delayed):
    actions, reg, spec, mid, token, titles, ids, sdk = _prepare(world, monkeypatch)
    interaction = FakeInteraction("mcs:a:" + token, message_id=mid)
    if delayed:
        origin = {**spec["delivery"]}
        origin = {key: origin[key] for key in ("profile", "application_id", "guild_id", "channel_id")}
        origin["message_id"] = str(mid)
        env = envelopes.notification(token, "discord:1001", origin)
        envelopes.publish_command(str(world.data / "cmd_int"), env)
        world.drain()

        async def followup():
            await actions._park_followup(interaction, env["request_id"], "discord:1001", origin, [1])
            await actions.sweep_followups()

        asyncio.run(followup())
        messages = sdk.Webhook.sent
    else:
        asyncio.run(world.interact(actions, interaction))
        messages = interaction.followup.sent
    assert len(messages) == 2
    assert all(message["ephemeral"] and len(message["content"]) <= 2000 for message in messages)
    joined = "\n".join(message["content"] for message in messages)
    assert all(joined.count(title) == 1 for title in titles)
    buttons = []
    for message, rid, title in zip(messages, ids, titles, strict=True):
        assert title in message["content"] and message["view"] is not MISSING
        controls = message["view"].items
        assert len(controls) == 2 and all(f"#{rid}" in button.label for button in controls)
        for button in controls:
            pinned = reg.token(button.custom_id.removeprefix("mcs:a:"))
            assert pinned["action"] == "task_status" and pinned["project_id"] == 1
        buttons.append(next(button for button in controls if "完了" in button.label))
    assert not any(event == "followup_failed" for event, _ in world.logs)
    denied = FakeInteraction(buttons[0].custom_id, user_id=1002, message_id=555)
    asyncio.run(world.interact(actions, denied))
    assert all(title not in message["content"] for title in titles for message in denied.followup.sent)
    assert world.led.db.execute("SELECT status FROM requests WHERE request_id=?", (ids[0],)).fetchone()[0] == "open"
    for button in buttons:
        asyncio.run(world.interact(actions, FakeInteraction(button.custom_id, message_id=555)))
    assert [row[0] for row in world.led.db.execute("SELECT status FROM requests ORDER BY request_id")] == ["done", "done"]


def test_plain_task_transports_keep_their_existing_chunk_ownership():
    items = [{"request_id": 1, "title": "x" * 1000, "status": "open"},
             {"request_id": 2, "title": "y" * 1000, "status": "open"}]
    result = {"outcome": "applied", "action": "tasks", "tasks": items}
    assert text.view_answer(result, lambda pid: True, markdown=False) == [
        (text.task_list_text(items, plain=True), items)]
