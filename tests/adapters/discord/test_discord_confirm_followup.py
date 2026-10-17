"""Discord 確定 and parked followup results: a confirmed human command
reports its result exactly once (inline wait or supervisor sweep), 確定
is refused once the runner flags withdraw interactivity while 取消 still
drops the preview, and a done-only 📋 task list reaches the clicker
through the sweep without view=None.

Synthetic temp dirs and the fake discord module only."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from adapters.common import envelopes, paths, registry
from adapters.discord import actions as A
from adapters.discord import actions as actions_mod
from discord_testkit import MISSING, SETTINGS, FakeInteraction, _fake_discord

DONE = "反映しました。"
CID = "c" * 16


# ---------- confirmed result is delivered once ---------------------------

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


# ---------- 確定 after the interactivity kill switch ----------------------

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
    assert confirm.response.message["content"] == "現在インタラクティブ通知は停止中です。"
    assert not os.listdir(dirs["cmd_int"])
    assert not reg.confirm(CID).get("in_flight")
    cancel = FakeInteraction("mcs:c:" + CID + ":cancel", message_id=9)
    asyncio.run(act.on_interaction(cancel))
    assert cancel.response.message["content"] == "取り消しました。"
    assert reg.confirm(CID) is None


# ---------- sweep of a done-only task list -------------------------------

class _Reg:
    def __init__(self, recs):
        self.recs = recs

    def followups(self):
        return dict(self.recs)

    def followup(self, cid):
        return self.recs.get(cid)

    def drop_followup(self, cid):
        return self.recs.pop(cid, None) is not None

    def put_tokens(self, ctx):
        pass


def test_sweep_sends_done_only_task_list_without_view(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    discord = sys.modules["discord"]
    discord.Webhook.sent = []
    result = {"request_id": None}
    monkeypatch.setattr(actions_mod.paths, "read_result",
                        lambda d, cid: result)
    tasks = [{"request_id": 1, "status": "done", "transitions": {}}]
    monkeypatch.setattr(actions_mod.text, "view_answer",
                        lambda r, allowed: [("タスク一覧", tasks)])
    act = object.__new__(actions_mod.Actions)
    logs = []
    act._reg = _Reg({"c1": {"application_id": "1", "token": "t"}})
    act._dirs = {"cmd_results": "unused"}
    act._bot = SimpleNamespace()
    act._log = lambda ev, **kw: logs.append((ev, kw))
    act._followup_authorized = lambda rec: True

    async def still_allowed(rec):
        return True

    act._actor_still_allowed = still_allowed
    act._allowed_pid = lambda pid: True
    asyncio.run(act.sweep_followups())
    assert not logs
    assert [m["content"] for m in discord.Webhook.sent] == ["タスク一覧"]
    assert discord.Webhook.sent[0]["view"] is MISSING
