"""A confirmed human command reports its result exactly once, whether
the inline wait or the supervisor followup sweep picks it up first.

Synthetic temp dirs and the fake discord module only."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from adapters.common import envelopes, paths, registry
from adapters.discord import actions as A
from discord_testkit import SETTINGS, _fake_discord
from test_mcs_discord import FakeInteraction

DONE = "反映しました。"


@pytest.mark.parametrize("sweep_delay", [0.1, 0.6])
def test_confirm_result_delivered_once(tmp_path, monkeypatch, sweep_delay):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    root = str(tmp_path)
    for d in paths.notify_dirs(root).values():
        os.makedirs(d, exist_ok=True)
    dirs = paths.ensure_dirs(root)
    with open(os.path.join(dirs["flags"], "notify.json"), "w") as f:
        json.dump({"interactive": True, "transport": "discord"}, f)
    reg = registry.Registry(dirs["state"], scope=SETTINGS)
    act = A.Actions(bot=object(), settings={**SETTINGS, "data_root": root},
                    root=root, reg=reg, log=lambda *a, **k: None)
    payload = envelopes.request_create(
        "discord:1001", {"project_id": 1, "source_message_id": 100,
                         "source_hash": "a" * 64},
        {"title": "t", "reason": "r"})
    reg.put_confirm("c" * 16, {
        "actor": "discord:1001",
        "origin": {"application_id": "1", "channel_id": "42",
                   "guild_id": "7", "profile": "mcs", "message_id": "9"},
        "payload": payload})
    ix = FakeInteraction("mcs:c:" + "c" * 16, message_id=9)

    async def supervisor():
        while not reg.followups():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)          # inline poll is asleep now
        cid = next(iter(reg.followups()))
        with open(os.path.join(dirs["cmd_results"], cid + ".json"),
                  "w") as f:
            json.dump({"outcome": "applied", "command_id": cid}, f)
        await asyncio.sleep(sweep_delay - 0.1)
        await act.sweep_followups()

    async def main():
        await asyncio.gather(act.on_interaction(ix), supervisor())

    asyncio.run(main())
    webhook = [s for s in sys.modules["discord"].Webhook.sent
               if s["content"] == DONE]
    inline = [s for s in ix.followup.sent if s["content"] == DONE]
    assert len(webhook) + len(inline) == 1
    assert not reg.followups()
