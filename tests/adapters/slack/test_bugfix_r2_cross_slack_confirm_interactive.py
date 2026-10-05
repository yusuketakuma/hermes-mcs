"""Regression: Slack 確定 is refused once the runner flags withdraw interactivity."""
import asyncio
import json
from pathlib import Path

import pytest

from test_mcs_slack_actions import _to_confirm, ack, fixture


@pytest.mark.parametrize("flags", [
    {"interactive": False, "transport": "slack"},
    {"interactive": True, "transport": "discord"},
    {"interactive": True, "transport": "slack", "restore_pending": True},
])
def test_confirm_after_kill_switch_is_denied_but_cancellable(tmp_path, flags):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="request")
        body, (confirm_action, cancel_action) = await _to_confirm(actions, app, dirs, 123)
        confirm_id = confirm_action["action_id"].split(":")[2]
        Path(dirs["flags"], "notify.json").write_text(json.dumps(flags), encoding="utf-8")
        before = len(app.client.messages)
        await actions._confirm(ack, body, confirm_action)
        assert [m["text"] for m in app.client.messages[before:]] == ["権限がありません。"]
        assert not any(json.loads(p.read_text()).get("cmd") == "request.create"
                       for p in Path(dirs["cmd_int"]).glob("*.json"))
        await actions._confirm(ack, body, cancel_action)
        assert app.client.messages[-1]["text"] == "取り消しました。"
        assert reg.confirm(confirm_id) is None
    asyncio.run(scenario())
