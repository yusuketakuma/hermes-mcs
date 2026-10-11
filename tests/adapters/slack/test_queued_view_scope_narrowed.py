"""A view click queued before the project scope narrowed never delivers old totals (synthetic)."""
import asyncio
import json
from pathlib import Path

import ledger
import notify_cards
import notify_cmds
import pytest
from adapters.common.registry import Registry
from adapters.common.text import SCOPE_CHANGED_TEXT
from adapters.slack.actions import Actions
from adapters.slack.delivery import SlackCardAdapter
from adapters.slack.paths import ensure_dirs
from notify_testkit import NOW, _add_request, _patient, _token_for
from slack_testkit import SCOPE, SLACK
from test_mcs_slack_actions import App, ack
from test_notify_slack import _command, _drain, _slack_render


async def _scenario(tmp_path, scope, expired):
    root = tmp_path / "data"
    root.mkdir()
    led = ledger.Ledger(str(root / "ledger.db"))
    try:
        render = _slack_render(led)
        spec = json.loads(render["spec_json"])
        assert _drain(led, _command(render))["granted"]
        receipt = _command(render, "transport_receipt")
        assert _drain(led, receipt)["applied"]
        token = _token_for(spec, "mytasks")
        _patient(led, 2, "SYNTHETIC B")
        _add_request(led, "SYNTHETIC A TASK", "Synthetic Actor")
        dirs = ensure_dirs(str(root))
        app = App()

        def instance(ids, reg):
            settings = {**SCOPE, "allowed_user_ids": {"U_OPERATOR"}, "project_ids": set(ids)}
            sender = SlackCardAdapter(app, team_id=SCOPE["team_id"],
                                      application_id=SCOPE["application_id"],
                                      channel_id=SCOPE["channel_id"], profile=SCOPE["profile"],
                                      allowed_user_ids=settings["allowed_user_ids"])
            actions = Actions(app, settings, dirs, reg, sender, lambda *a, **kw: None)
            actions.register()
            return actions

        reg = Registry(dirs["state"], scope=SCOPE)
        reg.put_tokens({token: {"action": "mytasks", "team_id": SCOPE["team_id"],
                                "channel_id": SCOPE["channel_id"],
                                "message_id": receipt["message_id"], "project_id": 1}})
        old = instance([1, 2], reg)
        body = {"team": {"id": SCOPE["team_id"]}, "api_app_id": SCOPE["application_id"],
                "channel": {"id": SCOPE["channel_id"]},
                "user": {"id": "U_OPERATOR", "name": "Synthetic Actor"},
                "message": {"ts": receipt["message_id"]}}
        action = {"action_id": "mcs:a:" + token, "value": token}
        await old._action(ack, body, action)
        env = json.loads(next(Path(dirs["cmd_int"]).glob("*.json")).read_text())
        assert env["input"] == {"name": "Synthetic Actor", "projects": [1, 2]}
        assert not app.client.messages and len(reg.followups()) == 1
        old.unload()
        # same-route restart with the narrowed scope, then a B task appears
        current_reg = Registry(dirs["state"], scope={**SCOPE, "project_ids": scope})
        current = instance(scope, current_reg)
        if expired:
            current_reg._data["followups"][env["command_id"]]["expires"] = NOW - 1
            current_reg.save(immediate=True)
        _add_request(led, "SYNTHETIC B NEW AFTER NARROWING", "Synthetic Actor", pid=2)
        result = {"errors": []}
        assert notify_cmds.drain_int_commands(led, result, SLACK, str(root)) == 1
        assert not result["errors"]
        await current.sweep_followups()
        sent = [m["text"] for m in app.client.messages]
        fresh = None
        if scope == [1] and not expired:
            app.client.messages.clear()
            await current._action(ack, body, action)
            result = {"errors": []}
            assert notify_cmds.drain_int_commands(led, result, SLACK, str(root)) == 1
            await current.sweep_followups()
            fresh = "\n".join(m["text"] for m in app.client.messages)
        return sent, fresh
    finally:
        led.close()


@pytest.mark.parametrize(("scope", "expired"), [([1], False), ([1, 2], False), ([], False), ([1], True)])
def test_queued_aggregate_view_respects_the_current_scope(tmp_path, monkeypatch, scope, expired):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    sent, fresh = asyncio.run(_scenario(tmp_path, scope, expired))
    text = "\n".join(sent)
    if scope == [1] and not expired:
        # the old [1,2] totals never reach the narrowed deployment
        assert sent == [SCOPE_CHANGED_TEXT]
        assert "未完了 1件" in fresh and "未完了 2件" not in fresh
        assert "SYNTHETIC B" not in fresh
    elif scope == [1, 2]:
        assert "未完了 2件" in text and "SYNTHETIC B NEW" in text
    else:
        assert sent == []
