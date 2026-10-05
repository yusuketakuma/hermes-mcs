"""Regression: Discord 確定 is refused once the runner flags withdraw
interactivity; 取消 still drops the preview. Synthetic temp dirs only."""
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

CID = "c" * 16


@pytest.mark.parametrize("flags", [
    {"interactive": False, "transport": "discord"},
    {"interactive": True, "transport": "slack"},
    {"interactive": True, "transport": "discord", "restore_pending": True},
    {"interactive": True, "transport": "discord", "route_epoch": 1},
])
def test_confirm_after_kill_switch_is_denied_but_cancellable(tmp_path, monkeypatch, flags):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    root = str(tmp_path)
    for d in paths.notify_dirs(root).values():
        os.makedirs(d, exist_ok=True)
    dirs = paths.ensure_dirs(root)
    with open(os.path.join(dirs["flags"], "notify.json"), "w") as f:
        json.dump(flags, f)
    reg = registry.Registry(dirs["state"], scope=SETTINGS)
    act = A.Actions(bot=object(), settings={**SETTINGS, "route_epoch": 2},
                    root=root, reg=reg, log=lambda *a, **k: None)
    payload = envelopes.request_create(
        "discord:1001", {"project_id": 1, "source_message_id": 100,
                         "source_hash": "a" * 64}, {"title": "t", "reason": "r"})
    reg.put_confirm(CID, {
        "actor": "discord:1001",
        "origin": {"application_id": "1", "channel_id": "42",
                   "guild_id": "7", "profile": "mcs", "message_id": "9"},
        "payload": payload})
    confirm = FakeInteraction("mcs:c:" + CID, message_id=9)
    asyncio.run(act.on_interaction(confirm))
    assert confirm.response.message["content"] == "権限がありません。"
    assert not os.listdir(dirs["cmd_int"])
    assert not reg.confirm(CID).get("in_flight")
    cancel = FakeInteraction("mcs:c:" + CID + ":cancel", message_id=9)
    asyncio.run(act.on_interaction(cancel))
    assert cancel.response.message["content"] == "取り消しました。"
    assert reg.confirm(CID) is None
