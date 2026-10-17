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
from notify_testkit import (
    NOW, _add_request, _dispatch, _intent, _latest_render, _patient,
    _seed_thread, _signal_row, _token_for,
)
from slack_testkit import SCOPE, SLACK
from slack_actions_testkit import App, ack
from slack_actions_testkit import _no_modal_wait  # noqa: F401  (used via usefixtures)
from test_notify_slack import _command, _drain

pytestmark = pytest.mark.usefixtures("_no_modal_wait")


async def _scenario(tmp_path, scope, expired):
    root = tmp_path / "data"
    root.mkdir()
    led = ledger.Ledger(str(root / "ledger.db"))
    try:
        _seed_thread(led)
        _signal_row(led, "synthetic-digest", mids=[100])
        _dispatch(led, _intent(led, kind="signal", pid=None, payload={
            "digest": True, "signal_keys": ["synthetic-digest"]}), SLACK)
        render = _latest_render(led)
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


def test_queued_digest_card_over_a_narrowed_scope_sends_no_blocks(tmp_path):
    from adapters.common.paths import safe_name
    from slack_actions_testkit import click, command, fixture, result, submitted

    async def scenario():
        actions, app, _reg, dirs = fixture(tmp_path, kind="digest")
        await actions._action(ack, *click())
        modal = app.client.views[0]["view"]["private_metadata"]
        Path(dirs["cmd_int"], safe_name(command(dirs)["command_id"]) + ".json").unlink()
        await actions._modal(ack, *submitted(modal, query="mine"))
        env = command(dirs)
        # counted over [123, 456]; this deployment now serves only 123
        parts = {"containers": [{"type": "heading", "text": "📊 MCS サマリー"},
                                {"type": "text", "text": "■ 新着\n・SYNTHETIC OLD 2件"}],
                 "footer": []}
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="digest", parts=parts,
               projects=[123, 456], text="*📊 MCS サマリー*\n■ 新着")
        await actions.sweep_followups()
        assert [m["text"] for m in app.client.messages] == [SCOPE_CHANGED_TEXT]
        assert all("blocks" not in m for m in app.client.messages)
    asyncio.run(scenario())
