"""A parked 📋 result whose tasks are all done (no transition buttons)
still reaches the clicker through the followup sweep — no view=None."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from discord_testkit import MISSING, _fake_discord

from adapters.discord import actions as actions_mod


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
    act._allowed_pid = lambda pid: True
    asyncio.run(act.sweep_followups())
    assert not logs
    assert [m["content"] for m in discord.Webhook.sent] == ["タスク一覧"]
    assert discord.Webhook.sent[0]["view"] is MISSING
